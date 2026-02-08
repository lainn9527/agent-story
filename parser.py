"""MD conversation parser — converts Grok_conversation.md into structured JSON."""

import json
import os
import re

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONVERSATION_PATH = os.path.join(BASE_DIR, "Grok_conversation.md")
OUTPUT_PATH = os.path.join(BASE_DIR, "data", "parsed_conversation.json")


def parse_conversation(path: str = CONVERSATION_PATH) -> list[dict]:
    """Split the markdown file on ``## User`` / ``## Grok`` headers and return
    a list of ``{"role": "user"|"gm", "content": "...", "index": N}`` dicts.
    """
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()

    # Split on ## User or ## Grok lines
    parts = re.split(r"^##\s+(User|Grok)\s*$", content, flags=re.MULTILINE)

    # parts[0] is everything before the first ## header (the "# Grok Conversation" title)
    # Then alternating: speaker, content, speaker, content …
    messages: list[dict] = []
    idx = 0
    i = 1  # skip preamble
    while i < len(parts) - 1:
        speaker = parts[i].strip()
        body = parts[i + 1].strip()
        if body:
            role = "user" if speaker == "User" else "gm"
            messages.append({"role": role, "content": body, "index": idx})
            idx += 1
        i += 2

    return messages


def save_parsed(messages: list[dict] | None = None, output: str = OUTPUT_PATH) -> list[dict]:
    """Parse (if needed) and persist to JSON. Returns the message list."""
    if messages is None:
        messages = parse_conversation()
    os.makedirs(os.path.dirname(output), exist_ok=True)
    with open(output, "w", encoding="utf-8") as f:
        json.dump(messages, f, ensure_ascii=False, indent=2)
    return messages


if __name__ == "__main__":
    msgs = save_parsed()
    print(f"Parsed {len(msgs)} messages → {OUTPUT_PATH}")
