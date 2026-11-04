"""Shared generation contracts, cached weights and whole-proof execution."""

import json
from pathlib import Path

from . import runtime as control

AUTOMATION = "intros\nsolve\n| rfl\n| assumption\n| aesop (config := { maxRuleApplications := 50 })\n| omega\n| ring\n| nlinarith"


def _render(tokenizer, messages, args):
    text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
    encoded = tokenizer.apply_chat_template(messages, tokenize=True, add_generation_prompt=True)
    ids = encoded["input_ids"] if hasattr(encoded, "keys") else encoded
    if (tokenizer.encode(text, add_special_tokens=False) != ids
            or tokenizer.decode(ids, skip_special_tokens=False, clean_up_tokenization_spaces=False) != text):
        raise ValueError("Tokenizer changes rendered prompt")
    if len(ids) + args.max_new_tokens > args.context_tokens:
        raise ValueError("Prompt plus output budget exceeds context; no silent truncation")
    return text, ids


def _cached_weights(cache):
    from huggingface_hub import hf_hub_download
    try:
        index = Path(hf_hub_download(control.REFERENCE, "model.safetensors.index.json",
                                    revision=control.REVISION, cache_dir=cache, local_files_only=True))
        payload = json.loads(index.read_text())
        paths = [Path(hf_hub_download(control.REFERENCE, name, revision=control.REVISION,
                                      cache_dir=cache, local_files_only=True))
                 for name in sorted(set(payload["weight_map"].values()))]
        if sum(path.stat().st_size for path in paths) < payload["metadata"]["total_size"]:
            raise ValueError("Incomplete cached shards")
        return {"snapshot": str(index.parent), "shards": len(paths)}
    except Exception as error:
        raise RuntimeError("Official pinned checkpoint is not fully cached; no download attempted. "
                           "Use the same HF cache as the successful backend control.") from error


def _summary(report):
    rows = report["tasks"]
    passed = lambda attempt: attempt.get("lean", {}).get("status") == "passed"
    solved = [any(passed(a) for a in row["attempts"]) for row in rows]
    baseline = [row.get("automation", {}).get("status") == "passed" for row in rows]
    first = [bool(row["attempts"]) and passed(row["attempts"][0]) for row in rows]
    return {"selected_tasks": len(rows), "completed_tasks": sum(r.get("complete", False) for r in rows),
            "first_try_solved": sum(first), "solved_with_repairs": sum(solved),
            "newly_solved_by_repair": sum(s and not f for s, f in zip(solved, first)),
            "automation_solved": sum(baseline),
            "model_only_solved": sum(s and not b for s, b in zip(solved, baseline)),
            "automation_only_solved": sum(b and not s for s, b in zip(solved, baseline)),
            "model_calls": sum(len(r["attempts"]) for r in rows),
            "generation_requests_started": report.get("generation_requests_started", 0),
            "note": "Curated compatibility smoke, not held-out accuracy or causal feedback ablation."}


def _run(report, generate, validate, save, max_attempts, seed, *,
         tasks, messages_factory, automation_first=False):
    for index, (task, row) in enumerate(zip(tasks, report["tasks"])):
        if row.get("complete") or any(a.get("lean", {}).get("status") == "passed" for a in row["attempts"]):
            row["complete"] = True
            save()
            continue
        if "automation" not in row:
            row["automation"] = validate(task, AUTOMATION)
            save()
        if automation_first and row["automation"]["status"] == "passed":
            row["complete"] = True
            save()
            continue
        while len(row["attempts"]) < max_attempts or (
                row["attempts"] and "lean" not in row["attempts"][-1]):
            if row["attempts"] and "lean" not in row["attempts"][-1]:
                attempt = row["attempts"][-1]
            else:
                previous = row["attempts"][-1] if row["attempts"] else None
                attempt_seed = seed + index * 10 + len(row["attempts"])
                report["generation_requests_started"] = report.get("generation_requests_started", 0) + 1
                save()
                attempt = generate(messages_factory(task, previous), attempt_seed)
                row["attempts"].append(attempt)
                save()
            attempt["lean"] = validate(task, attempt["raw_text"])
            save()
            print(json.dumps({"task": row["id"], "attempt": len(row["attempts"]),
                              "status": attempt["lean"]["status"], "done_reason": attempt.get("done_reason"),
                              "output_tokens": attempt.get("output_tokens")}), flush=True)
            if attempt["lean"]["status"] == "passed":
                break
        row["complete"] = True
        save()
    return _summary(report)
