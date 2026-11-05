"""Pinned miniF2F validation runs: stratified development cohort or full split."""

from __future__ import annotations

import argparse
from contextlib import ExitStack
import json
import math
from pathlib import Path
import re
import time

from lean_proof_search import runtime as control
from lean_proof_search import generation as smoke
from lean_proof_search.lean.tasks import _masked, _parse, _options, _spec, _proof_code, _validate


REPOSITORY = "yangky11/miniF2F-lean4"
COMMIT = "5746b7d6c47855ce1294bed87329618ff7f1bc31"
RAW = f"https://raw.githubusercontent.com/{REPOSITORY}/{COMMIT}/"
GROUPS = ("imo", "aime", "algebra", "induction", "numbertheory")


def _fetch(path):
    import requests
    response = requests.get(RAW + path, timeout=60)
    response.raise_for_status()
    return response.text


def _select(index, seed, per_group, selection="stratified"):
    names = re.findall(r"^import MiniF2F\.Valid\.([A-Za-z0-9_]+)\s*$", index, re.MULTILINE)
    if len(names) != len(set(names)) or not names:
        raise ValueError("Invalid or duplicate validation index entries")
    if selection == "full":
        return [(name, "_".join(name.split("_")[:2]) if name.startswith("mathd_") else name.split("_")[0])
                for name in sorted(names, key=lambda name: control._sha(f"{seed}:{name}"))]
    if selection != "stratified":
        raise ValueError("Unknown validation selection mode")
    selected = []
    for group in GROUPS:
        pool = [name for name in names if name.lower().startswith(group)]
        if len(pool) < per_group:
            raise ValueError(f"Not enough {group} validation tasks for fixed selection")
        selected.extend((name, group) for name in sorted(
            pool, key=lambda name: control._sha(f"{seed}:{name}"))[:per_group])
    return selected


def _prepare(root, seed, per_group, selection="stratified"):
    path = root / "cohort.json"
    if path.exists():
        cohort = json.loads(path.read_text())
        if (cohort["repository"] != REPOSITORY or cohort["split"] != "valid"
                or cohort["commit"] != COMMIT or cohort["seed"] != seed
                or cohort["per_group"] != per_group or cohort.get("selection", "stratified") != selection):
            raise ValueError("Existing cohort uses a different revision/selection")
        index = (root / "valid_index.lean").read_text()
        if (control._sha(index) != cohort["index_sha256"]
                or [(task["id"], task["group"]) for task in cohort["tasks"]] != _select(index, seed, per_group, selection)):
            raise ValueError("Saved cohort selection changed")
        for task in cohort["tasks"]:
            if task["ingest_status"] == "pending":
                continue
            source = (root / task["file"]).read_text()
            if control._sha(source) != task["source_sha256"]:
                raise ValueError("Saved benchmark source changed")
            if task["ingest_status"] == "parsed" and _parse(task["id"], source) != {
                    "prefix": task["prefix"], "header": task["header"]}:
                raise ValueError("Saved declaration differs from original source")
    else:
        root.mkdir(parents=True, exist_ok=True)
        index = _fetch("MiniF2F/Valid.lean")
        (root / "valid_index.lean").write_text(index)
        selected = _select(index, seed, per_group, selection)  # Freeze before any source/compatibility result.
        toolchain = _fetch("lean-toolchain").strip()
        upstream = json.loads(_fetch("lake-manifest.json"))
        mathlib = next(item["rev"] for item in upstream["packages"] if item["name"] == "mathlib")
        cohort = {"repository": REPOSITORY, "commit": COMMIT, "split": "valid", "seed": seed,
                  "per_group": per_group, "index_sha256": control._sha(index),
                  "upstream_toolchain": toolchain, "upstream_mathlib": mathlib,
                  "tasks": [{"id": name, "group": group, "file": f"sources/{name}.lean", "ingest_status": "pending"}
                            for name, group in selected]}
        if selection != "stratified":
            cohort["selection"] = selection
        control._write(path, cohort)
    ready = sum(task["ingest_status"] != "pending" for task in cohort["tasks"])
    if selection == "full":
        print(json.dumps({"cohort": "full", "split": "valid", "selected_tasks": len(cohort["tasks"]),
                          "source_files_ready": ready}), flush=True)
    for task in cohort["tasks"]:
        if task["ingest_status"] != "pending":
            continue
        name = task["id"]
        source = _fetch(f"MiniF2F/Valid/{name}.lean")
        (root / "sources").mkdir(exist_ok=True)
        (root / task["file"]).write_text(source)
        task["source_sha256"] = control._sha(source)
        try:
            task.update(_parse(name, source), ingest_status="parsed")
        except ValueError as error:
            task.update(ingest_status="unsupported", ingest_error=str(error))
        control._write(path, cohort)
        ready += 1
        if selection == "full" and (ready % 25 == 0 or ready == len(cohort["tasks"])):
            print(json.dumps({"cohort": "full", "source_files_ready": ready,
                              "selected_tasks": len(cohort["tasks"])}), flush=True)
    return cohort


def _messages(task, previous=None, *, proof_style="concise", retry_mode="auto"):
    source = task["prefix"] + task["header"] + " := by\n  sorry\n"
    instructions = (control.PLAN if proof_style == "detailed" else
                    "Keep the proof plan to at most three short sentences. Then return one final ```lean4 "
                    "code block containing the complete theorem and proof. No sketches, sorry, admit, "
                    "or new assumptions. Spend the output budget on the actual Lean proof.")
    messages = [{"role": "user", "content": "Complete the following Lean 4 code:\n```lean4\n"
                 + source + "```\n" + instructions}]
    if not previous or retry_mode == "resample" or previous.get("done_reason") in {"length", "time_limit"}:
        return messages
    try:
        failed = _proof_code(previous["raw_text"])
    except ValueError:
        return messages  # Fresh sample, not misleading feedback about prose/sketches.
    feedback = previous["lean"].get("diagnostics") or previous["lean"].get("error") or previous["lean"]["status"]
    messages.extend([
        {"role": "assistant", "content": "```lean4\n" + failed + "\n```"},
        {"role": "user", "content": "Verifier feedback:\n" + feedback
         + "\nCorrect the first substantive failure, changing the proof strategy if necessary. "
         "Return a complete proof of the original statement without sorry or new assumptions. "
         "Keep any plan brief, followed by one final Lean code block."},
    ])
    return messages


def _fit_prompt(tokenizer, messages, args):
    try:
        rendered, ids = smoke._render(tokenizer, messages, args)
        return messages, rendered, ids, "repair" if len(messages) > 1 else "fresh"
    except ValueError as error:
        if "exceeds context" not in str(error) or len(messages) == 1:
            raise
    # Keep complete proof/error context or make an explicit fresh attempt.
    rendered, ids = smoke._render(tokenizer, messages[:1], args)
    return messages[:1], rendered, ids, "fresh_context_fallback"


def _replay_source(path, cohort, toolchain, lake_sha):
    text = path.read_text()
    report = json.loads(text)
    manifest = report["manifest"]
    if (manifest.get("schema") not in {"minif2f_valid_dev_v1", "minif2f_valid_dev_v2", "minif2f_valid_dev_v3", "minif2f_valid_dev_v4"}
            or control._sha(manifest) != report["binding"]
            or manifest["cohort_sha256"] != control._sha(cohort)
            or control._sha(report["cohort"]) != control._sha(cohort)
            or [row["id"] for row in report["tasks"]] != [task["id"] for task in cohort["tasks"]]):
        raise ValueError("Replay source has changed bindings or a different cohort")
    if manifest["toolchain"] != toolchain or manifest["lake_manifest_sha256"] != lake_sha:
        raise ValueError("Replay requires the original Lean environment")
    return report, control._sha(text)


def _replay(task, row, source_row, validate, save, *, passed_only=False):
    candidates = source_row.get("replays", []) + source_row["attempts"]
    if source_row.get("search", {}).get("status") == "passed":
        candidates = candidates + [{"raw_text": source_row["search"]["proof"], "lean": {"status": "passed"}}]
    if passed_only:
        candidates = [candidate for candidate in candidates if candidate.get("lean", {}).get("status") == "passed"]
    for index in range(len(row.setdefault("replays", [])), len(candidates)):
        raw = candidates[index]["raw_text"]
        row["replays"].append({"source_attempt": index + 1, "raw_text": raw, "lean": validate(task, raw)})
        if row["replays"][-1]["lean"]["status"] == "passed":
            row["complete"] = True
        save()
        if row.get("complete"):
            break


def _summary(report):
    result = smoke._summary(report)
    model_solved = result.pop("solved_with_repairs")
    after_first = result["newly_solved_by_repair"]
    replayed = sum(any(a["lean"]["status"] == "passed" for a in row.get("replays", []))
                   for row in report["tasks"])
    search_solved = sum(row.get("search", {}).get("status") == "passed" for row in report["tasks"])
    search_calls = [call for row in report["tasks"] for call in row.get("search", {}).get("calls", [])]
    completed_search_calls = sum("raw_text" in call for call in search_calls)
    solved_ids = {row["id"] for row in report["tasks"]
                  if row.get("automation", {}).get("status") == "passed"
                  or row.get("search", {}).get("status") == "passed"
                  or any(a.get("lean", {}).get("status") == "passed"
                         for a in row["attempts"] + row.get("replays", []))}
    prior_solved = set(report.get("prior_solved_ids", []))
    repaired = sum(any(a.get("lean", {}).get("status") == "passed" and a.get("prompt_mode") == "repair"
                       for a in row["attempts"][1:]) for row in report["tasks"])
    result.update(compatible_tasks=sum(row.get("input_status") == "passed" for row in report["tasks"]),
                  selection=report.get("cohort", {}).get("selection", "stratified"),
                  input_failures=sum(row.get("input_status") != "passed" for row in report["tasks"]),
                  baseline_checked_tasks=sum("automation" in row for row in report["tasks"]),
                  model_eligible_tasks=sum(row.get("input_status") == "passed"
                                           and row.get("automation", {}).get("status") != "passed"
                                           and not any(a["lean"]["status"] == "passed" for a in row.get("replays", []))
                                           for row in report["tasks"]),
                  model_attempted_tasks=sum(bool(row["attempts"] or row.get("search", {}).get("calls"))
                                            for row in report["tasks"]),
                  model_solved=model_solved + search_solved, replayed_solved=replayed,
                  search_solved=search_solved, search_calls=completed_search_calls,
                  search_tactic_trials=sum(len(row.get("search", {}).get("trials", [])) for row in report["tasks"]),
                  search_proposal_errors=sum("proposal_error" in call for call in search_calls),
                  search_duplicate_candidates=sum(row.get("search", {}).get("duplicates", 0) for row in report["tasks"]),
                  model_only_solved=result["model_only_solved"] + replayed + search_solved,
                  newly_solved_after_first=after_first, newly_solved_by_repair=repaired,
                  newly_solved_by_resample=after_first - repaired,
                  model_calls=result["model_calls"] + completed_search_calls,
                  generation_requests_started=result["generation_requests_started"] + len(search_calls),
                  cumulative_model_calls=report.get("prior_model_calls", 0) + result["model_calls"] + completed_search_calls,
                  prior_solves_revalidated=len(prior_solved & solved_ids),
                  additional_solved_since_source=len(solved_ids - prior_solved) if "prior_solved_ids" in report else 0,
                  pending_model_tasks=sum(row.get("input_status") == "passed" and not row.get("complete")
                                          for row in report["tasks"]),
                  combined_solved=result["automation_solved"] + result["model_only_solved"] + replayed + search_solved,
                  note="miniF2F-valid development subset; not official test, not pretraining-held-out accuracy.")
    if result["selection"] == "full":
        result["note"] = "Full miniF2F validation split; not official test, not pretraining-held-out accuracy."
    return result


def _recover_native_crash(report, manifest, path):
    """Narrow, explicit code migration; retain every task and remaining budget."""
    old = report["manifest"]
    error = report.get("operational_error", {})
    allowed = {"resources/ProofSearch.lean", "search/controller.py", "benchmarks/minif2f.py"}
    code_fields = {"implementation", "runner_sha256", "search_controller_sha256", "search_helper_sha256"}
    if (old.get("schema") != "minif2f_valid_dev_v4" or old.get("proof_engine") != "search"
            or control._sha(old) != report["binding"]
            or control._sha(report["cohort"]) != old["cohort_sha256"]
            or error.get("type") != "RuntimeError"
            or not error.get("message", "").startswith("Native Lean exited:")):
        raise ValueError("Recovery requires an intact standalone search checkpoint stopped by a native Lean exit")
    if {k: v for k, v in old.items() if k not in code_fields} != {
            k: v for k, v in manifest.items() if k not in code_fields}:
        raise ValueError("Native recovery cannot change cohort, environment, model or budgets")
    old_code, new_code = old.get("implementation", {}), manifest.get("implementation", {})
    changed = {key for key in old_code.keys() | new_code.keys() if old_code.get(key) != new_code.get(key)}
    if not changed or not changed <= allowed:
        raise ValueError("Native recovery permits only helper, controller and runner fixes")
    for field, filename in (("runner_sha256", "benchmarks/minif2f.py"),
                            ("search_controller_sha256", "search/controller.py"),
                            ("search_helper_sha256", "resources/ProofSearch.lean")):
        if old.get(field) != old_code.get(filename) or manifest.get(field) != new_code.get(filename):
            raise ValueError("Native recovery implementation digests are inconsistent")
    raw = path.read_bytes()
    if json.loads(raw) != report:
        raise ValueError("Checkpoint changed while preparing native recovery")
    digest = control._sha(raw.decode("utf-8"))
    backup = path.with_name(f"report.pre_native_recovery_{digest[:12]}.json")
    if backup.exists():
        if backup.read_bytes() != raw:
            raise ValueError("Native recovery backup already exists with different contents")
    else:
        with backup.open("xb") as handle:
            handle.write(raw)
    report.setdefault("recovery_history", []).append({
        "kind": "native-crash-recovery", "source_sha256": digest, "backup": backup.name,
        "previous_manifest": old, "previous_binding": report["binding"], "error": error,
        "changed_modules": sorted(changed), "budget_reset": False,
        "recovered_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    })
    report.update(manifest=manifest, binding=control._sha(manifest))
    report.pop("operational_error", None)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--lean-project", type=Path, required=True)
    parser.add_argument("--cohort", choices=("stratified", "full"), default="stratified",
                        help="Five-category development cohort or every problem in the validation index")
    parser.add_argument("--per-group", type=int, help="Problems per category in stratified mode (default: 5)")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--baseline-only", action="store_true")
    parser.add_argument("--replay-report", type=Path,
                        help="Recheck saved model outputs in a new run before spending inference")
    parser.add_argument("--replay-passed-only", action="store_true",
                        help="Revalidate prior successes without rerunning known failed candidates")
    parser.add_argument("--allow-environment-port", action="store_true")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--dtype", choices=("bfloat16", "float16"), default="bfloat16")
    parser.add_argument("--hf-cache", type=Path)
    parser.add_argument("--context-tokens", type=int, default=8192)
    parser.add_argument("--max-new-tokens", type=int, default=4096)
    parser.add_argument("--max-attempts", type=int, choices=(1, 2), default=2)
    parser.add_argument("--proof-style", choices=("concise", "detailed"), default="concise")
    parser.add_argument("--retry-mode", choices=("auto", "resample"), default="auto")
    parser.add_argument("--proof-engine", choices=("whole", "search"), default="whole")
    parser.add_argument("--search-model-calls", type=int, default=8)
    parser.add_argument("--search-tactic-trials", type=int, default=24)
    parser.add_argument("--search-nodes", type=int, default=24)
    parser.add_argument("--search-depth", type=int, default=8)
    parser.add_argument("--search-visits", type=int, default=3,
                        help="Maximum valid tactic-proposal expansions per native state")
    parser.add_argument("--search-task", action="append",
                        help="Search only this selected task; others stay pending in the fixed cohort")
    parser.add_argument("--search-branching", type=int, choices=(1, 2, 3, 4), default=3)
    parser.add_argument("--search-seconds", type=float, default=600)
    parser.add_argument("--search-tactic-timeout", type=float, default=20)
    parser.add_argument("--max-heartbeats", type=int, default=200000)
    parser.add_argument("--max-rec-depth", type=int, default=1000)
    parser.add_argument("--lean-timeout", type=float, default=120)
    parser.add_argument("--request-timeout", type=float, default=300)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--recover-native-crash", action="store_true",
                        help="With --resume, migrate a native-exit checkpoint across a narrow helper/controller fix")
    args = parser.parse_args(argv)
    if args.recover_native_crash and (not args.resume or not (args.output_dir / "report.json").exists()):
        parser.error("--recover-native-crash requires --resume and an existing report.json")
    if args.cohort == "full" and args.per_group is not None:
        parser.error("--per-group applies only to --cohort stratified")
    if args.cohort == "stratified" and args.per_group is None:
        args.per_group = 5
    search_budget, native_search = None, None
    if args.proof_engine == "search":
        from lean_proof_search.search import controller as native_search
        search_budget = native_search.SearchBudget(
            model_calls=args.search_model_calls, tactic_trials=args.search_tactic_trials,
            nodes=args.search_nodes, depth=args.search_depth, visits=args.search_visits, seconds=args.search_seconds,
            tactic_timeout=args.search_tactic_timeout)
    if ((args.per_group is not None and args.per_group < 1) or args.seed < 0 or args.context_tokens <= args.max_new_tokens
            or args.max_new_tokens < 1 or args.max_heartbeats < 1 or args.max_rec_depth < 1
            or not math.isfinite(args.lean_timeout) or args.lean_timeout <= 0
            or not math.isfinite(args.request_timeout) or args.request_timeout <= 0):
        raise ValueError("Invalid cohort or token budget")
    if args.output_dir.exists() and not args.resume:
        raise FileExistsError("Output exists; use --resume or a new directory")
    cohort = _prepare(args.output_dir, args.seed, args.per_group, args.cohort)
    context = control._lean_context(args.lean_project)
    lake = json.loads((context[0] / "lake-manifest.json").read_text())
    mathlib = next((item["rev"] for item in lake.get("packages", []) if item["name"] == "mathlib"), None)
    if mathlib is None:
        import subprocess
        root = subprocess.run(["git", "rev-parse", "--show-toplevel"], cwd=context[0],
                               capture_output=True, text=True, check=True).stdout.strip()
        if Path(root).resolve() == context[0]:
            mathlib = subprocess.run(["git", "rev-parse", "HEAD"], cwd=context[0],
                                     capture_output=True, text=True, check=True).stdout.strip()
    exact = context[1] == cohort["upstream_toolchain"] and mathlib == cohort["upstream_mathlib"]
    if not exact and not args.allow_environment_port:
        raise ValueError("Benchmark pins differ; use exact environment or explicitly --allow-environment-port")
    tasks = cohort["tasks"]
    if args.search_task and (args.proof_engine != "search" or not set(args.search_task) <= {task["id"] for task in tasks}):
        raise ValueError("Search-task pilot must name tasks in the fixed selected cohort")
    lake_sha = control._sha(lake)
    replay, replay_sha = (_replay_source(args.replay_report, cohort, context[1], lake_sha)
                          if args.replay_report else (None, None))
    if args.replay_report and args.replay_report.resolve() == (args.output_dir / "report.json").resolve():
        raise ValueError("Replay source must be a separate report")
    manifest = {"schema": "minif2f_valid_dev_v4", "cohort_sha256": control._sha(cohort),
                "selection": args.cohort,
                "source_repository": "https://github.com/jajos12/Mathematics-ai-atp",
                "source_commit": "3d996a39cf0c7ea94d2dfecaac30d741e5c3e0f2",
                "environment_mode": "upstream_pins" if exact else "explicit_development_port",
                "toolchain": context[1], "mathlib_revision": mathlib,
                "lake_manifest_sha256": lake_sha, "seed": args.seed,
                "reference": control.REFERENCE, "reference_revision": control.REVISION,
                "context_tokens": args.context_tokens, "max_new_tokens": args.max_new_tokens,
                "max_attempts": args.max_attempts, "lean_timeout": args.lean_timeout,
                "proof_style": args.proof_style, "retry_mode": args.retry_mode,
                "proof_engine": args.proof_engine,
                "search_budget": vars(search_budget) if search_budget else None,
                "search_branching": args.search_branching if search_budget else None,
                "search_tasks": sorted(set(args.search_task)) if args.search_task else None,
                "search_controller_sha256": control._sha(Path(native_search.__file__).read_text()) if native_search else None,
                "search_helper_sha256": control._sha(native_search.LEAN_HELPER.read_text()) if native_search else None,
                "max_heartbeats": args.max_heartbeats, "max_rec_depth": args.max_rec_depth,
                "replay_report_sha256": replay_sha,
                "replay_passed_only": args.replay_passed_only,
                "request_timeout": args.request_timeout, "dtype": args.dtype, "device": args.device,
                "runtime": control._runtime(), "runner_sha256": control._sha(Path(__file__).read_text()),
                "implementation": control._implementation_hashes(),
                "smoke_sha256": control._sha(Path(smoke.__file__).read_text()),
                "backend_sha256": control._sha(Path(control.__file__).read_text())}
    path = args.output_dir / "report.json"
    binding = control._sha(manifest)
    report = {"manifest": manifest, "binding": binding, "cohort": cohort,
               "tasks": [{"id": task["id"], "attempts": []} for task in tasks]}
    if replay:
        report["prior_model_calls"] = replay.get("prior_model_calls", 0) + sum(
            len(row["attempts"]) + sum("raw_text" in call for call in row.get("search", {}).get("calls", []))
            for row in replay["tasks"])
        report["prior_solved_ids"] = [row["id"] for row in replay["tasks"]
                                      if row.get("automation", {}).get("status") == "passed"
                                      or row.get("search", {}).get("status") == "passed"
                                      or any(a.get("lean", {}).get("status") == "passed"
                                             for a in row["attempts"] + row.get("replays", []))]
    if path.exists():
        report = json.loads(path.read_text())
        if report["binding"] != binding:
            if args.recover_native_crash:
                report = _recover_native_crash(report, manifest, path)
            else:
                raise ValueError("Resume changes selection, sources, environment, code or budgets")
    save = lambda: control._write(path, report)
    validate = lambda task, raw: _validate(task, raw, context, args.lean_timeout,
                                         args.max_heartbeats, args.max_rec_depth)
    save()
    for task, row in zip(tasks, report["tasks"]):
        if "input_status" not in row:
            if task["ingest_status"] != "parsed":
                row["input_status"] = "unsupported"
                row["input_error"] = task["ingest_error"]
            else:
                preflight = (_spec(task, args.max_heartbeats, args.max_rec_depth)
                             + f"theorem Input_{task['id']} : True := by trivial\n#print axioms Input_{task['id']}\n")
                row["input_check"] = control._check_source(preflight, "Input_" + task["id"], context, args.lean_timeout)
                row["input_status"] = row["input_check"]["status"]
            if row["input_status"] != "passed":
                row["complete"] = True
                print(json.dumps({"task": task["id"], "input": row["input_status"]}), flush=True)
            save()
        if row["input_status"] == "passed" and "automation" not in row:
            row["automation"] = validate(task, smoke.AUTOMATION)
            if row["automation"]["status"] == "passed":
                row["complete"] = True
            save()
            print(json.dumps({"task": task["id"], "input": row["input_status"],
                               "automation": row["automation"]["status"]}), flush=True)
    if replay:
        for task, row, source_row in zip(tasks, report["tasks"], replay["tasks"]):
            if not row.get("complete"):
                _replay(task, row, source_row, validate, save, passed_only=args.replay_passed_only)
                print(json.dumps({"task": task["id"], "replayed": len(row["replays"]),
                                  "replayed_solved": row.get("complete", False)}), flush=True)
    report["summary"] = _summary(report)
    save()
    if not args.baseline_only and any(
            not row.get("complete") and (not args.search_task or row["id"] in args.search_task)
            for row in report["tasks"]):
        args.backend, args.min_free_disk_gib, args.local_files_only = "transformers", 1, True
        report["cached_weights"] = smoke._cached_weights(args.hf_cache)
        report["preflight"] = control._preflight(args)
        from transformers import PreTrainedTokenizerFast
        tokenizer = PreTrainedTokenizerFast.from_pretrained(control.REFERENCE, revision=control.REVISION,
                                                            cache_dir=args.hf_cache, local_files_only=True)
        with ExitStack() as stack:
            model = None

            def generate(messages, seed, remaining_seconds=None):
                nonlocal model
                started = time.monotonic()
                messages, rendered, ids, prompt_mode = _fit_prompt(tokenizer, messages, args)
                if model is None:
                    model = stack.enter_context(control.ReferenceGenerator(args, tokenizer))
                timeout = args.request_timeout
                if remaining_seconds is not None:
                    left = remaining_seconds - (time.monotonic() - started)
                    if left <= 0:
                        raise TimeoutError("Search time budget exhausted during model initialization")
                    args.request_timeout = min(timeout, left)
                try:
                    result = model.generate(ids, seed)
                    result["request_time_budget"] = args.request_timeout
                finally:
                    args.request_timeout = timeout
                result.update(messages=messages, prompt_mode=prompt_mode,
                              prompt_sha256=control._sha(rendered), input_ids_sha256=control._sha(ids))
                return result

            try:
                if native_search:
                    for index, (task, row) in enumerate(zip(tasks, report["tasks"])):
                        if row.get("complete") or (args.search_task and task["id"] not in args.search_task):
                            continue
                        native_search.search(
                            task, row.setdefault("search", {}), generate,
                            lambda task, raw, left: _validate(task, raw, context, min(args.lean_timeout, left),
                                                             args.max_heartbeats, args.max_rec_depth),
                            save, lambda left: native_search.NativeSession(
                                task, context, heartbeats=args.max_heartbeats, rec_depth=args.max_rec_depth,
                                timeout=min(args.lean_timeout, left)),
                            budget=search_budget, branching=args.search_branching, seed=args.seed + index * 100)
                        row["complete"] = True
                        report["summary"] = _summary(report)
                        save()
                        print(json.dumps({"task": task["id"], "search": row["search"]["status"],
                                          "stop_reason": row["search"].get("stop_reason"),
                                          "calls": len(row["search"]["calls"]),
                                          "trials": len(row["search"].get("trials", [])),
                                          "proposal_errors": sum("proposal_error" in call for call in row["search"]["calls"]),
                                          "nodes": len(row["search"].get("nodes", []))}), flush=True)
                else:
                    smoke._run(report, generate, validate, save, args.max_attempts, args.seed, tasks=tasks,
                               messages_factory=lambda task, previous: _messages(
                                   task, previous, proof_style=args.proof_style, retry_mode=args.retry_mode),
                               automation_first=True)
            except Exception as error:
                report["operational_error"] = {"type": type(error).__name__, "message": str(error)}
                report["summary"] = _summary(report)
                save()
                raise
    report.pop("operational_error", None)
    report["summary"] = _summary(report)
    save()
    print(json.dumps({"report": str(path), **report["summary"]}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
