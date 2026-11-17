"""Hourly version identity and query provenance, without production data."""

import hashlib
import tomllib
from pathlib import Path

import duckdb
import pytest
from test_ulasim_mcp import load_server

server = load_server()


@pytest.fixture
def versions():
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE SCHEMA marts")
        con.execute(
            "CREATE TABLE marts.dim_source_version(dataset VARCHAR, resource_id VARCHAR, "
            "source_month VARCHAR, source_sha256 VARCHAR, stored_sha256 VARCHAR, "
            "ingested_at TIMESTAMP)"
        )
        con.execute(
            "INSERT INTO marts.dim_source_version VALUES "
            "('rail_station_daily', 'daily', NULL, 'dailyhash', 'stored', '2026-01-01')"
        )
        yield con


def test_hourly_version_absent_is_null_and_daily_identity_unchanged(versions):
    version = server._data_version(versions)
    assert version["kimlik"] == hashlib.sha256(b"daily||dailyhash").hexdigest()[:12]
    assert version["saatlik_kimlik"] is None
    assert version["saatlik_kaynak_sayisi"] is None


def test_hourly_version_sorted_fallback_and_independent_of_daily(versions):
    before = server._data_version(versions)
    versions.execute(
        "INSERT INTO marts.dim_source_version VALUES "
        "('rail_hourly', 'b', '2024-02', NULL, 'fallback', '2026-01-02'), "
        "('rail_hourly', 'a', '2024-01', 'source', 'unused', '2026-01-03')"
    )
    version = server._data_version(versions)
    assert (
        version["saatlik_kimlik"]
        == hashlib.sha256(b"a|2024-01|source\nb|2024-02|stored:fallback").hexdigest()[
            :12
        ]
    )
    assert version["saatlik_kaynak_sayisi"] == 2
    for key in ("kimlik", "kaynak_sayisi", "kaynak_hash_eksik", "son_alim"):
        assert version[key] == before[key]
    versions.execute(
        "UPDATE marts.dim_source_version SET source_sha256 = ? WHERE resource_id = ?",
        ["changed", "a"],
    )
    after = server._data_version(versions)
    assert after["saatlik_kimlik"] != version["saatlik_kimlik"]
    assert after["kimlik"] == before["kimlik"]


@pytest.mark.parametrize("hourly", [False, True])
@pytest.mark.parametrize("has_source", [False, True])
def test_query_hourly_version_only_for_profile(
    versions, monkeypatch, hourly, has_source
):
    versions.execute(
        "CREATE TABLE marts.fct_station_group_hour_profile AS SELECT 8 AS ride_hour"
    )
    if has_source:
        versions.execute(
            "INSERT INTO marts.dim_source_version VALUES "
            "('rail_hourly', 'h', '2024-01', 'hash', 'stored', '2026-01-02')"
        )
    monkeypatch.setattr(server, "_with_connection", lambda action: action(versions))
    sql = (
        "SELECT ride_hour FROM marts.FCT_STATION_GROUP_HOUR_PROFILE"
        if hourly
        else "SELECT 8 AS ride_hour"
    )
    result = server.sorgu(sql)
    assert result["satirlar"] == [[8]]
    assert result["veri_surumu"] == server._data_version(versions)["kimlik"]
    assert ("saatlik_veri_surumu" in result) is hourly
    if hourly:
        expected = hashlib.sha256(b"h|2024-01|hash").hexdigest()[:12]
        assert result["saatlik_veri_surumu"] == (expected if has_source else None)


def test_hourly_eval_reference_uses_weekday_peak_and_relative_index():
    root = Path(__file__).resolve().parents[1]
    cases = tomllib.loads((root / "evals/cases.toml").read_text("utf-8"))["case"]
    case = next((c for c in cases if c["id"] == "hourly_profile_yenikapi"), None)
    assert case is not None, "Hourly profile eval is missing"
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE SCHEMA marts")
        con.execute(
            "CREATE TABLE marts.fct_station_group_hour_profile (station_group VARCHAR, "
            "day_type VARCHAR, ride_hour INTEGER, typical_passage DOUBLE, relative_index DOUBLE)"
        )
        con.executemany(
            "INSERT INTO marts.fct_station_group_hour_profile VALUES (?, ?, ?, ?, ?)",
            [
                ("Yenikapı", "weekday", 8, 30, 0.15),
                ("Yenikapı", "weekday", 17, 100, 0.5),
                ("Yenikapı", "sunday", 12, 200, 1),
                ("Other", "weekday", 19, 300, 1),
            ],
        )
        cursor = con.execute(case["reference_sql"])
        assert cursor.fetchall() == [(17, 15.0)]
        assert case["numbers"] == [column[0] for column in cursor.description]
