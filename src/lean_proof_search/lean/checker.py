"""Compile host-constructed sources and audit their fixed target theorem."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import textwrap
import time

ALLOWED_AXIOMS = {"propext", "Classical.choice", "Quot.sound"}


def _extract_body(raw):
    if raw.count("```") % 2:
        raise ValueError("Unterminated code block; final output may be truncated")
    blocks = re.findall(r"```(?:lean4?|Lean)?[ \t]*\n(.*?)```", raw, flags=re.DOTALL)
    code = textwrap.dedent(blocks[-1]).strip() if blocks else raw.strip()
    declaration = re.search(r"\b(?:theorem|lemma|example)\b.*?:=\s*by\b(.*)", code, re.DOTALL)
    if declaration:
        code = textwrap.dedent(declaration.group(1)).strip()
    elif code.startswith("by\n") or code.startswith("by "):
        code = textwrap.dedent(code[2:]).strip()
    if not code:
        raise ValueError("No tactic body found")
    return code


def _lean_context(project):
    project = project.resolve()
    toolchain = (project / "lean-toolchain").read_text().strip()
    installed = subprocess.run(["elan", "toolchain", "list"], capture_output=True, text=True,
                               check=True, timeout=15).stdout.splitlines()
    if toolchain not in {line.split()[0] for line in installed if line.strip()}:
        raise RuntimeError(f"Lean toolchain not installed: {toolchain}; no automatic install attempted")
    result = subprocess.run(["lake", "env", "printenv", "LEAN_PATH"], cwd=project,
                            capture_output=True, text=True, check=True, timeout=60)
    return project, toolchain, result.stdout.strip()


def _check_source(code, theorem_name, context, timeout):
    project, toolchain, lean_path = context
    env = {**os.environ, "LEAN_PATH": lean_path}
    started = time.monotonic()
    with tempfile.TemporaryDirectory(prefix="lean-backend-control-") as directory:
        source = Path(directory) / "Candidate.lean"
        source.write_text(code, encoding="utf-8")
        try:
            result = subprocess.run(["elan", "run", toolchain, "lean", str(source)],
                                    cwd=project, env=env, capture_output=True,
                                    text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"status": "validator_timeout", "seconds": round(time.monotonic() - started, 3)}
    output = result.stdout + result.stderr
    pattern = r"^'" + re.escape(theorem_name) + r"' depends on axioms:\s*\[([^\]]*)\]"
    match = re.search(pattern, output, re.MULTILINE)
    no_axioms = re.search(r"^'" + re.escape(theorem_name) + r"' does not depend on any axioms$",
                          output, re.MULTILINE) is not None
    axioms = [item.strip() for item in match.group(1).split(",") if item.strip()] if match else []
    passed = (result.returncode == 0 and (bool(match) or no_axioms)
              and set(axioms) <= ALLOWED_AXIOMS and "sorryAx" not in output)
    return {"status": "passed" if passed else "lean_rejected", "toolchain": toolchain,
            "exit_code": result.returncode, "axioms": axioms,
            "seconds": round(time.monotonic() - started, 3), "diagnostics": output}
