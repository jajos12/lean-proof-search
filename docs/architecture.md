# Architecture

## Modules

| Module | Responsibility |
|---|---|
| `lean.tasks` | Immutable source headers, candidate extraction, frozen target construction |
| `lean.checker` | Installed Lean context, compilation, permitted-axiom checking |
| `search.controller` | Native process, snapshots, prefixes, frontier, typed lookup |
| `resources/ProofSearch.lean` | Lean-native tactic/metavariable snapshots and parser operations |
| `models.reference` | Optional pinned cached BF16 inference |
| `generation` | Tokenizer fidelity, cached weights, whole-proof scheduling and counters |
| `benchmarks.minif2f` | Fixed validation cohort and experiment CLI |
| `artifacts` | Stable digests, atomic checkpoints, runtime and implementation provenance |
| `runtime` | Internal compatibility facade for extracted experiment contracts |

The search and verifier import no benchmark runner, ATP module, Torch, PyG or
Pantograph package. The generator/verifier/session factory are injected into
`run_search`. The bundled miniF2F adapter supplies the default wiring.

## Search mechanics

Nodes hold actual native Lean states and full accepted tactic paths. The frontier
prefers fewer expansions, fewer goals and greater verified prefix progress.
Model alternatives execute independently from a saved parent. All sibling goals
must be solved. A cumulative prefix is saved before the next prefix is attempted;
later failure or timeout does not erase its recorded path.

Native syntax boundaries preserve compound tactics and bullets. A nested proof
is unwrapped only when its proposition matches the actual goal. Redundant
universal-style introductions may be aligned by reverting user locals, but only
when Lean verifies the resulting sequent equals the frozen original proposition.
Generated headers never add hypotheses to the active state.

Each state receives up to two cached typed lexical premise queries before model
generation. Optional lookup errors are recorded and recoverable. Queries do not
consume model calls. Explicit model lookup requests are also supported.

Duplicates are checked per parent. Printed-goal equality is not a native-state
equivalence test and is not used to merge nodes. Native IDs are transient;
checkpoint resume recreates them by executing accepted paths.

## Verification

Zero native goals is only a candidate result. The complete path is independently
compiled against the original elaborated proposition. Only `propext`,
`Classical.choice`, and `Quot.sound` are permitted axioms. Host specification
stubs, sorry and extra generated assumptions cannot establish a solve.

## Reports

New reports use `minif2f_valid_dev_v4`. The manifest includes all package Python
and Lean implementation digests, original extraction commit, selected-source
hashes, environment/model pins, budgets and source-report digest. Migration uses
old reports as immutable candidate sources in new directories, not in-place resume.

Whole-proof generation, native search, replayed proofs and cumulative inference
are separate counters. Pilot task filters retain all selected tasks; other
unsolved tasks remain pending. GNN ranking is not part of the current controller.
