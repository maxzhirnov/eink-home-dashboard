from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from io import BytesIO
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from dotenv import load_dotenv
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from PIL import Image, ImageDraw, ImageFont


BASE_DIR = Path(__file__).resolve().parent
OUTPUT_PATH = BASE_DIR / "dashboard.png"
WIDTH = 800
HEIGHT = 480
CACHE_SECONDS = 30
DISPLAY_TZ = ZoneInfo("Europe/Moscow")
DEFAULT_WEATHER_ENTITY = "weather.yandex_weather"
CALENDAR_ENTITY = "calendar.family"
TEMP_HISTORY_HOURS = 6
TEMP_HISTORY_POINTS = 12
FINANCE_HISTORY_DAYS = 7
FINANCE_HISTORY_POINTS = 12


load_dotenv(BASE_DIR / ".env")

HA_URL = os.getenv("HA_URL", "").rstrip("/")
HA_TOKEN = os.getenv("HA_TOKEN", "")
WEATHER_ENTITY = os.getenv("WEATHER_ENTITY", DEFAULT_WEATHER_ENTITY)
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() in {"1", "true", "yes", "on"}

ENTITIES = {
    "living_temp": "sensor.temp_living_temperature",
    "living_humidity": "sensor.temp_living_humidity",
    "kitchen_temp": "sensor.temp_kitchen_temperature",
    "kitchen_humidity": "sensor.temp_kitchen_humidity",
    "usd_rub": "sensor.open_exchange_rates_usd_rub",
    "quote": "sensor.kanye_quote",
    "weather": WEATHER_ENTITY,
}

CONDITION_MAP = {
    "sunny": ("SUN", "clear"),
    "clear": ("SUN", "clear"),
    "skc": ("SUN", "clear"),
    "partlycloudy": ("PARTLY", "partly cloudy"),
    "bkn": ("PARTLY", "partly cloudy"),
    "cloudy": ("CLOUD", "cloudy"),
    "overcast": ("CLOUD", "cloudy"),
    "ovc": ("CLOUD", "cloudy"),
    "rainy": ("RAIN", "rain"),
    "rain": ("RAIN", "rain"),
    "-ra": ("RAIN", "rain"),
    "pouring": ("RAIN", "rain"),
    "snowy": ("SNOW", "snow"),
    "snow": ("SNOW", "snow"),
    "lightning": ("STORM", "storm"),
    "storm": ("STORM", "storm"),
}

FONT_CANDIDATES = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSans-Regular.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Regular.ttf",
]

FONT_BOLD_CANDIDATES = [
    "/usr/share/fonts/truetype/noto/NotoSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansCondensed-Bold.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSans-Bold.ttf",
]

app = FastAPI(title="E-Ink Dashboard Renderer")

_cache_lock = threading.Lock()
_cache: dict[str, Any] = {
    "png": None,
    "rendered_at": None,
    "expires_at": None,
    "errors": [],
}


def _font(size: int, bold: bool = False) -> ImageFont.ImageFont:
    candidates = FONT_BOLD_CANDIDATES if bold else FONT_CANDIDATES
    for path in candidates:
        if Path(path).exists():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default()


FONTS = {
    "micro": _font(12),
    "tiny": _font(13),
    "footer": _font(14),
    "small": _font(16),
    "body": _font(22),
    "finance": _font(34),
    "metric": _font(38),
    "weather": _font(34),
    "submetric": _font(20),
    "humidity": _font(19),
    "event": _font(40, bold=True),
    "label": _font(14, bold=True),
}


def _fetch_state(entity_id: str) -> tuple[dict[str, Any] | None, str | None]:
    if not HA_URL or not HA_TOKEN:
        return None, "HA_URL or HA_TOKEN is missing"

    url = f"{HA_URL}/api/states/{entity_id}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}

    try:
        response = requests.get(url, headers=headers, timeout=8)
        response.raise_for_status()
        return response.json(), None
    except requests.RequestException as exc:
        return None, f"{entity_id}: {exc}"
    except ValueError as exc:
        return None, f"{entity_id}: invalid JSON: {exc}"


def _fetch_calendar_events(limit: int = 3) -> tuple[list[dict[str, Any]], str | None]:
    if not HA_URL or not HA_TOKEN:
        return [], "HA_URL or HA_TOKEN is missing"

    now = datetime.now(DISPLAY_TZ)
    url = f"{HA_URL}/api/calendars/{CALENDAR_ENTITY}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    params = {
        "start": now.isoformat(),
        "end": (now + timedelta(days=21)).isoformat(),
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=8)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        return [], f"{CALENDAR_ENTITY}: {exc}"
    except ValueError as exc:
        return [], f"{CALENDAR_ENTITY}: invalid JSON: {exc}"

    if isinstance(payload, dict):
        raw_events = payload.get("events", [])
    elif isinstance(payload, list):
        raw_events = payload
    else:
        return [], f"{CALENDAR_ENTITY}: unexpected calendar response"

    events = [event for event in raw_events if isinstance(event, dict)]
    events.sort(key=lambda event: _event_start(event) or datetime.max.replace(tzinfo=DISPLAY_TZ))
    return events[:limit], None


def _fetch_entity_history(
    entity_keys: list[str],
    start: datetime,
    end: datetime,
    label: str,
    point_limit: int,
) -> tuple[dict[str, list[float]], str | None]:
    if not HA_URL or not HA_TOKEN:
        return {}, "HA_URL or HA_TOKEN is missing"

    entity_ids = [ENTITIES[key] for key in entity_keys]
    url = f"{HA_URL}/api/history/period/{start.isoformat()}"
    headers = {"Authorization": f"Bearer {HA_TOKEN}"}
    params = {
        "end_time": end.isoformat(),
        "filter_entity_id": ",".join(entity_ids),
        "minimal_response": "1",
        "no_attributes": "1",
    }

    try:
        response = requests.get(url, headers=headers, params=params, timeout=8)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        return {}, f"{label} history: {exc}"
    except ValueError as exc:
        return {}, f"{label} history: invalid JSON: {exc}"

    history: dict[str, list[float]] = {key: [] for key in entity_keys}
    if not isinstance(payload, list):
        return history, f"{label} history: unexpected response"

    entity_to_key = {entity_id: key for key, entity_id in ENTITIES.items() if key in history}
    for series in payload:
        if not isinstance(series, list) or not series:
            continue
        entity_id = series[0].get("entity_id") if isinstance(series[0], dict) else None
        key = entity_to_key.get(entity_id)
        if not key:
            continue
        values = [_float_or_none(point.get("state")) for point in series if isinstance(point, dict)]
        history[key] = [value for value in values if value is not None]

    return {key: _sample_points(values, point_limit) for key, values in history.items()}, None


def _fetch_temperature_history() -> tuple[dict[str, list[float]], str | None]:
    end = datetime.now(DISPLAY_TZ)
    start = end - timedelta(hours=TEMP_HISTORY_HOURS)
    return _fetch_entity_history(
        ["living_temp", "kitchen_temp"],
        start,
        end,
        "temperature",
        TEMP_HISTORY_POINTS,
    )


def _fetch_finance_history() -> tuple[dict[str, list[float]], str | None]:
    end = datetime.now(DISPLAY_TZ)
    start = end - timedelta(days=FINANCE_HISTORY_DAYS)
    return _fetch_entity_history(["usd_rub"], start, end, "finance", FINANCE_HISTORY_POINTS)


def _fetch_dashboard_data() -> tuple[dict[str, Any], list[str]]:
    data: dict[str, Any] = {}
    errors: list[str] = []

    if not HA_URL or not HA_TOKEN:
        return data, ["HA_URL or HA_TOKEN is missing"]

    calendar_events, calendar_error = _fetch_calendar_events(limit=3)
    data["calendar_events"] = calendar_events
    if calendar_error:
        errors.append(calendar_error)

    temperature_history, history_error = _fetch_temperature_history()
    data["temperature_history"] = temperature_history
    if history_error:
        errors.append(history_error)

    finance_history, finance_history_error = _fetch_finance_history()
    data["finance_history"] = finance_history
    if finance_history_error:
        errors.append(finance_history_error)

    for key, entity_id in ENTITIES.items():
        state, error = _fetch_state(entity_id)
        data[key] = state
        if error:
            errors.append(error)

    return data, errors


def _text_bbox(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def _truncate(
    draw: ImageDraw.ImageDraw,
    text: str,
    font: ImageFont.ImageFont,
    max_width: int,
    ellipsis: str = "...",
) -> str:
    text = str(text or "").strip()
    if _text_bbox(draw, text, font)[0] <= max_width:
        return text

    low = 0
    high = len(text)
    while low < high:
        mid = (low + high + 1) // 2
        candidate = text[:mid].rstrip() + ellipsis
        if _text_bbox(draw, candidate, font)[0] <= max_width:
            low = mid
        else:
            high = mid - 1
    return text[:low].rstrip() + ellipsis


def _parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _local_datetime(value: str | None) -> datetime | None:
    parsed = _parse_datetime(value)
    if not parsed:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=DISPLAY_TZ)
    return parsed.astimezone(DISPLAY_TZ)


def _event_title(event: dict[str, Any]) -> str:
    title = event.get("summary") or event.get("message") or event.get("title") or "Untitled event"
    return str(title).strip()


def _event_start_value(event: dict[str, Any]) -> str | None:
    start = event.get("start") or event.get("start_time")
    if isinstance(start, dict):
        return start.get("dateTime") or start.get("date")
    return start


def _event_start(event: dict[str, Any]) -> datetime | None:
    return _local_datetime(_event_start_value(event))


def _calendar_info(events: list[dict[str, Any]] | None) -> tuple[str, str]:
    if not events:
        return "No upcoming event", "Time unavailable"

    first = events[0]
    message = _event_title(first)
    start = _event_start(first)
    if not start:
        return message, "Time unavailable"

    date_text = start.strftime("%A, %b %-d")
    time_text = start.strftime("%H:%M")
    return message, f"{date_text} at {time_text}"


def _day_label(day: datetime, today: datetime) -> str:
    if day.date() == today.date():
        return "TODAY"
    if day.date() == (today + timedelta(days=1)).date():
        return "TOMORROW"
    return day.strftime("%a")


def _upcoming_date_label(start: datetime, now: datetime) -> str:
    if start.date() == now.date():
        return "Today"
    if start.date() == (now + timedelta(days=1)).date():
        return "Tomorrow"
    return start.strftime("%a %d.%m")


def _event_line(event: dict[str, Any], include_day: bool, today: datetime) -> str:
    start = _event_start(event)
    title = _event_title(event)
    if not start:
        return title
    if include_day:
        return f"{start.strftime('%a')} {start.strftime('%H:%M')}  {title}"
    return f"{start.strftime('%H:%M')}  {title}"


def _event_key(event: dict[str, Any]) -> tuple[str, str]:
    return (_event_title(event), _event_start_value(event) or "")


def _upcoming_after_hero(events: list[dict[str, Any]], limit: int = 2) -> list[dict[str, Any]]:
    if len(events) <= 1:
        return []

    hero_key = _event_key(events[0])
    upcoming: list[dict[str, Any]] = []
    seen = {hero_key}
    for event in events[1:]:
        key = _event_key(event)
        if key in seen:
            continue
        seen.add(key)
        upcoming.append(event)
        if len(upcoming) == limit:
            break
    return upcoming


def _grouped_event_lines(events: list[dict[str, Any]]) -> list[str]:
    if not events:
        return []

    today = datetime.now(DISPLAY_TZ)
    starts = [_event_start(event) for event in events]
    valid_starts = [start for start in starts if start]
    if not valid_starts:
        return [_event_title(event) for event in events[:2]]

    return [_event_line(event, include_day=True, today=today) for event in events[:2]]


def _state_value(entity: dict[str, Any] | None, suffix: str = "") -> str:
    if not entity:
        return "--"
    value = entity.get("state")
    if value in (None, "", "unknown", "unavailable"):
        return "--"
    return f"{value}{suffix}"


def _format_number(value: str) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)

    if number.is_integer():
        return str(int(number))
    return f"{number:.1f}"


def _float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sample_points(values: list[float], limit: int) -> list[float]:
    if len(values) <= limit:
        return values
    if limit <= 1:
        return values[-limit:]

    step = (len(values) - 1) / (limit - 1)
    return [values[round(index * step)] for index in range(limit)]


def _streak_direction(values: list[float], threshold: float = 0.05) -> tuple[str, int]:
    if len(values) < 2:
        return "", 0

    direction = ""
    count = 0
    for previous, current in zip(reversed(values[:-1]), reversed(values[1:])):
        delta = current - previous
        step_direction = "up" if delta > threshold else "down" if delta < -threshold else "flat"
        if step_direction == "flat":
            break
        if not direction:
            direction = step_direction
            count = 1
            continue
        if step_direction != direction:
            break
        count += 1
    return direction, count


def _finance_highlight(values: list[float] | None) -> str:
    if not values or len(values) < 2:
        return ""

    start = values[0]
    end = values[-1]
    delta = end - start
    abs_delta = abs(delta)
    direction, streak = _streak_direction(values)

    if streak >= 3 and direction == "down":
        return f"down {streak} days"
    if streak >= 3 and direction == "up":
        return f"up {streak} days"
    if abs_delta >= 1:
        verb = "up" if delta > 0 else "down"
        return f"{verb} {abs_delta:.1f} this week"
    if abs_delta >= 0.3:
        verb = "edging up" if delta > 0 else "edging down"
        return verb
    return "mostly flat"


def _format_temp(value: Any, suffix: str = "°") -> str:
    if value in (None, "", "unknown", "unavailable"):
        return "--"
    return f"{_format_number(str(value))}{suffix}"


def _numeric_state(entity: dict[str, Any] | None, suffix: str = "") -> str:
    if not entity:
        return "--"
    value = entity.get("state")
    if value in (None, "", "unknown", "unavailable"):
        return "--"
    return f"{_format_number(str(value))}{suffix}"


def _condition_info(condition: Any) -> tuple[str, str]:
    raw = str(condition or "").strip()
    key = raw.lower().replace("_", "").replace("-", "")
    if raw == "-ra":
        key = "-ra"
    ascii_label, label = CONDITION_MAP.get(key, ("WX", raw.lower() or "weather"))
    return ascii_label, label


def _is_rain(condition: Any) -> bool:
    ascii_label, label = _condition_info(condition)
    return ascii_label == "RAIN" or "rain" in label


def _dominant_condition(conditions: list[Any]) -> Any:
    counts: dict[str, int] = {}
    originals: dict[str, Any] = {}
    for condition in conditions:
        if not condition:
            continue
        key = str(condition).lower()
        counts[key] = counts.get(key, 0) + 1
        originals[key] = condition
    if not counts:
        return None
    dominant = max(counts, key=counts.get)
    return originals[dominant]


def _hourly_records(hourly: Any) -> list[dict[str, Any]]:
    now = datetime.now(DISPLAY_TZ)
    records: list[dict[str, Any]] = []
    if isinstance(hourly, list):
        for item in hourly:
            if not isinstance(item, dict):
                continue
            timestamp = _local_datetime(item.get("datetime"))
            if not timestamp or timestamp < now - timedelta(minutes=30):
                continue
            record = dict(item)
            record["timestamp"] = timestamp
            records.append(record)
    records.sort(key=lambda item: item["timestamp"])
    return records


def _temp_range(records: list[dict[str, Any]]) -> str:
    temps = [
        temp
        for temp in (_float_or_none(item.get("native_temperature")) for item in records)
        if temp is not None
    ]
    if not temps:
        return "--"
    low = int(round(min(temps)))
    high = int(round(max(temps)))
    if low == high:
        return f"{low}°"
    return f"{low}–{high}°"


def _rain_window(records: list[dict[str, Any]]) -> str:
    rainy = [item["timestamp"] for item in records if _is_rain(item.get("condition"))]
    if not rainy:
        return ""
    start = rainy[0]
    end = rainy[-1] + timedelta(hours=1)
    if start.date() == end.date():
        return f"rain {start.strftime('%H')}–{end.strftime('%H')}"
    return f"rain from {start.strftime('%H')}"


def _weather_summary(label: str, records: list[dict[str, Any]]) -> dict[str, str]:
    conditions = [item.get("condition") for item in records if item.get("condition")]
    rainy = any(_is_rain(condition) for condition in conditions)
    dominant = "rain" if rainy else _dominant_condition(conditions)
    ascii_label, condition_label = _condition_info(dominant)
    return {
        "label": label,
        "temp_range": _temp_range(records),
        "ascii": ascii_label,
        "condition": condition_label,
        "has_data": bool(records),
    }


def summarize_hourly_forecast(hourly: Any) -> list[dict[str, str]]:
    now = datetime.now(DISPLAY_TZ)
    today = now.date()
    records = _hourly_records(hourly)

    summary: list[dict[str, str]] = []
    for offset in range(3):
        day = today + timedelta(days=offset)
        day_records = [item for item in records if item["timestamp"].date() == day]
        if not day_records:
            continue
        label = "Today" if offset == 0 else day.strftime("%a")
        summary.append(_weather_summary(label, day_records))

    return summary


def parse_weather(weather: dict[str, Any] | None) -> dict[str, Any]:
    if not weather:
        return {
            "ascii": "WX",
            "temperature": "--",
            "summary": "weather unavailable",
            "forecast": summarize_hourly_forecast(None),
        }

    attrs = weather.get("attributes") or {}
    condition = attrs.get("yandex_condition") or weather.get("state")
    ascii_label, label = _condition_info(condition)
    temperature = _format_temp(attrs.get("temperature"), "°C")
    hourly = attrs.get("forecastHourly")
    hourly_records = _hourly_records(hourly)
    hourly_summary = summarize_hourly_forecast(hourly)

    rain_window = _rain_window(hourly_records[:18])
    if rain_window:
        summary = rain_window
    elif label == "clear":
        summary = "clear now"
    else:
        summary = f"{label} now"

    return {
        "ascii": ascii_label,
        "temperature": temperature,
        "feels_like": _format_temp(attrs.get("feels_like"), "°C"),
        "wind": _format_temp(attrs.get("wind_speed"), f" {attrs.get('wind_speed_unit', '')}".rstrip()),
        "summary": summary,
        "forecast": hourly_summary,
    }


def _humidity_text(entity: dict[str, Any] | None) -> str:
    if not entity:
        return "-- humidity"
    value = _float_or_none(entity.get("state"))
    if value is None:
        return "-- humidity"
    return f"{int(round(value))}% humidity"


def _draw_sparkline(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    height: int,
    values: list[float],
) -> None:
    if len(values) < 2:
        return

    low = min(values)
    high = max(values)
    span = high - low
    if span < 0.05:
        center_y = y + height // 2
        draw.line((x, center_y, x + width, center_y), fill=0, width=1)
        return

    points: list[tuple[int, int]] = []
    for index, value in enumerate(values):
        px = x + round(index * width / (len(values) - 1))
        normalized = (value - low) / span
        py = y + height - round(normalized * height)
        points.append((px, py))

    draw.line(points, fill=0, width=1)
    draw.ellipse((points[-1][0] - 1, points[-1][1] - 1, points[-1][0] + 1, points[-1][1] + 1), fill=0)


def _draw_climate(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    title: str,
    temperature: str,
    humidity: str,
    history: list[float] | None = None,
) -> None:
    draw.text((x, y), title.upper(), fill=0, font=FONTS["label"])
    draw.text((x, y + 30), _truncate(draw, temperature, FONTS["metric"], width), fill=0, font=FONTS["metric"])
    if history:
        _draw_sparkline(draw, x + 126, y + 43, 72, 20, history)
    draw.text((x + 2, y + 83), _truncate(draw, humidity, FONTS["humidity"], width), fill=0, font=FONTS["humidity"])


def _draw_finance(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    value: str,
    history: list[float] | None = None,
) -> None:
    draw.text((x, y), "USD/RUB", fill=0, font=FONTS["label"])
    draw.text((x, y + 32), _truncate(draw, value, FONTS["finance"], width), fill=0, font=FONTS["finance"])
    if history:
        _draw_sparkline(draw, x + 76, y + 42, 64, 20, history)
        highlight = _finance_highlight(history)
        if highlight:
            draw.text((x + 1, y + 82), _truncate(draw, highlight, FONTS["tiny"], width), fill=0, font=FONTS["tiny"])


def _draw_weather(draw: ImageDraw.ImageDraw, x: int, y: int, width: int, weather: dict[str, Any]) -> None:
    draw.text((x, y), "WEATHER", fill=0, font=FONTS["label"])
    draw.text((x, y + 30), _truncate(draw, weather["temperature"], FONTS["weather"], width), fill=0, font=FONTS["weather"])
    draw.text((x + 1, y + 78), _truncate(draw, weather["summary"], FONTS["micro"], width), fill=0, font=FONTS["micro"])

    row_y = y + 104
    temp_x = x + 92
    for index, day in enumerate(weather["forecast"][:3]):
        line_y = row_y + index * 24
        label = day["label"]
        draw.text((x, line_y), _truncate(draw, label, FONTS["small"], 86), fill=0, font=FONTS["small"])
        draw.text((temp_x, line_y), _truncate(draw, day["temp_range"], FONTS["small"], width - (temp_x - x)), fill=0, font=FONTS["small"])


def _draw_upcoming_events(
    draw: ImageDraw.ImageDraw,
    x: int,
    y: int,
    width: int,
    events: list[dict[str, Any]],
) -> None:
    now = datetime.now(DISPLAY_TZ)
    for index, event in enumerate(events[:2]):
        line_y = y + index * 17
        start = _event_start(event)
        title = _event_title(event)
        if not start:
            draw.text((x, line_y), _truncate(draw, title, FONTS["tiny"], width), fill=0, font=FONTS["tiny"])
            continue

        date_text = _upcoming_date_label(start, now)
        time_text = start.strftime("%H:%M")
        time_x = x + 92
        title_x = x + 142
        draw.text((x, line_y), _truncate(draw, date_text, FONTS["tiny"], 84), fill=0, font=FONTS["tiny"])
        draw.text((time_x, line_y), time_text, fill=0, font=FONTS["tiny"])
        draw.text((title_x, line_y), _truncate(draw, title, FONTS["tiny"], width - 142), fill=0, font=FONTS["tiny"])


def _render_dashboard(data: dict[str, Any], errors: list[str]) -> bytes:
    image = Image.new("1", (WIDTH, HEIGHT), 1)
    draw = ImageDraw.Draw(image)

    now = datetime.now(DISPLAY_TZ)
    updated = now.strftime("updated %H:%M")

    draw.text((40, 26), "HOME", fill=0, font=FONTS["tiny"])
    updated_width, _ = _text_bbox(draw, updated, FONTS["tiny"])
    draw.text((WIDTH - 40 - updated_width, 26), updated, fill=0, font=FONTS["tiny"])
    draw.line((40, 60, WIDTH - 40, 60), fill=0, width=1)

    calendar_events = data.get("calendar_events") or []
    event_name, event_time = _calendar_info(calendar_events)
    weather = parse_weather(data.get("weather"))
    upcoming_events = _upcoming_after_hero(calendar_events, limit=2)
    temperature_history = data.get("temperature_history") or {}
    finance_history = data.get("finance_history") or {}

    hero_width = 440
    draw.text((40, 96), _truncate(draw, event_name, FONTS["event"], hero_width), fill=0, font=FONTS["event"])
    draw.text((42, 154), _truncate(draw, event_time, FONTS["body"], hero_width), fill=0, font=FONTS["body"])
    if upcoming_events:
        draw.text((42, 190), "UPCOMING", fill=0, font=FONTS["micro"])
        _draw_upcoming_events(draw, 42, 210, hero_width, upcoming_events)
    _draw_weather(draw, 540, 96, 220, weather)

    draw.line((40, 272, WIDTH - 40, 272), fill=0, width=1)

    col_y = 298
    living_x = 40
    kitchen_x = 310
    finance_x = 592
    climate_width = 220
    finance_width = 168

    _draw_climate(
        draw,
        living_x,
        col_y,
        climate_width,
        "Living Room",
        _numeric_state(data.get("living_temp"), "°C"),
        _humidity_text(data.get("living_humidity")),
        temperature_history.get("living_temp"),
    )
    _draw_climate(
        draw,
        kitchen_x,
        col_y,
        climate_width,
        "Kitchen",
        _numeric_state(data.get("kitchen_temp"), "°C"),
        _humidity_text(data.get("kitchen_humidity")),
        temperature_history.get("kitchen_temp"),
    )
    _draw_finance(
        draw,
        finance_x,
        col_y,
        finance_width,
        _numeric_state(data.get("usd_rub")),
        finance_history.get("usd_rub"),
    )

    draw.line((40, 422, WIDTH - 40, 422), fill=0, width=1)
    quote = _state_value(data.get("quote"))
    if quote == "--":
        quote = "Quote unavailable"
    quote = quote.replace("\n", " ")
    if not quote.endswith("— YE") and not quote.endswith("(c) YE"):
        quote = f"{quote} — YE"
    draw.text((40, 446), _truncate(draw, quote, FONTS["footer"], WIDTH - 80), fill=0, font=FONTS["footer"])

    if DEBUG_MODE and errors:
        draw.text((40, HEIGHT - 18), _truncate(draw, "; ".join(errors), FONTS["tiny"], WIDTH - 80), fill=0, font=FONTS["tiny"])

    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    png = buffer.getvalue()
    OUTPUT_PATH.write_bytes(png)
    return png


def _get_dashboard_png(force: bool = False) -> bytes:
    now = datetime.now().astimezone()
    with _cache_lock:
        if (
            not force
            and _cache["png"] is not None
            and _cache["expires_at"] is not None
            and now < _cache["expires_at"]
        ):
            return _cache["png"]

        data, errors = _fetch_dashboard_data()
        png = _render_dashboard(data, errors)
        _cache["png"] = png
        _cache["rendered_at"] = now
        _cache["expires_at"] = now + timedelta(seconds=CACHE_SECONDS)
        _cache["errors"] = errors
        return png


@app.get("/dashboard.png")
def dashboard_png() -> Response:
    png = _get_dashboard_png()
    return Response(
        content=png,
        media_type="image/png",
        headers={"Cache-Control": f"public, max-age={CACHE_SECONDS}"},
    )


@app.get("/preview")
def preview() -> HTMLResponse:
    html = """
<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>E-Ink Dashboard Preview</title>
    <style>
      html, body {
        height: 100%;
        margin: 0;
        background: #1b1b1b;
        display: grid;
        place-items: center;
      }
      img {
        width: min(800px, calc(100vw - 32px));
        height: auto;
        image-rendering: pixelated;
        box-shadow: 0 24px 80px rgba(0, 0, 0, 0.45);
      }
    </style>
  </head>
  <body>
    <img src="/dashboard.png" width="800" height="480" alt="Dashboard preview">
  </body>
</html>
"""
    return HTMLResponse(html)


@app.get("/health")
def health() -> JSONResponse:
    rendered_at = _cache["rendered_at"]
    payload = {
        "ok": True,
        "configured": bool(HA_URL and HA_TOKEN),
        "ha_url_configured": bool(HA_URL),
        "ha_token_configured": bool(HA_TOKEN),
        "weather_entity": WEATHER_ENTITY,
        "debug_mode": DEBUG_MODE,
        "cache_seconds": CACHE_SECONDS,
        "has_cached_image": _cache["png"] is not None,
        "rendered_at": rendered_at.isoformat() if rendered_at else None,
        "errors": _cache["errors"],
    }
    return JSONResponse(payload)
