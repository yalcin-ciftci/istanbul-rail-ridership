import json
from datetime import datetime

import duckdb
import pytest
import rail_station_daily


@pytest.mark.parametrize(
    "resource_id", ["../evil", "..\\evil", "a/b", ".", "", "x.jsonl", "-leading"]
)
def test_unsafe_resource_id_is_rejected_before_network(server, resource_id):
    with pytest.raises(SystemExit) as exc:
        server.run(resource_id)
    assert exc.value.code == 2
    assert server.requests == []
    assert not list((server.root / "data").rglob("*.jsonl"))
    assert not (server.root.parent / "evil.jsonl").exists()


def test_default_resource_id_is_accepted():
    assert (
        rail_station_daily.resource_id_arg(rail_station_daily.DEFAULT_RESOURCE)
        == rail_station_daily.DEFAULT_RESOURCE
    )


def test_paginated_success(server):
    assert server.run() == 0
    rows = server.rows()
    assert len(rows) == 5
    assert len({row[0] for row in rows}) == 5
    assert {row[12] for row in rows} == {"resource-a"}
    assert {row[13] for row in rows} == {datetime(2026, 2, 3, 10, 41, 33, 518571)}
    assert all(row[14] is not None for row in rows)
    assert server.requests == [("resource-a", 0), ("resource-a", 2), ("resource-a", 4)]
    landing = server.root / "data/landing/rail_station_daily/resource-a.jsonl"
    lines = landing.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 5
    assert [json.loads(line) for line in lines] == server.resources["resource-a"]
    assert not list(server.root.rglob("*.tmp"))


def test_rerun_does_not_duplicate(server):
    assert server.run() == 0
    assert server.run() == 0
    assert len(server.rows()) == 5


def test_other_resource_is_preserved(server):
    server.add("resource-a", 3)
    server.add("resource-b", 2)
    assert server.run("resource-a") == 0
    assert server.run("resource-b") == 0
    before_b = [row for row in server.rows() if row[12] == "resource-b"]
    assert server.run("resource-a") == 0
    rows = server.rows()
    assert sum(row[12] == "resource-a" for row in rows) == 3
    assert sum(row[12] == "resource-b" for row in rows) == 2
    assert [row for row in rows if row[12] == "resource-b"] == before_b


def test_missing_resource_preserves_data(server):
    assert server.run() == 0
    before = server.rows()
    assert server.run("missing-resource") == 1
    assert server.rows() == before


def test_row_count_mismatch_preserves_data(server):
    assert server.run() == 0
    before = server.rows()
    server.resources["resource-a"].pop()
    assert server.run() == 1
    assert server.requests[-1] == ("resource-a", 4)
    assert server.rows() == before


def test_duplicate_id_preserves_data(server):
    assert server.run() == 0
    before = server.rows()
    server.resources["resource-a"][1]["_id"] = 1
    assert server.run() == 1
    assert server.rows() == before


def test_5xx_is_retried(server):
    server.statuses = [503, 200]
    assert server.run() == 0
    assert len(server.sleeps) == 1
    assert server.requests[:2] == [("resource-a", 0), ("resource-a", 0)]
    assert len(server.rows()) == 5


def test_4xx_is_not_retried(server):
    server.statuses = [404]
    assert server.run() == 1
    assert len(server.requests) == 1
    assert len(server.sleeps) == 0


def test_persistent_network_error_preserves_data(server):
    assert server.run() == 0
    before = server.rows()
    server.requests.clear()
    server.connect_error = True
    assert server.run() == 1
    assert len(server.sleeps) == 2
    assert len(server.requests) == 3
    assert server.rows() == before


def test_success_false_is_rejected(server):
    server.success = False
    assert server.run() == 1


def test_turkish_characters_are_preserved(server):
    assert server.run() == 0
    with duckdb.connect(str(server.db), read_only=True) as connection:
        towns = connection.execute("SELECT town FROM raw.rail_station_daily").fetchall()
    assert {row[0] for row in towns} == {"EyüpSultan", "Bakırköy"}
    assert all("\ufffd" not in row[0] for row in towns)


def test_table_schema(server):
    assert server.run() == 0
    with duckdb.connect(str(server.db), read_only=True) as connection:
        columns = connection.execute(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            ["raw", "rail_station_daily"],
        ).fetchall()
    assert columns == [
        ("_id", "BIGINT"),
        ("transaction_year", "VARCHAR"),
        ("transaction_month", "VARCHAR"),
        ("transaction_day", "VARCHAR"),
        ("line", "VARCHAR"),
        ("station_name", "VARCHAR"),
        ("station_number", "VARCHAR"),
        ("town", "VARCHAR"),
        ("longitude", "VARCHAR"),
        ("latitude", "VARCHAR"),
        ("passage_cnt", "VARCHAR"),
        ("passanger_cnt", "VARCHAR"),
        ("resource_id", "VARCHAR"),
        ("source_last_modified", "TIMESTAMP"),
        ("ingested_at", "TIMESTAMP WITH TIME ZONE"),
    ]
