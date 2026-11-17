"""Offline schema bootstrap and safeguards for the CI database."""

import importlib.util
import subprocess
import sys
from pathlib import Path

import duckdb
import hourly_transport
import pytest
import rail_station_daily
import yaml

ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools/init_empty_db.py"


def source_tables():
    tables = set()
    for path in (ROOT / "transform/models").rglob("*.yml"):
        for source in yaml.safe_load(path.read_text(encoding="utf-8")).get(
            "sources", []
        ):
            for table in source["tables"]:
                tables.add((source.get("schema", source["name"]), table["name"]))
    return tables


def run_tool(db):
    return subprocess.run(
        [sys.executable, str(TOOL), "--db", str(db)],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )


def columns(connection, schema, table):
    return connection.execute(
        "SELECT column_name, data_type, is_nullable FROM information_schema.columns "
        "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
        [schema, table],
    ).fetchall()


def test_daily_ingest_schema_before_refactor(server):
    assert server.run() == 0
    with duckdb.connect(str(server.db), read_only=True) as connection:
        assert columns(connection, "raw", "rail_station_daily") == [
            *(
                (name, kind, "YES")
                for name, kind in rail_station_daily.API_COLUMNS.items()
            ),
            ("resource_id", "VARCHAR", "YES"),
            ("source_last_modified", "TIMESTAMP", "YES"),
            ("ingested_at", "TIMESTAMP WITH TIME ZONE", "YES"),
        ]


@pytest.mark.parametrize("existing_empty_file", [False, True])
def test_creates_all_source_tables_empty(tmp_path, existing_empty_file):
    db = tmp_path / "ci database.duckdb"
    if existing_empty_file:
        db.touch()
    result = run_tool(db)
    assert result.returncode == 0, result.stderr
    with duckdb.connect(str(db), read_only=True) as connection:
        actual = set(
            connection.execute(
                "SELECT table_schema, table_name FROM information_schema.tables "
                "WHERE table_schema = 'raw'"
            ).fetchall()
        )
        assert actual == source_tables()
        for schema, table in actual:
            # Identifiers originate only from local database metadata.
            assert connection.execute(
                f'SELECT count(*) FROM "{schema}"."{table}"'
            ).fetchone() == (0,)


def test_rejects_nonempty_existing_file(tmp_path):
    db = tmp_path / "existing.duckdb"
    original = b"existing contents must survive"
    db.write_bytes(original)
    result = run_tool(db)
    assert result.returncode == 1
    assert "not empty" in result.stderr
    assert str(db) in result.stderr
    assert db.read_bytes() == original


@pytest.mark.parametrize(
    "db",
    [ROOT / "data/ulasim.duckdb", ROOT.parent / "istanbul-ulasim/data/ulasim.duckdb"],
)
def test_rejects_real_db_before_connect(db, monkeypatch, capsys):
    assert TOOL.is_file(), "bootstrap tool is missing"
    spec = importlib.util.spec_from_file_location("init_empty_db", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def forbidden_connect(*args, **kwargs):
        pytest.fail("Real database must never be opened")

    monkeypatch.setattr(module.duckdb, "connect", forbidden_connect)
    assert module.main(["--db", str(db)]) == 1
    assert "real database" in capsys.readouterr().err


def test_columns_match_ingest_definitions(tmp_path, server):
    assert server.run() == 0
    with duckdb.connect(str(server.db)) as expected:
        hourly_transport.create_tables(expected)
        expected_columns = {
            (schema, table): columns(expected, schema, table)
            for schema, table in source_tables()
        }
    db = tmp_path / "ci.duckdb"
    result = run_tool(db)
    assert result.returncode == 0, result.stderr
    with duckdb.connect(str(db), read_only=True) as actual:
        for (schema, table), definition in expected_columns.items():
            assert columns(actual, schema, table) == definition
