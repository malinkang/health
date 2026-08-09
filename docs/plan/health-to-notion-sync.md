# Hadge to Notion Keep synchronization plan

Status: complete

## Scope

- Add a dependency-free Python synchronizer for legacy activity/distances/workouts and the new Hadge body, vitals, nutrition, mobility, heart-rate, sleep, mindfulness, and blood-pressure CSV exports.
- Map workouts into the Keep workout database; create or reuse one clearly named database per other module under the configured Keep root page.
- Enforce deterministic UUID/date identities, identical-duplicate merging, conflicting-duplicate failure, complete Notion pagination, bounded 429/transient retries, and update-only semantics (never delete Notion pages).
- Normalize Notion date/timestamps before comparison so API millisecond formatting does not cause repeated updates.
- Add fixture-backed unit tests and a GitHub Actions workflow using only `NOTION_TOKEN` and `NOTION_ROOT_PAGE_ID` secrets.
- Document the Notion database/property mapping and the real-export acceptance steps. Do not write to a live Notion workspace or push during this implementation.

## Verification

- Run the complete unit-test suite locally.
- Run syntax/compile validation and inspect workflow YAML.
- Have an independent validation agent review retry, pagination, conflict, idempotency, mapping, and no-delete behavior.

## Completion criteria

- All fixtures and unit tests pass without network access.
- Workflow is syntactically coherent and watches every supported export directory.
- README documents setup, mappings, dry-run behavior, and the pending real-data/live-Notion acceptance gate.

## Result

Implemented the synchronizer, fixture-backed tests, workflow, and mapping documentation. Local unit tests and compile validation pass; live Notion acceptance remains intentionally deferred until real Hadge exports are available.
