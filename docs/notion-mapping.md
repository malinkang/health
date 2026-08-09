# Notion Keep mapping

The action creates/reuses child databases below `NOTION_ROOT_PAGE_ID`. Workouts use Keep's `运动` database. Activity and distances are joined into `健康日报` by `Date`. Other exports remain independent: `身体测量`, `生命体征`, `营养`, `活动能力`, `心率`, `睡眠`, `正念`, and `血压`.

Every database title property stores its stable source identity: workout and record `UUID`, or `Date` for daily and heart-rate data. CSV fields retain their Hadge column names. Numeric values use Notion numbers, start/end columns use dates, and type/unit/source/name values use text. This explicit 1:1 mapping avoids silently merging unlike HealthKit samples.

Before writing, the synchronizer loads all files, merges identical duplicates, and fails on conflicting UUID/date duplicates. It queries every Notion page using cursors, compares normalized timestamps (including Notion's `.000` formatting), and creates or updates only changed pages. It never deletes pages or clears properties absent from the current export. Rate limits and transient failures are retried up to five attempts, honoring `Retry-After`.

## Real-data acceptance gate

The current implementation is fixture-tested only. When Hadge uploads the real new directories: run unit tests, run the workflow manually with `dry_run=true`, inspect duplicate/conflict output and proposed counts, then run on a disposable/copied Keep root before enabling live synchronization. Do not push or enable live writes until that review is accepted.
