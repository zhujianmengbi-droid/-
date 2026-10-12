"""Pure helpers for BuGLand match history, using China server-local dates.

Unknown player statistics remain ``None``. A missing field becomes zero only
after the queried player has been found in that match's team data.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from typing import Any


CHINA_TZ = timezone(timedelta(hours=8), name="Asia/Shanghai")
STAT_FIELDS = {
    "final_kills": ("final_kill", "finalKill", "final_kills", "finalKills"),
    "beds": ("bed_destory", "bed_destroy", "bedDestroy", "beds_destroyed"),
    "final_deaths": ("final_death", "finalDeath", "final_deaths", "finalDeaths"),
}


def parse_time(value: Any) -> datetime | None:
    """Return a China-local aware datetime, or None for an invalid value.

    API date strings without a timezone are server-local China time. Epoch
    numbers are seconds or milliseconds since UTC, as determined by magnitude.
    """
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, date):
        result = datetime.combine(value, datetime.min.time())
    elif value is None or isinstance(value, bool):
        return None
    elif isinstance(value, (int, float)):
        try:
            stamp = float(value)
            if not math.isfinite(stamp):
                return None
            if abs(stamp) >= 100_000_000_000:
                stamp /= 1000.0
            return datetime.fromtimestamp(stamp, timezone.utc).astimezone(CHINA_TZ)
        except (OverflowError, OSError, ValueError):
            return None
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        # ISO 8601 covers the official yyyy-MM-dd HH:mm:ss representation.
        try:
            result = datetime.fromisoformat(text.replace("Z", "+00:00").replace("z", "+00:00"))
        except ValueError:
            try:
                return parse_time(float(text))
            except ValueError:
                return None
    else:
        return None
    if result.tzinfo is None:
        result = result.replace(tzinfo=CHINA_TZ)
    return result.astimezone(CHINA_TZ)


def _record_time(record: dict) -> datetime | None:
    for key in ("date", "end_time", "endTime", "timestamp", "played_at", "created_at", "start_time"):
        value = parse_time(record.get(key))
        if value is not None:
            return value
    return None


def record_key(record: dict) -> str:
    """Use match ID plus normalized match time so pagination can be deduped."""
    match_id = record.get("matchId") or record.get("_id") or record.get("match_id")
    timestamp = _record_time(record)
    when = timestamp.isoformat() if timestamp is not None else str(record.get("date") or "")
    if match_id:
        return f"{match_id}|{when}"
    # Malformed records without an ID should not all collapse into one row.
    identity = {key: record.get(key) for key in ("date", "type", "gamemode", "win", "player_uuid")}
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:20]
    return f"unknown:{digest}|{when}"


def _positive_int(value: Any, default: int) -> int:
    try:
        number = int(value)
        return number if number > 0 else default
    except (ValueError, TypeError, OverflowError):
        return default


def unpack_history(response: Any) -> tuple[list[dict], int, int, str]:
    """Unwrap v2's data.data and the older direct history payload.

    Explicit failed response codes raise ValueError. Unknown page size defaults
    to 10, matching the official example. Callers should use an empty page or
    verified date ordering to end a full-day scan, not this fallback alone.
    """
    if not isinstance(response, dict):
        raise ValueError("对局记录响应格式不正确")
    if "code" in response:
        try:
            code = int(response["code"])
        except (ValueError, TypeError, OverflowError):
            raise ValueError("对局记录返回了无法识别的状态码") from None
        if code != 0 and not 200 <= code < 300:
            message = response.get("message")
            message = message[:240] if isinstance(message, str) else "查询失败"
            raise ValueError(f"{message}（{code}）")
    payload = response.get("data")
    if isinstance(payload, dict):
        metadata = payload
        rows = payload.get("data", payload.get("records", []))
    else:
        metadata = response
        rows = payload if isinstance(payload, list) else response.get("records", [])
    if not isinstance(rows, list):
        raise ValueError("对局列表数据格式不正确")
    records = [dict(row) for row in rows if isinstance(row, dict)]
    page = _positive_int(metadata.get("page", response.get("page")), 1)
    page_size = _positive_int(
        metadata.get("pageSize", metadata.get("page_size", response.get("pageSize"))), 10
    )
    player_uuid = ""
    for container in (metadata, response):
        for key in ("playerUuid", "player_uuid", "uuid"):
            if container.get(key):
                player_uuid = str(container[key]).strip()
                break
        if player_uuid:
            break
    return records, page, page_size, player_uuid


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        number = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return int(number) if number.is_integer() else number


def _stat(player: dict, aliases: tuple[str, ...]) -> int | float | None:
    for key in aliases:
        if key in player:
            number = _number(player[key])
            if number is not None:
                return number
    return None


def _uuid(value: Any) -> str:
    return str(value or "").strip().casefold().replace("-", "").strip("{}")


def enrich_record(record: dict, details: dict, player_name: str = "", player_uuid: str = "") -> dict:
    """Copy a row and enrich its metadata and queried-player statistics."""
    result = dict(record)
    result["_stats_complete"] = False
    for aliases in STAT_FIELDS.values():
        for key in aliases:
            result.pop(key, None)
        result[aliases[0]] = None
    if not isinstance(details, dict):
        return result
    if "teams" not in details and isinstance(details.get("data"), dict):
        details = details["data"]
    for key in ("map", "start_time", "end_time"):
        if details.get(key) is not None:
            result[key] = details[key]
    start, end = parse_time(result.get("start_time")), parse_time(result.get("end_time"))
    if start is not None and end is not None and end >= start:
        seconds = (end - start).total_seconds()
        result["duration"] = int(seconds) if seconds.is_integer() else seconds
    elif _number(details.get("duration")) is not None:
        result["duration"] = _number(details["duration"])
    wanted_uuid = _uuid(player_uuid)
    wanted_name = str(player_name or "").strip().casefold()
    teams = details.get("teams", {})
    groups = teams.values() if isinstance(teams, dict) else teams if isinstance(teams, list) else []
    found = None
    for group in groups:
        if isinstance(group, dict):
            group = group.get("players", [])
        if not isinstance(group, list):
            continue
        for player in group:
            if not isinstance(player, dict):
                continue
            if wanted_uuid:
                matched = _uuid(player.get("player_uuid") or player.get("uuid")) == wanted_uuid
            else:
                matched = bool(wanted_name) and str(
                    player.get("player_name") or player.get("playername") or player.get("name") or ""
                ).strip().casefold() == wanted_name
            if matched:
                found = player
                break
        if found is not None:
            break
    if found is not None:
        result["_stats_complete"] = True
        for aliases in STAT_FIELDS.values():
            # Official match details omit counters whose value is zero.
            present = any(key in found for key in aliases)
            value = _stat(found, aliases)
            result[aliases[0]] = value if present else 0
            if present and value is None:
                result["_stats_complete"] = False
    return result


def _outcome(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in ("true", "1", "win", "won", "胜利"):
            return True
        if text in ("false", "0", "loss", "lose", "lost", "失败"):
            return False
    return None


def day_summary(records: list[dict], day: date | datetime | str | None = None) -> dict:
    """Summarize deduped China-local matches, exposing incomplete statistics.

    ``stats_complete`` is a bool; ``missing_stats`` counts incomplete matches.
    Each statistic is the known partial sum, or None if every match is unknown.
    An empty day has zero totals. ``stat_counts`` gives known counts per metric.
    """
    target = parse_time(day) if day is not None else datetime.now(CHINA_TZ)
    if target is None:
        raise ValueError("统计日期格式不正确")
    target_day = target.date()
    unique = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        when = _record_time(record)
        if when is None or when.date() != target_day:
            continue
        key = record_key(record)
        old = unique.get(key)
        if old is None:
            unique[key] = record
        elif record.get("_stats_complete") or not old.get("_stats_complete"):
            merged = dict(old)
            merged.update({name: value for name, value in record.items() if value is not None})
            unique[key] = merged
    result = {
        "day": target_day.isoformat(), "total": len(unique), "wins": 0, "losses": 0,
        "unknown_outcomes": 0, "final_kills": 0, "beds": 0, "final_deaths": 0,
        "stats_complete": True, "missing_stats": 0, "stat_counts": {key: 0 for key in STAT_FIELDS},
    }
    for record in unique.values():
        won = _outcome(record.get("win"))
        result["wins" if won is True else "losses" if won is False else "unknown_outcomes"] += 1
        complete = bool(record.get("_stats_complete"))
        for output_name, aliases in STAT_FIELDS.items():
            value = _stat(record, aliases)
            if value is not None:
                result[output_name] += value
                result["stat_counts"][output_name] += 1
            else:
                complete = False
        if not complete:
            result["missing_stats"] += 1
    for key in STAT_FIELDS:
        if result["total"] and not result["stat_counts"][key]:
            result[key] = None
    result["stats_complete"] = result["missing_stats"] == 0
    known_outcomes = result["wins"] + result["losses"]
    result["win_rate"] = result["wins"] / known_outcomes * 100 if known_outcomes else None
    return result
