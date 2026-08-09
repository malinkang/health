# Notion Keep mapping

The action creates/reuses child databases below `NOTION_ROOT_PAGE_ID`. Workouts use Keep's `运动` database. Activity and distances are joined into `健康日报` by `Date`. Other exports remain independent: `身体测量`, `生命体征`, `营养`, `活动能力`, `心率`, `睡眠`, `正念`, and `血压`.

Every database title property stores its stable source identity: workout `UUID`, or the `Asia/Shanghai` calendar date for daily data. High-frequency HealthKit samples are normalized before writing so the workspace remains useful and the first import stays within Notion's practical API limits:

- `身体测量`: latest value of each measurement type per day.
- `生命体征` and `活动能力`: daily average of each measurement type.
- `营养`: daily sum of each measurement type, including water.
- `睡眠`: duration per sleep stage and total asleep duration, computed from start/end timestamps and assigned to the local end date.
- `正念`: total duration per local end date.
- `血压`: daily average systolic and diastolic pressure.
- `心率` and `健康日报`: retain Hadge's existing daily rows.
- `运动`: retain one record per workout UUID.

Units are included in generated measurement property names. Numeric values use Notion numbers, while workout start/end values use dates normalized to Notion's minute precision.

Before writing, the synchronizer loads all files, merges identical duplicates, and fails on conflicting UUID/date duplicates. It queries every Notion page using cursors, compares normalized timestamps (including Notion's `.000` formatting), and creates or updates only changed pages. It never deletes pages or clears properties absent from the current export. Rate limits and transient failures are retried up to five attempts, honoring `Retry-After`.

## Real-data acceptance gate

Run unit tests, run the workflow manually with `dry_run=true`, inspect duplicate/conflict output and proposed counts, and then run a live write. A second live run must report only skipped records before scheduled synchronization is enabled.
