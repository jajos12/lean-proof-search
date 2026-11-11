# Lean Proof Search

Bounded LLM proof search over **native Lean snapshots**. A model proposes code,
Lean checks transitions, and the controller retains verified prefixes and
explores alternatives. A complete proof counts only after separate compilation
at the frozen original proposition and an allowed-axiom audit.

Extracted from Mathematics-ai-atp at
[`3d996a3`](https://github.com/jajos12/Mathematics-ai-atp/commit/3d996a39cf0c7ea94d2dfecaac30d741e5c3e0f2).
See [migration](docs/migration.md), [architecture](docs/architecture.md) and
[experiment provenance](docs/provenance.md).

## Install

Python **3.10+**, `elan`, and an existing compiled Lean/Mathlib project are
required for theorem runs. Native integration is verified on Lean
**4.10.0-rc1**. GPU inference is optional for using/testing the core package.

```sh
python -m pip install -e '.[test]'
```

To install the optional reference inference backend:

```sh
python -m pip install -e '.[inference]'
```

In the existing server `atp` environment, retain its working CUDA packages:

```sh
python -m pip install --no-deps --no-build-isolation -e .
```

The bundled backend uses cached **DeepSeek-Prover-V2-7B BF16**, pinned to
`a8d9e14432b2e8dd9df2a4d4e70f1ba9bc8d9b7b`. The controller accepts generation
callbacks, so other models can be integrated independently. Weights are loaded
once, with explicit tokenizer-fidelity and context-budget checks.

## REST API

Install the optional HTTP dependencies in your working environment:

```sh
python -m pip install -e '.[api]'
```

The existing server `atp` environment already has inference dependencies.
For a fresh GPU environment, install `'.[api,inference]'` instead. API installation
does not download model weights; inference uses the same pinned local HF cache.

Start from this repository root:

```sh
ATP_REPO=../Mathematics-ai-atp
LD_PRELOAD= lean-proof-api \
  --lean-project "$ATP_REPO/maths_ai/_support_files/sexpr_environment/mathlib4" \
  --job-dir runs/api --host 127.0.0.1 --port 8000
```

Interactive OpenAPI documentation: `http://127.0.0.1:8000/docs`.

| Endpoint | Behavior |
|---|---|
| `GET /health` | Worker readiness, live queue count, cached-model state |
| `POST /v1/proofs` | Enqueue an individual Lean proposition; returns HTTP 202 |
| `GET /v1/proofs/{id}` | Poll phase/progress, terminal result, verification and cost |
| `POST /v1/proofs/{id}/cancel` | Cancel queued work or request running cancellation |

In another terminal:

```sh
curl http://127.0.0.1:8000/health

curl -X POST http://127.0.0.1:8000/v1/proofs \
  -H 'Content-Type: application/json' \
  -d '{"statement":"∀ n : Nat, n + 0 = n","engine":"search","limits":{"model_calls":8,"seconds":300},"seed":42}'

# Replace proof_ID with the returned id:
curl http://127.0.0.1:8000/v1/proofs/proof_ID
curl -X POST http://127.0.0.1:8000/v1/proofs/proof_ID/cancel
```

Input is a **closed Lean proposition**, not a natural-language question or whole
theorem declaration. Project/imports/model/cache are server-controlled; client
paths and unknown request fields are rejected. `engine` is `whole` or `search`.
Automation is attempted first in either mode; easy goals can finish without
loading a model. The response reports actual successful method.

One worker serializes proof jobs and reuses one lazily loaded model across jobs.
Default queue capacity is 16 queued jobs plus one running job. Queue full returns
429; unavailable worker 503; invalid request 422; unknown job 404. Polling exposes
`phase` and `progress` while running, then `stats` on completion. There is no
benchmark-launch or streaming endpoint in this initial API.

Successful results contain `verified: true`, permitted axioms and complete
`lean_code`. Certificates are independently compiled without the host specification
axiom. Other terminal states are `exhausted`, `failed` and `cancelled`.

Request limits: **1–32 model calls**, **1–1,200 active seconds**, statement up to
16,000 characters. Defaults are eight calls and 300 seconds. Active time starts
when the worker begins, excludes queue/downtime, and includes input/automation,
model loading, generation and verification. Automation has a separate 20 s cap;
generation 120 s, compiler 180 s, each also bounded by remaining active time.
Search retains tested 96-trial/64-node/depth-16/6-expansion bounds and 20 s native
request limits. Whole mode uses independent complete-proof samples until success
or limits. Generation stopping is cooperative, not hard GPU cancellation.

Queued cancellation is immediate and frees capacity. Running cancellation is
best-effort at checkpoints; an ongoing generation/compiler call finishes or
times out first. Job artifacts/checkpoints live under `runs/api`. After process
restart, queued/interrupted jobs resume from the last saved state only when
policy/code/environment and request bindings match; otherwise they fail explicitly.
Saved completions are reused; an interrupted call without output remains charged
as a request slot. Abrupt termination can lose work/time since the last checkpoint.
Completed histories remain pollable and full traces stay on disk.

For remote binding, set `LEAN_PROOF_API_KEY` and use `--host 0.0.0.0`. Send
`Authorization: Bearer <key>` on proof endpoints; health stays unauthenticated.
The service refuses non-loopback binding without a key. Use one API process per
GPU; the launch command fixes one Uvicorn worker.

**Deployment boundary:** Lean elaborators/tactics can execute IO. This API is for
trusted callers; a bearer token is not a sandbox. Isolate Lean execution in a
restricted container/process environment before accepting untrusted public inputs,
and terminate HTTPS at your existing proxy for remote access.

Tests cover endpoints, auth, queue capacity, cancellation, restart policy, actual
Lean input/automation/whole/search certificate checks, and cached-backend reuse.
Model-path tests use scripted generators; they do not download weights or claim
new benchmark solves. Install `'.[test,api]'` to run API tests.

## Full miniF2F validation baseline, search disabled

Problem loading is implemented in `src/lean_proof_search/benchmarks/minif2f.py`.
`--cohort full` loads **all 244 validation problems**, including AMC and mathd
categories. `--cohort stratified` (default) retains the original 25-problem pilot;
`--per-group` applies only to that stratified mode.

From this repository root in `atp`, with the ATP checkout available as a sibling:

```sh
git pull --ff-only origin main
ATP_REPO=../Mathematics-ai-atp
LD_PRELOAD= lean-proof-search \
  --output-dir runs/minif2f_valid_full_whole \
  --lean-project "$ATP_REPO/maths_ai/_support_files/sexpr_environment/mathlib4" \
  --cohort full --proof-engine whole --max-attempts 1 --proof-style concise \
  --allow-environment-port --seed 42 --device cuda:0 --dtype bfloat16 \
  --context-tokens 12288 --max-new-tokens 4096 --request-timeout 120 \
  --max-heartbeats 1000000 --max-rec-depth 2048 --lean-timeout 180
```

This is a fresh automation-plus-whole-proof baseline. Automation runs first;
the model gets one complete-proof attempt on each compatible unsolved problem.
There is no native proof search, feedback retry, or carry-forward of earlier
proofs. Compare `automation_solved`, `model_solved`, and `combined_solved`, along
with input failures, calls and token/time records.

Selection and source hashes are fixed before compatibility/inference. Loading
checkpoints each file, shows progress every 25 files, and resumes interrupted
downloads with the same command plus `--resume`. Completed cached files are
verified and reused. Source/compatibility failures stay in the full denominator.
Use separate output directories for full and stratified runs. Official test
sources are untouched. Existing legacy cohorts without a selection field remain
valid stratified cohorts; their original selection/digests are preserved.

Verification: **65 tests pass**, including real Lean 4.10 regressions, full-mode
CLI execution, all-category selection, source-download resume, unsupported-input
retention and cache/mode checks. Actual pinned preparation fetched and parsed
**244/244 source headers**; server Lean compatibility/model outcomes are not
implied by parsing. The cached full split reopens without network access, and
its original 25-task subset matches the historical cohort digest exactly.

## Run the fixed miniF2F development pilot

From this repository root, with the old ATP checkout available as a sibling:

```sh
ATP_REPO=../Mathematics-ai-atp
LD_PRELOAD= lean-proof-search \
  --output-dir runs/prefix_context_pilot \
  --lean-project "$ATP_REPO/maths_ai/_support_files/sexpr_environment/mathlib4" \
  --replay-report "$ATP_REPO/maths_ai/gnn_inference/runs/minif2f_native_adapter_pilot/report.json" \
  --replay-passed-only --proof-engine search --allow-environment-port \
  --per-group 5 --seed 42 --device cuda:0 --dtype bfloat16 \
  --context-tokens 12288 --max-new-tokens 4096 --request-timeout 120 \
  --max-heartbeats 1000000 --max-rec-depth 2048 --lean-timeout 180 \
  --search-task aime_1988_p4 --search-task numbertheory_sumkmulnckeqnmul2pownm1 \
  --search-model-calls 32 --search-tactic-trials 96 --search-nodes 64 \
  --search-depth 16 --search-visits 6 --search-seconds 1200 --search-tactic-timeout 20
```

`python -m lean_proof_search` is equivalent to the console command. All paths
are explicit: the package does not need an ATP installation or import any GNN
code. `--replay-report` is optional; omit it for a fresh run. Reuse the same HF
cache as the original backend control (`--hf-cache` if needed).

The default stratified cohort is pinned to **25 validation-only problems**, five each from IMO,
AIME, algebra, induction and number theory. Selection precedes compatibility
checks. Original headers and source hashes are preserved; failures remain in
the denominator. Official test problems are not fetched. Pretraining exposure
is unknown; these development runs are not official held-out accuracy.

Upstream uses Lean **4.24.0** / Mathlib
`f897ebcf72cd16f89ab4577d0c826cd14afaafc7`. Existing server runs use an explicit
Lean **4.10.0-rc1** development port. `--allow-environment-port` records that
distinction instead of rewriting statements.

Add `--baseline-only` for CPU input/automation/replay checks. Repeat the same
command with `--resume` after interruption. Changed code/environment/budgets
require a new directory. Old ATP v1/v2/v3 reports can seed fresh runs; their
completed proofs are reverified, never trusted solely by saved status.

### Recover a native Lean process crash

Lean recursion/heartbeat limits are runtime exceptions; ordinary Lean `try/catch`
does not catch them. The helper explicitly catches runtime exceptions and resets
request recursion accounting. Exited native processes during split/lookup/step
are isolated; one fresh-process retry replays accepted prefixes. Wall timeouts
are not retried, and completed model responses remain checkpointed.

After installing this fix, continue the **same original command and output
directory**, adding:

```sh
--resume --recover-native-crash
```

The explicit recovery option only permits a saved standalone search run stopped
with `Native Lean exited:`. It verifies old bindings and allows implementation
changes only to native helper, controller and benchmark runner. Cohort, model,
environment and all budgets must match exactly. Normal `--resume` remains strict.

Before migration, the complete original JSON is archived next to the report as
`report.pre_native_recovery_<digest>.json`. `recovery_history` retains the old
manifest/binding/error and records the patch boundary. Completed tasks, verified
proofs, current graph, saved answers, call counts and active elapsed time are
preserved; budgets are **not reset**. Subsequent resumes are idempotent. Results
span a documented recovery revision, not an uninterrupted single-code run.

## Core API

```python
from lean_proof_search import NativeSession, ProofTask, SearchBudget, run_search, verify

task: ProofTask = {
    "id": "example_goal",
    "prefix": "import Lean\n",
    "header": "theorem example_goal (P : Prop) (h : P) : P",
    "ingest_status": "parsed",
}
```

`run_search` accepts a task, checkpoint record, generation callback, verification
callback and native-session factory. Generation receives messages, seed and
remaining active seconds. No model library is imported until its backend is
used. See the native tests for complete scripted examples and the benchmark
runner for the cached Transformers adapter.

## Tests

```sh
LEAN_PROOF_SEARCH_TOOLCHAIN=leanprover/lean4:v4.10.0-rc1 python -m pytest -q
```

Without that variable, native integration tests skip; pure Python tests still
run. Tests use scripted model responses, not downloaded weights, and verify
prefix retention, binder alignment, rollback, sibling goals, local lets,
typed lookup, forbidden axioms, cached replay and resume.

## Results and scope

Completed full-validation baseline: **159/244 (65.2%)** from 57 automation and
102 one-shot model proofs. Search on the remaining 81 compatible failures added
**23 proofs**, giving **182/244 (74.6%)**, a **9.4 percentage-point gain**.
Remaining: 58 compatible unsolved problems and four input failures.

Audited search cost: **1,059 completed model generations**, **1.45M output tokens**
and **13h20m summed active search time**. About 93% of that time went to unsolved
tasks. Twenty wins closed from a root candidate block; three used two accepted
blocks. Complete proof scripts and stored compiler/axiom checks were consistent.
See [full audit](docs/provenance.md#completed-full-validation-search-audit).

This is a recovered validation development-port run, not official test accuracy.
Additional coverage is measured with additional compute. Old reports and new
calls are accounted separately; no test-split result is reported.

Model weights, Mathlib builds, graphs and run artifacts remain external. GNN
guidance is an optional future adapter. This repository contains the prover,
not GNN training infrastructure.

## License

GPL-2.0, preserving the source project's license. See [LICENSE](LICENSE) and
[NOTICE](NOTICE) for extraction provenance and attribution.
