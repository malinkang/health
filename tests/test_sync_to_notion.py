import importlib.util, io, sys, unittest, urllib.error
from pathlib import Path

P = Path(__file__).parents[1] / "scripts/sync_to_notion.py"
S = importlib.util.spec_from_file_location("sync_to_notion", P); sync = importlib.util.module_from_spec(S); sys.modules[S.name] = sync; S.loader.exec_module(sync)

class FakeResponse:
    def __init__(self, data): self.data = data
    def __enter__(self): return self
    def __exit__(self, *args): pass
    def read(self): return self.data

class Tests(unittest.TestCase):
    def test_new_formats_load_with_uuid_and_date_keys(self):
        records = sync.load_records(Path(__file__).parent / "fixtures")
        self.assertEqual(records["body"]["2026-08-09"].mapping()["Body Mass (kg)"], 70.5)
        self.assertEqual(records["heart-rate"]["2026-08-09"].mapping()["Average"], 76)
        self.assertEqual(records["blood-pressure"]["2026-08-09"].mapping()["Systolic (mmHg)"], 120)
        self.assertEqual(records["sleep"]["2026-08-09"].mapping()["Asleep (minutes)"], 480)
        self.assertEqual(records["sleep"]["2026-08-09"].mapping()["Total Sleep (minutes)"], 480)

    def test_identical_duplicate_merges_and_conflict_fails(self):
        import tempfile
        with tempfile.TemporaryDirectory(dir=Path(__file__).parent) as d:
            root = Path(d); (root / "body").mkdir()
            header = "UUID,Start Date,End Date,Type,Value,Unit,Source\n"
            (root / "body/a.csv").write_text(header + "x,2026-01-01,2026-01-01,Mass,70,kg,A\n")
            (root / "body/b.csv").write_text(header + "x,2026-01-01,2026-01-01,Mass,70,kg,A\n")
            self.assertEqual(len(sync.load_records(root)["body"]), 1)
            (root / "body/b.csv").write_text(header + "x,2026-01-01,2026-01-01,Mass,71,kg,A\n")
            with self.assertRaisesRegex(ValueError, "Conflicting body key x"): sync.load_records(root)

    def test_daily_aggregation_uses_local_end_day_and_module_rules(self):
        records = {
            "old": sync.Record("body", "old", tuple(sorted({"Start Date": "2026-08-09T14:00:00Z", "End Date": "2026-08-09T14:00:00Z", "Type": "Body Mass", "Value": 70.0, "Unit": "kg"}.items()))),
            "new": sync.Record("body", "new", tuple(sorted({"Start Date": "2026-08-09T16:00:00Z", "End Date": "2026-08-09T16:00:00Z", "Type": "Body Mass", "Value": 71.0, "Unit": "kg"}.items()))),
        }
        aggregated = sync._aggregate_daily("body", records)
        self.assertEqual(aggregated["2026-08-09"].mapping()["Body Mass (kg)"], 70.0)
        self.assertEqual(aggregated["2026-08-10"].mapping()["Body Mass (kg)"], 71.0)

        vitals = {
            str(i): sync.Record("vitals", str(i), tuple(sorted({"End Date": "2026-08-09T08:00:00Z", "Type": "Resting Heart Rate", "Value": value, "Unit": "bpm"}.items())))
            for i, value in enumerate((60.0, 70.0))
        }
        self.assertEqual(sync._aggregate_daily("vitals", vitals)["2026-08-09"].mapping()["Resting Heart Rate (bpm)"], 65.0)

        water = {
            str(i): sync.Record("nutrition", str(i), tuple(sorted({"End Date": "2026-08-09T08:00:00Z", "Type": "Water", "Value": value, "Unit": "mL"}.items())))
            for i, value in enumerate((250.0, 300.0))
        }
        self.assertEqual(sync._aggregate_daily("nutrition", water)["2026-08-09"].mapping()["Water (mL)"], 550.0)

    def test_millisecond_and_offset_dates_are_idempotent(self):
        page = {"properties": {"Start Date": {"type": "date", "date": {"start": "2026-08-09T08:00:00.000Z", "end": None}}}}
        expected = {"Start Date": {"date": {"start": "2026-08-09T16:00:00+08:00"}}}
        self.assertFalse(sync.needs_update(page, expected))

    def test_pagination_uses_cursor(self):
        calls = []
        class Client(sync.NotionClient):
            def request(self, method, path, payload=None):
                calls.append((path, payload)); return {"results": [len(calls)], "has_more": len(calls) == 1, "next_cursor": "next" if len(calls) == 1 else None}
        self.assertEqual(Client("x").query("db"), [1, 2])
        self.assertEqual(calls[1][1]["start_cursor"], "next")

    def test_429_retries_then_succeeds(self):
        attempts, sleeps = [], []
        def opener(req):
            attempts.append(1)
            if len(attempts) == 1: raise urllib.error.HTTPError(req.full_url, 429, "rate", {"Retry-After": "0.25"}, io.BytesIO())
            return FakeResponse(b'{"ok": true}')
        self.assertTrue(sync.NotionClient("x", sleeps.append, opener).request("GET", "/x")["ok"])
        self.assertEqual(sleeps, [0.25])

    def test_retry_after_is_bounded_and_malformed_value_falls_back(self):
        for header, expected in (("999", 30.0), ("bad", 0.5)):
            attempts, sleeps = [], []
            def opener(req):
                attempts.append(1)
                if len(attempts) == 1: raise urllib.error.HTTPError(req.full_url, 429, "rate", {"Retry-After": header}, io.BytesIO())
                return FakeResponse(b"{}")
            sync.NotionClient("x", sleeps.append, opener).request("GET", "/x")
            self.assertEqual(sleeps, [expected])

    def test_429_exhaustion_fails(self):
        def opener(req): raise urllib.error.HTTPError(req.full_url, 429, "rate", {}, io.BytesIO())
        with self.assertRaises(sync.NotionError): sync.NotionClient("x", lambda _: None, opener).request("GET", "/x")

    def test_network_failure_retries_are_bounded(self):
        attempts = []
        def opener(req): attempts.append(1); raise urllib.error.URLError("offline")
        with self.assertRaises(sync.NotionError): sync.NotionClient("x", lambda _: None, opener).request("GET", "/x")
        self.assertEqual(len(attempts), 5)

    def test_no_delete_api_exists(self):
        self.assertNotIn("delete", {name.lower() for name in dir(sync.NotionClient)})

    def test_workout_maps_to_keep_schema_and_prefixed_id(self):
        record = sync.Record("workouts", "u1", tuple(sorted({"Name": "Running", "Start Date": "2026-08-09T08:00:00Z", "Duration": 60.0}.items())))
        schema = {"标题": {"type": "title"}, "Id": {"type": "rich_text"}, "开始时间": {"type": "date"}, "运动时长": {"type": "number"}}
        props = sync.record_properties(record, schema)
        self.assertEqual(sync.title_text(props["标题"]), "Running")
        self.assertEqual(sync.title_text(props["Id"]), "apple-health:u1")
        self.assertEqual(props["运动时长"]["number"], 60.0)

    def test_database_discovery_descends_into_keep_pages(self):
        class Client:
            def children(self, parent):
                return [{"id": "nested", "type": "child_page"}] if parent == "root" else [{"id": "workout-db", "type": "child_database", "child_database": {"title": "运动"}}]
        self.assertEqual(sync.find_databases(Client(), "root")["运动"], "workout-db")

    def test_existing_keep_title_does_not_add_second_title(self):
        calls = []
        class Client:
            def request(self, method, path, payload=None):
                calls.append((method, path, payload))
                return {"properties": {"标题": {"type": "title"}, "Id": {"type": "rich_text"}}}
        record = sync.Record("workouts", "u1", (("Name", "Run"),))
        sync.ensure_database(Client(), "root", "workouts", {"u1": record}, {"运动": "db"}, False)
        patches = [payload for method, _, payload in calls if method == "PATCH"]
        self.assertNotIn("Name", patches[0]["properties"])

if __name__ == "__main__": unittest.main()
