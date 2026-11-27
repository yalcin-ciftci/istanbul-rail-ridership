# Code map

See [README § Architecture](../README.md#architecture) for the pipeline overview and [Data model](../README.md#data-model) for the public table grains.

## Data flow

Run from the repository root. In PowerShell, set `$env:PYTHONUTF8 = "1"` before Python or dbt commands.

| Step | Entry point |
| --- | --- |
| İBB CKAN → daily raw DuckDB | `uv run python ingest/rail_station_daily.py --resource-id <id>` pages through `datastore_search`, lands JSONL, and updates `raw.rail_station_daily` and `raw.source_versions`. [Resource IDs](../README.md#quickstart). |
| İBB CKAN → hourly Parquet and raw DuckDB | `uv run python ingest/hourly_transport.py --month 2024-01` streams a monthly CSV and verifies rail-only Parquet. It loads `raw.rail_hourly` from that Parquet and installs the file after commit. |
| Raw → modeled DuckDB | `uv run dbt build --project-dir transform --profiles-dir transform` builds `staging`, `intermediate`, `hourly`, and `marts` in the same database. Override its path with `ULASIM_DUCKDB_PATH`. |
| Modeled → analysis DuckDB | `uv run python tools/publish_analysis_db.py` materializes `marts` tables and views, preserves comments, and replaces the target after row-count checks. Run after a successful build. |
| Analysis DuckDB → MCP → client | `uv run python ulasim_mcp/server.py` serves `sema`, `sorgu`, and `istasyon_bul` over stdio. Set `ULASIM_DB` for a custom copy. [Client commands for `codex exec`, `agy`, and `claude -p`](../README.md#use-it-with-other-mcp-clients). |

## Code map

| Directory | Start here |
| --- | --- |
| `ingest/` | `ingest/rail_station_daily.py` owns daily source DDL and the version registry. `ingest/hourly_transport.py` owns hourly DDL and the month manifest. |
| `transform/` | `transform/dbt_project.yml` configures schemas and seeds. `transform/profiles.yml` selects DuckDB. |
| `transform/models/staging/` | `transform/models/staging/stg_rail_station_daily.sql` casts and cleans daily entrance rows. |
| `transform/models/intermediate/` | `transform/models/intermediate/int_rail_entrance_deduped.sql` removes exact duplicates. `transform/models/intermediate/int_journey_quality_daily.sql` flags dates and `transform/models/intermediate/int_rail_entrance_daily.sql` computes estimates. |
| `transform/models/hourly/` | `transform/models/hourly/stg_rail_hourly.sql` types raw rows. `transform/models/hourly/fct_hourly_coverage_month.sql` flags months and feeds the hourly profile. |
| `transform/models/marts/` | `transform/models/marts/_marts.yml`, `transform/models/marts/_station_groups.yml`, and `transform/models/marts/_hour_profile.yml` describe the columns the agent sees. `transform/models/marts/dim_source_version.sql` exposes loaded source fingerprints. |
| `transform/macros/` | `transform/macros/normalize_station_key.sql` handles hourly joins. `transform/macros/generate_schema_name.sql` controls schema names. |
| `transform/seeds/` | Reviewed mappings, calendar events, external totals, and suspect date overrides. Start with `transform/seeds/station_group_map.csv`. |
| `transform/tests/` | Singular SQL tests check grains, totals, and coverage. See `transform/tests/assert_line_journeys_reconcile.sql`. |
| `metrics/` | `metrics/metrics.toml` defines metric IDs, columns, table coverage, descriptions, and Turkish answer rules. |
| `ulasim_mcp/` | `ulasim_mcp/server.py` owns tool signatures, response fields, DuckDB access, and source identities. `ulasim_mcp/metrics.py` validates the metric contract. |
| `tools/` | Publication, CI setup, mapping drafts, and rule generation. `tools/backfill_source_versions.py` registers legacy loads. |
| `evals/` | `evals/cases.toml` holds prompts, independent SQL, and checks. `evals/run_eval.py` runs the workflow and `evals/scoring.py` scores answers. |
| `tests/` | `tests/conftest.py` supplies temporary databases and mocked HTTP. Test modules below show the enforced contracts. |
| `examples/` | `examples/claude-code/ulasim.md` is the agent prompt with a generated metric-rule block. `examples/claude-code/mcp.json` and `examples/agy/mcp_config.json` register the same stdio server. |
| `.github/workflows/` | `.github/workflows/ci.yml` runs Ruff, pytest, and an empty-source dbt build on Ubuntu and Windows. |

## Invariants

| Rule | Regression check |
| --- | --- |
| Daily reruns replace one resource. Failures roll back rows and fingerprints. | `tests/test_rail_station_daily.py` (`test_other_resource_is_preserved`, `test_source_version_rollback`). |
| Daily staging retains the raw row count. Line totals belong in line facts and stay out of station facts. | `transform/tests/assert_row_count_matches_raw.sql` and `transform/models/marts/_mart_unit_tests.yml` (station and line daily unit tests). |
| Suspect dates are limited to 2022. The heuristic wins over `transform/seeds/journey_suspect_overrides.csv`. Answers disclose the adjusted counts as project estimates. | `transform/tests/assert_suspect_only_2022.sql`, `tests/test_journey_suspect_overrides.py`, and eval cases `suspect_2022_m2` / `hourly_override_2022`. |
| Hourly reruns replace one month. Coverage distinguishes `partial_days`, `low_volume`, and `empty` from `ok`. | `tests/test_hourly_transport.py` (`test_other_month_is_preserved_and_url_month_recorded`) and `tests/test_hourly_station_map.py` (`test_coverage_priority_history_mapping_and_empty_month`). |
| Every station code in the daily dimension has a seed entry. Group totals reconcile with station totals after accounting for ungrouped codes. | `transform/tests/assert_station_group_seed_coverage.sql` and `transform/tests/assert_station_group_journeys_reconcile.sql`. |
| Publication exposes only `marts`. MCP opens a read-only connection per call, disables external access, rejects other user schemas, and allows one SELECT in `sorgu`. | `tests/test_publish_analysis_db.py` (`test_publish_only_marts_materializes_views_and_preserves_source`) and `tests/test_ulasim_mcp.py` (`test_sorgu_security_configuration`, `test_tools_reject_database_with_non_marts_objects`, `test_sorgu_rejects_non_single_select_before_execution`). |
| Metric descriptions and agent rules stay in sync. `sema` surfaces İBB attribution. Eval cases can require daily and hourly version citations. | `tests/test_metrics_contract.py`, `tests/test_ulasim_mcp.py` (`test_sema_only_marts_comments_scope_and_caveats`), and eval cases `data_version_cited` / `hourly_profile_yenikapi`. |

## Where to change what

- **Add a metric.** Update `metrics/metrics.toml`, the mart SQL and its YAML. Run `uv run python tools/render_agent_rules.py` for rule changes. Update `evals/cases.toml` if answer expectations change.
- **Load a new data month.** Use `--month` for hourly or rerun the yearly resource for daily. If new station codes appear, use `tools/draft_station_groups.py` and review `transform/seeds/station_group_map.csv`. If daily extends past 2025, review `transform/models/marts/dim_calendar_day.sql`. Rebuild and republish.
- **Add an MCP tool field.** Update `ulasim_mcp/server.py` and `tests/test_ulasim_mcp.py`. For version or profile fields, also check `tests/test_hourly_profile_mcp.py` and the eval runner and scorer. Update the example prompt if the client must use the field.
- **Change hourly data.** Start with hourly ingest and staging for source shape. `tools/build_hourly_station_map.py` drafts `transform/seeds/hourly_station_map.csv`. Line mapping lives in `transform/seeds/hourly_line_map.csv`. Follow coverage into `transform/models/hourly/fct_station_group_hour_profile.sql` and its public projection `transform/models/marts/mart_station_group_hour_profile.sql`.

## Tests & evals

| Command | Checks or output |
| --- | --- |
| `uv run pytest -q` | Python regressions, mocked ingest, temporary DuckDB, MCP stdio, metric contract, and eval runner/scorer. |
| `uv run ruff check .` and `uv run ruff format --check .` | Lint and formatting, respectively. |
| `uv run dbt build --project-dir transform --profiles-dir transform` | Seeds and models, dbt unit tests, and data tests on the configured database. CI uses `uv run python tools/init_empty_db.py --db <temp-db>` and adds `--empty` to exercise model wiring and dbt unit tests without downloaded data. |
| `uv run python tools/render_agent_rules.py --check` | Detects drift in the generated rule block. |
| `uv run python evals/run_eval.py golden` | Runs independent marts SQL against the published copy and writes version-keyed references. |
| `uv run python evals/run_eval.py ask --answers <answer-dir> --workdir <empty-dir>` | Collects Claude answers with the example prompt and MCP config. Uses Claude quota. |
| `uv run python evals/run_eval.py score --answers <answer-dir>` | Checks saved answers for numbers, caveats, refusals, and version citations against the golden reference. See [eval workflow](../evals/README.md) for pinning the golden and repeating runs. |
