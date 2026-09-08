"""Split a GM v2 turn output into (narration, delta, note).

Transport = A (decided): the GM emits narration prose + ONE delta block delimited
by `<!--DELTA ... DELTA-->` (fenced ```json/```gm accepted as a fallback). This
parser is tolerant: a missing or malformed block yields an empty delta + a `note`
(the turn still narrates), never an exception.
"""

import json
import re

from story_core.gm_v2.contract import DELTA_CLOSE, DELTA_OPEN

# Greedy-safe: the explicit close marker delimits the block, so a non-greedy
# capture of arbitrary content (incl. nested braces / newlines) is correct.
_DELTA_RE = re.compile(re.escape(DELTA_OPEN) + r"(.*?)" + re.escape(DELTA_CLOSE), re.DOTALL)
_FENCE_RE = re.compile(r"```(?:json|gm_delta|gm)?[ \t]*\n?(.*?)```", re.DOTALL)
# Legacy production tags the story persona may still instruct (v2 uses the DELTA
# block instead). Stripped from narration so they never leak to the player.
# NOTE: the <!--ACTIONS--> marker is deliberately NOT in this list (kept for the UI).
_LEGACY_KW = r"(?:STATE|NPC|LORE|EVENT|IMG|TIME|DELTA)"
# 1) well-formed balanced tag: <!--STATE ... STATE-->
_LEGACY_TAG_RE = re.compile(r"<!--\s*(STATE|NPC|LORE|EVENT|IMG|TIME|DELTA)\b.*?\1\s*-->", re.DOTALL)
# 2) unbalanced opener: <!--STATE ...-->  ([^<>] so it can't swallow a later <!--ACTIONS-->)
_LEGACY_OPEN_RE = re.compile(r"<!--\s*" + _LEGACY_KW + r"\b[^<>]*?-->")
# 3) ORPHAN close residue with NO opening <!-- (the『STATE STATE-->』bug): KW (KW)* -->
_LEGACY_ORPHAN_RE = re.compile(r"(?:^|\s)" + _LEGACY_KW + r"(?:\s+" + _LEGACY_KW + r")*\s*-->", re.MULTILINE)
# 4) TRAILING bare/partial tag at the very END that the above missed — a lone「STATE」or a
#    half-streamed「STATE {…」/「<!--STATE {…」whose close never arrived (no <!-- AND no -->,
#    so 1–3 all skip it; .strip() only trims whitespace). `[^<>]*\Z` stops at any `<` → it can
#    never eat across a later `<!--ACTIONS-->` (and \Z then fails), so the kept ACTIONS block is safe.
_LEGACY_TRAILING_RE = re.compile(r"\n\s*(?:<!--\s*)?" + _LEGACY_KW + r"\b[^<>]*\Z")


def _strip_legacy_tags(text: str) -> str:
    """Strip legacy production-tag debris so it never reaches the player — balanced tags,
    unbalanced openers, orphan close fragments (`STATE STATE-->`), AND a trailing bare/partial
    tag with no delimiters at all (a lone end-of-output `STATE`). `<!--ACTIONS-->` is kept."""
    text = _LEGACY_TAG_RE.sub("", text)
    text = _LEGACY_OPEN_RE.sub("", text)
    text = _LEGACY_ORPHAN_RE.sub("", text)
    text = _LEGACY_TRAILING_RE.sub("", text)
    return text.strip()


def parse_turn_output(raw: object) -> tuple[str, dict, str | None]:
    """Return (narration, delta_dict, parse_note).

    narration: text with the delta block removed, stripped.
    delta_dict: parsed JSON object, or {} when absent/malformed.
    parse_note: None on a clean parse, else a short human-readable reason.
    """
    if not isinstance(raw, str):
        return "", {}, "non-string output"

    match = None
    delta_matches = list(_DELTA_RE.finditer(raw))
    if delta_matches:
        match = delta_matches[-1]
    else:
        fence_matches = list(_FENCE_RE.finditer(raw))
        if fence_matches:
            match = fence_matches[-1]

    if match is None:
        return _strip_legacy_tags(raw.strip()), {}, "no delta block found"

    delta: dict = {}
    note: str | None = None
    block = match.group(1).strip()
    try:
        parsed = json.loads(block)
        if isinstance(parsed, dict):
            delta = parsed
        else:
            note = "delta block not an object"
    except json.JSONDecodeError as exc:
        note = f"delta JSON parse failed: {exc}"

    narration = _strip_legacy_tags((raw[: match.start()] + raw[match.end():]).strip())
    return narration, delta, note


__all__ = ["parse_turn_output"]
