"""Flask backend bootstrap for 主神空間 RPG Web App."""

import logging
import logging.handlers
import os

# Version — single source of truth in VERSION file
_version_file = os.path.join(os.path.dirname(__file__), "VERSION")
if os.path.exists(_version_file):
    with open(_version_file) as _f:
        __version__ = _f.read().strip()
else:
    __version__ = "0.0.0"

from flask import Flask
from flask_compress import Compress

# ---------------------------------------------------------------------------
# Logging — console + rotating file
# ---------------------------------------------------------------------------
_log_fmt = logging.Formatter(
    "[%(asctime)s] %(levelname)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Console handler (for interactive use / tmux)
_console_h = logging.StreamHandler()
_console_h.setFormatter(_log_fmt)

# Rotating file handler — 5 MB per file, keep 3 backups
_log_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server.log")
_file_h = logging.handlers.RotatingFileHandler(
    _log_file, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8",
)
_file_h.setFormatter(_log_fmt)

# Root logger — captures Flask/Werkzeug + our "rpg" logger
logging.root.setLevel(logging.INFO)
logging.root.addHandler(_console_h)
logging.root.addHandler(_file_h)

log = logging.getLogger("rpg")

from routes.lore_routes import lore_bp
from routes.branch_routes import branch_bp
from routes.debug_routes import debug_bp
from routes.story_routes import story_bp
from routes.misc_routes import misc_bp
from routes.core_routes import core_bp, _sse_event
from story_core.app_helpers import *  # noqa: F401,F403

# Flask App
# ---------------------------------------------------------------------------
app = Flask(__name__)
app.config["COMPRESS_STREAMS"] = False
Compress(app)
app.register_blueprint(lore_bp)
app.register_blueprint(branch_bp)
app.register_blueprint(debug_bp)
app.register_blueprint(story_bp)
app.register_blueprint(misc_bp)
app.register_blueprint(core_bp)


if __name__ == "__main__":
    _ensure_data_dir()
    _cleanup_incomplete_branches()
    _init_lore_indexes()
    _init_dungeon_templates()
    port = int(os.environ.get("PORT", 5051))
    app.run(debug=True, host="0.0.0.0", port=port)
