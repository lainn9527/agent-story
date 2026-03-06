import base64
import io
import json
import urllib.error

import image_gen


def test_extract_gemini_image_bytes_inline_data():
    raw = b"\x89PNG\r\n\x1a\nfake"
    data = base64.b64encode(raw).decode("ascii")
    response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inline_data": {
                                "mime_type": "image/png",
                                "data": data,
                            }
                        }
                    ]
                }
            }
        ]
    }
    extracted = image_gen._extract_gemini_image_bytes(response)
    assert extracted is not None
    payload, mime = extracted
    assert payload == raw
    assert mime == "image/png"


def test_extract_gemini_image_bytes_inlineData():
    raw = b"\xff\xd8\xfffakejpg"
    data = base64.b64encode(raw).decode("ascii")
    response = {
        "candidates": [
            {
                "content": {
                    "parts": [
                        {
                            "inlineData": {
                                "mimeType": "image/jpeg",
                                "data": data,
                            }
                        }
                    ]
                }
            }
        ]
    }
    extracted = image_gen._extract_gemini_image_bytes(response)
    assert extracted is not None
    payload, mime = extracted
    assert payload == raw
    assert mime == "image/jpeg"


def test_extract_gemini_image_bytes_missing_inline_returns_none():
    response = {
        "candidates": [
            {
                "content": {
                    "parts": [{"text": "no image part"}]
                }
            }
        ]
    }
    assert image_gen._extract_gemini_image_bytes(response) is None


def test_is_key_error_400_only_for_specific_markers():
    assert image_gen._is_key_error(400, "API key not valid. Please pass a valid API key.") is True
    assert image_gen._is_key_error(400, "Invalid argument: responseModalities not supported") is False
    assert image_gen._is_key_error(429, "Too Many Requests") is False


def test_is_quota_exhausted_error_markers():
    assert image_gen._is_quota_exhausted_error(429, "Too Many Requests") is True
    assert image_gen._is_quota_exhausted_error(400, "status: RESOURCE_EXHAUSTED") is True
    assert image_gen._is_quota_exhausted_error(400, "quota exceeded for this model") is True
    assert image_gen._is_quota_exhausted_error(400, "Invalid argument: responseModalities not supported") is False


def test_download_image_prefers_gemini(monkeypatch, tmp_path):
    called = {"gemini": 0, "pollinations": 0}
    captured = {"model": None}

    def _fake_gemini(dest, prompt, model_override=None, **kwargs):
        called["gemini"] += 1
        captured["model"] = model_override
        with open(dest, "wb") as f:
            f.write(b"x")
        return True

    def _fake_pollinations(dest, prompt):
        called["pollinations"] += 1
        return True

    monkeypatch.setattr(image_gen, "_download_via_gemini", _fake_gemini)
    monkeypatch.setattr(image_gen, "_download_via_pollinations", _fake_pollinations)

    provider = image_gen._download_image(str(tmp_path / "img.png"), "test prompt", model_override="gemini-x")
    assert provider == "gemini"
    assert called["gemini"] == 1
    assert called["pollinations"] == 0
    assert captured["model"] == "gemini-x"


def test_download_image_fallback_pollinations(monkeypatch, tmp_path):
    called = {"gemini": 0, "pollinations": 0}

    def _fake_gemini(dest, prompt, model_override=None, **kwargs):
        called["gemini"] += 1
        return False

    def _fake_pollinations(dest, prompt):
        called["pollinations"] += 1
        with open(dest, "wb") as f:
            f.write(b"y")
        return True

    monkeypatch.setattr(image_gen, "_download_via_gemini", _fake_gemini)
    monkeypatch.setattr(image_gen, "_download_via_pollinations", _fake_pollinations)

    provider = image_gen._download_image(str(tmp_path / "img.png"), "test prompt")
    assert provider == "pollinations"
    assert called["gemini"] == 1
    assert called["pollinations"] == 1


def test_extract_imagen_image_bytes_from_predictions():
    raw = b"\x89PNG\r\n\x1a\nfake-imagen"
    data = base64.b64encode(raw).decode("ascii")
    response = {"predictions": [{"bytesBase64Encoded": data, "mimeType": "image/png"}]}
    extracted = image_gen._extract_imagen_image_bytes(response)
    assert extracted is not None
    payload, mime = extracted
    assert payload == raw
    assert mime == "image/png"


def test_download_via_gemini_imagen_model_uses_predict(monkeypatch, tmp_path):
    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    raw = b"imagen-bytes"
    encoded = base64.b64encode(raw).decode("ascii")
    captured = {"url": "", "body": {}}

    def _fake_urlopen(req, timeout=90, context=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp({"predictions": [{"bytesBase64Encoded": encoded}]})

    monkeypatch.setattr(image_gen, "_load_gemini_cfg", lambda: {})
    monkeypatch.setattr(image_gen, "get_available_keys", lambda cfg: [{"key": "test-key"}])
    monkeypatch.setattr(image_gen.urllib.request, "urlopen", _fake_urlopen)

    dest = tmp_path / "img.png"
    ok = image_gen._download_via_gemini(str(dest), "robot skateboard", model_override="imagen-4.0-ultra-generate-001")
    assert ok is True
    assert dest.read_bytes() == raw
    assert captured["url"].endswith(":predict")
    assert captured["body"]["instances"][0]["prompt"] == "robot skateboard"
    assert captured["body"]["parameters"]["sampleCount"] == 1
    assert captured["body"]["parameters"]["aspectRatio"] in {"1:1", "3:4", "4:3", "9:16", "16:9"}


def test_download_via_gemini_gemini_image_model_uses_generate_content(monkeypatch, tmp_path):
    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    raw = b"gemini-image-bytes"
    encoded = base64.b64encode(raw).decode("ascii")
    captured = {"url": "", "body": {}}

    def _fake_urlopen(req, timeout=90, context=None):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"inline_data": {"data": encoded, "mime_type": "image/png"}}
                            ]
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr(image_gen, "_load_gemini_cfg", lambda: {})
    monkeypatch.setattr(image_gen, "get_available_keys", lambda cfg: [{"key": "test-key"}])
    monkeypatch.setattr(image_gen.urllib.request, "urlopen", _fake_urlopen)

    dest = tmp_path / "img.png"
    ok = image_gen._download_via_gemini(str(dest), "scene prompt", model_override="gemini-2.5-flash-image")
    assert ok is True
    assert dest.read_bytes() == raw
    assert captured["url"].endswith(":generateContent")
    assert captured["body"]["contents"][0]["parts"][0]["text"] == "scene prompt"
    assert captured["body"]["generationConfig"]["responseModalities"] == ["IMAGE"]


def test_download_via_gemini_quota_exhausted_tries_next_key(monkeypatch, tmp_path):
    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    raw = b"next-key-image"
    encoded = base64.b64encode(raw).decode("ascii")
    calls = []
    marked = []

    def _fake_urlopen(req, timeout=90, context=None):
        headers = dict(req.header_items())
        key = headers.get("X-goog-api-key")
        calls.append(key)
        if len(calls) == 1:
            body = {
                "error": {
                    "code": 400,
                    "message": "RESOURCE_EXHAUSTED: quota exceeded",
                    "status": "RESOURCE_EXHAUSTED",
                }
            }
            raise urllib.error.HTTPError(
                req.full_url,
                400,
                "Bad Request",
                hdrs=None,
                fp=io.BytesIO(json.dumps(body).encode("utf-8")),
            )
        return _Resp(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"inline_data": {"data": encoded, "mime_type": "image/png"}}
                            ]
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr(image_gen, "_load_gemini_cfg", lambda: {})
    monkeypatch.setattr(
        image_gen,
        "get_available_keys",
        lambda cfg: [{"key": "first-key"}, {"key": "second-key"}],
    )
    monkeypatch.setattr(image_gen, "mark_rate_limited", lambda key: marked.append(key))
    monkeypatch.setattr(image_gen.urllib.request, "urlopen", _fake_urlopen)

    dest = tmp_path / "img.png"
    ok = image_gen._download_via_gemini(str(dest), "scene prompt", model_override="gemini-2.5-flash-image")
    assert ok is True
    assert dest.read_bytes() == raw
    assert calls == ["first-key", "second-key"]
    assert marked == ["first-key"]


def test_get_image_status_returns_warning_once(monkeypatch, tmp_path):
    monkeypatch.setattr(image_gen, "STORIES_DIR", str(tmp_path / "stories"))
    image_gen._set_image_warning(
        "story-x",
        "img-x.png",
        image_gen.FREE_QUOTA_WARNING_CODE,
        image_gen.FREE_QUOTA_WARNING_MESSAGE,
    )

    first = image_gen.get_image_status("story-x", "img-x.png")
    second = image_gen.get_image_status("story-x", "img-x.png")

    assert first["ready"] is False
    assert first["warning"]["code"] == image_gen.FREE_QUOTA_WARNING_CODE
    assert "warning" not in second


def test_download_via_gemini_sets_free_quota_warning(monkeypatch, tmp_path):
    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    raw = b"paid-key-image"
    encoded = base64.b64encode(raw).decode("ascii")
    calls = []

    def _fake_urlopen(req, timeout=90, context=None):
        headers = dict(req.header_items())
        key = headers.get("X-goog-api-key")
        calls.append(key)
        if len(calls) == 1:
            body = {
                "error": {
                    "code": 400,
                    "message": "RESOURCE_EXHAUSTED: quota exceeded",
                    "status": "RESOURCE_EXHAUSTED",
                }
            }
            raise urllib.error.HTTPError(
                req.full_url,
                400,
                "Bad Request",
                hdrs=None,
                fp=io.BytesIO(json.dumps(body).encode("utf-8")),
            )
        return _Resp(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"inline_data": {"data": encoded, "mime_type": "image/png"}}
                            ]
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr(image_gen, "_load_gemini_cfg", lambda: {})
    monkeypatch.setattr(
        image_gen,
        "get_available_keys",
        lambda cfg: [
            {"key": "free-key", "tier": "free"},
            {"key": "paid-key", "tier": "paid"},
        ],
    )
    monkeypatch.setattr(image_gen.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(image_gen, "STORIES_DIR", str(tmp_path / "stories"))

    story_id = "story-y"
    filename = "img-y.png"
    image_gen._consume_image_warning(story_id, filename)

    dest = tmp_path / "img.png"
    ok = image_gen._download_via_gemini(
        str(dest),
        "scene prompt",
        model_override="gemini-2.5-flash-image",
        story_id=story_id,
        filename=filename,
    )
    status = image_gen.get_image_status(story_id, filename)

    assert ok is True
    assert dest.read_bytes() == raw
    assert calls == ["free-key", "paid-key"]
    assert status["warning"]["code"] == image_gen.FREE_QUOTA_WARNING_CODE
    assert status["warning"]["message"] == image_gen.FREE_QUOTA_WARNING_MESSAGE


def test_download_via_gemini_429_treated_as_quota_error(monkeypatch, tmp_path):
    class _Resp:
        def __init__(self, payload):
            self._payload = payload

        def read(self):
            return json.dumps(self._payload).encode("utf-8")

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

    raw = b"quota-swap-image"
    encoded = base64.b64encode(raw).decode("ascii")
    calls = []
    marked = []

    def _fake_urlopen(req, timeout=90, context=None):
        headers = dict(req.header_items())
        key = headers.get("X-goog-api-key")
        calls.append(key)
        if len(calls) == 1:
            body = {
                "error": {
                    "code": 429,
                    "message": "Too Many Requests",
                    "status": "RESOURCE_EXHAUSTED",
                }
            }
            raise urllib.error.HTTPError(
                req.full_url,
                429,
                "Too Many Requests",
                hdrs=None,
                fp=io.BytesIO(json.dumps(body).encode("utf-8")),
            )
        return _Resp(
            {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {"inline_data": {"data": encoded, "mime_type": "image/png"}}
                            ]
                        }
                    }
                ]
            }
        )

    monkeypatch.setattr(image_gen, "_load_gemini_cfg", lambda: {})
    monkeypatch.setattr(
        image_gen,
        "get_available_keys",
        lambda cfg: [
            {"key": "free-key", "tier": "free"},
            {"key": "paid-key", "tier": "paid"},
        ],
    )
    monkeypatch.setattr(image_gen, "mark_rate_limited", lambda key: marked.append(key))
    monkeypatch.setattr(image_gen.urllib.request, "urlopen", _fake_urlopen)
    monkeypatch.setattr(image_gen, "STORIES_DIR", str(tmp_path / "stories"))

    story_id = "story-429"
    filename = "img-429.png"

    dest = tmp_path / "img.png"
    ok = image_gen._download_via_gemini(
        str(dest),
        "scene prompt",
        model_override="gemini-2.5-flash-image",
        story_id=story_id,
        filename=filename,
    )
    status = image_gen.get_image_status(story_id, filename)

    assert ok is True
    assert dest.read_bytes() == raw
    assert calls == ["free-key", "paid-key"]
    assert marked == ["free-key"]
    assert status["warning"]["code"] == image_gen.FREE_QUOTA_WARNING_CODE
    assert status["warning"]["message"] == image_gen.FREE_QUOTA_WARNING_MESSAGE
