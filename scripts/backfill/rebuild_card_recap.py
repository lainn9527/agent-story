"""Build a reviewable recap candidate from full history without mutating gameplay.

Usage: python3 scripts/backfill/rebuild_card_recap.py naomi --output /tmp/naomi-recap.json
Candidate includes source digest: apply only if source timeline is still identical.
"""
import argparse
import hashlib
import json
import logging
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from story_core import compaction
from story_core.branch_tree import get_full_timeline
from story_core.card_mode.recap import COMPACT_PROMPT, META_COMPACT_PROMPT, RECAP_VERSION


def timeline_digest(timeline):
    source = [{k: m.get(k) for k in ("index", "role", "content", "message_type")} for m in timeline]
    return hashlib.sha256(json.dumps(source, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("story_id")
    parser.add_argument("--branch", default="main")
    parser.add_argument("--output", required=True)
    parser.add_argument("--batch-chars", type=int, default=60000)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    timeline = get_full_timeline(args.story_id, args.branch)
    previous = compaction.load_recap(args.story_id, args.branch)
    original_dir = compaction.STORIES_DIR
    try:
        with tempfile.TemporaryDirectory() as temporary:
            compaction.STORIES_DIR = temporary
            compaction._run_compaction(
                args.story_id, args.branch, timeline,
                prompt_template=COMPACT_PROMPT, meta_prompt_template=META_COMPACT_PROMPT,
                max_message_chars=None, batch_chars=args.batch_chars, recap_version=RECAP_VERSION,
            )
            candidate = compaction.load_recap(args.story_id, args.branch)
    finally:
        compaction.STORIES_DIR = original_dir
    if candidate.get("compacted_through_index") != len(timeline) - compaction.RECENT_WINDOW - 1:
        raise RuntimeError("Rebuild incomplete; existing gameplay was not changed")
    output = {"story_id": args.story_id, "branch_id": args.branch,
              "source_digest": timeline_digest(timeline), "source_recap": previous,
              "recap": candidate}
    Path(args.output).write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(json.dumps({"output": args.output, "recap_chars": len(candidate["recap_text"]),
                      "covered": candidate["compacted_through_index"]}))


if __name__ == "__main__":
    main()
