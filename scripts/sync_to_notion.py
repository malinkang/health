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
    "heart-rate": "心率", "sleep": "睡眠", "mindfulness": "正念", "blood-pressure": "血压",
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
    values: tuple[tuple[str, str | float | None], ...]
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

def _aggregate_daily(module: str, records: dict[str, Record]) -> dict[str, Record]:
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
        elif module == "sleep":
            metric = f"{str(values.get('Type') or 'Sleep').strip()} (minutes)"
            duration = _duration_minutes(values)
            bucket[metric] = bucket.get(metric, 0.0) + duration
            if str(values.get("Type") or "").strip() in {"Asleep", "Asleep Unspecified", "Core", "Deep", "REM"}:
                bucket["Total Sleep (minutes)"] = bucket.get("Total Sleep (minutes)", 0.0) + duration
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
    for module in DAILY_AGGREGATE_MODULES:
        result[module] = _aggregate_daily(module, result[module])
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
    return None

def expected_value(spec: dict) -> Any:
    if "title" in spec: return title_text(spec)
    if "rich_text" in spec: return title_text(spec)
    if "number" in spec: return spec["number"]
    if "date" in spec: return spec["date"]
    return None

def needs_update(page: dict, expected: dict[str, dict]) -> bool:
    current = page.get("properties", {})
    for name, spec in expected.items():
        a, b = actual_value(current.get(name, {})), expected_value(spec)
        if isinstance(a, dict) and isinstance(b, dict):
            if not iso_equal(a.get("start"), b.get("start")) or not iso_equal(a.get("end"), b.get("end")): return True
        elif not same(a, b): return True
    return False

def rich(value: Any) -> dict: return {"rich_text": [{"text": {"content": str(value)}}]}
def title(value: Any) -> dict: return {"title": [{"text": {"content": str(value)}}]}

WORKOUT_FIELDS = {"Start Date": "开始时间", "End Date": "结束时间", "Duration": "运动时长", "Distance": "距离", "Elevation Ascended": "海拔爬升", "Flights Climbed": "爬楼层数", "Swim Strokes": "游泳划水次数", "Total Energy": "消耗热量", "Type": "Apple Health 类型"}

def identity_name(module: str, schema: dict[str, dict]) -> str:
    if module == "workouts":
        for candidate in ("Id", "id", "UUID"):
            if schema.get(candidate, {}).get("type") == "rich_text": return candidate
        raise NotionError("Keep 运动 database requires a rich-text Id/UUID property")
    return next((n for n, p in schema.items() if p.get("type") == "title"), "Name")

def record_properties(record: Record, schema: dict[str, dict]) -> dict[str, dict]:
    values = record.mapping(); props: dict[str, dict] = {}
    title_name = next((n for n, p in schema.items() if p.get("type") == "title"), "Name")
    if record.module == "workouts":
        props[title_name] = title(values.get("Name") or "Workout")
        props[identity_name(record.module, schema)] = rich("apple-health:" + record.key)
    else: props[title_name] = title(record.key)
    for name, value in values.items():
        target = WORKOUT_FIELDS.get(name, name) if record.module == "workouts" else name
        if value is None or target not in schema: continue
        kind = schema[target].get("type")
        if kind == "number" and isinstance(value, (float, int)): props[target] = {"number": value}
        elif kind == "date": props[target] = {"date": {"start": str(value)}}
        elif kind == "rich_text": props[target] = rich(value)
    if record.module == "workouts" and schema.get("来源", {}).get("type") == "rich_text": props["来源"] = rich("Apple Health / Hadge")
    return props

def schema_for(records: dict[str, Record]) -> dict[str, dict]:
    if records and next(iter(records.values())).module == "workouts":
        return {"Name": {"title": {}}, "Id": {"rich_text": {}}, "开始时间": {"date": {}}, "结束时间": {"date": {}}, "运动时长": {"number": {"format": "number"}}, "距离": {"number": {"format": "number"}}, "海拔爬升": {"number": {"format": "number"}}, "爬楼层数": {"number": {"format": "number"}}, "游泳划水次数": {"number": {"format": "number"}}, "消耗热量": {"number": {"format": "number"}}, "Apple Health 类型": {"number": {"format": "number"}}, "来源": {"rich_text": {}}}
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

def sync(root: Path, client: NotionClient, notion_root: str, dry: bool = False) -> dict[str, int]:
    modules = load_records(root)  # validate every source before any write
    found = find_databases(client, notion_root); counts = {"created": 0, "updated": 0, "skipped": 0}
    for module, records in modules.items():
        if not records: continue
        print(f"Syncing {module}: {len(records)} normalized records", flush=True)
        database_id, schema = ensure_database(client, notion_root, module, records, found, dry)
        pages = client.query(database_id) if database_id else []
        by_key = {}
        id_name = identity_name(module, schema)
        for page in pages:
            identity_prop = page.get("properties", {}).get(id_name, {})
            key = title_text(identity_prop)
            if module == "workouts" and key.startswith("apple-health:"): key = key.removeprefix("apple-health:")
            if key in by_key: raise ValueError(f"Conflicting Notion {module} key {key}")
            by_key[key] = page
        for key, record in records.items():
            props = record_properties(record, schema); old = by_key.get(key)
            if old is None:
                counts["created"] += 1
                if not dry: client.request("POST", "/pages", {"parent": {"database_id": database_id}, "properties": props})
            elif needs_update(old, props):
                counts["updated"] += 1
                if not dry: client.request("PATCH", f"/pages/{old['id']}", {"properties": props})
            else: counts["skipped"] += 1
        print(f"Completed {module}: {json.dumps(counts)}", flush=True)
    return counts

def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--repo", type=Path, default=Path(__file__).parents[1]); parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(); token = os.environ.get("NOTION_TOKEN"); root = os.environ.get("NOTION_ROOT_PAGE_ID")
    if not token or not root: parser.error("NOTION_TOKEN and NOTION_ROOT_PAGE_ID are required")
    print(json.dumps(sync(args.repo, NotionClient(token, min_interval=0.34), root, args.dry_run), ensure_ascii=False))
    return 0

if __name__ == "__main__": raise SystemExit(main())
