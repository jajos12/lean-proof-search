# Extraction and experiment provenance

Source fork: `https://github.com/jajos12/Mathematics-ai-atp`.
Source upstream: `https://github.com/iCog-Labs-Dev/Mathematics-ai-atp`.
Extraction commit: `3d996a39cf0c7ea94d2dfecaac30d741e5c3e0f2`.

Migrated source components:

- `scripts/proof_search.py` → `search/controller.py`
- `lean/ProofSearch.lean` → packaged `resources/ProofSearch.lean` (verbatim)
- `scripts/run_minif2f_dev.py` → `benchmarks/minif2f.py` and `lean/tasks.py`
- Used helpers from `run_proof_smoke.py` → `generation.py`
- Used helpers from `compare_prover_backends.py` → `lean/checker.py`,
  `models/reference.py`, `artifacts.py`, and the internal runtime facade
- Native-search and miniF2F regression tests → standalone tests

Extraction equivalence check compared 28 critical function/class ASTs with the
source commit, normalizing import paths and docstrings only: all matched.
The native helper is byte-identical, SHA-256
`ece7ad3e391a9484889ce51e1966f7856977dd68aa4a1dfffd3becb45deb6118`.
Package metadata/schema changes and explicit generation callbacks are migration
boundaries; they do not change theorem selection, generation or verification.

## Frozen assets

| Component | Pin |
|---|---|
| Reference model | `deepseek-ai/DeepSeek-Prover-V2-7B` |
| Model revision | `a8d9e14432b2e8dd9df2a4d4e70f1ba9bc8d9b7b` |
| miniF2F repo | `yangky11/miniF2F-lean4` |
| miniF2F revision | `5746b7d6c47855ce1294bed87329618ff7f1bc31` |
| Cohort | Valid only, seed 42, five each IMO/AIME/algebra/induction/number theory |
| Upstream Lean | `leanprover/lean4:v4.24.0` |
| Upstream Mathlib | `f897ebcf72cd16f89ab4577d0c826cd14afaafc7` |
| Native server Lean | `leanprover/lean4:v4.10.0-rc1` |
| Server Mathlib | `29dcec074de168ac2bf835a77ef68bbe069194c5` |

Observed reference server Python packages: Torch `2.7.1+cu118`, Transformers
`5.18.0`, tokenizers `0.23.2`, Accelerate `1.15.0`, requests `2.34.2`.
These are provenance, not an instruction to overwrite a working CUDA environment.

Sampling retained: temperature 0.6, top-p 0.95, top-k 50, min-p 0,
repetition penalty 1.0, one beam and deterministic recorded seeds.

## Last measured source result

**12/25** accumulated development solves: three automation, nine prior model
proofs. Adapter pilot attempted AIME 1988 and binomial sum, adding zero solves
after 24 new calls (118 cumulative). Ten other compatible tasks remained pending.

The repaired prefix/binder/premise implementation was published at extraction
commit, and its server run was pending. A local human-repaired AIME proof on
Lean 4.29 is diagnostic evidence, not an automated solve. Native interactive
backend is verified for 4.10; a separate 4.29 interactive probe disconnected.

Neither repository migration nor passing scripted tests changes benchmark
coverage. Results remain development-port measurements with unknown pretraining
exposure, not official miniF2F test accuracy.

## Extraction verification

- **54 tests passed** in a fresh editable-install virtual environment containing
  only core/test dependencies, including real pinned Lean 4.10 integration.
- **54 tests passed** again from an installed wheel, run with isolated Python
  outside both source checkouts. The wheel contains the native Lean helper.
- Core import checks confirm ATP/GNN, Torch, Transformers and Pantograph are not
  imported. ATP is absent from the wheel environment's import path.
- All four historical supplied reports (v1, v2, two v3 reports) pass binding and
  replay-source validation. Common summaries retain **8/12/12/12 solves** and
  **31/58/94/118 cumulative model calls**, respectively.
- The one-time 28-definition AST equivalence and verbatim helper checks are
  documented above. No new inference or benchmark solve is part of extraction.
