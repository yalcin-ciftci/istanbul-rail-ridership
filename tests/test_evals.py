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
        ("2025-12-31", 2025),
        ("31.12.2025", 2025),
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
        ("Kapsam sonu 31.12.2025.", True),
        ("31 Aralık 2025 sonrası bulunmuyor.", True),
        ("Kapsam sonu 2025-12-31, 123456 yolculuk.", False),
        ("Kapsam sonu 2025-12-31, 123.456 yolculuk.", False),
        ("Dün yok.", False),
        ("2025-12-31", False),
    ],
)
def test_refusal(answer, ok):
    assert all(check["ok"] for check in evaluate(answer, refusal=True)) is ok


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
