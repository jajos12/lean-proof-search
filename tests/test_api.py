"""HTTP queue lifecycle and actual Lean certificate verification without weights."""

import json
import os
import threading
import time
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from lean_proof_search import runtime
from lean_proof_search.service import (JobCancelled, ProofExecutor, ProofJobs, ProofRequest,
                                      ServiceConfig, create_app, main)


def wait(client, job_id, expected=None):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        result = client.get("/v1/proofs/" + job_id).json()
        if result["status"] in (expected or {"succeeded", "exhausted", "failed", "cancelled"}):
            return result
        time.sleep(0.01)
    pytest.fail(f"Job did not finish: {result}")


class FakeExecutor:
    model = None
    def policy(self):
        return {"fixed": "test"}
    def execute(self, job, checkpoint, cancelled):
        if cancelled():
            raise JobCancelled()
        job.update(status="succeeded", result={"verified": True, "lean_code": "test certificate", "method": "test"})
        checkpoint()
    def close(self):
        pass


def test_submit_health_poll_and_request_validation(tmp_path):
    config = ServiceConfig(lean_project=tmp_path, job_dir=tmp_path / "jobs")
    with TestClient(create_app(config, executor=FakeExecutor())) as client:
        assert client.get("/health").json()["worker_ready"] is True
        submitted = client.post("/v1/proofs", json={"statement": "True"})
        assert submitted.status_code == 202
        job_id = submitted.json()["id"]
        result = wait(client, job_id)
        assert result["status"] == "succeeded" and result["result"]["verified"]
        assert result["stats"]["model_calls"] == 0
        assert client.get("/v1/proofs/unknown").status_code == 404
        assert client.post("/v1/proofs/unknown/cancel").status_code == 404
        for payload in ({"statement": " "}, {"statement": "True", "engine": "unknown"},
                        {"statement": "True", "limits": {"model_calls": 33}},
                        {"statement": "True", "limits": {"seconds": 1201}},
                        {"statement": "True", "lean_project": "/client/path"}):
            assert client.post("/v1/proofs", json=payload).status_code == 422
    saved = json.loads((config.job_dir / (job_id + ".json")).read_text())
    assert saved["status"] == "succeeded" and saved["binding"]


def test_api_token_and_non_loopback_launch_guard(tmp_path):
    config = ServiceConfig(lean_project=tmp_path, job_dir=tmp_path / "jobs", api_key="test-secret")
    policy = ProofExecutor(config, context=(tmp_path, "test", "")).policy()
    assert "test-secret" not in json.dumps(policy)
    with TestClient(create_app(config, executor=FakeExecutor())) as client:
        assert client.get("/health").status_code == 200
        assert client.post("/v1/proofs", json={"statement": "True"}).status_code == 401
        assert client.post("/v1/proofs", headers={"Authorization": "Bearer wrong"}, json={"statement": "True"}).status_code == 401
        response = client.post("/v1/proofs", headers={"Authorization": "Bearer test-secret"}, json={"statement": "True"})
        assert response.status_code == 202
        time.sleep(0.05)
    assert all("test-secret" not in path.read_text() for path in config.job_dir.glob("*.json"))
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv("LEAN_PROOF_API_KEY", raising=False)
        with pytest.raises(SystemExit) as error:
            main(["--lean-project", str(tmp_path), "--host", "0.0.0.0"])
        assert error.value.code == 2


def test_queue_capacity_and_queued_running_cancellation(tmp_path):
    started, release = threading.Event(), threading.Event()
    class Blocking(FakeExecutor):
        def execute(self, job, checkpoint, cancelled):
            started.set()
            release.wait(timeout=5)
            if cancelled():
                raise JobCancelled()
            super().execute(job, checkpoint, cancelled)
    config = ServiceConfig(lean_project=tmp_path, job_dir=tmp_path / "jobs", queue_size=1)
    with TestClient(create_app(config, executor=Blocking())) as client:
        first = client.post("/v1/proofs", json={"statement": "True"}).json()["id"]
        assert started.wait(timeout=2)
        second = client.post("/v1/proofs", json={"statement": "True"}).json()["id"]
        assert client.post("/v1/proofs", json={"statement": "True"}).status_code == 429
        assert client.post(f"/v1/proofs/{second}/cancel").json()["status"] == "cancelled"
        replacement = client.post("/v1/proofs", json={"statement": "True"})
        assert replacement.status_code == 202
        assert client.post(f"/v1/proofs/{first}/cancel").status_code == 202
        release.set()
        assert wait(client, first)["status"] == "cancelled"
        assert wait(client, replacement.json()["id"])["status"] == "succeeded"
        assert client.get(f"/v1/proofs/{second}").json()["status"] == "cancelled"


def test_restart_retains_pending_execution_and_rejects_changed_policy(tmp_path):
    config = ServiceConfig(lean_project=tmp_path, job_dir=tmp_path / "jobs")
    jobs = ProofJobs(config, FakeExecutor())
    job_id = jobs.submit(ProofRequest(statement="True"))["id"]
    record = jobs.jobs[job_id]
    record.update(status="running", active_seconds=42, execution={"saved_answer": "preserve"})
    jobs._persist(record)
    class Restored(FakeExecutor):
        def execute(self, job, *args):
            assert job["active_seconds"] == 42 and job["execution"]["saved_answer"] == "preserve"
            super().execute(job, *args)
    with TestClient(create_app(config, executor=Restored())) as client:
        assert wait(client, job_id)["status"] == "succeeded"
    other_id = ProofJobs(config, FakeExecutor()).submit(ProofRequest(statement="True"))["id"]
    class Changed(FakeExecutor):
        def policy(self):
            return {"fixed": "changed"}
    with TestClient(create_app(config, executor=Changed())) as client:
        result = client.get("/v1/proofs/" + other_id).json()
        assert result["status"] == "failed" and result["error"]["code"] == "restart_mismatch"


def test_restart_retains_active_plus_full_queue_without_dropping_jobs(tmp_path):
    config = ServiceConfig(lean_project=tmp_path, job_dir=tmp_path / "jobs", queue_size=1)
    jobs = ProofJobs(config, FakeExecutor())
    first = jobs.submit(ProofRequest(statement="True"))["id"]
    assert jobs.pending.get_nowait() == first
    jobs.pending.task_done()
    jobs.jobs[first]["status"] = "running"
    jobs._persist(jobs.jobs[first])
    second = jobs.submit(ProofRequest(statement="True"))["id"]
    restored = ProofJobs(config, FakeExecutor())
    assert restored.pending.qsize() == 2
    assert restored.pending.get_nowait() == first
    assert restored.pending.get_nowait() == second
    assert all(job["status"] == "queued" for job in restored.jobs.values())


def context(tmp_path):
    version = os.environ.get("LEAN_PROOF_SEARCH_TOOLCHAIN") or os.environ.get("MATHS_AI_MINIF2F_CORE_LEAN")
    if not version:
        pytest.skip("installed Lean core required")
    return tmp_path, version, ""


def test_api_real_lean_automation_certificate_and_input_rejection(tmp_path):
    lean = context(tmp_path)
    config = ServiceConfig(lean_project=tmp_path, job_dir=tmp_path / "jobs", imports=("Lean",), opens=(),
                           automation="intros\nrfl")
    executor = ProofExecutor(config, context=lean)
    with TestClient(create_app(config, executor=executor)) as client:
        good = client.post("/v1/proofs", json={"statement": "∀ n : Nat, n = n"}).json()["id"]
        result = wait(client, good)
        assert result["status"] == "succeeded", result
        proof = result["result"]
        assert proof["method"] == "automation" and proof["verified"]
        assert "axiom Expected_" not in proof["lean_code"]
        independent = runtime._check_source(proof["lean_code"], good, lean, 10)
        assert independent["status"] == "passed"
        invalid = client.post("/v1/proofs", json={"statement": "unknown_proposition"}).json()["id"]
        rejected = wait(client, invalid)
        assert rejected["status"] == "failed" and rejected["error"]["code"] == "input_rejected"
        assert client.get("/health").json()["model_loaded"] is False


@pytest.mark.parametrize("engine", ["whole", "search"])
def test_api_real_lean_model_paths(engine, tmp_path):
    lean = context(tmp_path)
    config = ServiceConfig(lean_project=tmp_path, job_dir=tmp_path / "jobs", imports=("Lean",), opens=(), automation="rfl")
    class Scripted(ProofExecutor):
        def __init__(self):
            super().__init__(config, context=lean)
            self.responses = iter(["intro P h", "constructor", "exact h", "exact h"])
        def _generate(self, messages, seed, seconds):
            code = ("theorem proposed (P : Prop) (h : P) : P ∧ P := by exact ⟨h, h⟩"
                    if engine == "whole" else next(self.responses))
            return {"raw_text": "```lean4\n" + code + "\n```", "seed": seed, "output_tokens": 3}
    with TestClient(create_app(config, executor=Scripted())) as client:
        job_id = client.post("/v1/proofs", json={"statement": "∀ P : Prop, P → P ∧ P", "engine": engine,
                                               "limits": {"model_calls": 4, "seconds": 60}}).json()["id"]
        result = wait(client, job_id)
        assert result["status"] == "succeeded", result
        assert result["result"]["method"] == engine
        assert result["stats"]["model_calls"] == (1 if engine == "whole" else 4)
        assert runtime._check_source(result["result"]["lean_code"], job_id, lean, 10)["status"] == "passed"


def test_reference_backend_is_loaded_once_and_reused(tmp_path, monkeypatch):
    config = ServiceConfig(lean_project=tmp_path)
    executor = ProofExecutor(config, context=(tmp_path, "test", ""))
    loads = []
    class Tokenizer:
        @classmethod
        def from_pretrained(cls, *a, **kw):
            assert kw["local_files_only"]
            return cls()
        def apply_chat_template(self, messages, *, tokenize, **kw):
            text = messages[0]["content"]
            return list(text.encode()) if tokenize else text
        def encode(self, text, **kw):
            return list(text.encode())
        def decode(self, ids, **kw):
            return bytes(ids).decode()
    class Model:
        def __init__(self, *args):
            pass
        def __enter__(self):
            loads.append(1)
            return self
        def generate(self, ids, seed):
            return {"raw_text": "rfl", "seed": seed}
        def close(self):
            pass
    monkeypatch.setattr("lean_proof_search.service.generation._cached_weights", lambda *a: {})
    monkeypatch.setattr(runtime, "_preflight", lambda *a: {})
    monkeypatch.setattr(runtime, "ReferenceGenerator", Model)
    monkeypatch.setitem(sys.modules, "transformers", SimpleNamespace(PreTrainedTokenizerFast=Tokenizer))
    executor._generate([{"role": "user", "content": "first"}], 1, 10)
    executor._generate([{"role": "user", "content": "second"}], 2, 10)
    assert loads == [1]
    executor.close()


def test_whole_job_resume_verifies_saved_answer_without_regeneration(tmp_path):
    lean = context(tmp_path)
    config = ServiceConfig(lean_project=tmp_path, imports=("Lean",), opens=())
    class SavedOnly(ProofExecutor):
        def _generate(self, *args):
            pytest.fail("saved model completion must be reused")
    executor = SavedOnly(config, context=lean)
    request = ProofRequest(statement="∀ P : Prop, P → P ∧ P", engine="whole",
                           limits={"model_calls": 1, "seconds": 30})
    job = {"id": "proof_" + "a" * 32, "request": request.model_dump(), "active_seconds": 5,
           "execution": {"input": {"status": "passed"}, "automation": {"status": "lean_rejected"},
                         "calls": [{"raw_text": '```lean4\ntheorem saved (P : Prop) (h : P) : P ∧ P := by exact ⟨h, h⟩\n```',
                                    "seed": 42}]}}
    executor.execute(job, lambda: None, lambda: False)
    assert job["status"] == "succeeded" and len(job["execution"]["calls"]) == 1
    assert job["active_seconds"] >= 5
