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
        self.assertEqual(len(records["sleep"]), 1)
        sleep = next(iter(records["sleep"].values())).mapping()
        self.assertEqual(sleep["睡眠时长（分钟）"], 410)
        self.assertEqual(sleep["卧床时长（分钟）"], 480)
        self.assertEqual(sleep["清醒次数"], 1)
        self.assertEqual(sleep["Apple Health Key"], "apple-health:" + sleep["Sleep Key"])
        self.assertNotIn("Core (minutes)", sleep)
        self.assertNotIn("REM (minutes)", sleep)
        self.assertNotIn("In Bed (minutes)", sleep)
        self.assertEqual(len(records["sleep-stages"]), 5)
        self.assertEqual(records["sleep-stages"]["core1"].mapping()["Sleep Day"], "2026-08-09")
        self.assertEqual(records["sleep-stages"]["core1"].mapping()["Stage"], "浅睡")
        self.assertNotIn("unspecified1", records["sleep-stages"])

    def test_sleep_sessions_split_same_day_and_preserve_uncovered_fallback(self):
        def row(key, start, end, kind):
            return sync.Record("sleep", key, tuple(sorted({
                "Start Date": start, "End Date": end, "Type": kind, "Value": 1,
            }.items())))
        records = {
            "core": row("core", "2026-01-01T00:00:00Z", "2026-01-01T01:00:00Z", "Core"),
            "awake": row("awake", "2026-01-01T01:00:00Z", "2026-01-01T01:10:00Z", "Awake"),
            "fallback": row("fallback", "2026-01-01T00:00:00Z", "2026-01-01T04:00:00Z", "Asleep Unspecified"),
            "nap": row("nap", "2026-01-01T08:00:00Z", "2026-01-01T09:00:00Z", "Core"),
            # A broad bed interval must not bridge the four-hour gap.
            "bed": row("bed", "2026-01-01T00:00:00Z", "2026-01-01T10:00:00Z", "In Bed"),
        }
        sleep_records, stages = sync._sleep_records(records)
        self.assertEqual(len(sleep_records), 2)
        self.assertEqual({record.mapping()["Sleep Day"] for record in sleep_records.values()}, {"2026-01-01"})
        durations = sorted(record.mapping()["睡眠时长（分钟）"] for record in sleep_records.values())
        self.assertEqual(durations, [60.0, 230.0])
        beds = sorted(record.mapping()["卧床时长（分钟）"] for record in sleep_records.values())
        self.assertEqual(beds, [120.0, 240.0])
        self.assertEqual(len(stages), 3)

    def test_duplicate_stage_intervals_use_minute_stage_identity(self):
        def row(key, start, end):
            return sync.Record("sleep", key, tuple(sorted({
                "Start Date": start, "End Date": end, "Type": "Core", "Value": 1,
            }.items())))
        sleep_records, stages = sync._sleep_records({
            "z": row("z", "2026-01-01T00:00:40Z", "2026-01-01T00:30:20Z"),
            "a": row("a", "2026-01-01T00:00:10Z", "2026-01-01T00:30:50Z"),
        })
        self.assertEqual(len(sleep_records), 1)
        self.assertEqual(len(stages), 1)
        self.assertEqual(next(iter(stages)), "a")

    def test_sleep_range_identity_reuses_legacy_native_page(self):
        records = sync.load_records(Path(__file__).parent / "fixtures")
        record = next(iter(records["sleep"].values()))
        values = record.mapping()
        page = {"id": "legacy-sleep", "properties": {
            "名称": {"type": "title", "title": [{"plain_text": values["Sleep Day"]}]},
            "Apple Health Key": {"type": "rich_text", "rich_text": []},
            "睡眠时间": {"type": "date", "date": {"start": values["Sleep Start"], "end": values["Sleep End"]}},
            "卧床时间": {"type": "date", "date": {"start": values["Bed Start"], "end": values["Bed End"]}},
        }}
        schema = sync.schema_for(records["sleep"])
        self.assertEqual(sync._page_sleep_key(page, schema), record.key)

    def test_sleep_stage_relation_uses_session_key_not_title(self):
        records = sync.load_records(Path(__file__).parent / "fixtures")
        sleep_page_schema = sync.schema_for(records["sleep"])
        sleep_pages = []
        for index, record in enumerate(records["sleep"].values()):
            values = record.mapping()
            sleep_pages.append({"id": f"sleep-{index}", "properties": {
                "名称": {"type": "title", "title": [{"plain_text": values["Sleep Day"]}]},
                "Apple Health Key": {"type": "rich_text", "rich_text": [{"plain_text": values["Apple Health Key"]}]},
                "睡眠时间": {"type": "date", "date": {"start": values["Sleep Start"], "end": values["Sleep End"]}},
                "卧床时间": {"type": "date", "date": {"start": values["Bed Start"], "end": values["Bed End"]}},
            }})
        keyed = {sync._page_sleep_key(page, sleep_page_schema): page["id"] for page in sleep_pages}
        stage = next(iter(records["sleep-stages"].values())).mapping()
        self.assertEqual(keyed[stage["Sleep Key"]], "sleep-0")

    def test_sleep_schema_has_only_native_duration_fields(self):
        records = sync.load_records(Path(__file__).parent / "fixtures")
        schema = sync.schema_for(records["sleep"])
        self.assertEqual(schema["Apple Health Key"], {"rich_text": {}})
        self.assertEqual(set(schema) & {"Asleep (minutes)", "Core (minutes)", "Deep (minutes)", "REM (minutes)", "In Bed (minutes)"}, set())
        options = schema_for_stage = sync.schema_for(records["sleep-stages"])["睡眠阶段"]["select"]["options"]
        self.assertEqual({option["name"] for option in options}, {"清醒", "浅睡", "深睡", "快速眼动睡眠"})

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
        self.assertEqual(sync.mismatched_properties(page, expected), [])

    def test_mismatch_diagnostics_return_names_not_values(self):
        page = {"properties": {"Distance": {"type": "number", "number": 1}}}
        expected = {"Distance": {"number": 2}}
        self.assertEqual(sync.mismatched_properties(page, expected), ["Distance"])

    def test_select_and_relation_values_are_idempotent(self):
        page = {"properties": {
            "Stage": {"type": "select", "select": {"name": "深睡"}},
            "Sleep": {"type": "relation", "relation": [{"id": "b"}, {"id": "a"}]},
        }}
        expected = {"Stage": {"select": {"name": "深睡"}}, "Sleep": {"relation": [{"id": "a"}, {"id": "b"}]}}
        self.assertEqual(sync.mismatched_properties(page, expected), [])

    def test_sleep_maps_to_keep_native_properties(self):
        record = sync.Record("sleep", "sleep-key", tuple(sorted({
            "Apple Health Key": "apple-health:sleep-key",
            "Sleep Day": "2026-08-09",
            "Sleep Start": "2026-08-08T15:30:42Z", "Sleep End": "2026-08-08T22:30:11Z",
            "Bed Start": "2026-08-08T15:00:00Z", "Bed End": "2026-08-08T23:00:00Z",
            "睡眠时长（分钟）": 410.0, "卧床时长（分钟）": 480.0, "清醒次数": 1,
        }.items())))
        schema = {
            "名称": {"type": "title"}, "Apple Health Key": {"type": "rich_text"}, "日期": {"type": "date"}, "睡眠时间": {"type": "date"},
            "卧床时间": {"type": "date"}, "睡眠时长（分钟）": {"type": "number"},
            "卧床时长（分钟）": {"type": "number"}, "清醒次数": {"type": "number"}, "日": {"type": "relation"},
        }
        props = sync.record_properties(record, schema, {"2026-08-09": "day-page"})
        self.assertEqual(sync.title_text(props["名称"]), "2026-08-09")
        self.assertEqual(sync.title_text(props["Apple Health Key"]), "apple-health:sleep-key")
        self.assertEqual(props["日期"]["date"]["start"], "2026-08-09")
        self.assertEqual(props["睡眠时间"]["date"], {"start": "2026-08-08T15:30:00Z", "end": "2026-08-08T22:30:00Z"})
        self.assertEqual(props["日"]["relation"], [{"id": "day-page"}])

    def test_sleep_stage_maps_to_keep_schema_and_parent(self):
        record = sync.Record("sleep-stages", "u1", tuple(sorted({
            "Name": "2026-08-09 深睡 00:40-02:00", "Sleep Day": "2026-08-09", "Stage": "深睡",
            "Start Date": "2026-08-08T16:40:20Z", "End Date": "2026-08-08T18:00:30Z", "Duration Minutes": 80.166667,
        }.items())))
        schema = {
            "名称": {"type": "title"}, "Apple Health Id": {"type": "rich_text"}, "日期": {"type": "date"},
            "睡眠阶段": {"type": "select"}, "阶段时间": {"type": "date"}, "阶段时长（分钟）": {"type": "number"},
            "睡眠记录": {"type": "relation"},
        }
        props = sync.sleep_stage_properties(record, schema, "sleep-page", "睡眠记录")
        self.assertEqual(sync.title_text(props["Apple Health Id"]), "apple-health:u1")
        self.assertEqual(props["睡眠阶段"]["select"]["name"], "深睡")
        self.assertEqual(props["阶段时间"]["date"]["start"], "2026-08-08T16:40:00Z")
        self.assertEqual(props["睡眠记录"]["relation"], [{"id": "sleep-page"}])

    def test_legacy_stage_page_matches_interval_and_normalizes_rem(self):
        record = sync.Record("sleep-stages", "new-uuid", tuple(sorted({
            "Name": "2026-08-09 快速眼动睡眠 00:40-02:00", "Sleep Day": "2026-08-09",
            "Sleep Key": "sleep-key", "Stage": "快速眼动睡眠",
            "Start Date": "2026-08-08T16:40:20Z", "End Date": "2026-08-08T18:00:30Z",
            "Duration Minutes": 80.166667,
        }.items())))
        schema = {
            "名称": {"type": "title"}, "Apple Health Id": {"type": "rich_text"}, "日期": {"type": "date"},
            "睡眠阶段": {"type": "select", "select": {"options": [{"name": "REM"}]}},
            "阶段时间": {"type": "date"}, "阶段时长（分钟）": {"type": "number"},
            "睡眠记录": {"type": "relation"},
        }
        legacy_page = {"id": "legacy-stage", "properties": {
            "名称": {"type": "title", "title": [{"plain_text": "old stage"}]},
            "Apple Health Id": {"type": "rich_text", "rich_text": []},
            "睡眠阶段": {"type": "select", "select": {"name": "REM"}},
            "阶段时间": {"type": "date", "date": {"start": "2026-08-08T16:40:00Z", "end": "2026-08-08T18:00:00Z"}},
        }}
        self.assertEqual(sync._page_sleep_stage_key(legacy_page, schema), sync._sleep_stage_key("快速眼动睡眠", record.mapping()["Start Date"], record.mapping()["End Date"]))
        props = sync.sleep_stage_properties(record, schema, "sleep-page", "睡眠记录")
        self.assertEqual(props["睡眠阶段"]["select"]["name"], "REM")
        self.assertEqual(sync.title_text(props["Apple Health Id"]), "apple-health:new-uuid")

    def test_stage_schema_adds_four_standard_options_to_legacy_select(self):
        calls = []
        existing = {
            "名称": {"type": "title"}, "Apple Health Id": {"type": "rich_text"}, "日期": {"type": "date"},
            "睡眠阶段": {"type": "select", "select": {"options": [{"name": "REM"}]}},
            "阶段时间": {"type": "date"}, "阶段时长（分钟）": {"type": "number"},
            "睡眠记录": {"type": "relation", "relation": {"database_id": "sleep-db"}},
        }
        class Client:
            def request(self, method, path, payload=None):
                calls.append((method, path, payload))
                if method == "GET": return {"properties": existing}
                return {"properties": existing}
        record = sync.Record("sleep-stages", "u1", (("Stage", "快速眼动睡眠"),))
        _, schema, _ = sync.ensure_sleep_stage_database(Client(), "root", {"u1": record}, {"睡眠分段": "stage-db"}, "sleep-db", False)
        option_patch = next(payload for method, path, payload in calls if method == "PATCH" and "睡眠阶段" in payload.get("properties", {}))
        names = {option["name"] for option in option_patch["properties"]["睡眠阶段"]["select"]["options"]}
        self.assertTrue({"清醒", "浅睡", "深睡", "快速眼动睡眠"} <= names)
        self.assertIn("REM", names)
        self.assertTrue({"清醒", "浅睡", "深睡", "快速眼动睡眠", "REM"} <= {
            option["name"] for option in schema["睡眠阶段"]["select"]["options"]
        })

    def test_only_legacy_empty_sleep_pages_are_repair_candidates(self):
        stale = {"properties": {
            "Core (minutes)": {"type": "number", "number": 20},
            "睡眠时长（分钟）": {"type": "number", "number": None},
        }}
        migrated = {"properties": {
            "Core (minutes)": {"type": "number", "number": 20},
            "睡眠时长（分钟）": {"type": "number", "number": 20},
        }}
        unrelated = {"properties": {"睡眠时长（分钟）": {"type": "number", "number": None}}}
        self.assertTrue(sync.is_stale_legacy_sleep_page(stale))
        self.assertFalse(sync.is_stale_legacy_sleep_page(migrated))
        self.assertFalse(sync.is_stale_legacy_sleep_page(unrelated))

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
        record = sync.Record("workouts", "u1", tuple(sorted({"Name": "Running", "Start Date": "2026-08-09T08:00:42Z", "Duration": 60.0}.items())))
        schema = {"标题": {"type": "title"}, "Id": {"type": "rich_text"}, "开始时间": {"type": "date"}, "运动时长": {"type": "number"}}
        props = sync.record_properties(record, schema)
        self.assertEqual(sync.title_text(props["标题"]), "Running")
        self.assertEqual(sync.title_text(props["Id"]), "apple-health:u1")
        self.assertEqual(props["开始时间"]["date"]["start"], "2026-08-09T08:00:00Z")
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
