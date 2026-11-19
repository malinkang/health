#!/usr/bin/env python3
"""Synchronize Apple Health (Hadge) water records to Keep and record synced UUIDs."""
from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable
from zoneinfo import ZoneInfo

KEEP_API_BASE = "https://api.gotokeep.com"
KEEP_LOGIN_PATH = "/v1.1/users/login"
KEEP_WATER_ADD_PATH = "/diet-data-webapp/v1/drinking-water/add"
KEEP_WATER_ITEM_PATH = "/diet-data-webapp/v1/drinking-water/item/"
DEFAULT_STATE_FILE = ".sync-state/keep-water.json"
DEFAULT_TIMEZONE = "Asia/Shanghai"
RETRYABLE_STATUS_CODES = {408, 409, 429, 500, 502, 503, 504}


class KeepSyncError(RuntimeError):
    pass


@dataclass(frozen=True)
class WaterRecord:
    uuid: str
    start_date: str
    end_date: str
    drinking_time_ms: int
    local_date: str
    water_ml: int
    raw_value: float
    unit: str
    source: str
    source_file: str


def parse_iso_timestamp(value: str) -> datetime:
    text = (value or "").strip()
    if not text:
        raise ValueError("Empty timestamp")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def normalize_water_ml(raw_value: str | float | int, unit: str) -> tuple[float, int]:
    value = float(raw_value)
    if value <= 0:
        raise ValueError(f"Water amount must be positive, got {raw_value}")
    normalized_unit = (unit or "mL").strip().lower()
    if normalized_unit in {"ml", "毫升"}:
        ml = round(value)
    elif normalized_unit in {"l", "升"}:
        ml = round(value * 1000.0)
    elif normalized_unit in {"fl oz", "fl_oz", "oz"}:
        ml = round(value * 29.5735295625)
    else:
        raise ValueError(f"Unsupported water unit: {unit}")
    if ml <= 0:
        raise ValueError(f"Rounded water amount must be positive, got {ml}")
    return value, int(ml)


def load_water_records(root: Path, tz_name: str = DEFAULT_TIMEZONE) -> list[WaterRecord]:
    nutrition_dir = root / "nutrition"
    if not nutrition_dir.exists():
        return []
    tz = ZoneInfo(tz_name)
    by_uuid: dict[str, WaterRecord] = {}
    for csv_path in sorted(nutrition_dir.glob("*.csv")):
        rel_path = csv_path.relative_to(root).as_posix()
        with csv_path.open(newline="", encoding="utf-8-sig") as handle:
            reader = csv.DictReader(handle)
            for line_no, row in enumerate(reader, 2):
                record_type = (row.get("Type") or "").strip()
                if record_type.lower() != "water":
                    continue
                uuid = (row.get("UUID") or "").strip()
                if not uuid:
                    raise ValueError(f"Missing UUID at {rel_path}:{line_no}")
                start_str = (row.get("Start Date") or "").strip()
                end_str = (row.get("End Date") or "").strip()
                ts_str = start_str or end_str
                if not ts_str:
                    raise ValueError(f"Missing timestamp for UUID {uuid} at {rel_path}:{line_no}")
                dt = parse_iso_timestamp(ts_str)
                drinking_time_ms = int(round(dt.timestamp() * 1000))
                local_date = dt.astimezone(tz).strftime("%Y-%m-%d")
                unit = (row.get("Unit") or "mL").strip()
                raw_val, water_ml = normalize_water_ml(row.get("Value") or "", unit)
                source = (row.get("Source") or "").strip()
                record = WaterRecord(
                    uuid=uuid,
                    start_date=start_str or ts_str,
                    end_date=end_str or ts_str,
                    drinking_time_ms=drinking_time_ms,
                    local_date=local_date,
                    water_ml=water_ml,
                    raw_value=raw_val,
                    unit=unit,
                    source=source,
                    source_file=rel_path,
                )
                existing = by_uuid.get(uuid)
                if existing is not None:
                    if (
                        existing.drinking_time_ms != record.drinking_time_ms
                        or existing.water_ml != record.water_ml
                    ):
                        raise ValueError(
                            f"Conflicting water record for UUID {uuid} at {rel_path}:{line_no}"
                        )
                    continue
                by_uuid[uuid] = record
    return sorted(by_uuid.values(), key=lambda r: (r.drinking_time_ms, r.uuid))


def load_sync_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"version": 1, "records": {}}
    data = json.loads(state_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("records"), dict):
        raise ValueError(f"Invalid sync state format in {state_path}")
    return data


def save_sync_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    ordered_records = dict(sorted(state.get("records", {}).items()))
    payload = {
        "version": 1,
        "records": ordered_records,
    }
    state_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


class KeepWaterClient:
    def __init__(
        self,
        token: str,
        *,
        base_url: str = KEEP_API_BASE,
        tz_name: str = DEFAULT_TIMEZONE,
        sleep: Callable[[float], None] = time.sleep,
        opener: Callable[..., Any] = urllib.request.urlopen,
        min_interval: float = 0.25,
    ):
        cleaned = (token or "").strip()
        if cleaned.lower().startswith("bearer "):
            cleaned = cleaned[7:].strip()
        if not cleaned:
            raise KeepSyncError("Missing Keep authorization token")
        self.token = cleaned
        self.base_url = base_url.rstrip("/")
        self.tz_name = tz_name
        self.sleep = sleep
        self.opener = opener
        self.min_interval = min_interval

    @classmethod
    def login_with_password(
        cls,
        mobile: str,
        password: str,
        *,
        country_code: str = "86",
        base_url: str = KEEP_API_BASE,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> str:
        form = urllib.parse.urlencode(
            {"mobile": mobile, "password": password, "countryCode": country_code}
        ).encode("utf-8")
        req = urllib.request.Request(
            base_url.rstrip("/") + KEEP_LOGIN_PATH,
            data=form,
            method="POST",
            headers={
                "User-Agent": "Keep/9.1.65 (Android 15)",
                "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
            },
        )
        try:
            with opener(req, timeout=20) as resp:
                body = json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            raise KeepSyncError(f"Keep login failed with HTTP {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise KeepSyncError("Keep login network failure") from exc
        token = (body.get("data") or {}).get("token") if isinstance(body, dict) else None
        if not isinstance(token, str) or not token.strip():
            raise KeepSyncError("Keep login response missing token")
        return token.strip()

    def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": f"Bearer {self.token}",
            "User-Agent": "Keep/9.1.65 (Android 15)",
            "x-keep-timezone": self.tz_name,
        }
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=UTF-8"
        req = urllib.request.Request(
            self.base_url + path,
            data=data,
            method=method,
            headers=headers,
        )
        for attempt in range(4):
            if self.min_interval > 0:
                self.sleep(self.min_interval)
            try:
                with self.opener(req, timeout=20) as resp:
                    raw = resp.read().decode("utf-8") or "{}"
                    result = json.loads(raw)
                    if not isinstance(result, dict) or result.get("ok") is not True:
                        error_code = result.get("errorCode") if isinstance(result, dict) else None
                        raise KeepSyncError(
                            f"Keep API {path} returned ok={result.get('ok') if isinstance(result, dict) else None} errorCode={error_code}"
                        )
                    return result
            except urllib.error.HTTPError as exc:
                if exc.code not in RETRYABLE_STATUS_CODES or attempt == 3:
                    raise KeepSyncError(f"Keep API {path} failed with HTTP {exc.code}") from exc
                self.sleep(min(0.5 * (2 ** attempt), 15.0))
            except urllib.error.URLError as exc:
                if attempt == 3:
                    raise KeepSyncError(f"Keep API {path} network error") from exc
                self.sleep(min(0.5 * (2 ** attempt), 8.0))
        raise AssertionError("unreachable")

    def add_water(self, drinking_time_ms: int, water_ml: int) -> str:
        result = self._request(
            "POST",
            KEEP_WATER_ADD_PATH,
            {"drinkingTime": drinking_time_ms, "water": water_ml},
        )
        data = result.get("data")
        keep_id = data.get("id") if isinstance(data, dict) else None
        if not isinstance(keep_id, str) or not keep_id.strip():
            raise KeepSyncError("Keep add_water response missing data.id")
        return keep_id.strip()

    def get_water_item(self, keep_id: str) -> dict[str, Any] | None:
        result = self._request(
            "GET",
            KEEP_WATER_ITEM_PATH + urllib.parse.quote(keep_id, safe=""),
        )
        data = result.get("data")
        return data if isinstance(data, dict) else None


def resolve_keep_token(opener: Callable[..., Any] = urllib.request.urlopen) -> str:
    token = (os.getenv("KEEP_TOKEN") or os.getenv("TOKEN") or "").strip()
    if token:
        return token
    mobile = (os.getenv("KEEP_MOBILE") or os.getenv("MOBILE") or "").strip()
    password = (os.getenv("KEEP_PASSWORD") or os.getenv("PASSWORD") or "").strip()
    country_code = (os.getenv("KEEP_COUNTRY_CODE") or os.getenv("COUNTRY_CODE") or "86").strip()
    if mobile and password:
        return KeepWaterClient.login_with_password(
            mobile, password, country_code=country_code, opener=opener
        )
    raise KeepSyncError(
        "Missing Keep credentials: set KEEP_TOKEN (or KEEP_MOBILE and KEEP_PASSWORD)"
    )


def sync_water_to_keep(
    root: Path,
    client: KeepWaterClient | None,
    *,
    state_path: Path | None = None,
    dry_run: bool = False,
    tz_name: str = DEFAULT_TIMEZONE,
    now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> dict[str, int]:
    resolved_state_path = state_path or (root / DEFAULT_STATE_FILE)
    state = load_sync_state(resolved_state_path)
    synced_records: dict[str, Any] = state.setdefault("records", {})
    records = load_water_records(root, tz_name=tz_name)

    counts = {
        "total": len(records),
        "created": 0,
        "skipped": 0,
    }

    for record in records:
        if record.uuid in synced_records:
            counts["skipped"] += 1
            continue
        if dry_run:
            print(
                f"[DRY-RUN] Would sync UUID={record.uuid} date={record.local_date} "
                f"time_ms={record.drinking_time_ms} water={record.water_ml}mL",
                flush=True,
            )
            counts["created"] += 1
            continue

        if client is None:
            raise KeepSyncError("KeepWaterClient is required when dry_run is False")

        keep_id = client.add_water(record.drinking_time_ms, record.water_ml)
        synced_records[record.uuid] = {
            "keep_id": keep_id,
            "drinking_time_ms": record.drinking_time_ms,
            "local_date": record.local_date,
            "start_date": record.start_date,
            "water_ml": record.water_ml,
            "source": record.source,
            "source_file": record.source_file,
            "synced_at": now_fn().replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        }
        # Persist immediately after each successful upstream write so a partial run
        # never re-imports already created items on retry.
        save_sync_state(resolved_state_path, state)
        counts["created"] += 1
        print(
            f"Synced UUID={record.uuid} -> Keep id={keep_id} "
            f"({record.local_date}, {record.water_ml}mL)",
            flush=True,
        )

    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Synchronize Apple Health (Hadge) water records to Keep."
    )
    parser.add_argument("--root", type=Path, default=Path("."))
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help=f"Path to sync state JSON (default: <root>/{DEFAULT_STATE_FILE})",
    )
    parser.add_argument("--timezone", default=DEFAULT_TIMEZONE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    state_path = args.state_file.resolve() if args.state_file else (root / DEFAULT_STATE_FILE)

    client = None
    if not args.dry_run:
        token = resolve_keep_token()
        client = KeepWaterClient(token, tz_name=args.timezone)

    counts = sync_water_to_keep(
        root,
        client,
        state_path=state_path,
        dry_run=args.dry_run,
        tz_name=args.timezone,
    )
    print(f"Keep water sync complete: {counts}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
