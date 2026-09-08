#!/usr/bin/env python3
"""MCP adapter — exposes the gm_tools as typed MCP tools for in-session play.

This is the SECOND front-end over the same `gm_tools` core (the Bash CLI is the
first). Same implementations, same dispatch — only the calling convention
differs. A headless API loop would be a third front-end reusing gm_tools.DISPATCH.

Setup (one-time; MCP tools go live only after a session reconnect):
  pip install mcp
  # register via .mcp.json at repo root (already provided), then restart the
  # Claude Code session so it picks up the server.

Run standalone (stdio transport):
  python scripts/play/mcp_server.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # for `import gm_tools`

import gm_tools  # noqa: E402

try:
    from mcp.server.fastmcp import FastMCP
except ImportError:  # pragma: no cover
    sys.stderr.write("MCP SDK not installed. Run: pip install mcp\n")
    raise

mcp = FastMCP("story-gm")


@mcp.tool()
def roll_dice(story_id: str, branch_id: str, action: str = "") -> dict:
    """Roll a d100 fate die with attribute modifiers (real RNG). Returns
    outcome + a label-free trend hint. Call BEFORE narrating an action whose
    success is uncertain — preserves GM-fairness."""
    return gm_tools.roll_dice(story_id, branch_id, action=action)


@mcp.tool()
def search_lore(story_id: str, query: str, limit: int = 3) -> dict:
    """Retrieve the top relevant world-lore entries (small output). Use to stay
    consistent with established setting without reading the whole lore file."""
    return gm_tools.search_lore(story_id, query, limit=limit)


@mcp.tool()
def apply_state(story_id: str, branch_id: str, delta: dict) -> dict:
    """Apply a STATE delta to the character state. Schema-validated but NOT
    lossy-guarded — pass clean grounded deltas. Supports `<field>` overwrite,
    `<num>_delta`, map merge (inventory/relationships/systems; ""/null removes),
    and `<list>_add`/`<list>_remove`."""
    return gm_tools.apply_state(story_id, branch_id, delta)


@mcp.tool()
def update_npc(story_id: str, branch_id: str, npcs: list) -> dict:
    """Upsert NPCs. Set `lifecycle_status` (active|offstage|terminal) explicitly
    — never rely on prose keywords. Use `memory_add` to append durable per-NPC
    memory that survives compaction."""
    return gm_tools.update_npc(story_id, branch_id, npcs)


@mcp.tool()
def commit_turn(story_id: str, branch_id: str, user_msg: str, gm_msg: str,
                dice: dict | None = None, freehand: list | None = None) -> dict:
    """Append the player action + GM reply (dice attaches to the player msg).
    Put any off-tool actions in `freehand` so they land in the tool-gap ledger."""
    return gm_tools.commit_turn(story_id, branch_id, user_msg, gm_msg, dice=dice, freehand=freehand)


@mcp.tool()
def recap(story_id: str, branch_id: str, recap_text: str | None = None) -> dict:
    """Read the recap, or write a GM-authored recap_text to compact old turns
    (the token lever for long sessions). Preserve key NPC facts in the recap."""
    return gm_tools.recap(story_id, branch_id, recap_text=recap_text)


@mcp.tool()
def seed_branch(story_id: str, branch_id: str, mode: str = "A", name: str | None = None) -> dict:
    """Create a fresh branch (seeded state/messages/npcs) for a new game."""
    return gm_tools.seed_branch(story_id, branch_id, mode=mode, name=name)


if __name__ == "__main__":
    mcp.run()
