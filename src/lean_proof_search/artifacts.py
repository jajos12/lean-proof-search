"""Stable report bindings and atomic checkpoints."""

import hashlib
import importlib.metadata
from importlib.resources import files
import json
from pathlib import Path


def _sha(value):
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _write(path, payload):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


def _runtime():
    result = {}
    for name in ("lean-proof-search", "torch", "transformers", "tokenizers", "accelerate", "requests"):
        try:
            result[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            result[name] = None
    return result


def _implementation_hashes():
    root = Path(str(files("lean_proof_search")))
    paths = sorted(root.rglob("*.py")) + sorted(root.rglob("*.lean"))
    return {str(path.relative_to(root)): _sha(path.read_text()) for path in paths}
