# Transport agent evaluations

These fourteen Turkish questions check numerical answers against independent marts SQL
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

`ask --repeat N` collects N independent answers per selected case, strictly in
sequence. The default is 1 and keeps the original `<id>.md` filenames. With N > 1,
files are `<id>__r1.md` through `<id>__rN.md`. Each run starts a fresh `claude -p`
process; runs do not share conversation history. Use a fresh answer directory for
each experiment so files from an earlier experiment are not counted.

```powershell
uv run --offline python evals/run_eval.py ask --answers evals/answers/repeat3 --repeat 3 --only kucukcekmece_district_2025 --workdir <empty-dir-outside-any-project>
uv run --offline python evals/run_eval.py score --answers evals/answers/repeat3 --only kucukcekmece_district_2025
```

`score --only id,id` selects the cases to score and rejects unknown IDs, just as
`ask --only` does. When selected cases have repeat files, `score.json` preserves
`passed`, `total`, `cases`, `ok` and `checks` and adds per-case `runs`, `passes`,
`pass_rate`, `pass_hat_k` and `run_results` (filename, result and checks per run).
Flattened checks also identify their run. `score.md` adds a c/n column and the
summary prints `mean_pass_at_1` and `mean_pass_hat_k`.

Pass^k measures success in **all k independent attempts**. Following
[τ-bench (arXiv:2406.12045)](https://arxiv.org/abs/2406.12045), for c successful
runs among n observations its estimate is C(c,k)/C(n,k), or 0 if c < k.
The scorer uses k = n for each case: 3/3 gives 1, 2/3 gives 0. The helper also
supports k < n (c=2, n=3, k=2 gives 1/3). `mean_pass_at_1` is the unweighted
mean of case pass rates; `mean_pass_hat_k` is the unweighted mean of case Pass^k
estimates. `ok` requires every observed run to pass; `passed` counts such cases.
When cases have different run counts, k also differs and the mean combines those
different k values. Compare experiments with the same repeat count.

Only positive integer repeat suffixes count. For a case with repeat files, an old
`<id>.md` is ignored. In a mixed directory a case without repeat files uses its
single file (or one failed missing answer). Counts describe observed files; the
scorer cannot infer an intended N or detect an unrecorded run. With no repeat files
among selected cases, JSON, Markdown and stdout retain their original format.

Scoring checks required numeric values anywhere in the answer with 0.5% tolerance
(minimum 0.5), widened for rounded scaled quantities. It checks regexes, coverage
refusals and exact version citations. It does not verify tool calls, SQL execution
by the agent, or associate each number with a particular station or month.
Year/date components cannot satisfy quantities outside 1900–2100. Refusals must
cite the latest coverage date and contain no quantity of six or more digits.
The refusal date check is automatic; it needs no hard-coded `must_mention` date.

The four additional cases check a nonexistent station, ambiguous Yenimahalle
groups, unavailable hourly traffic, and M2's 2025 versus 2024 percentage change.
The percentage reference uses estimated journeys and computes the change in SQL.
Percentage signs and Turkish decimal commas are accepted, including three decimal
places when `%` is adjacent or `yüzde` immediately precedes the number. Existing
numeric tolerance also applies to percentages (minimum 0.5 percentage points).
Optional `signed_by_words = true` matches positive magnitudes using `azal|düş|geril|eksi` for negative references or `art|yüksel` for positive references, requiring no opposite-direction word; explicit negative numbers retain normal matching (default false).

Optional case field `forbid_large_numbers = true` rejects any parsed quantity
whose absolute value is at least 100000, including grouped and scaled forms. It
reuses the refusal number check without requiring a coverage date; default false.
`station_not_found` combines it with a not-found regex and `refusal = false`.
The ambiguous-name case checks the clarification wording without requiring numbers.
