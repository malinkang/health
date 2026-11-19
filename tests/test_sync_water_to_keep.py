import importlib.util
import io
import json
import sys
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

P = Path(__file__).parents[1] / "scripts/sync_water_to_keep.py"
S = importlib.util.spec_from_file_location("sync_water_to_keep", P)
sync_keep = importlib.util.module_from_spec(S)
sys.modules[S.name] = sync_keep
S.loader.exec_module(sync_keep)


class FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.data = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self):
        return self.data


class SyncWaterToKeepTests(unittest.TestCase):
    def test_load_water_records_filters_and_rounds_ml(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "nutrition").mkdir()
            (root / "nutrition/2026.csv").write_text(
                "UUID,Start Date,End Date,Type,Value,Unit,Source\n"
                "u2,2026-04-01T01:57:28Z,2026-04-01T01:57:28Z,Water,350.00035,mL,WaterMinder\n"
                "u1,2026-01-02T06:20:14Z,2026-01-02T06:20:14Z,Water,500.0005,mL,WaterMinder\n"
                "u3,2026-04-01T02:00:00Z,2026-04-01T02:00:00Z,DietaryEnergy,120,kcal,Food\n",
                encoding="utf-8",
            )
            records = sync_keep.load_water_records(root)
            self.assertEqual([r.uuid for r in records], ["u1", "u2"])
            self.assertEqual(records[0].water_ml, 500)
            self.assertEqual(records[0].local_date, "2026-01-02")
            self.assertEqual(records[1].water_ml, 350)
            self.assertEqual(records[1].local_date, "2026-04-01")

    def test_sync_creates_only_unsynced_records_and_persists_state(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "nutrition").mkdir()
            (root / "nutrition/2026.csv").write_text(
                "UUID,Start Date,End Date,Type,Value,Unit,Source\n"
                "u1,2026-01-02T06:20:14Z,2026-01-02T06:20:14Z,Water,500.0005,mL,WaterMinder\n"
                "u2,2026-04-01T01:57:28Z,2026-04-01T01:57:28Z,Water,350.00035,mL,WaterMinder\n",
                encoding="utf-8",
            )
            calls = []

            def opener(req, timeout=20):
                body = json.loads(req.data.decode("utf-8"))
                calls.append((req.full_url, body, req.headers.get("Authorization")))
                return FakeHTTPResponse({"ok": True, "data": {"id": f"keep-{len(calls)}"}, "errorCode": 0})

            client = sync_keep.KeepWaterClient("test-jwt-token", opener=opener, min_interval=0)
            fixed_now = lambda: datetime(2026, 11, 19, 7, 30, 0, tzinfo=timezone.utc)

            first_counts = sync_keep.sync_water_to_keep(root, client, now_fn=fixed_now)
            self.assertEqual(first_counts, {"total": 2, "created": 2, "skipped": 0})
            self.assertEqual(len(calls), 2)
            self.assertEqual(calls[0][1]["water"], 500)
            self.assertEqual(calls[1][1]["water"], 350)

            state_file = root / sync_keep.DEFAULT_STATE_FILE
            self.assertTrue(state_file.exists())
            state_data = json.loads(state_file.read_text(encoding="utf-8"))
            self.assertEqual(state_data["records"]["u1"]["keep_id"], "keep-1")
            self.assertEqual(state_data["records"]["u2"]["keep_id"], "keep-2")

            # Second run must skip already synced UUIDs without making HTTP calls
            second_counts = sync_keep.sync_water_to_keep(root, client, now_fn=fixed_now)
            self.assertEqual(second_counts, {"total": 2, "created": 0, "skipped": 2})
            self.assertEqual(len(calls), 2)

    def test_partial_failure_saves_earlier_records(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "nutrition").mkdir()
            (root / "nutrition/2026.csv").write_text(
                "UUID,Start Date,End Date,Type,Value,Unit,Source\n"
                "u1,2026-01-02T06:20:14Z,2026-01-02T06:20:14Z,Water,500,mL,WaterMinder\n"
                "u2,2026-04-01T01:57:28Z,2026-04-01T01:57:28Z,Water,350,mL,WaterMinder\n",
                encoding="utf-8",
            )
            call_count = 0

            def opener(req, timeout=20):
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    return FakeHTTPResponse({"ok": True, "data": {"id": "keep-ok-1"}, "errorCode": 0})
                raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO())

            client = sync_keep.KeepWaterClient("token", opener=opener, min_interval=0)
            with self.assertRaises(sync_keep.KeepSyncError):
                sync_keep.sync_water_to_keep(root, client)

            state = sync_keep.load_sync_state(root / sync_keep.DEFAULT_STATE_FILE)
            self.assertIn("u1", state["records"])
            self.assertNotIn("u2", state["records"])


if __name__ == "__main__":
    unittest.main()
