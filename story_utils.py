"""Shared utilities for story data access."""

import json
import os

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORIES_DIR = os.path.join(BASE_DIR, "data", "stories")


def get_character_name(story_id: str, branch_id: str) -> str:
    """Read player character name from branch character_state.json."""
    path = os.path.join(STORIES_DIR, story_id, "branches", branch_id, "character_state.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            state = json.load(f)
        return state.get("name", "玩家")
    except (FileNotFoundError, json.JSONDecodeError):
        return "玩家"
