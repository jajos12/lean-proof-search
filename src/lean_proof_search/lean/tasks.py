"""Original Lean declarations, extraction and frozen-proposition validation."""

import re

from .. import runtime as control


def _masked(source):
    """Mask comments/strings while preserving offsets for header boundaries."""
    chars = list(source)
    i, depth = 0, 0
    while i < len(source):
        if depth:
            if source.startswith("/-", i):
                depth += 1
                chars[i:i+2] = "  "
                i += 2
            elif source.startswith("-/", i):
                depth -= 1
                chars[i:i+2] = "  "
                i += 2
            else:
                if source[i] != "\n":
                    chars[i] = " "
                i += 1
        elif source.startswith("/-", i):
            depth = 1
            chars[i:i+2] = "  "
            i += 2
        elif source.startswith("--", i):
            while i < len(source) and source[i] != "\n":
                chars[i] = " "
                i += 1
        elif source[i] == '"':
            chars[i] = " "
            i += 1
            while i < len(source):
                char = source[i]
                if char != "\n":
                    chars[i] = " "
                i += 1
                if char == "\\" and i < len(source):
                    chars[i] = " "
                    i += 1
                elif char == '"':
                    break
        else:
            i += 1
    if depth:
        raise ValueError("Unterminated source comment")
    return "".join(chars)


def _parse(name, source):
    masked = _masked(source)
    declaration = re.search(r"^[ \t]*theorem\s+" + re.escape(name) + r"(?=\s|\(|\{|:)", masked, re.MULTILINE)
    if declaration is None:
        raise ValueError("Expected standalone named theorem not found")
    prefix = source[:declaration.start()]
    for line in _masked(prefix).splitlines():
        line = line.strip()
        if line and not (line in {"import Mathlib", "import Aesop"}
                         or line.startswith("open ") or line.startswith("set_option ")):
            raise ValueError("Unsupported pre-theorem context; no dependency/proof guessing")
    depth = 0
    for i in range(declaration.start(), len(masked) - 1):
        if masked[i] in "([{":
            depth += 1
        elif masked[i] in ")]}":
            depth -= 1
        elif depth == 0 and masked[i:i+2] == ":=":
            return {"prefix": prefix, "header": source[declaration.start():i].rstrip()}
    raise ValueError("Top-level proof assignment not found")


def _options(heartbeats, rec_depth):
    return f"\nset_option maxHeartbeats {heartbeats}\nset_option maxRecDepth {rec_depth}\n"


def _spec(task, heartbeats=200000, rec_depth=1000):
    name = task["id"]
    stub, spec = "Expected_" + name, "Spec_" + name
    header = re.sub(r"^theorem\s+" + re.escape(name), "axiom " + stub, task["header"].lstrip(), count=1)
    # Stub elaborates the original type; final axiom audit forbids its use as evidence.
    return (task["prefix"] + _options(heartbeats, rec_depth)
            + f"run_elab do\n  if (← Lean.getEnv).contains `{name} then\n"
            '    throwError "Benchmark target already available in imports"\n'
            + header + "\n"
            "run_elab do\n"
            f"  let info ← Lean.getConstInfo `{stub}\n"
            '  if !info.levelParams.isEmpty then throwError "Universe-polymorphic benchmark target unsupported"\n'
            "  Lean.addDecl (.defnDecl {\n"
            f"    name := `{spec}\n"
            "    levelParams := []\n    type := Lean.mkSort .zero\n    value := info.type\n"
            "    hints := .abbrev\n    safety := .safe\n  })\n")


def _proof_code(raw):
    """Extract a candidate, never treating an unfenced reasoning essay as Lean."""
    if raw.count("```") % 2:
        raise ValueError("Unterminated final code block")
    blocks = re.findall(r"```([^\n`]*)\n(.*?)```", raw, re.DOTALL)
    if blocks:
        language, code = blocks[-1]
        if language.strip().lower() not in {"", "lean", "lean4"}:
            raise ValueError("Final code block is not Lean")
        code = code.strip()
    else:
        code = raw.strip()
        if not re.match(r"(?:theorem|lemma|example|by|intro|intros|rintro|have|let|calc|"
                        r"exact|apply|refine|constructor|cases|rcases|obtain|induction|"
                        r"rw|simp|simpa|simp_all|rfl|assumption|aesop|omega|ring|ring_nf|"
                        r"linarith|nlinarith|norm_num|positivity|unfold|first|solve)\b", code):
            raise ValueError("No final Lean code block or recognizable tactic body; discussion is not a proof")
    if not code:
        raise ValueError("Empty final Lean code block")
    if re.search(r"\b(?:sorry|admit)\b", _masked(code)):
        raise ValueError("Final Lean proof contains a sorry/admit placeholder")
    return code


def _validate(task, raw, context, timeout, heartbeats=200000, rec_depth=1000):
    if task["ingest_status"] != "parsed":
        return {"status": "input_unsupported", "error": task["ingest_error"]}
    try:
        code = _proof_code(raw)
    except ValueError as error:
        return {"status": "no_complete_body", "error": str(error)}
    decls = list(re.finditer(r"^[ \t]*(?:theorem|lemma)\s+([A-Za-z_][\w']*)", code, re.MULTILINE))
    if decls:
        chosen = next((m for m in decls if m.group(1) == task["id"]), decls[0])
        candidate = code[decls[0].start():]
        candidate_name = chosen.group(1)
    else:
        try:
            body = control._extract_body(code)
        except ValueError as error:
            return {"status": "no_complete_body", "error": str(error)}
        candidate_name = task["id"]
        candidate = task["header"] + " := by\n" + "\n".join("  " + line for line in body.splitlines())
    namespace, checked = "Candidate_" + task["id"], "Checked_" + task["id"]
    source = (_spec(task, heartbeats, rec_depth) + f"namespace {namespace}\n" + candidate
              + f"\nend {namespace}\n" + _options(heartbeats, rec_depth)
              + f"theorem {checked} : Spec_{task['id']} := by exact {namespace}.{candidate_name}\n"
              f"#print axioms {checked}\n")
    return {**control._check_source(source, checked, context, timeout), "candidate_code": code}
