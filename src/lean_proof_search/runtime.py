"""Internal experiment facade; backends remain importable independently."""

from .artifacts import _sha, _write, _runtime, _implementation_hashes
from .lean.checker import ALLOWED_AXIOMS, _extract_body, _lean_context, _check_source
from .models.reference import PLAN, REFERENCE, REVISION, ReferenceGenerator, _preflight
