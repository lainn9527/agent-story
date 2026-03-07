"""CLI wrapper for the auto-play runner.

Keeps the historical `python auto_play.py ...` entrypoint stable while the
implementation lives under `story_core/`.
"""

from story_core.auto_play import *  # noqa: F401,F403


if __name__ == "__main__":
    config = parse_args()
    auto_play(config)
