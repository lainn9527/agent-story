import base64

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


def test_download_image_prefers_gemini(monkeypatch, tmp_path):
    called = {"gemini": 0, "pollinations": 0}

    def _fake_gemini(dest, prompt):
        called["gemini"] += 1
        with open(dest, "wb") as f:
            f.write(b"x")
        return True

    def _fake_pollinations(dest, prompt):
        called["pollinations"] += 1
        return True

    monkeypatch.setattr(image_gen, "_download_via_gemini", _fake_gemini)
    monkeypatch.setattr(image_gen, "_download_via_pollinations", _fake_pollinations)

    provider = image_gen._download_image(str(tmp_path / "img.png"), "test prompt")
    assert provider == "gemini"
    assert called["gemini"] == 1
    assert called["pollinations"] == 0


def test_download_image_fallback_pollinations(monkeypatch, tmp_path):
    called = {"gemini": 0, "pollinations": 0}

    def _fake_gemini(dest, prompt):
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
