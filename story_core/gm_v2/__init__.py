"""GM v2 — production Gemini pipeline (parallel to the legacy /api/send path).

Architecture: one applier core, swappable brain. The GM (Gemini) AUTHORS a clean,
grounded delta in a single pass (narration + a `<!--DELTA {json} DELTA-->` block),
instead of a second model reverse-engineering the prose. The server then:

  PARSE (parse.py) -> deterministic GATE (gate.py) -> thin APPLY -> async CRITIC

This module is intentionally app-free and side-effect-free at import time so it can
be unit-tested without Flask. See memory: gemini-port-design.
"""

from story_core.gm_v2.parse import parse_turn_output
from story_core.gm_v2.ops import gate_delta

__all__ = ["parse_turn_output", "gate_delta"]
