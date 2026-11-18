"""Measure metadata and generated consumer synchronization checks (no database)."""

import importlib.util
import subprocess
import sys
import tomllib
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = ROOT / "metrics" / "metrics.toml"
RENDERER = ROOT / "tools" / "render_agent_rules.py"
AGENT = ROOT / "examples" / "claude-code" / "ulasim.md"


def contract():
    assert CONTRACT.is_file(), "Metric contract is missing"
    return tomllib.loads(CONTRACT.read_text(encoding="utf-8"))


def test_contract_schema():
    data = contract()
    assert type(data["version"]) is int
    assert data["version"] == 2
    assert next(rule for rule in data["rule"] if rule["id"] == "data_version") == {
        "id": "data_version",
        "text_tr": "Sayı verdiğin cevabın sonunda kullandığın veri sürümünü yaz (sorgu sonucundaki veri_surumu kimliği). Kimlik boşsa bunu söyle; aynı soru aynı kimlikte aynı sonucu vermelidir.",
    }
    for key in ("metric", "rule"):
        ids = [entry["id"] for entry in data[key]]
        assert len(ids) == len(set(ids))
        assert all(isinstance(value, str) and value.strip() for value in ids)
    assert sum(item["role"] == "default" for item in data["metric"]) == 1
    roles = {item["id"]: item["role"] for item in data["metric"]}
    assert roles["journey_count_estimated"] == "default"
    assert roles["journey_count"] == "source"
    assert roles["passenger_count"] == "restricted"
    for metric in data["metric"]:
        assert metric["role"] in {
            "default",
            "source",
            "restricted",
            "analysis",
            "interpretation",
        }
        for field in ("label_tr", "definition_tr", "description_en"):
            assert isinstance(metric[field], str) and metric[field].strip()
        assert metric["tables"] and all(metric["tables"])
        assert len(metric["tables"]) == len(set(metric["tables"]))
    assert {
        "default_metric",
        "suspect_warning",
        "hourly_override",
        "passenger_not_people",
        "station_group",
        "line_level",
        "district",
        "weekend",
        "source_license",
    } <= {rule["id"] for rule in data["rule"]}
    assert all(rule["text_tr"].strip() for rule in data["rule"])


def test_marts_descriptions_and_sql_match_contract():
    models = {}
    for filename in ("_marts.yml", "_station_groups.yml", "_hour_profile.yml"):
        document = yaml.safe_load(
            (ROOT / "transform" / "models" / "marts" / filename).read_text(
                encoding="utf-8"
            )
        )
        for model in document["models"]:
            assert model["name"] not in models
            models[model.get("config", {}).get("alias", model["name"])] = {
                col["name"]: col.get("description") for col in model["columns"]
            }
    for metric in contract()["metric"]:
        column = metric.get("column", metric["id"])
        actual_tables = {name for name, cols in models.items() if column in cols}
        assert actual_tables == set(metric["tables"])
        for table in metric["tables"]:
            assert models[table][column] == metric["description_en"]
            sql = (
                ROOT
                / "transform"
                / "models"
                / "marts"
                / f"{'mart_station_group_hour_profile' if table == 'fct_station_group_hour_profile' else table}.sql"
            ).read_text(encoding="utf-8")
            assert column in sql


def test_agent_cli_check_from_other_directory(tmp_path):
    assert RENDERER.is_file(), "Agent rule renderer is missing"
    result = subprocess.run(
        [sys.executable, str(RENDERER), "--check"],
        check=False,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert result.returncode == 0, result.stderr


def test_agent_stale_block_check_and_render(tmp_path, monkeypatch):
    assert RENDERER.is_file(), "Agent rule renderer is missing"
    spec = importlib.util.spec_from_file_location("render_rules_under_test", RENDERER)
    renderer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(renderer)
    path = tmp_path / "agent.md"
    original = AGENT.read_text(encoding="utf-8")
    before, rest = original.split("<!-- metrics:begin -->")
    _, after = rest.split("<!-- metrics:end -->")
    stale = before + "<!-- metrics:begin -->\nstale\n<!-- metrics:end -->" + after
    path.write_text(stale, encoding="utf-8")
    monkeypatch.setattr(renderer, "AGENT_PATH", path)
    assert renderer.main(["--check"]) == 1
    assert path.read_text(encoding="utf-8") == stale
    assert renderer.main([]) == 0
    assert path.read_text(encoding="utf-8") == original
    assert renderer.main(["--check"]) == 0


def test_hourly_contract_and_generated_rules():
    data = contract()
    metrics = {m["id"]: m for m in data["metric"]}
    assert metrics["typical_hourly_passage"]["role"] == "analysis"
    assert metrics["relative_hourly_intensity"]["role"] == "interpretation"
    rules = {r["id"]: r["text_tr"] for r in data["rule"]}
    assert {
        "hourly_typical",
        "hourly_reliability",
        "hourly_scope",
        "hourly_relative",
    } <= rules.keys()
    block = (
        AGENT.read_text("utf-8")
        .split("<!-- metrics:begin -->")[1]
        .split("<!-- metrics:end -->")[0]
    )
    for text in rules.values():
        assert text in block


def test_daily_gap_rule_matches_mart_and_generated_agent():
    rules = {r["id"]: r["text_tr"] for r in contract()["rule"]}
    assert "daily_line_gap" in rules, "Daily line coverage warning is missing"
    document = yaml.safe_load(
        (ROOT / "transform/models/marts/_line_daily_gap.yml").read_text("utf-8")
    )
    model = next(
        m for m in document["models"] if m["name"] == "fct_line_daily_gap_year"
    )
    assert model["description"] == rules["daily_line_gap"]
    block = (
        AGENT.read_text("utf-8")
        .split("<!-- metrics:begin -->")[1]
        .split("<!-- metrics:end -->")[0]
    )
    assert rules["daily_line_gap"] in block
