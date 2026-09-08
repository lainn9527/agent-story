# `story_design/_shared/`

Anything under this folder is **NOT** treated as a story. Reserved for cross-story
content reuse. Story enumeration (`_list_story_design_ids` and friends) skips any
folder name starting with `_`.

## How sharing works

Story system prompts (`story_design/<id>/system_prompt.txt`) support an
`@include <relpath>` directive on its own line. Path is **relative to
`story_design/`**.

```
@include _shared/gm_rules.txt
```

The directive is expanded **after** Python `.format(...)` substitutes the
`{character_state}` / `{world_lore}` / etc. placeholders, so included files may
freely contain `{` / `}` (JSON examples, code snippets) without escaping.

Constraints:
- Path must stay inside `story_design/` (no `..`, no absolute paths).
- Recursive includes are allowed up to depth 5.
- Files larger than 256 KB are rejected.
- Failures are rendered inline as `<!-- @include … (reason) -->` so they show up
  in the prompt rather than disappearing silently.

## Layout

```
_shared/
  README.md                  ← this file
  _template/                 ← scaffold copied by scripts/new_story.py / world_create.py
    system_prompt.txt        ← references the @include files below
    world_lore.json
    character_schema.json
    default_character_state.json
    parsed_conversation.json
    nsfw_preferences.json

  # Shared snippets (extracted from story_original; reusable across worlds)
  fate_system.txt            ← 命運走向系統（命運骰）
  format_conventions.txt     ← 對話/內心/旁白/系統訊息/分隔線格式
  npc_acting.txt             ← NPC 演繹指南
  time_marker.txt            ← <!--TIME ... TIME--> tag 規格
  gm_command_mode.txt        ← /gm 金手指指令模式
  power_tier.txt             ← D/C/B/A/S 戰力等級框架（**可選**：RPG 戰鬥世界用）

  # Legacy placeholders (referenced by old wizard outputs; safe to leave empty
  # or fill with your own content)
  gm_rules.txt
  tag_format.txt
```

Reference any of these from a story's `system_prompt.txt` via `@include`. The
new template under `_template/` already wires in the recommended set; remove
or add includes per story as needed (e.g. drop `power_tier.txt` for a
non-combat school setting).

## What is *not* shareable

- `world_lore.json` (each story has its own world)
- `lore.db` (runtime RAG index, per-story by design)
- `character_schema.json` / `default_character_state.json` (state shape is world-specific)
- `parsed_conversation.json` (opening script, world-specific)

Stick `nsfw_preferences.json`, GM rule snippets, tag format specs, and fate /
team / dice system descriptions in `_shared/`. World-specific content stays in
each story folder.

## Adding a new story

```
python scripts/new_story.py xianxia --name "仙俠世界" --switch
```

Then edit `story_design/xianxia/system_prompt.txt` and
`story_design/xianxia/world_lore.json` to fill in the world-specific parts.
