"""Offline version backfill, mart projection and publication checks."""

import hashlib
import importlib.util
import json
from pathlib import Path

import duckdb
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
FIELDS = [
    "dataset",
    "resource_id",
    "source_month",
    "source_last_modified",
    "source_sha256",
    "stored_sha256",
    "stored_path",
    "row_count",
    "ingested_at",
    "hash_note",
]


def load_tool(name):
    path = ROOT / "tools" / f"{name}.py"
    assert path.is_file(), f"Missing tool: {name}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_backfill_legacy_hashes_and_preservation(server, monkeypatch, capsys):
    assert server.run() == 0
    module = load_tool("backfill_source_versions")
    monkeypatch.setattr(module, "PROJECT_ROOT", server.root)
    with duckdb.connect(str(server.db)) as con:
        ingested_version = con.execute("SELECT * FROM raw.source_versions").fetchall()
    assert module.main(["--db", "test.duckdb"]) == 0
    with duckdb.connect(str(server.db)) as con:
        assert (
            con.execute("SELECT * FROM raw.source_versions").fetchall()
            == ingested_version
        )
    landing = server.root / "data/landing/rail_station_daily/resource-a.jsonl"
    old = "".join(
        json.dumps(r, ensure_ascii=False) + "\n"
        for r in reversed(server.resources["resource-a"])
    ).encode()
    landing.write_bytes(old)
    parquet = server.root / "data/landing/hourly_transport/rail_2024-01.parquet"
    parquet.parent.mkdir(parents=True)
    with duckdb.connect(str(server.db)) as con:
        con.execute("DROP TABLE raw.source_versions")
        con.execute("CREATE TABLE raw.rail_hourly AS SELECT '2024-01' AS source_month")
        con.execute(
            "CREATE TABLE raw.hourly_transport_months AS SELECT '2024-01' AS month, 'hour' AS resource_id, TIMESTAMP '2026-11-15' AS source_last_modified, now() AS ingested_at, 'ok' AS status"
        )
        con.execute("COPY raw.rail_hourly TO ? (FORMAT parquet)", [str(parquet)])
    monkeypatch.chdir(server.root.parent)
    assert module.main(["--db", "test.duckdb"]) == 0
    expected = hashlib.sha256(
        "".join(
            json.dumps(r, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
            for r in server.resources["resource-a"]
        ).encode()
    ).hexdigest()
    assert landing.read_bytes() == old
    with duckdb.connect(str(server.db)) as con:
        rows = con.execute(
            "SELECT dataset, source_sha256, stored_sha256, row_count, hash_note FROM raw.source_versions ORDER BY dataset"
        ).fetchall()
        assert rows == [
            (
                "rail_hourly",
                None,
                hashlib.sha256(parquet.read_bytes()).hexdigest(),
                1,
                "kaynak CSV 2b öncesi alındı ve saklanmadı; yalnız Parquet parmak izi var",
            ),
            ("rail_station_daily", expected, hashlib.sha256(old).hexdigest(), 5, None),
        ]
        before = con.execute(
            "SELECT * FROM raw.source_versions ORDER BY dataset"
        ).fetchall()
    assert module.main(["--db", "test.duckdb"]) == 0
    with duckdb.connect(str(server.db)) as con:
        assert (
            con.execute("SELECT * FROM raw.source_versions ORDER BY dataset").fetchall()
            == before
        )
    output = capsys.readouterr().out
    assert "written=2 skipped_existing=0 missing_files=0" in output
    assert "written=1 skipped_existing=1 missing_files=0" in output
    landing.write_bytes(b"changed; known source must be preserved")
    assert module.main(["--db", "test.duckdb"]) == 0
    with duckdb.connect(str(server.db)) as con:
        assert (
            con.execute("SELECT * FROM raw.source_versions ORDER BY dataset").fetchall()
            == before
        )


@pytest.mark.parametrize("failure", ["missing_db", "missing_file", "invalid_landing"])
def test_backfill_failures(server, monkeypatch, capsys, failure):
    assert server.run() == 0
    module = load_tool("backfill_source_versions")
    monkeypatch.setattr(module, "PROJECT_ROOT", server.root)
    if failure == "missing_db":
        assert module.main(["--db", "missing.duckdb"]) == 1
        assert not (server.root / "missing.duckdb").exists()
        assert "Error:" in capsys.readouterr().err
        return
    with duckdb.connect(str(server.db)) as con:
        con.execute("DELETE FROM raw.source_versions")
    landing = server.root / "data/landing/rail_station_daily/resource-a.jsonl"
    if failure == "missing_file":
        landing.rename(landing.with_suffix(".old"))
    else:
        # A later resource fails after resource-a has already been written.
        server.add("resource-b", 1)
        assert server.run("resource-b") == 0
        with duckdb.connect(str(server.db)) as con:
            con.execute("DELETE FROM raw.source_versions")
        bad = landing.with_name("resource-b.jsonl")
        bad.write_text("not JSON\n", encoding="utf-8")
    assert module.main(["--db", "test.duckdb"]) == (
        0 if failure == "missing_file" else 1
    )
    with duckdb.connect(str(server.db)) as con:
        assert con.execute("SELECT * FROM raw.source_versions").fetchall() == []
    if failure == "missing_file":
        assert "missing_files=1" in capsys.readouterr().out


def test_source_mart_and_publish(server):
    assert server.run() == 0
    model = ROOT / "transform/models/marts/dim_source_version.sql"
    assert model.is_file()
    sql = model.read_text(encoding="utf-8").replace(
        "{{ source('raw', 'source_versions') }}", "raw.source_versions"
    )
    source = yaml.safe_load(
        (ROOT / "transform/models/staging/_sources.yml").read_text(encoding="utf-8")
    )
    table = next(
        t for t in source["sources"][0]["tables"] if t["name"] == "source_versions"
    )
    assert [c["name"] for c in table["columns"]] == FIELDS
    marts = yaml.safe_load(
        (ROOT / "transform/models/marts/_marts.yml").read_text(encoding="utf-8")
    )
    mart = next(t for t in marts["models"] if t["name"] == "dim_source_version")
    assert [c["name"] for c in mart["columns"]] == FIELDS
    assert all(c["description"] for c in mart["columns"])
    assert {
        "accepted_values": {
            "arguments": {"values": ["rail_station_daily", "rail_hourly"]}
        }
    } in mart["columns"][0]["data_tests"]
    key = mart["data_tests"][0]["unique"]["arguments"]["column_name"]
    for c in mart["columns"]:
        if c["name"] in {
            "dataset",
            "resource_id",
            "stored_sha256",
            "row_count",
            "ingested_at",
        }:
            assert "not_null" in c["data_tests"]
    with duckdb.connect(str(server.db)) as con:
        con.execute("CREATE SCHEMA marts")
        con.execute("CREATE TABLE marts.dim_source_version AS " + sql)
        assert [
            r[0] for r in con.execute("DESCRIBE marts.dim_source_version").fetchall()
        ] == FIELDS
        expected = con.execute("SELECT * FROM raw.source_versions").fetchall()
        # Exercise the configured built-in unique expression, including NULL month.
        check = (
            f"SELECT {key} AS unique_field, count(*) AS n "
            f"FROM marts.dim_source_version WHERE {key} IS NOT NULL "
            f"GROUP BY {key} HAVING count(*) > 1"
        )
        assert con.execute(check).fetchall() == []
        con.execute("BEGIN")
        con.execute(
            "INSERT INTO marts.dim_source_version SELECT * FROM marts.dim_source_version"
        )
        assert con.execute(check).fetchone()[1] == 2
        con.execute("ROLLBACK")
    target = server.root / "analysis.duckdb"
    assert load_tool("publish_analysis_db").publish(server.db, target) == 1
    with duckdb.connect(str(target)) as con:
        assert (
            con.execute("SELECT * FROM marts.dim_source_version").fetchall() == expected
        )
