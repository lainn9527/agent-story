"""Card mode in-card clock: minute-level tracking and header synchronization."""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
import os
import re

from story_core.story_io import (
    _branch_dir,
    _load_json,
    _save_json,
)

log = logging.getLogger("rpg")

DEFAULT_START_ISO = "2026-05-16T02:08:00"
MAX_FORWARD_JUMP_MINUTES = 12 * 60  # 12 hours max jump tolerance

_DATE_RE = re.compile(r"📅\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})")
_TIME_RE = re.compile(r"⏰\s*(\d{1,2}):(\d{2})")
_EMOJI_CHARS = r"\U00010000-\U0010ffff\u2600-\u27bf\u2300-\u23ff"
_LOC_RE = re.compile(rf"📌\s*([^　\n\r{_EMOJI_CHARS}|｜]+)")
_WEATHER_RE = re.compile(
    r"(?:🌤️|🌤|☀️|☀|⛅|☁️|☁|🌧️|🌧|🌨️|🌨|🌪️|🌪|🌫️|🌫|💨|❄️|❄|🔥|🕯️|🕯|🛠️|🛠|🛡️|🛡|"
    r"🌌|✨|🍃|🌿|🌲|🌅|🌆|🌇|🌙|⚡|🌀|🏮|🍵|🏕️|🏕)\s*([^\n\r]+)"
)
_PLAIN_HEADER_RE = re.compile(
    r"^\s*(\d{4})[/-](\d{1,2})[/-](\d{1,2})"
    r"(?:\s*[（(][^）)\n]*[）)]\s*|\s+)(\d{1,2}):(\d{2})"
    r"\s*[｜|]\s*([^｜|\n]+)\s*[｜|]\s*([^\n]+?)\s*$"
)


def _clock_path(story_id: str, branch_id: str = "main") -> str:
    return os.path.join(_branch_dir(story_id, branch_id), "card_clock.json")


def load_clock(story_id: str, branch_id: str = "main") -> dict:
    """Load clock data: {start_iso, elapsed_minutes, last_weather}."""
    path = _clock_path(story_id, branch_id)
    data = _load_json(path, {})
    if not isinstance(data, dict) or "start_iso" not in data:
        data = {
            "start_iso": DEFAULT_START_ISO,
            "elapsed_minutes": 0,
            "last_weather": "陰",
        }
    # Older saves predate location persistence; recover the latest scene header.
    if not data.get("last_location"):
        messages = _load_json(os.path.join(_branch_dir(story_id, branch_id), "messages.json"), [])
        for message in reversed(messages):
            if message.get("role") == "gm":
                parsed = parse_header_time(message.get("content", ""))
                if parsed and parsed.get("location"):
                    data["last_location"] = parsed["location"]
                    break
    return data


def save_clock(story_id: str, branch_id: str, clock_data: dict) -> None:
    """Persist clock data."""
    path = _clock_path(story_id, branch_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    _save_json(path, clock_data)


def now(story_id: str, branch_id: str = "main") -> datetime:
    """Return the current in-card datetime: start_iso + elapsed_minutes."""
    clock_data = load_clock(story_id, branch_id)
    start_iso = clock_data.get("start_iso", DEFAULT_START_ISO)
    try:
        base_dt = datetime.fromisoformat(start_iso)
    except Exception:
        base_dt = datetime.fromisoformat(DEFAULT_START_ISO)
    elapsed = max(0, int(clock_data.get("elapsed_minutes", 0)))
    return base_dt + timedelta(minutes=elapsed)


def advance(story_id: str, branch_id: str = "main", minutes: int = 0) -> datetime:
    """Advance the in-card clock by `minutes`. Ignores non-positive values."""
    if minutes <= 0:
        return now(story_id, branch_id)

    clock_data = load_clock(story_id, branch_id)
    current_elapsed = int(clock_data.get("elapsed_minutes", 0))
    clock_data["elapsed_minutes"] = current_elapsed + int(minutes)
    save_clock(story_id, branch_id, clock_data)
    log.info("clock: advanced %s/%s by %d mins (now %d mins)", story_id, branch_id, minutes, clock_data["elapsed_minutes"])
    return now(story_id, branch_id)


def parse_header_time(text: str) -> dict | None:
    """Parse header details (date, time, location, weather) from narration text.

    Expected format (at beginning of narration):
      📅 2026/5/16　⏰ 02:32　📌 舊大學一樓女廁　🌤️ 陰，水管低沉的咕嚕聲
    """
    if not text:
        return None

    first_lines = "\n".join(text.strip().splitlines()[:3])

    time_match = _TIME_RE.search(first_lines)
    if not time_match:
        # Grounding-style headers are also used by narrators. Require a full
        # date and both scene separators so ordinary prose is not a clock.
        for line in text.strip().splitlines()[:3]:
            plain = _PLAIN_HEADER_RE.fullmatch(line)
            if plain:
                year, month, day, hour, minute, location, weather = plain.groups()
                return {
                    "year": int(year), "month": int(month), "day": int(day),
                    "hour": int(hour), "minute": int(minute),
                    "location": location.strip(), "weather": weather.strip(),
                }
        return None

    hour = int(time_match.group(1))
    minute = int(time_match.group(2))

    date_match = _DATE_RE.search(first_lines)
    year, month, day = None, None, None
    if date_match:
        year = int(date_match.group(1))
        month = int(date_match.group(2))
        day = int(date_match.group(3))

    loc_match = _LOC_RE.search(first_lines)
    location = loc_match.group(1).strip() if loc_match else ""

    weather_match = _WEATHER_RE.search(first_lines)
    weather = weather_match.group(1).strip() if weather_match else ""
    if not weather and loc_match:
        rem_lines = first_lines[loc_match.end():].splitlines()
        rem = rem_lines[0] if rem_lines else ""
        weather = re.sub(rf"^[\s　{_EMOJI_CHARS}\ufe0f|｜·・,，、]+", "", rem).strip()

    return {
        "year": year,
        "month": month,
        "day": day,
        "hour": hour,
        "minute": minute,
        "location": location,
        "weather": weather,
    }


def sync_from_narration(story_id: str, branch_id: str = "main", narration: str = "") -> bool:
    """Synchronize the in-card clock based on the model's output header.

    Rules:
    - Explicit full dates are authoritative forward timestamps; undated headers
      retain the 12-hour guard. Returns True when a valid header owns this turn.
    - Only undated headers infer a day transition; explicit dates never roll over.
    - Persists location and weather from accepted headers.
    """
    parsed = parse_header_time(narration)
    if not parsed:
        return False

    clock_data = load_clock(story_id, branch_id)
    cur_dt = now(story_id, branch_id)

    # Determine target datetime
    target_year = parsed["year"] or cur_dt.year
    target_month = parsed["month"] or cur_dt.month
    target_day = parsed["day"] or cur_dt.day

    try:
        target_dt = datetime(target_year, target_month, target_day, parsed["hour"], parsed["minute"])
    except ValueError:
        log.warning("clock: invalid parsed header datetime: %s", parsed)
        return False

    # Check for day rollover if hour appears to have rolled back
    explicit_date = all(parsed[k] is not None for k in ("year", "month", "day"))
    if target_dt < cur_dt and not explicit_date:
        rollover_cues = ("跨日", "次日", "隔天", "第二天", "明天", "破曉", "清晨")
        if any(cue in narration for cue in rollover_cues):
            target_dt += timedelta(days=1)

    diff_seconds = (target_dt - cur_dt).total_seconds()
    diff_minutes = int(diff_seconds / 60)

    weather = parsed.get("weather")
    changed = False

    if diff_minutes > 0 and (explicit_date or diff_minutes <= MAX_FORWARD_JUMP_MINUTES):
        clock_data["elapsed_minutes"] = int(clock_data.get("elapsed_minutes", 0)) + diff_minutes
        changed = True
        log.info(
            "clock: synced from header +%d mins to %s for %s/%s",
            diff_minutes, target_dt.isoformat(), story_id, branch_id
        )
    elif diff_minutes > MAX_FORWARD_JUMP_MINUTES:
        log.warning(
            "clock: header time %s jumps +%d mins (> 12h) from current %s; ignoring jump as hallucination",
            target_dt.isoformat(), diff_minutes, cur_dt.isoformat()
        )
    elif diff_minutes < 0:
        log.debug(
            "clock: header time %s is behind current %s without day transition; ignoring",
            target_dt.isoformat(), cur_dt.isoformat()
        )

    if diff_minutes < 0 or (not explicit_date and diff_minutes > MAX_FORWARD_JUMP_MINUTES):
        return True

    location = parsed.get("location")
    if location and location != clock_data.get("last_location"):
        clock_data["last_location"] = location
        changed = True

    if weather and weather != clock_data.get("last_weather"):
        clock_data["last_weather"] = weather
        changed = True

    if changed:
        save_clock(story_id, branch_id, clock_data)
    return True
