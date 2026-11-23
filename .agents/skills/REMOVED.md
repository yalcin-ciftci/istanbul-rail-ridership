# Removed upstream files

These files from the bundled upstream skills do not apply to this DuckDB/Python project and were removed from this repository. The remaining skill files are unchanged copies, so some of them still link to the removed files; the full versions are available upstream at the recorded commits.

From [obra/superpowers](https://github.com/obra/superpowers) at commit `22f0b463e7a5e579904dd6211a520321035526f7` (MIT):

- `systematic-debugging/find-polluter.sh`
- `systematic-debugging/condition-based-waiting-example.ts`

From [dbt-labs/dbt-agent-skills](https://github.com/dbt-labs/dbt-agent-skills) at commit `b8facde20b7a92c40ce3e6b86b71c4b92aa44040` (Apache-2.0):

- `adding-dbt-unit-test/references/warehouse-bigquery-caveats.md`
- `adding-dbt-unit-test/references/warehouse-bigquery-data-types.md`
- `adding-dbt-unit-test/references/warehouse-postgres-data-types.md`
- `adding-dbt-unit-test/references/warehouse-redshift-caveats.md`
- `adding-dbt-unit-test/references/warehouse-redshift-data-types.md`
- `adding-dbt-unit-test/references/warehouse-snowflake-data-types.md`
- `adding-dbt-unit-test/references/warehouse-spark-data-types.md`

## Removed whole skills

- `impeccable` from [pbakaus/impeccable](https://github.com/pbakaus/impeccable) at commit `d41d9860b0094dcb68030657da63461212835aaa` (Apache-2.0), removed 2026-11-24 together with its license file and commit record. Its first step runs a downloaded engine every session; the engine has telemetry on by default (`impeccable.style/api`), an account/OAuth login, a `live` mode that injects scripts into the page, and hooks that write project files. The same review work is covered by `frontend-design`, the accessibility skills and the chrome-devtools MCP (screenshots, Lighthouse).
