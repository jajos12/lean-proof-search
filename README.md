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

The cohort is pinned to **25 validation-only problems**, five each from IMO,
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

Latest measured source-system coverage: **12/25 accumulated development solves**
(three automation, nine generated proofs). The previous two-task search pilot
added zero solves. The prefix/binder/proactive-lookup repair and this repository
extraction do not imply a new measured solve count. Carry-forward results and
new calls are reported separately with cumulative cost.

Model weights, Mathlib builds, graphs and run artifacts remain external. GNN
guidance is an optional future adapter. This repository contains the prover,
not GNN training infrastructure.

## License

GPL-2.0, preserving the source project's license. See [LICENSE](LICENSE) and
[NOTICE](NOTICE) for extraction provenance and attribution.
