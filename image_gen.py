"""Image generation via Gemini image API with Pollinations fallback."""

import base64
import hashlib
import json
import logging
import os
import ssl
import threading
import urllib.error
import urllib.parse
import urllib.request

from gemini_key_manager import get_available_keys, mark_rate_limited

log = logging.getLogger("rpg")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STORIES_DIR = os.path.join(BASE_DIR, "data", "stories")
LLM_CONFIG_PATH = os.path.join(BASE_DIR, "llm_config.json")

POLLINATIONS_BASE = "https://image.pollinations.ai/prompt"
GEMINI_BASE = "https://generativelanguage.googleapis.com/v1beta/models"
GEMINI_IMAGE_MODEL = "gemini-2.5-flash-image"
IMAGE_WIDTH = 768
IMAGE_HEIGHT = 512
FREE_QUOTA_WARNING_CODE = "free_quota_exhausted"
FREE_QUOTA_WARNING_MESSAGE = "Gemini 免費圖片額度已用盡，已自動切換到下一把 API key。"

_image_warning_lock = threading.Lock()
_image_warnings: dict[tuple[str, str], dict] = {}

# Build SSL context using certifi certificates when available.
try:
    import certifi
    _ssl_ctx = ssl.create_default_context(cafile=certifi.where())
except Exception:
    _ssl_ctx = ssl.create_default_context()


def _images_dir(story_id: str) -> str:
    d = os.path.join(STORIES_DIR, story_id, "images")
    os.makedirs(d, exist_ok=True)
    return d


def _make_filename(message_index: int, prompt: str) -> str:
    h = hashlib.md5(prompt.encode("utf-8")).hexdigest()[:8]
    return f"img_{message_index}_{h}.png"


def _set_image_warning(story_id: str | None, filename: str | None, code: str, message: str):
    if not story_id or not filename:
        return
    with _image_warning_lock:
        _image_warnings[(story_id, filename)] = {"code": code, "message": message}


def _consume_image_warning(story_id: str, filename: str) -> dict | None:
    with _image_warning_lock:
        return _image_warnings.pop((story_id, filename), None)


def _load_gemini_cfg() -> dict:
    try:
        with open(LLM_CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        g = cfg.get("gemini")
        return g if isinstance(g, dict) else {}
    except Exception:
        return {}


def _is_key_error(http_code: int, body_text: str) -> bool:
    if http_code in (401, 403):
        return True
    if http_code != 400:
        return False
    body = body_text.lower()
    key_error_markers = (
        "api key not valid",
        "invalid api key",
        "request is missing required authentication credential",
        "api_key_invalid",
    )
    return any(m in body for m in key_error_markers)


def _is_quota_exhausted_error(http_code: int, body_text: str) -> bool:
    if http_code == 429:
        return True
    if http_code != 400:
        return False
    body = body_text.lower()
    quota_markers = (
        "resource_exhausted",
        "quota",
        "rate limit",
        "too many requests",
        "exceeded",
        "insufficient tokens",
        "billing",
    )
    return any(m in body for m in quota_markers)


def _is_retryable_http_error(http_code: int) -> bool:
    return http_code == 408 or 500 <= http_code < 600


def _aspect_ratio(width: int, height: int) -> str:
    # Keep request aligned with current display ratio (768x512 -> 3:2).
    if width * 2 == height * 3:
        return "3:2"
    if width * 9 == height * 16:
        return "16:9"
    if width == height:
        return "1:1"
    return "3:2"


def _imagen_aspect_ratio(width: int, height: int) -> str:
    """Map display ratio to Imagen-supported aspect ratios."""
    ratio = width / height if height else 1.0
    candidates = {
        "1:1": 1.0,
        "3:4": 3 / 4,
        "4:3": 4 / 3,
        "9:16": 9 / 16,
        "16:9": 16 / 9,
    }
    return min(candidates.items(), key=lambda kv: abs(kv[1] - ratio))[0]


def _is_imagen_model(model: str) -> bool:
    return model.strip().lower().startswith("imagen-")


def _try_decode_base64(data_b64: str) -> bytes | None:
    if not isinstance(data_b64, str) or not data_b64:
        return None
    try:
        return base64.b64decode(data_b64, validate=True)
    except Exception:
        return None


def _extract_gemini_image_bytes(response_data: dict) -> tuple[bytes, str] | None:
    for candidate in response_data.get("candidates", []):
        content = candidate.get("content", {})
        for part in content.get("parts", []):
            inline = part.get("inline_data") or part.get("inlineData")
            if not isinstance(inline, dict):
                continue
            data_b64 = inline.get("data", "")
            if not data_b64:
                continue
            try:
                image_bytes = base64.b64decode(data_b64, validate=True)
            except Exception:
                continue
            mime = inline.get("mime_type") or inline.get("mimeType") or "image/png"
            if image_bytes:
                return image_bytes, mime
    return None


def _extract_imagen_image_bytes(response_data: dict) -> tuple[bytes, str] | None:
    def _extract_from_node(node) -> tuple[bytes, str] | None:
        if isinstance(node, dict):
            mime = node.get("mimeType") or node.get("mime_type") or "image/png"
            for key in ("bytesBase64Encoded", "imageBytes", "data"):
                payload = _try_decode_base64(node.get(key, ""))
                if payload:
                    return payload, mime
            for val in node.values():
                extracted = _extract_from_node(val)
                if extracted:
                    return extracted
        elif isinstance(node, list):
            for item in node:
                extracted = _extract_from_node(item)
                if extracted:
                    return extracted
        return None

    predictions = response_data.get("predictions", [])
    extracted = _extract_from_node(predictions)
    if extracted:
        return extracted
    return _extract_from_node(response_data.get("generatedImages", []))


def _download_via_gemini(
    dest: str,
    prompt: str,
    model_override: str | None = None,
    *,
    story_id: str | None = None,
    filename: str | None = None,
) -> bool:
    gemini_cfg = _load_gemini_cfg()
    model = (model_override or "").strip() or gemini_cfg.get("image_model") or GEMINI_IMAGE_MODEL
    keys = get_available_keys(gemini_cfg)
    if not keys:
        log.info("    image_gen: Gemini unavailable (no active API keys)")
        return False

    imagen_model = _is_imagen_model(model)
    if imagen_model:
        method = "predict"
        body = {
            "instances": [{"prompt": prompt}],
            "parameters": {
                "sampleCount": 1,
                "aspectRatio": _imagen_aspect_ratio(IMAGE_WIDTH, IMAGE_HEIGHT),
            },
        }
        extractor = _extract_imagen_image_bytes
    else:
        method = "generateContent"
        body = {
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["IMAGE"],
                "imageConfig": {
                    "aspectRatio": _aspect_ratio(IMAGE_WIDTH, IMAGE_HEIGHT),
                },
            },
        }
        extractor = _extract_gemini_image_bytes
    payload = json.dumps(body).encode("utf-8")

    for key_info in keys:
        api_key = key_info.get("key", "")
        if not api_key:
            continue
        url = f"{GEMINI_BASE}/{model}:{method}"
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=90, context=_ssl_ctx) as resp:
                result = json.loads(resp.read().decode("utf-8"))
            extracted = extractor(result)
            if not extracted:
                log.warning("    image_gen: Gemini returned no image data via %s", method)
                continue
            image_bytes, mime = extracted
            with open(dest, "wb") as f:
                f.write(image_bytes)
            log.info("    image_gen: saved via Gemini (%s) %s (%d bytes)", mime, os.path.basename(dest), len(image_bytes))
            return True
        except urllib.error.HTTPError as e:
            body_text = e.read().decode("utf-8", errors="replace")[:300]
            if _is_quota_exhausted_error(e.code, body_text):
                mark_rate_limited(api_key)
                if key_info.get("tier") == "free":
                    _set_image_warning(
                        story_id,
                        filename,
                        FREE_QUOTA_WARNING_CODE,
                        FREE_QUOTA_WARNING_MESSAGE,
                    )
                log.info("    image_gen: Gemini quota exhausted HTTP %d on ...%s, trying next key", e.code, api_key[-6:])
                continue
            if _is_key_error(e.code, body_text):
                mark_rate_limited(api_key)
                log.info("    image_gen: Gemini key error HTTP %d on ...%s, trying next key", e.code, api_key[-6:])
                continue
            if _is_retryable_http_error(e.code):
                log.warning(
                    "    image_gen: Gemini transient HTTP %d on ...%s, trying next key",
                    e.code,
                    api_key[-6:],
                )
                continue
            log.warning("    image_gen: Gemini HTTP %d — %s", e.code, body_text)
            return False
        except Exception as e:
            log.warning("    image_gen: Gemini request failed on ...%s — %s", api_key[-6:], e)
            continue

    return False


def _download_via_pollinations(dest: str, prompt: str) -> bool:
    encoded = urllib.parse.quote(prompt, safe="")
    url = f"{POLLINATIONS_BASE}/{encoded}?width={IMAGE_WIDTH}&height={IMAGE_HEIGHT}&nologo=true"
    req = urllib.request.Request(url, headers={"User-Agent": "StoryRPG/1.0"})

    # First try with normal certificate verification.
    try:
        log.info("    image_gen: fallback Pollinations %s", url[:120])
        with urllib.request.urlopen(req, timeout=90, context=_ssl_ctx) as resp:
            data = resp.read()

        with open(dest, "wb") as f:
            f.write(data)
        log.info("    image_gen: saved via Pollinations %s (%d bytes)", os.path.basename(dest), len(data))
        return True
    except ssl.SSLError as e:
        # Compatibility fallback for occasional broken cert chains on third-party host.
        log.warning("    image_gen: Pollinations TLS verify failed, retrying insecure fallback — %s", e)
        try:
            insecure_ctx = ssl.create_default_context()
            insecure_ctx.check_hostname = False
            insecure_ctx.verify_mode = ssl.CERT_NONE
            with urllib.request.urlopen(req, timeout=90, context=insecure_ctx) as resp:
                data = resp.read()
            with open(dest, "wb") as f:
                f.write(data)
            log.warning(
                "    image_gen: saved via Pollinations insecure TLS fallback %s (%d bytes)",
                os.path.basename(dest),
                len(data),
            )
            return True
        except Exception as e2:
            log.warning("    image_gen: Pollinations insecure fallback failed %s — %s", os.path.basename(dest), e2)
            return False
    except Exception as e:
        log.warning("    image_gen: Pollinations failed %s — %s", os.path.basename(dest), e)
        return False


def _download_image(
    dest: str,
    prompt: str,
    model_override: str | None = None,
    *,
    story_id: str | None = None,
    filename: str | None = None,
) -> str | None:
    if _download_via_gemini(
        dest,
        prompt,
        model_override=model_override,
        story_id=story_id,
        filename=filename,
    ):
        return "gemini"
    if _download_via_pollinations(dest, prompt):
        return "pollinations"
    return None


def generate_image_async(
    story_id: str, prompt: str, message_index: int, model: str | None = None
) -> str:
    """Start background image download. Returns expected filename."""
    filename = _make_filename(message_index, prompt)
    dest = os.path.join(_images_dir(story_id), filename)

    if os.path.exists(dest):
        return filename

    def _download():
        provider = _download_image(
            dest,
            prompt,
            model_override=model,
            story_id=story_id,
            filename=filename,
        )
        if not provider:
            log.warning("    image_gen: FAILED %s (all providers)", filename)

    t = threading.Thread(target=_download, daemon=True)
    t.start()
    return filename


def get_image_status(story_id: str, filename: str) -> dict:
    """Check whether an image file has been downloaded."""
    path = os.path.join(_images_dir(story_id), filename)
    status = {"ready": os.path.exists(path), "filename": filename}
    warning = _consume_image_warning(story_id, filename)
    if warning:
        status["warning"] = warning
    return status


def get_image_path(story_id: str, filename: str) -> str | None:
    """Return absolute path to an image file, or None if not found."""
    path = os.path.join(_images_dir(story_id), filename)
    if os.path.exists(path):
        return path
    return None
