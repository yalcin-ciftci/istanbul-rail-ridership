import importlib.util
import subprocess
from pathlib import Path

import duckdb
import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "publish_analysis_db.py"


def load_publisher():
    assert SCRIPT.is_file(), "Analysis database publisher has not been implemented"
    spec = importlib.util.spec_from_file_location("publish_analysis_db", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_source(path):
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE SCHEMA raw")
        con.execute("CREATE SCHEMA staging")
        con.execute("CREATE SCHEMA marts")
        con.execute("CREATE TABLE raw.x AS SELECT 999 AS value")
        con.execute("CREATE TABLE staging.x AS SELECT 888 AS value")
        con.execute('CREATE TABLE marts."İstasyon" AS SELECT 42 AS value')
        con.execute('CREATE VIEW marts."a""b" AS SELECT value FROM marts."İstasyon"')
        con.execute("CREATE TABLE marts.empty (value INTEGER)")


def test_publish_only_marts_materializes_views_and_preserves_source(tmp_path, capsys):
    publisher = load_publisher()
    source, target = tmp_path / "source.duckdb", tmp_path / "analysis.duckdb"
    make_source(source)
    original = source.read_bytes()
    temporary = Path(str(target) + ".tmp")
    temporary.write_bytes(b"stale temporary file")
    assert publisher.main(["--source", str(source), "--target", str(target)]) == 0
    assert source.read_bytes() == original
    assert not temporary.exists()
    with duckdb.connect(str(target), read_only=True) as con:
        assert con.execute(
            "SELECT table_schema, table_name, table_type FROM information_schema.tables "
            "ORDER BY table_name"
        ).fetchall() == [
            ("marts", 'a"b', "BASE TABLE"),
            ("marts", "empty", "BASE TABLE"),
            ("marts", "İstasyon", "BASE TABLE"),
        ]
        assert con.execute('SELECT value FROM marts."İstasyon"').fetchall() == [(42,)]
        assert con.execute('SELECT value FROM marts."a""b"').fetchall() == [(42,)]
        assert con.execute("SELECT count(*) FROM marts.empty").fetchone() == (0,)
        for schema in ("raw", "staging"):
            with pytest.raises(duckdb.Error):
                con.execute(f"SELECT * FROM {schema}.x")
    output = capsys.readouterr().out
    assert 'table=marts.a"b rows=1' in output
    assert "table=marts.İstasyon rows=1" in output
    assert "table=marts.empty rows=0" in output
    assert f"published={target} tables=3" in output


@pytest.mark.parametrize("failure", ["no_marts", "missing_source", "replace"])
def test_publish_failure_preserves_target_and_cleans_tmp(
    tmp_path, capsys, monkeypatch, failure
):
    publisher = load_publisher()
    source, target = tmp_path / "source.duckdb", tmp_path / "analysis.duckdb"
    with duckdb.connect(str(target)) as con:
        con.execute("CREATE SCHEMA marts")
        con.execute("CREATE TABLE marts.old AS SELECT 7 AS value")
    original = target.read_bytes()
    if failure == "no_marts":
        with duckdb.connect(str(source)) as con:
            con.execute("CREATE TABLE x AS SELECT 1 AS value")
    elif failure == "replace":
        make_source(source)

        def fail_replace(src, dst):
            raise OSError("target locked")

        monkeypatch.setattr(publisher.os, "replace", fail_replace)
    assert publisher.main(["--source", str(source), "--target", str(target)]) == 1
    assert capsys.readouterr().err.startswith("Error: ")
    assert target.read_bytes() == original
    assert not Path(str(target) + ".tmp").exists()
    if failure == "missing_source":
        assert not source.exists()


def test_cli_error_exit_code(tmp_path):
    assert SCRIPT.is_file(), "Analysis database publisher has not been implemented"
    result = subprocess.run(
        [
            "uv",
            "run",
            "--project",
            str(ROOT),
            "python",
            str(SCRIPT),
            "--source",
            str(tmp_path / "absent.duckdb"),
            "--target",
            str(tmp_path / "analysis.duckdb"),
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == 1
    assert result.stderr.startswith("Error: ")


def test_defaults_are_relative_to_repository_not_cwd(tmp_path, monkeypatch):
    publisher = load_publisher()
    # Relocate the module's repository anchor; never touch the real data directory.
    monkeypatch.setattr(publisher, "__file__", str(tmp_path / "tools" / SCRIPT.name))
    (tmp_path / "data").mkdir()
    make_source(tmp_path / "data" / "ulasim.duckdb")
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert publisher.main([]) == 0
    assert (tmp_path / "data" / "ulasim_analysis.duckdb").is_file()
