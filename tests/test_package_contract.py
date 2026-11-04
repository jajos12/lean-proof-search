"""Installation independence, legacy reports and verifier/tokenizer contracts."""

import importlib.resources
import json
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

from lean_proof_search import artifacts, generation, runtime
from lean_proof_search.benchmarks import minif2f
from lean_proof_search.lean import checker
from lean_proof_search.search.controller import LEAN_HELPER


def test_installed_core_import_is_independent_of_atp_and_inference_libraries(tmp_path):
    code = """
import sys
import lean_proof_search
from lean_proof_search import NativeSession, SearchBudget, verify
assert not any(k == 'maths_ai' or k.startswith('maths_ai.') for k in sys.modules)
assert not any(k == 'lean_proof_search.benchmarks' or k.startswith('lean_proof_search.benchmarks.') for k in sys.modules)
assert not any(k in sys.modules for k in ('torch', 'transformers', 'torch_geometric', 'pantograph'))
from lean_proof_search.search.controller import LEAN_HELPER
assert LEAN_HELPER.is_file()
print(lean_proof_search.__version__)
"""
    result = subprocess.run([sys.executable, "-I", "-c", code], cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "0.1.0"


def test_lean_resource_is_packaged_and_verbatim_from_extraction():
    assert LEAN_HELPER == Path(str(importlib.resources.files("lean_proof_search").joinpath("resources/ProofSearch.lean")))
    assert artifacts._sha(LEAN_HELPER.read_text()) == "ece7ad3e391a9484889ce51e1966f7856977dd68aa4a1dfffd3becb45deb6118"


def test_manifest_covers_all_implementation_modules_and_helper():
    hashes = artifacts._implementation_hashes()
    for name in ("lean/tasks.py", "lean/checker.py", "search/controller.py", "models/reference.py",
                 "generation.py", "benchmarks/minif2f.py", "resources/ProofSearch.lean"):
        assert len(hashes[name]) == 64


@pytest.mark.parametrize("schema", ["minif2f_valid_dev_v1", "minif2f_valid_dev_v2", "minif2f_valid_dev_v3", "minif2f_valid_dev_v4"])
def test_legacy_and_standalone_reports_can_seed_fresh_runs(tmp_path, schema):
    cohort = {"tasks": [{"id": "goal"}]}
    manifest = {"schema": schema, "cohort_sha256": artifacts._sha(cohort),
                "toolchain": "lean", "lake_manifest_sha256": "lake"}
    report = {"manifest": manifest, "binding": artifacts._sha(manifest), "cohort": cohort,
              "tasks": [{"id": "goal", "attempts": []}]}
    path = tmp_path / "report.json"
    path.write_text(json.dumps(report))
    loaded, digest = minif2f._replay_source(path, cohort, "lean", "lake")
    assert loaded == report and digest == artifacts._sha(path.read_text())


@pytest.mark.parametrize("axioms,exit_code,expected", [
    ("propext, Classical.choice, Quot.sound", 0, "passed"),
    ("sorryAx", 0, "lean_rejected"),
    ("propext, fabricated_assumption", 0, "lean_rejected"),
    ("propext", 1, "lean_rejected"),
])
def test_compiler_status_and_axiom_audit(tmp_path, monkeypatch, axioms, exit_code, expected):
    paths = []
    def compile_(command, **kwargs):
        path = Path(command[-1])
        paths.append(path)
        assert "theorem checked : True" in path.read_text()
        return SimpleNamespace(returncode=exit_code, stdout=f"'checked' depends on axioms: [{axioms}]\n", stderr="")
    monkeypatch.setattr(checker.subprocess, "run", compile_)
    result = checker._check_source("theorem checked : True := by trivial\n#print axioms checked\n",
                                   "checked", (tmp_path, "lean", ""), 1)
    assert result["status"] == expected
    assert not paths[0].exists()


def test_validator_timeout_remains_separate_from_rejected_proof(tmp_path, monkeypatch):
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired(args[0], 1)
    monkeypatch.setattr(checker.subprocess, "run", timeout)
    assert checker._check_source("", "checked", (tmp_path, "lean", ""), 1)["status"] == "validator_timeout"


@pytest.mark.parametrize("encoding", [False, True])
def test_tokenizer_fidelity_and_context_reservation(encoding):
    class Tokenizer:
        def apply_chat_template(self, messages, *, tokenize, **kwargs):
            text = messages[0]["content"]
            ids = list(text.encode())
            return ({"input_ids": ids} if encoding else ids) if tokenize else text
        def encode(self, text, **kwargs):
            return list(text.encode())
        def decode(self, ids, **kwargs):
            return bytes(ids).decode()
    args = SimpleNamespace(context_tokens=16, max_new_tokens=8)
    assert generation._render(Tokenizer(), [{"role": "user", "content": "goal"}], args) == ("goal", [103, 111, 97, 108])
    with pytest.raises(ValueError, match="exceeds context"):
        generation._render(Tokenizer(), [{"role": "user", "content": "long goal context"}], args)


def test_reference_model_and_dataset_pins_preserved():
    assert runtime.REFERENCE == "deepseek-ai/DeepSeek-Prover-V2-7B"
    assert runtime.REVISION == "a8d9e14432b2e8dd9df2a4d4e70f1ba9bc8d9b7b"
    assert minif2f.COMMIT == "5746b7d6c47855ce1294bed87329618ff7f1bc31"
