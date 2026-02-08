"""Image generation via Pollinations.ai — async background download."""

import hashlib
import logging
import os
import ssl
import threading
import urllib.parse
import urllib.request

log = logging.getLogger("rpg")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORIES_DIR = os.path.join(BASE_DIR, "data", "stories")

POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
IMAGE_WIDTH = 768
IMAGE_HEIGHT = 512


def _images_dir(story_id: str) -> str:
    d = os.path.join(STORIES_DIR, story_id, "images")
    os.makedirs(d, exist_ok=True)
    return d


def _make_filename(message_index: int, prompt: str) -> str:
    h = hashlib.md5(prompt.encode("utf-8")).hexdigest()[:8]
    return f"img_{message_index}_{h}.png"


def generate_image_async(story_id: str, prompt: str, message_index: int) -> str:
    """Start background download from Pollinations.ai. Returns expected filename."""
    filename = _make_filename(message_index, prompt)
    dest = os.path.join(_images_dir(story_id), filename)

    if os.path.exists(dest):
        return filename

    def _download():
        try:
            encoded = urllib.parse.quote(prompt, safe="")
            url = f"{POLLINATIONS_BASE}/{encoded}?width={IMAGE_WIDTH}&height={IMAGE_HEIGHT}&nologo=true"
            log.info("    image_gen: downloading %s", url[:120])

            req = urllib.request.Request(url, headers={"User-Agent": "StoryRPG/1.0"})
            ctx = ssl.create_default_context()
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=90, context=ctx) as resp:
                data = resp.read()

            with open(dest, "wb") as f:
                f.write(data)
            log.info("    image_gen: saved %s (%d bytes)", filename, len(data))
        except Exception as e:
            log.warning("    image_gen: FAILED %s — %s", filename, e)

    t = threading.Thread(target=_download, daemon=True)
    t.start()
    return filename


def get_image_status(story_id: str, filename: str) -> dict:
    """Check whether an image file has been downloaded."""
    path = os.path.join(_images_dir(story_id), filename)
    return {"ready": os.path.exists(path), "filename": filename}


def get_image_path(story_id: str, filename: str) -> str | None:
    """Return absolute path to an image file, or None if not found."""
    path = os.path.join(_images_dir(story_id), filename)
    if os.path.exists(path):
        return path
    return None
