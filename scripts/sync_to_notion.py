#!/usr/bin/env python3
"""Idempotently synchronize Hadge CSV exports to Notion Keep databases."""
from __future__ import annotations

import argparse, csv, json, math, os, time, urllib.error, urllib.parse, urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from zoneinfo import ZoneInfo

NOTION_VERSION = "2022-06-28"
RETRYABLE = {408, 409, 429, 500, 502, 503, 504}
UUID_MODULES = ("body", "vitals", "nutrition", "mobility", "sleep", "mindfulness", "blood-pressure")
DATE_MODULES = ("heart-rate",)
LEGACY_MODULES = ("activity", "distances")
ALL_MODULES = LEGACY_MODULES + UUID_MODULES + DATE_MODULES + ("workouts",)
DAILY_AGGREGATE_MODULES = ("body", "vitals", "nutrition", "mobility", "sleep", "mindfulness", "blood-pressure")
LOCAL_TIMEZONE = ZoneInfo("Asia/Shanghai")
DATABASE_TITLES = {
    "activity": "健康日报", "distances": "健康日报", "workouts": "运动",
    "body": "身体测量", "vitals": "生命体征", "nutrition": "营养", "mobility": "活动能力",
    "heart-rate": "心率", "sleep": "睡眠", "sleep-stages": "睡眠分段",
    "mindfulness": "正念", "blood-pressure": "血压",
}
SLEEP_STAGE_DATABASE_TITLES = ("睡眠分段", "睡眠阶段", "睡眠分期", "Sleep Stage", "Sleep Stages")
ASLEEP_TYPES = {"Asleep", "Asleep Unspecified", "Core", "Deep", "REM"}
SLEEP_STAGE_NAMES = {
    "Awake": "清醒", "Core": "浅睡", "Deep": "深睡", "REM": "快速眼动睡眠",
}
SLEEP_STAGE_ALIASES = {
    "清醒": "清醒", "Awake": "清醒",
    "浅睡": "浅睡", "Core": "浅睡",
    "深睡": "深睡", "Deep": "深睡",
    "快速眼动睡眠": "快速眼动睡眠", "REM": "快速眼动睡眠",
}
STANDARD_SLEEP_STAGE_OPTIONS = (
    {"name": "清醒", "color": "red"},
    {"name": "浅睡", "color": "blue"},
    {"name": "深睡", "color": "purple"},
    {"name": "快速眼动睡眠", "color": "green"},
)
SLEEP_IDENTITY_PROPERTY = "Apple Health Key"
SLEEP_INTERNAL_FIELDS = {
    "Name", "Sleep Day", "Sleep Key", "Apple Health Key", "Sleep Start", "Sleep End", "Bed Start", "Bed End",
}
LEGACY_SLEEP_VALUE_FIELDS = {
    "Asleep (minutes)", "Asleep Unspecified (minutes)", "Awake (minutes)",
    "Core (minutes)", "Deep (minutes)", "In Bed (minutes)", "REM (minutes)", "Total Sleep (minutes)",
}

class NotionError(RuntimeError): pass

def parse_time(value: str) -> datetime:
    value = value.strip()
    if value.endswith("Z"): value = value[:-1] + "+00:00"
    result = datetime.fromisoformat(value)
    return result if result.tzinfo else result.replace(tzinfo=timezone.utc)

def iso_equal(a: str | None, b: str | None) -> bool:
    if a is None or b is None: return a == b
    try: return parse_time(a).astimezone(timezone.utc) == parse_time(b).astimezone(timezone.utc)
    except ValueError: return a == b

def scalar(value: str | None) -> str | float | None:
    if value is None or not value.strip(): return None
    try: return float(value)
    except ValueError: return value.strip()

def same(a: Any, b: Any) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
    return a == b

@dataclass(frozen=True)
class Record:
    module: str
    key: str
    values: tuple[tuple[str, Any], ...]
    def mapping(self) -> dict[str, Any]: return dict(self.values)

def _read_files(root: Path, module: str):
    folder = root / module
    for path in sorted(folder.glob("*.csv")):
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for line, row in enumerate(csv.DictReader(handle), 2): yield path, line, row

def _identity(module: str, row: dict[str, str]) -> str:
    if module in UUID_MODULES or module == "workouts":
        return (row.get("UUID") or "").strip()
    return (row.get("Date") or "").strip()

def _metric_name(kind: Any, unit: Any = None) -> str:
    name = str(kind or "Value").strip()
    unit_name = str(unit or "").strip()
    return f"{name} ({unit_name})" if unit_name else name

def _local_day(value: Any) -> str:
    return parse_time(str(value)).astimezone(LOCAL_TIMEZONE).date().isoformat()

def _duration_minutes(values: dict[str, Any]) -> float:
    start = parse_time(str(values["Start Date"]))
    end = parse_time(str(values["End Date"]))
    return max(0.0, (end - start).total_seconds() / 60.0)

def _rounded(value: float) -> float:
    return round(value, 6)

def _notion_date(value: Any) -> str:
    normalized = parse_time(str(value)).replace(second=0, microsecond=0)
    return normalized.isoformat().replace("+00:00", "Z")

def _merge_intervals(intervals: list[tuple[datetime, datetime]]) -> list[tuple[datetime, datetime]]:
    merged: list[list[datetime]] = []
    for start, end in sorted(intervals):
        if end <= start: continue
        if not merged or start > merged[-1][1]: merged.append([start, end])
        elif end > merged[-1][1]: merged[-1][1] = end
    return [(start, end) for start, end in merged]

def _interval_minutes(intervals: list[tuple[datetime, datetime]]) -> float:
    return _rounded(sum((end - start).total_seconds() for start, end in _merge_intervals(intervals)) / 60.0)

def _subtract_intervals(
    interval: tuple[datetime, datetime], blockers: list[tuple[datetime, datetime]]
) -> list[tuple[datetime, datetime]]:
    """Return the parts of an interval not covered by any blocker."""
    remaining = [interval]
    for block_start, block_end in _merge_intervals(blockers):
        next_remaining: list[tuple[datetime, datetime]] = []
        for start, end in remaining:
            if block_end <= start or end <= block_start:
                next_remaining.append((start, end))
                continue
            if start < block_start:
                next_remaining.append((start, min(end, block_start)))
            if block_end < end:
                next_remaining.append((max(start, block_end), end))
        remaining = next_remaining
        if not remaining: break
    return [(start, end) for start, end in remaining if end > start]

def _sleep_session_key(
    sleep_start: datetime,
    sleep_end: datetime,
    bed_start: datetime,
    bed_end: datetime,
) -> str:
    return "|".join(
        _notion_date(value) for value in (sleep_start, sleep_end, bed_start, bed_end)
    )

def _sleep_stage_key(stage: str, start: Any, end: Any) -> str:
    """Use Notion's minute precision for a stable stage identity."""
    return "|".join((_canonical_sleep_stage(stage), _notion_date(start), _notion_date(end)))

def _canonical_sleep_stage(value: Any) -> str:
    return SLEEP_STAGE_ALIASES.get(str(value or "").strip(), str(value or "").strip())

def _sleep_sessions(records: dict[str, Record]) -> list[dict[str, Any]]:
    items = []
    for key, record in records.items():
        values = record.mapping()
        item_type = str(values.get("Type") or "")
        if item_type not in ASLEEP_TYPES | {"Awake", "In Bed"}: continue
        if not values.get("Start Date") or not values.get("End Date"): continue
        start = parse_time(str(values["Start Date"])); end = parse_time(str(values["End Date"]))
        if end <= start: continue
        items.append({"key": key, "type": item_type, "start": start, "end": end, "record": record})

    detailed = [item for item in items if item["type"] in {"Core", "Deep", "REM"}]
    fallback = [item for item in items if item["type"] in {"Asleep", "Asleep Unspecified"}]
    detailed_intervals = [(item["start"], item["end"]) for item in detailed]
    awake_intervals = [
        (item["start"], item["end"]) for item in items if item["type"] == "Awake"
    ]
    fallback_blockers = detailed_intervals + awake_intervals
    retained_fallback = []
    for item in fallback:
        # AutoSleep's broad fallback rows often surround detailed Watch rows.
        # Keep only the uncovered pieces so a tail or nap is not discarded.
        parts = _subtract_intervals((item["start"], item["end"]), fallback_blockers)
        for index, (start, end) in enumerate(parts):
            retained_fallback.append({
                **item,
                "key": f"{item['key']}#fallback-{index}",
                "start": start,
                "end": end,
            })
    candidates = detailed + retained_fallback + [item for item in items if item["type"] == "Awake"]
    stages = sorted(candidates, key=lambda item: (item["start"], item["end"], item["key"]))
    sessions: list[dict[str, Any]] = []
    max_gap_seconds = 180 * 60
    for item in stages:
        if not sessions or (item["start"] - sessions[-1]["end"]).total_seconds() > max_gap_seconds:
            sessions.append({"start": item["start"], "end": item["end"], "items": [item]})
        else:
            sessions[-1]["items"].append(item)
            sessions[-1]["end"] = max(sessions[-1]["end"], item["end"])

    sessions = [session for session in sessions if any(item["type"] in ASLEEP_TYPES for item in session["items"])]
    bed_rows = sorted((item for item in items if item["type"] == "In Bed"), key=lambda item: (item["start"], item["end"]))
    for bed in bed_rows:
        overlapping = [session for session in sessions if bed["start"] < session["end"] and session["start"] < bed["end"]]
        if not overlapping:
            sessions.append({"start": bed["start"], "end": bed["end"], "items": [bed], "bed_start": bed["start"], "bed_end": bed["end"]})
            continue
        overlapping.sort(key=lambda session: session["start"])
        for index, session in enumerate(overlapping):
            session["items"].append(bed)
            candidate_start = bed["start"] if index == 0 else session["start"]
            candidate_end = bed["end"] if index == len(overlapping) - 1 else session["end"]
            session["bed_start"] = min(session.get("bed_start", session["start"]), candidate_start)
            session["bed_end"] = max(session.get("bed_end", session["end"]), candidate_end)

    for session in sessions:
        session.setdefault("bed_start", session["start"]); session.setdefault("bed_end", session["end"])
    return sorted(sessions, key=lambda session: (session["start"], session["end"]))

def _sleep_records(records: dict[str, Record]) -> tuple[dict[str, Record], dict[str, Record]]:
    sleep_records: dict[str, Record] = {}
    stage_records: dict[str, Record] = {}
    for session in _sleep_sessions(records):
        day = session["end"].astimezone(LOCAL_TIMEZONE).date().isoformat()
        typed_intervals: dict[str, list[tuple[datetime, datetime]]] = {}
        for item in session["items"]:
            typed_intervals.setdefault(item["type"], []).append((item["start"], item["end"]))
        detailed_asleep = [interval for name in ("Core", "Deep", "REM") for interval in typed_intervals.get(name, [])]
        fallback_asleep = [interval for name in ("Asleep", "Asleep Unspecified") for interval in typed_intervals.get(name, [])]
        retained_fallback = [
            interval for interval in fallback_asleep
            if not any(interval[0] < detail[1] and detail[0] < interval[1] for detail in detailed_asleep)
        ]
        asleep = detailed_asleep + retained_fallback if detailed_asleep else fallback_asleep
        if not asleep: continue
        sleep_merged = _merge_intervals(asleep)
        sleep_start = min(start for start, _ in sleep_merged); sleep_end = max(end for _, end in sleep_merged)
        bed_start = min(session["bed_start"], sleep_start); bed_end = max(session["bed_end"], sleep_end)
        session_key = f"{_notion_date(sleep_start)}|{_notion_date(sleep_end)}|{_notion_date(bed_start)}|{_notion_date(bed_end)}"
        values: dict[str, Any] = {
            "Name": day, "Sleep Day": day, "Sleep Key": session_key,
            "Apple Health Key": "apple-health:" + session_key,
            "Sleep Start": sleep_start.isoformat(), "Sleep End": sleep_end.isoformat(),
            "Bed Start": bed_start.isoformat(), "Bed End": bed_end.isoformat(),
            "睡眠时长（分钟）": _interval_minutes(asleep),
            "卧床时长（分钟）": _rounded((bed_end - bed_start).total_seconds() / 60.0),
            "清醒次数": len(_merge_intervals(typed_intervals.get("Awake", []))),
        }
        if session_key in sleep_records: raise ValueError(f"Conflicting sleep session key {session_key}")
        sleep_records[session_key] = Record("sleep", session_key, tuple(sorted(values.items())))

        stage_items: dict[str, dict[str, Any]] = {}
        for item in session["items"]:
            stage_name = SLEEP_STAGE_NAMES.get(item["type"])
            if not stage_name: continue
            canonical_key = _sleep_stage_key(stage_name, item["start"], item["end"])
            old = stage_items.get(canonical_key)
            if old is None or item["key"] < old["key"]:
                stage_items[canonical_key] = item
        for canonical_key, item in sorted(stage_items.items()):
            stage_name = SLEEP_STAGE_NAMES[item["type"]]
            local_start = item["start"].astimezone(LOCAL_TIMEZONE); local_end = item["end"].astimezone(LOCAL_TIMEZONE)
            name = f"{day} {stage_name} {local_start:%H:%M}-{local_end:%H:%M}"
            values = {
                "Name": name, "Sleep Day": day, "Sleep Key": session_key, "Stage": stage_name,
                "Start Date": item["start"].isoformat(), "End Date": item["end"].isoformat(),
                "Duration Minutes": _rounded((item["end"] - item["start"]).total_seconds() / 60.0),
            }
            stage_records[item["key"]] = Record("sleep-stages", item["key"], tuple(sorted(values.items())))
    return sleep_records, stage_records

def _aggregate_daily(module: str, records: dict[str, Record]) -> dict[str, Record]:
    if module == "sleep": return _sleep_records(records)[0]
    buckets: dict[str, dict[str, Any]] = {}
    for key, record in records.items():
        values = record.mapping()
        timestamp = values.get("End Date") or values.get("Start Date")
        if not timestamp:
            raise ValueError(f"Missing timestamp for {module} key {key}")
        day = _local_day(timestamp)
        bucket = buckets.setdefault(day, {})

        if module == "body":
            metric = _metric_name(values.get("Type"), values.get("Unit"))
            marker = (parse_time(str(timestamp)), key)
            current = bucket.get(metric)
            if current is None or marker > current[:2]:
                bucket[metric] = (*marker, values.get("Value"))
        elif module in {"vitals", "mobility"}:
            metric = _metric_name(values.get("Type"), values.get("Unit"))
            bucket.setdefault(metric, []).append(float(values["Value"]))
        elif module == "nutrition":
            metric = _metric_name(values.get("Type"), values.get("Unit"))
            bucket[metric] = bucket.get(metric, 0.0) + float(values["Value"])
        elif module == "mindfulness":
            bucket["Mindful Minutes"] = bucket.get("Mindful Minutes", 0.0) + _duration_minutes(values)
        elif module == "blood-pressure":
            unit = values.get("Unit")
            bucket.setdefault(_metric_name("Systolic", unit), []).append(float(values["Systolic"]))
            bucket.setdefault(_metric_name("Diastolic", unit), []).append(float(values["Diastolic"]))

    result: dict[str, Record] = {}
    for day, bucket in sorted(buckets.items()):
        values: dict[str, Any] = {}
        for metric, value in bucket.items():
            if module == "body": value = value[2]
            elif module in {"vitals", "mobility", "blood-pressure"}: value = sum(value) / len(value)
            if isinstance(value, float): value = _rounded(value)
            values[metric] = value
        result[day] = Record(module, day, tuple(sorted(values.items())))
    return result

def load_records(root: Path) -> dict[str, dict[str, Record]]:
    result: dict[str, dict[str, Record]] = {m: {} for m in ALL_MODULES}
    for module in ALL_MODULES:
        target_module = "activity" if module == "distances" else module
        target = result[target_module]
        for path, line, row in _read_files(root, module):
            key = _identity(module, row)
            if not key: continue
            values = {k.strip(): scalar(v) for k, v in row.items() if k and k.strip() not in {"UUID", "Date"} and scalar(v) is not None}
            if module == "distances" and key in target:
                values = {**target[key].mapping(), **values}
            rec = Record(target_module, key, tuple(sorted(values.items())))
            old = target.get(key)
            if old:
                old_values = old.mapping()
                if any(not same(old_values[k], v) for k, v in rec.values if k in old_values):
                    raise ValueError(f"Conflicting {target_module} key {key} at {path}:{line}")
                rec = Record(target_module, key, tuple(sorted({**old_values, **values}.items())))
            target[key] = rec
    result.pop("distances", None)
    _, sleep_stages = _sleep_records(result["sleep"])
    for module in DAILY_AGGREGATE_MODULES:
        result[module] = _aggregate_daily(module, result[module])
    result["sleep-stages"] = sleep_stages
    return result

class NotionClient:
    def __init__(self, token: str, sleep: Callable[[float], None] = time.sleep, opener=urllib.request.urlopen, min_interval: float = 0.0):
        self.token, self.sleep, self.opener, self.min_interval = token, sleep, opener, min_interval
    def request(self, method: str, path: str, payload: dict | None = None) -> dict:
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request("https://api.notion.com/v1" + path, data=data, method=method,
            headers={"Authorization": "Bearer " + self.token, "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"})
        for attempt in range(5):
            if self.min_interval: self.sleep(self.min_interval)
            try:
                with self.opener(req) as response: return json.loads(response.read() or b"{}")
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRYABLE or attempt == 4:
                    raise NotionError(f"Notion API {exc.code}: request failed") from exc
                retry = exc.headers.get("Retry-After") if exc.headers else None
                try: delay = float(retry) if retry else 0.5 * 2**attempt
                except (TypeError, ValueError): delay = 0.5 * 2**attempt
                self.sleep(max(0.0, min(delay, 30.0)))
            except urllib.error.URLError as exc:
                if attempt == 4: raise NotionError("Notion API network request failed") from exc
                self.sleep(min(0.5 * 2**attempt, 8))
        raise AssertionError("unreachable")
    def paged(self, method: str, path: str, payload: dict | None = None) -> list[dict]:
        items, cursor = [], None
        while True:
            page = dict(payload or {})
            if method == "GET":
                suffix = "?page_size=100" + (("&start_cursor=" + urllib.parse.quote(cursor)) if cursor else "")
                response = self.request(method, path + suffix)
            else:
                page["page_size"] = 100
                if cursor: page["start_cursor"] = cursor
                response = self.request(method, path, page)
            items.extend(response.get("results", [])); cursor = response.get("next_cursor")
            if not response.get("has_more") or not cursor: return items
    def children(self, block: str) -> list[dict]: return self.paged("GET", f"/blocks/{block}/children")
    def query(self, database: str) -> list[dict]: return self.paged("POST", f"/databases/{database}/query", {})

def title_text(prop: dict) -> str:
    parts = prop.get("title") or prop.get("rich_text") or []
    return "".join(x.get("plain_text") or x.get("text", {}).get("content", "") for x in parts)

def actual_value(prop: dict) -> Any:
    kind = prop.get("type")
    if kind in {"title", "rich_text"}: return title_text(prop)
    if kind == "number": return prop.get("number")
    if kind == "date": return prop.get("date")
    if kind == "select": return (prop.get("select") or {}).get("name")
    if kind == "relation": return tuple(sorted(item.get("id") for item in prop.get("relation", []) if item.get("id")))
    return None

def expected_value(spec: dict) -> Any:
    if "title" in spec: return title_text(spec)
    if "rich_text" in spec: return title_text(spec)
    if "number" in spec: return spec["number"]
    if "date" in spec: return spec["date"]
    if "select" in spec: return (spec.get("select") or {}).get("name")
    if "relation" in spec: return tuple(sorted(item.get("id") for item in spec.get("relation", []) if item.get("id")))
    return None

def mismatched_properties(page: dict, expected: dict[str, dict]) -> list[str]:
    current = page.get("properties", {})
    mismatches = []
    for name, spec in expected.items():
        a, b = actual_value(current.get(name, {})), expected_value(spec)
        if isinstance(a, dict) and isinstance(b, dict):
            if not iso_equal(a.get("start"), b.get("start")) or not iso_equal(a.get("end"), b.get("end")): mismatches.append(name)
        elif not same(a, b): mismatches.append(name)
    return mismatches

def needs_update(page: dict, expected: dict[str, dict]) -> bool:
    return bool(mismatched_properties(page, expected))

def rich(value: Any) -> dict: return {"rich_text": [{"text": {"content": str(value)}}]}
def title(value: Any) -> dict: return {"title": [{"text": {"content": str(value)}}]}
def date_range(start: Any, end: Any | None = None) -> dict:
    value = {"start": _notion_date(start)}
    if end is not None: value["end"] = _notion_date(end)
    return {"date": value}

WORKOUT_FIELDS = {"Start Date": "开始时间", "End Date": "结束时间", "Duration": "运动时长", "Distance": "距离", "Elevation Ascended": "海拔爬升", "Flights Climbed": "爬楼层数", "Swim Strokes": "游泳划水次数", "Total Energy": "消耗热量", "Type": "Apple Health 类型"}

def identity_name(module: str, schema: dict[str, dict]) -> str:
    if module in {"workouts", "sleep", "sleep-stages"}:
        if module == "workouts": candidates = ("Id", "id", "UUID")
        elif module == "sleep": candidates = (SLEEP_IDENTITY_PROPERTY, "Apple Health Id", "Sleep Key", "Id", "UUID")
        else: candidates = ("Apple Health Id", "Id", "UUID")
        for candidate in candidates:
            if schema.get(candidate, {}).get("type") == "rich_text": return candidate
        if module != "sleep":
            raise NotionError(f"{DATABASE_TITLES[module]} database requires a rich-text identity property")
    return next((n for n, p in schema.items() if p.get("type") == "title"), "Name")

def record_properties(record: Record, schema: dict[str, dict], relation_ids: dict[str, str] | None = None) -> dict[str, dict]:
    values = record.mapping(); props: dict[str, dict] = {}
    title_name = next((n for n, p in schema.items() if p.get("type") == "title"), "Name")
    if record.module == "workouts":
        props[title_name] = title(values.get("Name") or "Workout")
        props[identity_name(record.module, schema)] = rich("apple-health:" + record.key)
    elif record.module == "sleep":
        props[title_name] = title(values.get("Sleep Day") or record.key)
        identity = identity_name(record.module, schema)
        if schema.get(identity, {}).get("type") == "rich_text":
            props[identity] = rich(values.get("Apple Health Key") or "apple-health:" + record.key)
    else: props[title_name] = title(record.key)
    if record.module == "sleep":
        if schema.get("日期", {}).get("type") == "date": props["日期"] = {"date": {"start": values.get("Sleep Day") or record.key}}
        if schema.get("睡眠时间", {}).get("type") == "date": props["睡眠时间"] = date_range(values["Sleep Start"], values["Sleep End"])
        if schema.get("卧床时间", {}).get("type") == "date": props["卧床时间"] = date_range(values["Bed Start"], values["Bed End"])
        day_page_id = (relation_ids or {}).get(values.get("Sleep Day") or record.key)
        if day_page_id and schema.get("日", {}).get("type") == "relation": props["日"] = {"relation": [{"id": day_page_id}]}
    for name, value in values.items():
        if name in SLEEP_INTERNAL_FIELDS: continue
        target = WORKOUT_FIELDS.get(name, name) if record.module == "workouts" else name
        if value is None or target not in schema: continue
        kind = schema[target].get("type")
        if kind == "number" and isinstance(value, (float, int)): props[target] = {"number": value}
        elif kind == "date": props[target] = {"date": {"start": _notion_date(value)}}
        elif kind == "rich_text": props[target] = rich(value)
    if record.module == "workouts" and schema.get("来源", {}).get("type") == "rich_text": props["来源"] = rich("Apple Health / Hadge")
    return props

def schema_for(records: dict[str, Record]) -> dict[str, dict]:
    module = next(iter(records.values())).module if records else ""
    if module == "workouts":
        return {"Name": {"title": {}}, "Id": {"rich_text": {}}, "开始时间": {"date": {}}, "结束时间": {"date": {}}, "运动时长": {"number": {"format": "number"}}, "距离": {"number": {"format": "number"}}, "海拔爬升": {"number": {"format": "number"}}, "爬楼层数": {"number": {"format": "number"}}, "游泳划水次数": {"number": {"format": "number"}}, "消耗热量": {"number": {"format": "number"}}, "Apple Health 类型": {"number": {"format": "number"}}, "来源": {"rich_text": {}}}
    if module == "sleep":
        schema = {
            "Name": {"title": {}}, SLEEP_IDENTITY_PROPERTY: {"rich_text": {}}, "日期": {"date": {}},
            "睡眠时间": {"date": {}}, "卧床时间": {"date": {}},
            "睡眠时长（分钟）": {"number": {"format": "number"}}, "卧床时长（分钟）": {"number": {"format": "number"}},
            "清醒次数": {"number": {"format": "number"}},
        }
        for record in records.values():
            for name, value in record.values:
                if name not in SLEEP_INTERNAL_FIELDS and isinstance(value, (int, float)) and name not in schema:
                    schema[name] = {"number": {"format": "number"}}
        return schema
    if module == "sleep-stages":
        return {
            "Name": {"title": {}}, "Apple Health Id": {"rich_text": {}}, "日期": {"date": {}},
            "睡眠阶段": {"select": {"options": list(STANDARD_SLEEP_STAGE_OPTIONS)}},
            "阶段时间": {"date": {}}, "阶段时长（分钟）": {"number": {"format": "number"}},
        }
    schema: dict[str, dict] = {"Name": {"title": {}}}
    for record in records.values():
        for name, value in record.values:
            if name == "Name": continue
            if name in {"Start Date", "End Date"}: schema[name] = {"date": {}}
            elif isinstance(value, (float, int)): schema[name] = {"number": {"format": "number"}}
            else: schema[name] = {"rich_text": {}}
    return schema

def find_databases(client: NotionClient, root: str) -> dict[str, str]:
    found, queue, visited = {}, [root], set()
    while queue:
        parent = queue.pop(0)
        if parent in visited: continue
        visited.add(parent)
        for block in client.children(parent):
            if block.get("type") == "child_database": found[block.get("child_database", {}).get("title", "")] = block["id"]
            elif block.get("type") == "child_page": queue.append(block["id"])
    return found

def _normalized_id(value: Any) -> str:
    return str(value or "").replace("-", "").lower()

def relation_database_id(prop: dict) -> str | None:
    relation = prop.get("relation") or {}
    return relation.get("database_id") or relation.get("data_source_id")

def relation_property_name(schema: dict[str, dict], target_database_id: str) -> str | None:
    target = _normalized_id(target_database_id)
    for name, prop in schema.items():
        if prop.get("type") == "relation" and _normalized_id(relation_database_id(prop)) == target:
            return name
    return None

def _date_range_from_page(page: dict, names: tuple[str, ...]) -> tuple[str, str] | None:
    properties = page.get("properties", {})
    candidates = [properties.get(name, {}) for name in names]
    candidates.extend(prop for name, prop in properties.items() if name not in names and prop.get("type") == "date")
    for prop in candidates:
        value = prop.get("date") or {}
        if value.get("start") and value.get("end"):
            return value["start"], value["end"]
    return None

def _page_sleep_key(page: dict, schema: dict[str, dict]) -> str | None:
    """Extract a stable identity from an Apple Health key or native ranges."""
    identity_name_value = next(
        (name for name in (SLEEP_IDENTITY_PROPERTY, "Apple Health Id", "Sleep Key", "Id", "UUID")
         if schema.get(name, {}).get("type") == "rich_text"),
        None,
    )
    if identity_name_value:
        identity = title_text(page.get("properties", {}).get(identity_name_value, {})).strip()
        if identity:
            return identity.removeprefix("apple-health:")
    sleep_range = _date_range_from_page(page, ("睡眠时间", "Sleep Time"))
    bed_range = _date_range_from_page(page, ("卧床时间", "Bed Time"))
    if sleep_range and bed_range:
        return _sleep_session_key(
            parse_time(sleep_range[0]), parse_time(sleep_range[1]),
            parse_time(bed_range[0]), parse_time(bed_range[1]),
        )
    return None

def _page_sleep_stage_key(page: dict, schema: dict[str, dict]) -> str | None:
    properties = page.get("properties", {})
    stage = None
    for name in ("睡眠阶段", "Stage", "阶段"):
        prop = properties.get(name, {})
        if prop.get("type") == "select":
            stage = (prop.get("select") or {}).get("name")
            if stage: break
    if not stage:
        return None
    date_range = _date_range_from_page(page, ("阶段时间", "Stage Time", "睡眠阶段时间"))
    if not date_range: return None
    return _sleep_stage_key(stage, date_range[0], date_range[1])

def _merge_sleep_stage_options(existing: dict) -> tuple[list[dict], bool]:
    select = existing.get("select") or {}
    current = list(select.get("options") or [])
    names = {str(option.get("name") or "") for option in current}
    merged = list(current)
    for option in STANDARD_SLEEP_STAGE_OPTIONS:
        if option["name"] not in names:
            merged.append(dict(option)); names.add(option["name"])
    return merged, len(merged) != len(current)

def related_day_page_ids(client: NotionClient, sleep_schema: dict[str, dict]) -> dict[str, str]:
    day_prop = sleep_schema.get("日", {})
    target = relation_database_id(day_prop) if day_prop.get("type") == "relation" else None
    if not target: return {}
    result: dict[str, str] = {}
    for page in client.query(target):
        properties = page.get("properties", {})
        day = None
        for prop in properties.values():
            if prop.get("type") == "date" and (prop.get("date") or {}).get("start"):
                day = prop["date"]["start"][:10]; break
        if not day:
            title_prop = next((prop for prop in properties.values() if prop.get("type") == "title"), {})
            value = title_text(title_prop)
            if len(value) >= 11 and "年" in value and "月" in value and "日" in value:
                try: day = datetime.strptime(value[:11], "%Y年%m月%d日").date().isoformat()
                except ValueError: pass
        if day: result[day] = page["id"]
    return result

def ensure_sleep_stage_database(
    client: NotionClient,
    root: str,
    records: dict[str, Record],
    found: dict[str, str],
    sleep_database_id: str,
    dry: bool,
) -> tuple[str | None, dict[str, dict], str]:
    database_id = next((found.get(name) for name in SLEEP_STAGE_DATABASE_TITLES if found.get(name)), None)
    desired = schema_for(records)
    relation_spec = {"relation": {"database_id": sleep_database_id, "single_property": {}}}
    if not database_id:
        desired_with_relation = {**desired, "睡眠记录": relation_spec}
        if dry:
            return None, {name: {"type": next(iter(spec)), **spec} for name, spec in desired_with_relation.items()}, "睡眠记录"
        created = client.request("POST", "/databases", {
            "parent": {"type": "page_id", "page_id": root},
            "title": [{"text": {"content": DATABASE_TITLES["sleep-stages"]}}],
            "properties": desired_with_relation,
        })
        database_id = created["id"]; found[DATABASE_TITLES["sleep-stages"]] = database_id

    db = client.request("GET", f"/databases/{database_id}")
    existing = db.get("properties", {})
    if any(prop.get("type") == "title" for prop in existing.values()):
        desired = {name: spec for name, spec in desired.items() if "title" not in spec}
    for name, spec in desired.items():
        if name in existing and existing[name].get("type") != next(iter(spec)):
            raise NotionError(f"Database {DATABASE_TITLES['sleep-stages']} property {name} has incompatible type")
    missing = {name: spec for name, spec in desired.items() if name not in existing}
    relation_name = relation_property_name(existing, sleep_database_id)
    if not relation_name:
        relation_name = "睡眠记录"
        if relation_name in existing and existing[relation_name].get("type") != "relation":
            raise NotionError("Database 睡眠分段 property 睡眠记录 has incompatible type")
        if relation_name not in existing: missing[relation_name] = relation_spec
    if missing:
        if not dry:
            db = client.request("PATCH", f"/databases/{database_id}", {"properties": missing})
            existing = db.get("properties", existing)
        else:
            existing = {**existing, **{name: {"type": next(iter(spec)), **spec} for name, spec in missing.items()}}
    stage_prop = existing.get("睡眠阶段", {})
    if stage_prop.get("type") == "select":
        options, changed = _merge_sleep_stage_options(stage_prop)
        if changed:
            patch = {"properties": {"睡眠阶段": {"select": {"options": options}}}}
            if not dry:
                db = client.request("PATCH", f"/databases/{database_id}", patch)
                existing = {**existing, **db.get("properties", {})}
                returned_stage = existing.get("睡眠阶段", stage_prop)
                existing["睡眠阶段"] = {
                    **returned_stage,
                    "type": "select",
                    "select": {**(returned_stage.get("select") or {}), "options": options},
                }
            else:
                existing = {**existing, "睡眠阶段": {**stage_prop, "select": {**(stage_prop.get("select") or {}), "options": options}}}
    return database_id, existing, relation_name

def sleep_stage_properties(
    record: Record,
    schema: dict[str, dict],
    sleep_page_id: str | None,
    relation_name: str,
) -> dict[str, dict]:
    values = record.mapping()
    title_name = next((name for name, prop in schema.items() if prop.get("type") == "title"), "Name")
    identity = identity_name("sleep-stages", schema)
    props = {
        title_name: title(values["Name"]),
        identity: rich("apple-health:" + record.key),
    }
    if schema.get("日期", {}).get("type") == "date": props["日期"] = {"date": {"start": values["Sleep Day"]}}
    if schema.get("睡眠阶段", {}).get("type") == "select":
        stage_name = _canonical_sleep_stage(values["Stage"])
        options = {
            str(option.get("name")) for option in (schema["睡眠阶段"].get("select") or {}).get("options", [])
        }
        # Legacy Keep databases sometimes only have a REM option. The schema
        # migration adds the canonical option, but preserve a legacy alias if
        # an API response has not reflected the patch yet.
        if options and stage_name not in options:
            aliases = {"快速眼动睡眠": "REM", "浅睡": "Core", "深睡": "Deep", "清醒": "Awake"}
            stage_name = aliases.get(stage_name, stage_name)
        props["睡眠阶段"] = {"select": {"name": stage_name}}
    if schema.get("阶段时间", {}).get("type") == "date": props["阶段时间"] = date_range(values["Start Date"], values["End Date"])
    if schema.get("阶段时长（分钟）", {}).get("type") == "number": props["阶段时长（分钟）"] = {"number": values["Duration Minutes"]}
    if sleep_page_id and schema.get(relation_name, {}).get("type") == "relation":
        props[relation_name] = {"relation": [{"id": sleep_page_id}]}
    return props

def is_stale_legacy_sleep_page(page: dict) -> bool:
    properties = page.get("properties", {})
    has_legacy_value = any((properties.get(name) or {}).get("number") is not None for name in LEGACY_SLEEP_VALUE_FIELDS)
    has_native_value = any((properties.get(name) or {}).get("number") is not None for name in ("睡眠时长（分钟）", "卧床时长（分钟）"))
    return has_legacy_value and not has_native_value

def ensure_database(client: NotionClient, root: str, module: str, records: dict[str, Record], found: dict[str, str], dry: bool) -> tuple[str | None, dict]:
    db_title = DATABASE_TITLES[module]
    database_id = found.get(db_title)
    if not database_id:
        if dry: return None, {k: {"type": next(iter(v))} for k, v in schema_for(records).items()}
        created = client.request("POST", "/databases", {"parent": {"type": "page_id", "page_id": root}, "title": [{"text": {"content": db_title}}], "properties": schema_for(records)})
        database_id = created["id"]; found[db_title] = database_id
    db = client.request("GET", f"/databases/{database_id}")
    existing = db.get("properties", {}); desired = schema_for(records)
    if any(prop.get("type") == "title" for prop in existing.values()):
        desired = {name: spec for name, spec in desired.items() if "title" not in spec}
    for name, spec in desired.items():
        if name in existing and existing[name].get("type") != next(iter(spec)):
            if not (module == "workouts" and name == "运动时长" and existing[name].get("type") == "formula"):
                raise NotionError(f"Database {db_title} property {name} has incompatible type")
    missing = {name: spec for name, spec in desired.items() if name not in existing}
    if missing:
        if not dry:
            db = client.request("PATCH", f"/databases/{database_id}", {"properties": missing})
            returned = db.get("properties", {})
            existing = {**existing, **{n: {"type": next(iter(s))} for n, s in missing.items()}, **returned}
        else: existing = {**existing, **{n: {"type": next(iter(s))} for n, s in missing.items()}}
    return database_id, existing

def sync(
    root: Path,
    client: NotionClient,
    notion_root: str,
    dry: bool = False,
    selected_modules: set[str] | None = None,
) -> dict[str, int]:
    modules = load_records(root)  # validate every source before any write
    if selected_modules:
        unknown = selected_modules - set(modules)
        if unknown: raise ValueError(f"Unknown modules: {', '.join(sorted(unknown))}")
    found = find_databases(client, notion_root); counts = {"created": 0, "updated": 0, "skipped": 0, "archived": 0}
    session_page_ids: dict[str, str] = {}
    for module, records in modules.items():
        if selected_modules and module not in selected_modules: continue
        if not records: continue
        print(f"Syncing {module}: {len(records)} normalized records", flush=True)
        mismatch_counts: dict[str, int] = {}
        if module == "sleep-stages":
            sleep_database_id = found.get(DATABASE_TITLES["sleep"])
            if not sleep_database_id: raise NotionError("Sleep stage synchronization requires the 睡眠 database")
            sleep_db = client.request("GET", f"/databases/{sleep_database_id}")
            sleep_schema = sleep_db.get("properties", {})
            sleep_pages: dict[str, str] = {}
            for page in client.query(sleep_database_id):
                sleep_key = _page_sleep_key(page, sleep_schema)
                if not sleep_key: continue
                if sleep_key in sleep_pages and sleep_pages[sleep_key] != page["id"]:
                    raise ValueError(f"Conflicting Notion sleep key {sleep_key}")
                sleep_pages[sleep_key] = page["id"]
            sleep_pages.update(session_page_ids)
            database_id, schema, relation_name = ensure_sleep_stage_database(
                client, notion_root, records, found, sleep_database_id, dry
            )
            pages = client.query(database_id) if database_id else []
            id_name = identity_name(module, schema)
            by_key: dict[str, dict] = {}
            by_interval: dict[str, dict] = {}
            for page in pages:
                key = title_text(page.get("properties", {}).get(id_name, {}))
                if key.startswith("apple-health:"): key = key.removeprefix("apple-health:")
                if key:
                    if key in by_key: raise ValueError(f"Conflicting Notion {module} key {key}")
                    by_key[key] = page
                interval_key = _page_sleep_stage_key(page, schema)
                if interval_key:
                    if interval_key in by_interval and by_interval[interval_key]["id"] != page["id"]:
                        raise ValueError(f"Conflicting Notion {module} interval {interval_key}")
                    by_interval[interval_key] = page
            for key, record in records.items():
                values = record.mapping()
                props = sleep_stage_properties(record, schema, sleep_pages.get(values["Sleep Key"]), relation_name)
                interval_key = _sleep_stage_key(values["Stage"], values["Start Date"], values["End Date"])
                old = by_key.get(key) or by_interval.get(interval_key)
                if old is None:
                    counts["created"] += 1
                    if not dry: client.request("POST", "/pages", {"parent": {"database_id": database_id}, "properties": props})
                elif mismatches := mismatched_properties(old, props):
                    counts["updated"] += 1
                    for name in mismatches: mismatch_counts[name] = mismatch_counts.get(name, 0) + 1
                    if not dry: client.request("PATCH", f"/pages/{old['id']}", {"properties": props})
                else: counts["skipped"] += 1
            if mismatch_counts: print(f"Mismatched {module}: {json.dumps(mismatch_counts, ensure_ascii=False, sort_keys=True)}", flush=True)
            print(f"Completed {module}: {json.dumps(counts)}", flush=True)
            continue

        database_id, schema = ensure_database(client, notion_root, module, records, found, dry)
        pages = client.query(database_id) if database_id else []
        day_page_ids = related_day_page_ids(client, schema) if module == "sleep" and database_id else {}
        by_key: dict[str, dict] = {}
        by_sleep_key: dict[str, dict] = {}
        id_name = identity_name(module, schema)
        for page in pages:
            identity_prop = page.get("properties", {}).get(id_name, {})
            key = title_text(identity_prop)
            if key.startswith("apple-health:"): key = key.removeprefix("apple-health:")
            if key:
                if key in by_key: raise ValueError(f"Conflicting Notion {module} key {key}")
                by_key[key] = page
            if module == "sleep":
                sleep_key = _page_sleep_key(page, schema)
                if sleep_key:
                    if sleep_key in by_sleep_key and by_sleep_key[sleep_key]["id"] != page["id"]:
                        raise ValueError(f"Conflicting Notion sleep key {sleep_key}")
                    by_sleep_key[sleep_key] = page
        if module == "sleep":
            for page in pages:
                sleep_key = _page_sleep_key(page, schema)
                if sleep_key not in records and is_stale_legacy_sleep_page(page):
                    counts["archived"] += 1
                    if not dry: client.request("PATCH", f"/pages/{page['id']}", {"archived": True})
        for key, record in records.items():
            props = record_properties(record, schema, day_page_ids)
            old = by_sleep_key.get(key) if module == "sleep" else by_key.get(key)
            if old is None:
                counts["created"] += 1
                if not dry:
                    created = client.request("POST", "/pages", {"parent": {"database_id": database_id}, "properties": props})
                    if module == "sleep" and created.get("id"): session_page_ids[key] = created["id"]
            elif mismatches := mismatched_properties(old, props):
                counts["updated"] += 1
                for name in mismatches: mismatch_counts[name] = mismatch_counts.get(name, 0) + 1
                if not dry:
                    updated = client.request("PATCH", f"/pages/{old['id']}", {"properties": props})
                    if module == "sleep": session_page_ids[key] = updated.get("id", old["id"])
            else: counts["skipped"] += 1
        if mismatch_counts: print(f"Mismatched {module}: {json.dumps(mismatch_counts, ensure_ascii=False, sort_keys=True)}", flush=True)
        print(f"Completed {module}: {json.dumps(counts)}", flush=True)
    return counts

def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--repo", type=Path, default=Path(__file__).parents[1]); parser.add_argument("--dry-run", action="store_true"); parser.add_argument("--modules", default="")
    args = parser.parse_args(); token = os.environ.get("NOTION_TOKEN"); root = os.environ.get("NOTION_ROOT_PAGE_ID")
    if not token or not root: parser.error("NOTION_TOKEN and NOTION_ROOT_PAGE_ID are required")
    selected = {value.strip() for value in args.modules.split(",") if value.strip()} or None
    print(json.dumps(sync(args.repo, NotionClient(token, min_interval=0.34), root, args.dry_run, selected), ensure_ascii=False))
    return 0

if __name__ == "__main__": raise SystemExit(main())
