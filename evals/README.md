# Transport agent evaluations

These ten Turkish questions check numerical answers against independent marts SQL
and check selected metric-contract caveats in answer text. References use
`journey_count_estimated`; station questions use physical station groups.
The district case includes an entire group if any member has the requested source
district. T3 is the stationless line documented in `docs/M1.md`.

Run in order after ingest and dbt have finished:

```powershell
uv run python tools/publish_analysis_db.py
uv run python evals/run_eval.py golden
uv run python evals/run_eval.py ask --answers evals/answers/current --workdir <empty-dir-outside-any-project>
uv run python evals/run_eval.py score --answers evals/answers/current
```

`golden` reads the analysis database in read-only mode (`--db` overrides it), imports
the MCP version function without starting its server, and writes
`evals/golden/<veri_surumu>.json`. References belong to that exact data version.
Re-running the same version overwrites it and warns if any case changed.
`score --golden evals/golden/<veri_surumu>.json` pins a reference explicitly;
otherwise the most recently modified golden file is used. Reports are `score.json`
and `score.md` in the answer directory. Exit codes: 0 success, 1 operational error,
2 failed answer checks.

`ask` **spends Claude quota**. It runs `claude -p --agent ulasim` once per case,
allows 300 seconds per answer, and records failures as `ERROR:` while continuing.
Headless runs cannot answer permission prompts, so the command pre-allows only the
agent's three MCP tools (`--allowedTools=...`). Use `--workdir` with a directory
outside any Claude Code project: the runner writes the example agent
(`examples/claude-code/ulasim.md`) and MCP config there and runs each question
with `--strict-mcp-config`, so project hooks, CLAUDE.md files, notes and other MCP
servers cannot leak answers into the agent's context. Without `--workdir` the
agent and MCP server must already be configured in the current directory. Avoid
starting many `ask` processes at the same moment: sessions started together were observed to come up
without the MCP tools, and the agent then printed tool calls as text.
Use `--agent` or `--only id,id` to customize collection. All three subcommands
accept `--cases`; relative paths resolve against the repository, independent of cwd.

Scoring checks required numeric values anywhere in the answer with 0.5% tolerance
(minimum 0.5), widened for rounded scaled quantities. It checks regexes, coverage
refusals and exact version citations. It does not verify tool calls, SQL execution
by the agent, or associate each number with a particular station or month.
Year/date components cannot satisfy quantities outside 1900–2100. Refusals must
cite the latest coverage date and contain no quantity of six or more digits.
The refusal date check is automatic; it needs no hard-coded `must_mention` date.
