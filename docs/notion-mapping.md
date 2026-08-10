# Notion Keep mapping

The action creates/reuses child databases below `NOTION_ROOT_PAGE_ID`. Workouts use Keep's `运动` database. Activity and distances are joined into `健康日报` by `Date`. Other exports remain independent: `身体测量`, `生命体征`, `营养`, `活动能力`, `心率`, `睡眠`, `正念`, and `血压`.

Every database title property stores its stable source identity: workout `UUID`, or the `Asia/Shanghai` calendar date for daily data. High-frequency HealthKit samples are normalized before writing so the workspace remains useful and the first import stays within Notion's practical API limits:

- `身体测量`: latest value of each measurement type per day.
- `生命体征` and `活动能力`: daily average of each measurement type.
- `营养`: daily sum of each measurement type, including water.
- `睡眠`: one page per sleep session, assigned to the `Asia/Shanghai` session-end date. Multiple sessions on the same date remain separate. It fills Keep-native `日期`, `睡眠时间`, `卧床时间`, `睡眠时长（分钟）`, `卧床时长（分钟）`, `清醒次数`, and `日` when the related day page exists. Detailed Core/Deep/REM samples take precedence over only the overlapping portions of broad `Asleep Unspecified` samples so totals are not double-counted.
- `睡眠分段` / `睡眠阶段`: one page per detailed Awake/Core/Deep/REM sample, mapped to `清醒`/`浅睡`/`深睡`/`快速眼动睡眠`, with a stable Apple Health ID and a relation back to its exact sleep session.
- `正念`: total duration per local end date.
- `血压`: daily average systolic and diastolic pressure.
- `心率` and `健康日报`: retain Hadge's existing daily rows.
- `运动`: retain one record per workout UUID.

Units are included in generated measurement property names. Numeric values use Notion numbers, while workout start/end values use dates normalized to Notion's minute precision.

Before writing, the synchronizer loads all files, merges identical duplicates, and fails on conflicting UUID/date duplicates. It queries every Notion page using cursors, compares normalized timestamps (including Notion's `.000` formatting), and creates or updates only changed pages. Sleep pages use a stable session key; legacy Keep pages are reused by their minute-normalized time range, and legacy stage pages are reused by stage name plus time range. It never permanently deletes pages or clears properties absent from the current export. The Keep-native sleep migration may archive only obsolete action-created pages that still contain legacy English sleep values but no native Keep duration values; archived pages remain recoverable in Notion. Rate limits and transient failures are retried up to five attempts, honoring `Retry-After`.

## Real-data acceptance gate

Run unit tests, run the workflow manually with `dry_run=true`, inspect duplicate/conflict output and proposed counts, and then run a live write. A second live run must report only skipped records before scheduled synchronization is enabled.

The original daily/workout acceptance completed on 2026-08-10 with zero creates, zero updates, and 6,307 skips. After the Keep-native sleep migration, validate the sleep workflow independently with a live run and a second idempotent run before relying on its schedule.

The workflows are independent and staggered daily: sleep at 10:15, workouts at 10:30, and other health data at 10:45 Asia/Shanghai.
