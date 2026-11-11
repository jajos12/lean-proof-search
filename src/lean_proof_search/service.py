"""Asynchronous single-worker proof API; inference dependencies stay optional."""

from __future__ import annotations

import argparse
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import queue
import re
import secrets
import threading
import time
from types import SimpleNamespace
from typing import Literal
import uuid

from fastapi import Depends, FastAPI, Header, HTTPException, Response, status
from pydantic import BaseModel, ConfigDict, Field, field_validator

from . import generation, runtime
from .lean import tasks
from .search import controller


class ProofLimits(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    model_calls: int = Field(default=8, ge=1, le=32, strict=True)
    seconds: float = Field(default=300, ge=1, le=1200)


class ProofRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    statement: str = Field(min_length=1, max_length=16000, strict=True)
    engine: Literal["whole", "search"] = "search"
    limits: ProofLimits = Field(default_factory=ProofLimits)
    seed: int = Field(default=42, ge=0, le=2**31 - 1, strict=True)

    @field_validator("statement")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("statement must contain a Lean proposition")
        return value.strip()


@dataclass(frozen=True)
class ServiceConfig:
    lean_project: Path
    job_dir: Path = Path("runs/api")
    hf_cache: Path | None = None
    device: str = "cuda:0"
    dtype: str = "bfloat16"
    context_tokens: int = 12288
    max_new_tokens: int = 4096
    generation_timeout: float = 120
    lean_timeout: float = 180
    automation_timeout: float = 20
    heartbeats: int = 1000000
    rec_depth: int = 2048
    queue_size: int = 16
    api_key: str | None = field(default=None, repr=False)
    imports: tuple[str, ...] = ("Lean", "Mathlib", "Aesop")
    opens: tuple[str, ...] = ("BigOperators",)
    automation: str = generation.AUTOMATION

    def __post_init__(self):
        if self.queue_size < 1 or self.context_tokens <= self.max_new_tokens or self.max_new_tokens < 1:
            raise ValueError("Invalid queue or token configuration")
        if self.generation_timeout <= 0 or self.lean_timeout <= 0 or self.automation_timeout <= 0 or self.heartbeats < 1 or self.rec_depth < 1:
            raise ValueError("Invalid execution limits")

    @property
    def prefix(self):
        return ("".join("import " + name + "\n" for name in self.imports)
                + "set_option autoImplicit false\n"
                + "".join("open " + name + "\n" for name in self.opens))


class JobCancelled(Exception):
    pass


class ProofExecutor:
    """One cached backend, owned exclusively by the queue's worker thread."""

    def __init__(self, config, *, context=None):
        self.config = config
        self.context = context or runtime._lean_context(config.lean_project)
        self.model = self.tokenizer = None
        self.args = SimpleNamespace(
            backend="transformers", min_free_disk_gib=1, local_files_only=True,
            hf_cache=config.hf_cache, device=config.device, dtype=config.dtype,
            context_tokens=config.context_tokens, max_new_tokens=config.max_new_tokens,
            request_timeout=config.generation_timeout,
        )

    def policy(self):
        project = self.context[0]
        lake = project / "lake-manifest.json"
        return {"schema": "proof_api_v1", "toolchain": self.context[1],
                "lake_manifest_sha256": runtime._sha(lake.read_text()) if lake.exists() else None,
                "implementation": runtime._implementation_hashes(), "runtime": runtime._runtime(),
                "reference": runtime.REFERENCE, "revision": runtime.REVISION,
                "device": self.config.device, "dtype": self.config.dtype,
                "context_tokens": self.config.context_tokens, "max_new_tokens": self.config.max_new_tokens,
                "generation_timeout": self.config.generation_timeout, "lean_timeout": self.config.lean_timeout,
                "automation_timeout": self.config.automation_timeout,
                "search_limits": {"tactic_trials": 96, "nodes": 64, "depth": 16, "visits": 6, "tactic_timeout": 20},
                "heartbeats": self.config.heartbeats, "rec_depth": self.config.rec_depth,
                "prefix": self.config.prefix, "automation": self.config.automation}

    def close(self):
        if self.model is not None:
            self.model.close()
            self.model = None

    def _generate(self, messages, seed, seconds):
        started = time.monotonic()
        if self.tokenizer is None:
            generation._cached_weights(self.config.hf_cache)
            from transformers import PreTrainedTokenizerFast
            self.tokenizer = PreTrainedTokenizerFast.from_pretrained(
                runtime.REFERENCE, revision=runtime.REVISION, cache_dir=self.config.hf_cache, local_files_only=True)
        rendered, ids = generation._render(self.tokenizer, messages, self.args)
        if self.model is None:
            runtime._preflight(self.args)
            self.model = runtime.ReferenceGenerator(self.args, self.tokenizer).__enter__()
        left = seconds - (time.monotonic() - started)
        if left <= 0:
            raise TimeoutError("Job time budget exhausted during model initialization")
        self.args.request_timeout = min(self.config.generation_timeout, left)
        try:
            result = self.model.generate(ids, seed)
        finally:
            self.args.request_timeout = self.config.generation_timeout
        return {**result, "messages": messages, "prompt_sha256": runtime._sha(rendered),
                "input_ids_sha256": runtime._sha(ids)}

    def execute(self, job, checkpoint, cancelled):
        request = ProofRequest.model_validate(job["request"])
        task = {"id": job["id"], "prefix": self.config.prefix,
                "header": "theorem " + job["id"] + " : " + request.statement, "ingest_status": "parsed"}
        state = job.setdefault("execution", {})
        started, previous = time.monotonic(), job.get("active_seconds", 0)

        def remaining():
            return request.limits.seconds - previous - (time.monotonic() - started)

        def save():
            job["active_seconds"] = previous + time.monotonic() - started
            checkpoint()
            if cancelled():
                raise JobCancelled()

        def verify(task, raw, seconds):
            return tasks._validate(task, raw, self.context, max(0.001, min(self.config.lean_timeout, seconds, remaining())),
                                   self.config.heartbeats, self.config.rec_depth)

        def accept(raw, check, method):
            # Return a standalone certificate without the host specification axiom.
            job["phase"] = "final_verification"
            save()
            code = check["candidate_code"]
            decls = list(re.finditer(r"^[ \t]*(?:theorem|lemma)\s+([A-Za-z_][\w']*)", code, re.MULTILINE))
            if decls:
                chosen = next((decl for decl in decls if decl.group(1) == task["id"]), decls[0])
                candidate, name = code[decls[0].start():], chosen.group(1)
            else:
                name = task["id"]
                body = runtime._extract_body(code)
                candidate = task["header"] + " := by\n" + "\n".join("  " + line for line in body.splitlines())
            namespace = "Candidate_" + task["id"]
            portable = (task["prefix"] + tasks._options(self.config.heartbeats, self.config.rec_depth)
                        + "def Spec_" + task["id"] + " : Prop := " + request.statement + "\n"
                        + f"namespace {namespace}\n{candidate}\nend {namespace}\n"
                        + f"theorem {task['id']} : Spec_{task['id']} := by exact {namespace}.{name}\n"
                        + f"#print axioms {task['id']}\n")
            state["portable_check"] = runtime._check_source(
                portable, task["id"], self.context, max(0.001, min(self.config.lean_timeout, remaining())))
            if state["portable_check"]["status"] != "passed":
                timeout = state["portable_check"]["status"] == "validator_timeout"
                job.update(status="exhausted" if timeout else "failed",
                           error={"code": "certificate_timeout" if timeout else "certificate_rejected",
                                  "message": "Standalone proof certificate failed verification"})
            else:
                job.update(status="succeeded", result={"verified": True, "method": method,
                                                       "lean_code": portable, "axioms": state["portable_check"]["axioms"]})
            save()

        try:
            save()
            if "input" not in state:
                job["phase"] = "input_check"
                save()
                source = (tasks._spec(task, self.config.heartbeats, self.config.rec_depth)
                          + "theorem Input_" + task["id"] + " : True := by trivial\n#print axioms Input_" + task["id"] + "\n")
                state["input"] = runtime._check_source(source, "Input_" + task["id"], self.context,
                                                      max(0.001, min(self.config.lean_timeout, remaining())))
                save()
            if state["input"]["status"] != "passed":
                timed_out = state["input"]["status"] == "validator_timeout"
                job.update(status="exhausted" if timed_out else "failed",
                           error={"code": "input_timeout" if timed_out else "input_rejected",
                           "message": state["input"].get("diagnostics", state["input"]["status"])[:8000]})
                return
            if "automation" not in state and remaining() > 0:
                job["phase"] = "automation"
                save()
                state["automation"] = verify(task, self.config.automation, min(self.config.automation_timeout, remaining()))
                save()
            if state.get("automation", {}).get("status") == "passed":
                accept(self.config.automation, state["automation"], "automation")
                return
            if remaining() <= 0:
                job["status"] = "exhausted"
                return
            if request.engine == "search":
                job["phase"] = "search"
                record = state.setdefault("search", {})
                budget = (controller.SearchBudget(**record["budget"]) if "budget" in record else
                          controller.SearchBudget(model_calls=request.limits.model_calls, seconds=remaining(),
                                                  tactic_trials=96, nodes=64, depth=16, visits=6))
                def generate(messages, seed, seconds):
                    job["phase"] = "generating"
                    save()
                    result = self._generate(messages, seed, min(seconds, remaining()))
                    job["phase"] = "checking_tactic"
                    return result
                controller.search(task, record, generate, verify, save,
                                  lambda left: controller.NativeSession(
                                      task, self.context, heartbeats=self.config.heartbeats, rec_depth=self.config.rec_depth,
                                      timeout=max(0.001, min(self.config.lean_timeout, left, remaining()))),
                                  budget=budget, branching=3, seed=request.seed)
                if record["status"] == "passed":
                    accept(record["proof"], record["lean"], "search")
                else:
                    job["status"] = "exhausted"
            else:
                calls = state.setdefault("calls", [])
                while remaining() > 0:
                    pending = calls and "raw_text" in calls[-1] and "lean" not in calls[-1]
                    if pending:
                        call = calls[-1]
                    elif len(calls) < request.limits.model_calls:
                        job["phase"] = "generating"
                        call = {"status": "started", "seed": request.seed + len(calls)}
                        calls.append(call)
                        save()
                        prompt = (task["prefix"] + task["header"] + " := by\n  sorry\n\n"
                                  "Complete the original theorem. Give a brief plan and one final Lean code block. "
                                  "No sorry, admit or new assumptions.")
                        call.update(self._generate([{"role": "user", "content": prompt}], call["seed"], remaining()))
                        save()
                    else:
                        break
                    job["phase"] = "final_verification"
                    call["lean"] = verify(task, call["raw_text"], remaining())
                    save()
                    if call["lean"]["status"] == "passed":
                        accept(call["raw_text"], call["lean"], "whole")
                        return
                job["status"] = "exhausted"
        except TimeoutError:
            job["status"] = "exhausted"
        finally:
            job["active_seconds"] = previous + time.monotonic() - started


TERMINAL = {"succeeded", "exhausted", "failed", "cancelled"}


class ProofJobs:
    def __init__(self, config, executor):
        self.config, self.executor = config, executor
        self.policy = executor.policy()
        self.binding = runtime._sha(self.policy)
        self.jobs, self.cancelled = {}, set()
        self.lock = threading.RLock()
        self.pending = queue.Queue()
        self.stopping = threading.Event()
        self.worker = None
        config.job_dir.mkdir(parents=True, exist_ok=True)
        restored = []
        for path in sorted(config.job_dir.glob("proof_*.json")):
            job = json.loads(path.read_text())
            if not re.fullmatch(r"proof_[0-9a-f]{32}", job.get("id", "")):
                continue
            if job["status"] not in TERMINAL:
                if job.get("cancel_requested"):
                    job.update(status="cancelled", phase="cancelled")
                elif (job.get("binding") == self.binding
                      and runtime._sha(job.get("policy", {})) == job["binding"]
                      and job.get("request_sha256") == runtime._sha(job["request"])):
                    job.update(status="queued", phase="queued")
                    restored.append(job["id"])
                else:
                    job.update(status="failed", error={"code": "restart_mismatch",
                               "message": "Service policy changed; submit a new job"})
                runtime._write(path, job)
            self.jobs[job["id"]] = job if job["status"] not in TERMINAL else self._compact(job)
        for job_id in sorted(restored, key=lambda key: self.jobs[key]["created_at"]):
            self.pending.put_nowait(job_id)

    def _compact(self, job):
        return {key: value for key, value in job.items() if key != "execution"}

    def _persist(self, job):
        job["updated_at"] = time.time()
        runtime._write(self.config.job_dir / (job["id"] + ".json"), job)

    def start(self):
        self.worker = threading.Thread(target=self._run, daemon=True)
        self.worker.start()

    def stop(self):
        self.stopping.set()
        if self.worker:
            self.worker.join(timeout=5)

    def submit(self, request):
        with self.lock:
            if self.queued_count() >= self.config.queue_size:
                raise HTTPException(status_code=429, detail="Proof queue is full")
            job_id = "proof_" + uuid.uuid4().hex
            job = {"id": job_id, "status": "queued", "phase": "queued", "request": request.model_dump(),
                   "request_sha256": runtime._sha(request.model_dump()),
                   "created_at": time.time(), "active_seconds": 0, "policy": self.policy, "binding": self.binding}
            self.jobs[job_id] = job
            self._persist(job)
            self.pending.put_nowait(job_id)
            return {"id": job_id, "status": "queued", "status_url": "/v1/proofs/" + job_id}

    def queued_count(self):
        with self.lock:
            return sum(job["status"] == "queued" for job in self.jobs.values())

    def view(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="Proof job not found")
            result = {key: job[key] for key in ("id", "status", "phase", "request", "result", "error",
                                               "created_at", "updated_at", "active_seconds", "cancel_requested") if key in job}
            result["stats" if job["status"] in TERMINAL else "progress"] = job.get("stats", {"model_calls": 0})
            return result

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="Proof job not found")
            if job["status"] not in TERMINAL:
                self.cancelled.add(job_id)
                job["cancel_requested"] = True
                if job["status"] == "queued":
                    job.update(status="cancelled", phase="cancelled")
                    self._persist(job)
            return {"id": job_id, "status": job["status"], "cancel_requested": job_id in self.cancelled}

    def _run(self):
        try:
            while not self.stopping.is_set():
                try:
                    job_id = self.pending.get(timeout=0.2)
                except queue.Empty:
                    continue
                with self.lock:
                    job = self.jobs[job_id]
                    if job["status"] == "cancelled":
                        self.pending.task_done()
                        continue
                    job.update(status="running", phase="input_check")
                def checkpoint():
                    with self.lock:
                        execution = job.get("execution", {})
                        record = execution.get("search", {})
                        calls = record.get("calls", []) + execution.get("calls", [])
                        job["stats"] = {"model_calls": sum("raw_text" in call for call in calls),
                                        "requests_started": len(calls), "tactic_trials": len(record.get("trials", [])),
                                        "states": len(record.get("nodes", [])),
                                        "output_tokens": sum(call.get("output_tokens", 0) for call in calls),
                                        "input_tokens": sum(call.get("input_tokens", 0) for call in calls),
                                        "elapsed_seconds": job.get("active_seconds", 0)}
                        self._persist(job)
                checkpoint()
                try:
                    self.executor.execute(job, checkpoint, lambda: job_id in self.cancelled or self.stopping.is_set())
                    if job["status"] == "running":
                        job.update(status="failed", error={"code": "missing_result", "message": "Executor returned without a result"})
                except JobCancelled:
                    job.update(status="cancelled" if job_id in self.cancelled else "queued")
                except Exception as error:
                    job.update(status="failed", error={"code": "execution_error", "message": str(error)[:8000]})
                if job["status"] in TERMINAL:
                    job["phase"] = job["status"]
                checkpoint()
                if job["status"] in TERMINAL:
                    with self.lock:
                        self.jobs[job_id] = self._compact(job)
                self.pending.task_done()
        finally:
            self.executor.close()


def create_app(config: ServiceConfig, *, executor=None):
    jobs = ProofJobs(config, executor or ProofExecutor(config))
    @asynccontextmanager
    async def lifespan(app):
        jobs.start()
        try:
            yield
        finally:
            jobs.stop()
    app = FastAPI(title="Lean Proof Search", version="0.1.0", lifespan=lifespan)
    app.state.proof_jobs = jobs
    def authorized(authorization: str | None = Header(default=None)):
        if config.api_key and not secrets.compare_digest((authorization or "").encode(), ("Bearer " + config.api_key).encode()):
            raise HTTPException(status_code=401, detail="Invalid API bearer token", headers={"WWW-Authenticate": "Bearer"})
    @app.get("/health")
    def health(response: Response):
        ready = bool(jobs.worker and jobs.worker.is_alive())
        if not ready:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        return {"status": "ok" if ready else "unavailable", "worker_ready": ready,
                "queue_size": jobs.queued_count(), "model_loaded": bool(getattr(jobs.executor, "model", None))}
    @app.post("/v1/proofs", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(authorized)])
    def submit(request: ProofRequest):
        if not jobs.worker or not jobs.worker.is_alive():
            raise HTTPException(status_code=503, detail="Proof worker is unavailable")
        return jobs.submit(request)
    @app.get("/v1/proofs/{job_id}", dependencies=[Depends(authorized)])
    def inspect(job_id: str):
        return jobs.view(job_id)
    @app.post("/v1/proofs/{job_id}/cancel", dependencies=[Depends(authorized)])
    def cancel(job_id: str, response: Response):
        result = jobs.cancel(job_id)
        if result["status"] not in TERMINAL:
            response.status_code = status.HTTP_202_ACCEPTED
        return result
    return app


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lean-project", type=Path, required=True)
    parser.add_argument("--job-dir", type=Path, default=Path("runs/api"))
    parser.add_argument("--hf-cache", type=Path)
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--dtype", choices=("bfloat16", "float16"), default="bfloat16")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--queue-size", type=int, default=16)
    args = parser.parse_args(argv)
    key = os.environ.get("LEAN_PROOF_API_KEY")
    if args.host not in {"127.0.0.1", "localhost", "::1"} and not key:
        parser.error("Set LEAN_PROOF_API_KEY before binding to a non-loopback address")
    config = ServiceConfig(lean_project=args.lean_project, job_dir=args.job_dir, hf_cache=args.hf_cache,
                           device=args.device, dtype=args.dtype, queue_size=args.queue_size, api_key=key)
    import uvicorn
    uvicorn.run(create_app(config), host=args.host, port=args.port, workers=1)


if __name__ == "__main__":
    main()
