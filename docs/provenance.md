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

## Completed full-validation search audit

Report: `runs/minif2f_valid_full_search/report.json`; supplied audit copy SHA-256
`d493edb7bb9695a587fd6e6af54b6418d276326c95490ed62e14e296139a76a6`
(25,610,710 bytes). This is a saved-record audit, not another model run.

### Verification and recovery

- Manifest/cohort bindings and recalculated summary match. All implementation
  digests match commit `1ddf0bf`; the recovery ledger's previous implementation
  matches `24741c4`. Exactly the documented helper/controller/runner files change.
- One recovery is recorded. Cohort, environment, model and budgets are unchanged;
  `budget_reset` is false. Previous manifest binding verifies. The archived
  pre-recovery report itself was not supplied for independent byte comparison.
- All 244 tasks finish; all 159 baseline solves are retained. Exactly the 81
  compatible baseline failures are searched. Final operational error is absent.
- Disjoint success sets are **57 automation + 102 replayed model + 23 search**.
  All 182 stored successful compilation records have zero exit status and only
  permitted axioms. All 23 new proof scripts reconstruct exactly from their
  winning recorded paths and match final-check records. This audit does not
  independently recompile the corpus in the server environment.
- Final verifier rejects **25** closed native candidates across two tasks:
  23 depend on a host `Expected_...` specification axiom and two contain
  `sorryAx` in compilation records. None counts as a solve. This does not mean
  the latter two necessarily contained an explicit model-written sorry tactic.

### Result

**182/244 = 74.59%**, compared with **159/244 = 65.16%**: **+23 proofs / +9.43
percentage points**. Search resolves **23/81 = 28.40%** of compatible baseline
failures; 58 remain unsolved. All four input failures are confirmed ambiguity
between Real π and Nat's π notation:
`imo_1962_p4`, `imo_1965_p1`, `aime_1997_p11`, `imo_1966_p4`.

Recorded execution uses RTX 3090 BF16, Torch **2.15.0**, Transformers **5.18.0**,
tokenizers **0.23.2**, Accelerate **1.15.0**, requests **2.34.2**, and the pinned
Lean **4.10.0-rc1** development port. These are the actual final-run values,
distinct from the earlier source-server environment above.

### Additional search cost

| Metric | Recorded value |
|---|---:|
| Completed new model generations | 1,059 |
| Controller generation attempts | 1,082 |
| Pre-generation context overflows | 23 |
| Cumulative completed model calls, including 183 baseline | 1,242 |
| Generated output tokens | 1,450,808 |
| Prompt tokens | 1,760,894 |
| Summed model-generation wall time | 11h24m47s |
| Summed active search time | 13h20m16s |
| Native tactic/prefix trials | 1,989 |
| Stored graph nodes, including roots | 1,133 |
| Duplicate candidates skipped | 180 |

Active search time includes native work and model loading, but excludes initial
source/input/automation/replay phases and downtime while fixing the crash.
Baseline generation-token/time totals are not included in this report; only
its call count is carried forward. Amortized additional cost is **46.0 completed
generations / 34.8 active minutes per new proof**, including failed searches.

Successful tasks consume **67 calls and 54m52s active time**; failures consume
992 completed generations plus 23 rejected attempts and **12h25m23s**. Thus
**93.14%** of active search time goes to unresolved tasks. Median successful
task: **2 calls / 93.65 s**. Nineteen of 23 successes finish within five minutes,
22 within ten. These are observations, not a counterfactual shorter-budget score.

### How proofs were completed

- **20/23** winners close directly from the root with one accepted candidate
  block. Each closes at the end of its selected parsed prefix group.
- **3/23** use an intermediate state and a second accepted block:
  `induction_ineq_nsqlefactn`, `amc12_2000_p11`, and
  `algebra_xmysqpymzsqpzmxsqeqxyz_xpypzp6dvdx3y3z3`.
- No winning path exceeds two accepted blocks. A block may contain many tactics;
  this is graph-path depth, not mathematical proof length. The gain is real,
  but the successful behavior here is mostly root-level candidate generation,
  adaptation and verification rather than deep multi-state search.
- Five winning tasks receive nonempty automatic premise hints. Across all search
  tasks, 43 receive nonempty hints, 74 query-cache entries are stored, and model
  makes zero explicit lookup requests. Hint exposure does not establish use or
  necessity. No GNN is active.

### Remaining limitations

Failure stops: **28 frontier**, **17 wall-time**, **8 model-call**, **5 trial-cap**.
Twenty-three failed tasks never retain a successor; six saturate the 64-node cap.
Diagnostic categories overlap: arithmetic failures on 39/58 tasks, introduction
context errors on 26, native timeouts on 22, unknown local names on 14, type/
unification errors on 13, recursion limits on six, rewrite mismatches on five,
and unknown global constants on one. These are recorded error categories, not
exclusive mathematical root-cause labels.

Proposal errors total 229: 105 placeholder proofs, 71 unclosed blocks, 16 missing
code responses, eight declarations in tactic proposals, five empty alternatives,
one malformed proposal schema and 23 prompt-context overflows. All 23 overflows
occur on `amc12a_2009_p15`; they consume request slots without reaching generation.
They explain the difference between attempted and completed-call counters.
Per-task call/trial/node/depth bounds hold; largest active-time overshoot is
1.35 seconds, consistent with cooperative generation/cleanup stopping.

Freeze this report and its recovery history as the completed validation result.
Official test remains untouched. No further model experiment was performed as
part of this audit.

This score belongs to this exact full run, not the union of historical pilots.
For example, earlier console output reported an AIME 1988 pilot success, while
`aime_1988_p4` exhausts time in this full run. Historical pilot proofs were not
replayed into the fresh 159-proof baseline. Cohort ordering changes recorded
generation seeds, and execution protocols also evolved between experiments.
