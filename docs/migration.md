# Server migration

The original ATP checkout can continue its current run. This extraction uses
source commit `3d996a39cf0c7ea94d2dfecaac30d741e5c3e0f2`; its code, artifacts and
reports remain available for provenance.

## Existing `atp` environment

From the parent directory containing `Mathematics-ai-atp`:

```sh
git clone https://github.com/jajos12/lean-proof-search.git
```

The repository is private initially; use authenticated GitHub access on the
server (or `gh repo clone jajos12/lean-proof-search`).

Open the new repository directory and run:

```sh
python -m pip install --no-deps --no-build-isolation -e .
lean-proof-search --help
```

Use the full command in README. Relative external paths assume sibling checkouts;
absolute paths also work. Existing Mathlib need not be copied or rebuilt. Existing
HF cache is reused; use `--hf-cache` for a nondefault location. No model download
is attempted by the reference runner.

If the most recent `minif2f_prefix_context_pilot/report.json` exists, it can be the
source instead of the older adapter-pilot report. Source report must match the
selected cohort and actual Lean toolchain/Lake manifest. Prior successful proofs
are rechecked. Changed package/module hashes require a fresh output directory.

Old `MATHS_AI_MINIF2F_CORE_LEAN` test configuration remains accepted as a migration
alias; new tests use `LEAN_PROOF_SEARCH_TOOLCHAIN`.

## Fresh environment

```sh
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test,inference]'
```

Install CUDA-enabled Torch suitable for the host and provide a compiled Lean
project matching the intended experiment. The historical server environment is
recorded in `docs/provenance.md`; dependencies are optional so existing working
CUDA installations can be retained.

## Independence

Core and CLI imports resolve entirely inside `lean_proof_search`. ATP is only an
optional location for external historical reports and an existing Lean project.
A different compiled project path and a fresh run require no ATP checkout at all.
