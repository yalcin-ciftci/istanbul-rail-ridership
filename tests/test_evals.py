"""Offline contract tests using disposable analytical databases only."""

import importlib
import json
import subprocess
import sys
import tomllib
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]


def implementation(name):
    path = ROOT / "evals" / f"{name}.py"
    assert path.exists(), f"Missing eval implementation: {name}"
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    return importlib.import_module(f"evals.{name}")


def test_cases_and_rule_contract():
    runner = implementation("run_eval")
    cases = runner.load_cases(ROOT / "evals/cases.toml")
    assert len(cases) >= 10
    assert len({case["id"] for case in cases}) == len(cases)
    contract = tomllib.loads((ROOT / "metrics/metrics.toml").read_text("utf-8"))
    rules = {rule["id"] for rule in contract["rule"]}
    for case in cases:
        assert set(case["rules"]) <= rules
        runner.validate_sql(case["reference_sql"])


def test_hourly_override_reference_and_estimate_warning():
    runner = implementation("run_eval")
    cases = runner.load_cases(ROOT / "evals/cases.toml")
    case = cases[6]
    assert case["id"] == "hourly_override_2022"
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE SCHEMA marts")
        con.execute(
            "CREATE TABLE marts.fct_line_daily "
            "(ride_date DATE, journey_count BIGINT, journey_count_estimated BIGINT, "
            "has_suspect_journeys BOOLEAN)"
        )
        con.execute(
            "INSERT INTO marts.fct_line_daily VALUES "
            "('2022-10-30', 200, 100, true), ('2022-10-31', 202, 101, true), "
            "('2022-10-31', 400, 200, true), ('2022-11-01', 204, 102, true), "
            "('2022-11-02', 206, 103, true), ('2022-11-03', 208, 104, true), "
            "('2022-11-04', 210, 105, true), ('2022-11-05', 212, 106, true), "
            "('2022-11-06', 214, 107, true), ('2022-11-07', 216, 108, true), "
            "('2022-11-08', 218, 218, false)"
        )
        rows = con.execute(case["reference_sql"]).fetchall()
    reference = {
        "columns": ["ride_date", "journeys"],
        "rows": [[str(row[0]), row[1]] for row in rows],
    }
    assert reference["rows"] == [
        ["2022-10-31", 301],
        ["2022-11-01", 102],
        ["2022-11-02", 103],
        ["2022-11-03", 104],
        ["2022-11-04", 105],
        ["2022-11-05", 106],
        ["2022-11-06", 107],
        ["2022-11-07", 108],
    ]
    scoring = implementation("scoring")
    answer = "301; 102; 103; 104; 105; 106; 107; 108"
    assert not all(
        check["ok"] for check in scoring.score_answer(case, answer, reference, "v")
    )
    assert all(
        check["ok"]
        for check in scoring.score_answer(
            case, answer + " proje tahmini", reference, "v"
        )
    )


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1; SELECT 2",
        "DELETE FROM marts.t",
        "SELECT x FROM raw.t",
        'SELECT x FROM "raw"."t"',
        "SELECT x FROM t",
        "SELECT x FROM read_csv('file.csv')",
        "SELECT x FROM marts.t JOIN raw.u USING(x)",
        "SELECT x FROM other.marts.t",
    ],
)
def test_sql_rejects_non_marts_and_non_select(sql):
    runner = implementation("run_eval")
    with pytest.raises(ValueError):
        runner.validate_sql(sql)


@pytest.mark.parametrize(
    "answer, expected",
    [
        ("71.514.519", 71514519),
        ("71,514,519", 71514519),
        ("71514519", 71514519),
        ("71 514 519", 71514519),
        ("%34,5", 34.5),
        ("34.5", 34.5),
        ("71,5 milyon", 71500000),
        ("71.5 million", 71500000),
        ("217 bin", 217000),
        ("217k", 217000),
        ("1.234,56", 1234.56),
        ("1,234.56", 1234.56),
    ],
)
def test_number_formats(answer, expected):
    scoring = implementation("scoring")
    assert scoring.number_matches(answer, expected)


@pytest.mark.parametrize(
    "answer, expected, ok",
    [
        ("1,2 milyon", 1249999, True),
        ("1,2 milyon", 1250001, False),
        ("217k", 217499, True),
        ("217k", 219000, False),
        ("2025", 2030, True),
        ("2025", 2025, True),
        ("2025", 2105, False),
        ("999", 71514519, False),
        ("100", 100.5, True),
    ],
)
def test_number_tolerance(answer, expected, ok):
    assert implementation("scoring").number_matches(answer, expected) is ok


def evaluate(answer, **overrides):
    case = {
        "id": "sample",
        "numbers": [],
        "must_mention": [],
        "must_not_mention": [],
        "rules": [],
        "refusal": False,
    }
    case.update(overrides)
    return implementation("scoring").score_answer(
        case,
        answer,
        {"columns": ["min_date", "max_date"], "rows": [["2021-01-01", "2025-12-31"]]},
        "abc123def456",
    )


@pytest.mark.parametrize(
    "answer, ok",
    [
        ("Kapsam sonu 2025-12-31, dün yok.", True),
        ("Kapsam sonu 31.12.2025.", False),
        ("31 Aralık 2025 sonrası bulunmuyor.", True),
        ("Kapsam sonu 2025-12-31, 123456 yolculuk.", False),
        ("Kapsam sonu 2025-12-31, 123.456 yolculuk.", False),
        ("Dün yok.", False),
        ("2025-12-31", False),
    ],
)
def test_refusal(answer, ok):
    assert all(check["ok"] for check in evaluate(answer, refusal=True)) is ok


@pytest.mark.parametrize("heading", ["**Nasıl hesaplandı", "**NASIL HESAPLANDI"])
def test_refusal_footer_cannot_supply_refusal(heading):
    answer = (
        "**Cevap:** 2024-05-15 08:00'de Yenikapı'da 18.540 kişi geçti.\n"
        f"{heading}:** SQL\nKapsam dışı değildir.\n"
        "**Veri kapsamı:** 2021-01-01–2025-12-31."
    )
    checks = {c["check"]: c["ok"] for c in evaluate(answer, refusal=True)}
    assert checks == {
        "refusal_date": True,
        "refusal_text": False,
        "refusal_number": True,
    }


def test_refusal_accepts_soyleyemiyorum_in_answer():
    assert all(
        c["ok"]
        for c in evaluate(
            "**Cevap:** Bunu söyleyemiyorum.\n**Nasıl hesaplandı:** Sorgu çalıştırılmadı.\n"
            "**Son tarih:** 2025-12-31.",
            refusal=True,
        )
    )


def test_refusal_without_heading_limits_search_to_600_characters():
    checks = evaluate("2025-12-31 " + "a " * 300 + "yok", refusal=True)
    assert not next(c["ok"] for c in checks if c["check"] == "refusal_text")


@pytest.mark.parametrize(
    "text, parts",
    [
        ("2026-11-18", (2026, 11, 18)),
        ("2026.11.18", (2026, 11, 18)),
        ("18.11.2026", (2026, 11, 18)),
        ("2025-12-31", (2025, 12, 31)),
        ("31.12.2025", (2025, 12, 31)),
    ],
)
def test_date_parts_never_match_expected_numbers(text, parts):
    scoring = implementation("scoring")
    for expected in parts:
        assert not scoring.number_matches(text, expected)
    assert all(n.date_part for n in scoring.extract_numbers(text))
    assert scoring.number_matches(text + " tepe saat 18", 18)


@pytest.mark.parametrize(
    "version, answer, ok",
    [
        ("missing", "tipik turnike", False),
        (None, "tipik turnike", False),
        ("", "tipik turnike", False),
        ("hourly123", "tipik turnike daily123", False),
        ("hourly123", "tipik turnike hourly123", True),
    ],
)
def test_hourly_version_scored_through_golden(version, answer, ok, tmp_path):
    runner = implementation("run_eval")
    golden_file = tmp_path / "golden.json"
    payload = {
        "veri_surumu": "daily123",
        "cases": {"profile": {"columns": [], "rows": []}},
    }
    if version != "missing":
        payload["saatlik_veri_surumu"] = version
    golden_file.write_text(json.dumps(payload), encoding="utf-8")
    (tmp_path / "profile.md").write_text(answer, encoding="utf-8")
    assert runner.score(
        [{"id": "profile", "require_hourly_version": True}], tmp_path, golden_file
    ) == (0 if ok else 2)
    result = json.loads((tmp_path / "score.json").read_text("utf-8"))
    check = result["cases"]["profile"]["checks"][0]
    assert check["check"] == "hourly_version"
    if not version or version == "missing":
        assert check["detail"] == "golden'da saatlik_veri_surumu yok"


@pytest.mark.parametrize("has_hourly", [False, True])
def test_golden_writes_hourly_identity(fixture_eval, monkeypatch, has_hourly):
    runner = implementation("run_eval")
    db, cases, golden_dir, _ = fixture_eval
    if has_hourly:
        with duckdb.connect(str(db)) as con:
            con.execute(
                "INSERT INTO marts.dim_source_version VALUES ('rail_hourly', 'h', '2024-01', 'hash', 'stored', '2026-01-02')"
            )
    monkeypatch.setattr(runner, "GOLDEN_DIR", golden_dir)
    assert runner.main(["golden", "--db", str(db), "--cases", str(cases)]) == 0
    payload = json.loads(next(golden_dir.glob("*.json")).read_text("utf-8"))
    assert payload["saatlik_veri_surumu"] == ("7926870d2d1a" if has_hourly else None)


@pytest.mark.parametrize(
    "answer, ok",
    [
        ("turnike", False),
        ("tipik", False),
        ("medyan turnike", True),
        ("tipik turnike", True),
    ],
)
def test_hourly_profile_requires_typical_and_turnstile(answer, ok):
    runner = implementation("run_eval")
    case = next(
        c
        for c in runner.load_cases(ROOT / "evals/cases.toml")
        if c["id"] == "hourly_profile_yenikapi"
    )
    checks = evaluate(answer, must_mention=case["must_mention"])
    assert all(c["ok"] for c in checks) is ok


@pytest.mark.parametrize(
    "answer, ok", [("Sürüm abc123def456", True), ("Sürüm yok", False)]
)
def test_version(answer, ok):
    checks = evaluate(answer, rules=["data_version"])
    assert all(check["ok"] for check in checks) is ok
    assert any(check["check"] == "data_version" for check in checks)


def test_regex_and_number_rows():
    scoring = implementation("scoring")
    case = {
        "numbers": ["journeys"],
        "number_rows": 1,
        "must_mention": ["(?i)turnike"],
        "must_not_mention": ["tekil kişi"],
    }
    reference = {"columns": ["journeys"], "rows": [[100], [200]]}
    checks = scoring.score_answer(case, "100 turnike", reference, "v")
    assert len(checks) == 3
    assert all(check["ok"] for check in checks)
    checks = scoring.score_answer(case, "100 tekil kişi", reference, "v")
    assert sum(not check["ok"] for check in checks) == 2


@pytest.fixture
def fixture_eval(tmp_path):
    db = tmp_path / "analysis.duckdb"
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE SCHEMA marts")
        con.execute("CREATE TABLE marts.sample(d DATE, journeys DECIMAL(12,2))")
        con.execute("INSERT INTO marts.sample VALUES ('2025-12-31', 123456.25)")
        con.execute(
            "CREATE TABLE marts.dim_source_version(dataset VARCHAR, resource_id VARCHAR, "
            "source_month VARCHAR, source_sha256 VARCHAR, stored_sha256 VARCHAR, "
            "ingested_at TIMESTAMP)"
        )
        con.execute(
            "INSERT INTO marts.dim_source_version VALUES "
            "('rail_station_daily', 'r', NULL, 'abc', 'def', '2026-01-01')"
        )
    cases = tmp_path / "cases.toml"
    cases.write_text(
        """[[case]]
id = "total"
question_tr = "Kaç yolculuk?"
reference_sql = "SELECT d, journeys FROM marts.sample"
numbers = ["journeys"]
must_mention = []
must_not_mention = []
rules = []
refusal = false
[[case]]
id = "scope"
question_tr = "Dün?"
reference_sql = "SELECT min(d) AS min_date, max(d) AS max_date FROM marts.sample"
numbers = []
must_mention = []
must_not_mention = []
rules = []
refusal = true
""",
        encoding="utf-8",
    )
    return db, cases, tmp_path / "golden", tmp_path / "answers"


def test_golden_version_serialization_warning_and_readonly(
    fixture_eval, monkeypatch, capsys
):
    runner = implementation("run_eval")
    db, cases, golden_dir, _ = fixture_eval
    monkeypatch.setattr(runner, "GOLDEN_DIR", golden_dir)
    before = db.read_bytes()
    assert runner.main(["golden", "--db", str(db), "--cases", str(cases)]) == 0
    golden_file = next(golden_dir.glob("*.json"))
    golden = json.loads(golden_file.read_text("utf-8"))
    assert golden_file.stem == golden["veri_surumu"]
    assert len(golden_file.stem) == 12
    assert golden["olusturma"].endswith("+00:00")
    assert golden["cases"]["total"] == {
        "columns": ["d", "journeys"],
        "rows": [["2025-12-31", 123456.25]],
    }
    assert db.read_bytes() == before
    golden["cases"]["total"]["rows"][0][1] = 1
    golden_file.write_text(json.dumps(golden), encoding="utf-8")
    assert runner.main(["golden", "--db", str(db), "--cases", str(cases)]) == 0
    output = capsys.readouterr().out
    assert "Warning:" in output and "total" in output


def test_golden_missing_version(fixture_eval, capsys):
    runner = implementation("run_eval")
    db, cases, _, _ = fixture_eval
    with duckdb.connect(str(db)) as con:
        con.execute("DROP TABLE marts.dim_source_version")
    assert runner.main(["golden", "--db", str(db), "--cases", str(cases)]) == 1
    assert "Error: veri sürümü yok" in capsys.readouterr().err


def test_score_reports_and_exit_codes(fixture_eval, monkeypatch, capsys):
    runner = implementation("run_eval")
    db, cases, golden_dir, answers = fixture_eval
    monkeypatch.setattr(runner, "GOLDEN_DIR", golden_dir)
    assert runner.main(["golden", "--db", str(db), "--cases", str(cases)]) == 0
    answers.mkdir()
    (answers / "total.md").write_text("123.456,25 yolculuk", encoding="utf-8")
    args = ["score", "--answers", str(answers), "--cases", str(cases)]
    assert runner.main(args) == 2
    report = json.loads((answers / "score.json").read_text("utf-8"))
    assert report["passed"] == 1 and report["total"] == 2
    assert report["cases"]["scope"]["checks"][0]["detail"] == "cevap yok"
    assert "cevap yok" in (answers / "score.md").read_text("utf-8")
    (answers / "scope.md").write_text(
        "Kapsam sonu 2025-12-31, dün yok.", encoding="utf-8"
    )
    assert runner.main(args + ["--golden", str(next(golden_dir.glob("*.json")))]) == 0
    assert "passed=2/2" in capsys.readouterr().out


def test_ask_arguments_timeout_and_continuation(fixture_eval, monkeypatch):
    runner = implementation("run_eval")
    _, cases, _, answers = fixture_eval
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        if len(calls) == 1:
            raise subprocess.TimeoutExpired(args, 300)
        return subprocess.CompletedProcess(args, 0, "Kapsam yok", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert runner.main(["ask", "--answers", str(answers), "--cases", str(cases)]) == 0
    assert calls[0][0] == [
        "claude",
        "-p",
        "--agent",
        "ulasim",
        f"--allowedTools={runner.AGENT_TOOLS}",
        "Kaç yolculuk?",
    ]
    assert calls[0][1]["timeout"] == 300
    assert calls[0][1].get("shell", False) is False
    assert (answers / "total.md").read_text("utf-8").startswith("ERROR:")
    assert (answers / "scope.md").read_text("utf-8") == "Kapsam yok"


def test_ask_only_and_process_error(fixture_eval, monkeypatch):
    runner = implementation("run_eval")
    _, cases, _, answers = fixture_eval

    def fake_run(args, **kwargs):
        assert args == [
            "claude",
            "-p",
            "--agent",
            "custom",
            f"--allowedTools={runner.AGENT_TOOLS}",
            "Dün?",
        ]
        return subprocess.CompletedProcess(args, 1, "", "agent failed")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert (
        runner.main(
            [
                "ask",
                "--answers",
                str(answers),
                "--cases",
                str(cases),
                "--only",
                "scope",
                "--agent",
                "custom",
            ]
        )
        == 0
    )
    assert not (answers / "total.md").exists()
    assert "ERROR:" in (answers / "scope.md").read_text("utf-8")


def test_ask_workdir_isolates_agent_and_mcp(fixture_eval, monkeypatch, tmp_path):
    runner = implementation("run_eval")
    _, cases, _, answers = fixture_eval
    workdir = tmp_path / "clean"
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "Kapsam yok", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    args = ["ask", "--answers", str(answers), "--cases", str(cases)]
    assert runner.main(args + ["--only", "scope", "--workdir", str(workdir)]) == 0
    command, kwargs = calls[0]
    assert kwargs["cwd"] == workdir.resolve()
    assert kwargs["stdin"] is subprocess.DEVNULL
    assert command[:7] == [
        "claude",
        "-p",
        "--strict-mcp-config",
        "--mcp-config",
        str(workdir.resolve() / "mcp.json"),
        "--agents",
        str(workdir.resolve() / "agents.json"),
    ]
    assert command[-3:] == ["ulasim", f"--allowedTools={runner.AGENT_TOOLS}", "Dün?"]
    mcp = json.loads((workdir / "mcp.json").read_text("utf-8"))
    server = mcp["mcpServers"]["ulasim"]
    assert "<path-to-repo>" not in json.dumps(mcp)
    assert server["args"][-1] == f"{ROOT.as_posix()}/ulasim_mcp/server.py"
    agent = json.loads((workdir / "agents.json").read_text("utf-8"))["ulasim"]
    assert agent["tools"] == runner.AGENT_TOOLS.split(",")
    assert agent["model"] == "sonnet"
    assert agent["prompt"].startswith("Sen İstanbul raylı sistem")
    assert "<path-to-repo>" not in agent["prompt"]


def test_ask_without_workdir_keeps_current_directory(fixture_eval, monkeypatch):
    runner = implementation("run_eval")
    _, cases, _, answers = fixture_eval
    calls = []

    def fake_run(args, **kwargs):
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, "Kapsam yok", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    args = ["ask", "--answers", str(answers), "--cases", str(cases), "--only", "scope"]
    assert runner.main(args) == 0
    assert calls[0][1]["cwd"] is None
    assert "--strict-mcp-config" not in calls[0][0]


@pytest.mark.parametrize("repeat", [1, 3])
def test_ask_repeat_filenames_and_sequential_answers(fixture_eval, monkeypatch, repeat):
    runner = implementation("run_eval")
    _, cases, _, answers = fixture_eval
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args[-1])
        return subprocess.CompletedProcess(args, 0, f"answer {len(calls)}", "")

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    assert (
        runner.main(
            [
                "ask",
                "--cases",
                str(cases),
                "--answers",
                str(answers),
                "--only",
                "total",
                "--repeat",
                str(repeat),
            ]
        )
        == 0
    )
    expected = (
        ["total.md"]
        if repeat == 1
        else ["total__r1.md", "total__r2.md", "total__r3.md"]
    )
    assert sorted(p.name for p in answers.iterdir()) == expected
    assert [(answers / name).read_text("utf-8") for name in expected] == [
        f"answer {i}" for i in range(1, repeat + 1)
    ]
    assert calls == ["Kaç yolculuk?"] * repeat


@pytest.mark.parametrize("repeat", ["0", "-1"])
def test_ask_rejects_nonpositive_repeat(fixture_eval, repeat):
    _, cases, _, answers = fixture_eval
    assert (
        implementation("run_eval").main(
            [
                "ask",
                "--cases",
                str(cases),
                "--answers",
                str(answers),
                "--repeat",
                repeat,
            ]
        )
        == 1
    )
    assert not answers.exists()


@pytest.mark.parametrize("c,n,k,want", [(3, 3, 3, 1), (2, 3, 3, 0), (2, 3, 2, 1 / 3)])
def test_pass_hat_k(c, n, k, want):
    assert implementation("scoring").pass_hat_k(c, n, k) == pytest.approx(want)


def test_score_repeats_and_only(fixture_eval, monkeypatch):
    runner = implementation("run_eval")
    db, cases, golden_dir, answers = fixture_eval
    monkeypatch.setattr(runner, "GOLDEN_DIR", golden_dir)
    assert runner.main(["golden", "--db", str(db), "--cases", str(cases)]) == 0
    answers.mkdir()
    for i in range(1, 4):
        (answers / f"total__r{i}.md").write_text(
            "123456.25" if i < 3 else "ERROR: failed", encoding="utf-8"
        )
        (answers / f"scope__r{i}.md").write_text(
            "Kapsam sonu 2025-12-31, yok.", encoding="utf-8"
        )
    # A stale single-run file must not count as a fourth run.
    (answers / "total.md").write_text("123456.25", encoding="utf-8")
    args = ["score", "--cases", str(cases), "--answers", str(answers)]
    assert runner.main(args) == 2
    report = json.loads((answers / "score.json").read_text("utf-8"))
    total = report["cases"]["total"]
    assert (total["runs"], total["passes"], total["ok"]) == (3, 2, False)
    assert total["pass_rate"] == pytest.approx(2 / 3)
    assert total["pass_hat_k"] == 0
    assert report["cases"]["scope"]["pass_hat_k"] == 1
    assert report["mean_pass_at_1"] == pytest.approx(5 / 6)
    assert report["mean_pass_hat_k"] == 0.5
    assert "2/3" in (answers / "score.md").read_text("utf-8")
    assert runner.main(args + ["--only", "scope"]) == 0
    report = json.loads((answers / "score.json").read_text("utf-8"))
    assert report["total"] == report["passed"] == 1
    assert list(report["cases"]) == ["scope"]
    assert runner.main(args + ["--only", "unknown"]) == 1


def test_score_only_legacy_shape(fixture_eval, monkeypatch):
    runner = implementation("run_eval")
    db, cases, golden_dir, answers = fixture_eval
    monkeypatch.setattr(runner, "GOLDEN_DIR", golden_dir)
    runner.main(["golden", "--db", str(db), "--cases", str(cases)])
    answers.mkdir()
    (answers / "total.md").write_text("123456.25", encoding="utf-8")
    assert (
        runner.main(
            [
                "score",
                "--cases",
                str(cases),
                "--answers",
                str(answers),
                "--only",
                "total",
            ]
        )
        == 0
    )
    report = json.loads((answers / "score.json").read_text("utf-8"))
    assert set(report) == {"veri_surumu", "passed", "total", "cases"}
    assert set(report["cases"]["total"]) == {"ok", "checks"}
    assert (answers / "score.md").read_text("utf-8").endswith("- total: geçti\n")


@pytest.mark.parametrize(
    "quantity,ok",
    [
        ("", True),
        ("99999", True),
        ("100000", False),
        ("123.456", False),
        ("123,456", False),
        ("0,2 milyon", False),
    ],
)
def test_forbid_large_numbers_without_refusal_date(quantity, ok):
    checks = evaluate(
        f"2024 için eşleşen istasyon bulunamadı. {quantity}",
        forbid_large_numbers=True,
        must_mention=["(?i)bulunamad|yok|eşleşen"],
    )
    assert all(check["ok"] for check in checks) is ok


@pytest.mark.parametrize(
    "answer,want",
    [
        ("%12,345", 12.345),
        ("12.345%", 12.345),
        ("yüzde -12,345", -12.345),
        ("%34,5", 34.5),
    ],
)
def test_percentage_decimal_precision(answer, want):
    numbers = implementation("scoring").extract_numbers(answer)
    assert len(numbers) == 1
    assert numbers[0].value == pytest.approx(want)


def test_new_cases_reference_sql_in_memory():
    cases = implementation("run_eval").load_cases(ROOT / "evals/cases.toml")
    # These four earlier cases share this fixture; hourly has its own fixture.
    new = {case["id"]: case for case in cases[10:14]}
    assert list(new) == [
        "station_not_found",
        "ambiguous_station_yenimahalle",
        "hourly_out_of_scope",
        "yoy_percentage_m2",
    ]
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE SCHEMA marts")
        con.execute("CREATE TABLE marts.dim_station_group(station_group VARCHAR)")
        con.execute(
            "INSERT INTO marts.dim_station_group VALUES ('Yenimahalle'), ('Yenimahalle (Marmaray)'), ('Yeni Mahalle'), ('Other')"
        )
        con.execute("CREATE TABLE marts.fct_station_daily(ride_date DATE)")
        con.execute(
            "INSERT INTO marts.fct_station_daily VALUES ('2021-01-01'), ('2025-12-31')"
        )
        con.execute(
            "CREATE TABLE marts.fct_line_daily(ride_date DATE, line_name VARCHAR, journey_count_estimated BIGINT, journey_count BIGINT)"
        )
        con.execute(
            "INSERT INTO marts.fct_line_daily VALUES ('2024-01-01', 'M2-YENIKAPI-HACIOSMAN', 100, 900), ('2024-12-31', 'M2-YENIKAPI-HACIOSMAN', 100, 900), ('2025-01-01', 'M2-YENIKAPI-HACIOSMAN', 250, 900), ('2025-01-01', 'M4', 999, 999), ('2026-01-01', 'M2-YENIKAPI-HACIOSMAN', 999, 999)"
        )
        references = {}
        for key, case in new.items():
            cursor = con.execute(case["reference_sql"])
            references[key] = {
                "columns": [c[0] for c in cursor.description],
                "rows": cursor.fetchall(),
            }
    assert references["station_not_found"]["rows"] == []
    assert {row[0] for row in references["ambiguous_station_yenimahalle"]["rows"]} == {
        "Yenimahalle",
        "Yenimahalle (Marmaray)",
        "Yeni Mahalle",
    }
    assert [str(v) for v in references["hourly_out_of_scope"]["rows"][0]] == [
        "2021-01-01",
        "2025-12-31",
    ]
    percentage = new["yoy_percentage_m2"]
    ref = references[percentage["id"]]
    assert ref["rows"][0][ref["columns"].index(percentage["numbers"][0])] == 25
    scoring = implementation("scoring")
    assert all(
        c["ok"] for c in scoring.score_answer(percentage, "%25,0 artış", ref, "v")
    )
    assert not all(
        c["ok"] for c in scoring.score_answer(percentage, "%50 artış", ref, "v")
    )


@pytest.mark.parametrize(
    "answer",
    [
        "M2 hattının (Yenikapı–Hacıosman) 2025 yolculuk sayısı 2024'e göre **yüzde 5,51 azaldı**.",
        "M2 (Yenikapı–Hacıosman) hattının yolculuk sayısı 2025'te 2024'e göre **yüzde 5,51 azaldı**.",
        "M2 hattının 2025 toplam yolculuğu 2024'e göre **yaklaşık %5,51 azaldı**.",
    ],
)
def test_signed_by_words_real_answer_excerpts(answer):
    cases = implementation("run_eval").load_cases(ROOT / "evals/cases.toml")
    case = next(c for c in cases if c["id"] == "yoy_percentage_m2")
    reference = {"columns": ["percentage_change"], "rows": [[-5.514670632430195]]}
    assert all(
        c["ok"]
        for c in implementation("scoring").score_answer(case, answer, reference, "v")
    )


@pytest.mark.parametrize(
    "answer,expected,enabled,ok",
    [
        ("yüzde 5,51 arttı", -5.5147, True, False),
        ("yüzde 5,51 azaldı ve arttı", -5.5147, True, False),
        ("yüzde 5,51", -5.5147, True, False),
        ("yüzde 5,51 azaldı", -5.5147, None, False),
        ("yüzde 5,51 azaldı", -5.5147, False, False),
        ("yüzde -5,51 arttı", -5.5147, True, True),
        ("yüzde -5,51", -5.5147, None, True),
        ("yüzde 5,51 düştü", -5.5147, True, True),
        ("yüzde 5,51 geriledi", -5.5147, True, True),
        ("eksi yüzde 5,51", -5.5147, True, True),
        ("yüzde 5,51 yükseldi", -5.5147, True, False),
        ("yüzde 5,51 arttı", 5.5147, True, True),
        ("yüzde 5,51 yükseldi", 5.5147, True, True),
        ("yüzde 5,51 azaldı", 5.5147, True, False),
        ("yüzde 5,51 arttı ve düştü", 5.5147, True, False),
        ("yüzde 5,51", 5.5147, None, True),
        ("yüzde 9,51 azaldı", -5.5147, True, False),
        (
            "yüzde 5,51 azaldı. 2024 artık yıl olduğundan 366 günlük.",
            -5.5147,
            True,
            True,
        ),
    ],
)
def test_signed_by_words_direction_and_opt_in(answer, expected, enabled, ok):
    case = {"numbers": ["change"]}
    if enabled is not None:
        case["signed_by_words"] = enabled
    checks = implementation("scoring").score_answer(
        case, answer, {"columns": ["change"], "rows": [[expected]]}, "v"
    )
    assert all(c["ok"] for c in checks) is ok


def test_station_not_found_accepts_bulamadim():
    cases = implementation("run_eval").load_cases(ROOT / "evals/cases.toml")
    case = next(c for c in cases if c["id"] == "station_not_found")
    checks = implementation("scoring").score_answer(
        case,
        'Veride "Zeytinlik Meydanı" adında bir istasyon bulamadım, '
        "bu yüzden 2024 için bir yolculuk sayısı veremiyorum.",
        {"columns": ["station_group"], "rows": []},
        "v",
    )
    assert all(c["ok"] for c in checks)


@pytest.mark.parametrize(
    "text,want",
    [
        ("-%5,51", -5.51),
        ("+%5,51", 5.51),
        ("%-5,51", -5.51),
        ("%5-%6", 6),
        ("2024-%5", 5),
    ],
)
def test_review_percentage_sign(text, want):
    scoring = implementation("scoring")
    assert scoring.extract_numbers(text)[-1].value == pytest.approx(want)
    if want < 0:
        assert scoring.number_matches(text, -5.5147, True)
        assert not scoring.number_matches(text, 5.51)


@pytest.mark.parametrize(
    "answer,ok",
    [
        (
            "M2 hattı 2025'te %5,51 büyüdü (Not: bazı günlerde veri eksikliği var).",
            False,
        ),
        ("M2 hattı değişimi %5,51 oldu. Öte yandan 2023'te seferler azaldı.", False),
        ("yüzde 5,51 azaldı. Yaz aylarında artış görüldü.", True),
        ("yüzde 5,51 azaldı\n```sql\nSELECT 1 AS artis_yuzdesi\n```", True),
        ("%5,51 düşük", False),
        ("%5,51 düşün", False),
        ("%5,51 eksik", False),
        ("EKSİ yüzde 5,51", True),
        ("%5.51 azaldı! Artış oldu.", True),
        ("%5,51 oldu? Azaldı.", False),
        ("%5,51 oldu\nAzaldı.", False),
        ("1.234.567 yolculukta %5,51 azaldı.", True),
        ("```\n%5,51 azaldı\n```\nSayı veremiyorum.", False),
    ],
)
def test_review_direction_sentence(answer, ok):
    assert implementation("scoring").number_matches(answer, -5.5147, True) is ok


@pytest.mark.parametrize(
    "text", ["2021.01.01 - 2025.12.31", "1.1.2024", "01.01.2024", "123456abcdef"]
)
def test_review_dates_and_identifiers_not_large(text):
    checks = implementation("scoring").score_answer(
        {"forbid_large_numbers": True, "refusal": True},
        f"Veri kapsamı 2025-12-31; {text}; istasyon bulunmuyor.",
        {"columns": ["last"], "rows": [["2025-12-31"]]},
        "v",
    )
    assert all(c["ok"] for c in checks)


@pytest.mark.parametrize(
    "text,want",
    [
        ("217 bin", 217000),
        ("217k", 217000),
        ("12.345", 12345),
        ("% 12,345", 12.345),
        ("12,345 %", 12.345),
        ("Yüzde 12,345", 12.345),
    ],
)
def test_review_preserves_quantity_forms(text, want):
    assert implementation("scoring").extract_numbers(text)[0].value == pytest.approx(
        want
    )


@pytest.mark.parametrize(
    "field", ["signed_by_words", "forbid_large_numbers", "require_hourly_version"]
)
def test_review_rejects_string_boolean(tmp_path, field):
    text = (ROOT / "evals/cases.toml").read_text("utf-8")
    text = text.replace("[[case]]", f'[[case]]\n{field} = "false"', 1)
    path = tmp_path / "cases.toml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(TypeError, match=field):
        implementation("run_eval").load_cases(path)


def test_review_rejects_empty_only():
    with pytest.raises(ValueError):
        implementation("run_eval").select_cases([{"id": "total"}], "")


@pytest.mark.parametrize("c,n,k", [(1, 1, 2), (2, 1, 1), (0, 1, 0)])
def test_review_pass_hat_k_invalid(c, n, k):
    with pytest.raises(ValueError):
        implementation("scoring").pass_hat_k(c, n, k)


def test_review_pass_hat_k_zero_successes():
    assert implementation("scoring").pass_hat_k(0, 3, 3) == 0
