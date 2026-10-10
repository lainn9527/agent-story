"""Cache version for the shared world/card presentation assets."""
import hashlib
from pathlib import Path
from flask import current_app


def chat_asset_version():
    root = Path(current_app.static_folder)
    names = ("app.js", "card.js", "chat_ui.js", "style.css", "card.css")
    stamps = [f"{name}:{(root / name).stat().st_mtime_ns}" for name in names]
    return hashlib.sha256("|".join(stamps).encode()).hexdigest()[:12]
