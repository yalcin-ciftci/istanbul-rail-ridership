"""Exercise daily quality SQL with synthetic data, without the project database."""

from pathlib import Path

import duckdb
from jinja2 import Template

ROOT = Path(__file__).resolve().parents[1]


def test_quality_override_and_rule_precedence():
    sql = (
        ROOT / "transform/models/intermediate/int_journey_quality_daily.sql"
    ).read_text(encoding="utf-8")
    with duckdb.connect(":memory:") as con:
        con.execute(
            "CREATE TABLE int_rail_entrance_deduped "
            "(ride_date DATE, journey_count BIGINT, passenger_count BIGINT)"
        )
        con.execute(
            "INSERT INTO int_rail_entrance_deduped VALUES "
            "('2022-04-01', 210, 100), ('2022-10-31', 211, 100), "
            "('2022-11-08', 107, 100), ('2022-11-01', 210, 100)"
        )
        con.execute("CREATE TABLE journey_suspect_overrides (ride_date DATE)")
        con.execute(
            "INSERT INTO journey_suspect_overrides VALUES "
            "('2022-10-31'), ('2022-11-01')"
        )
        result = con.execute(Template(sql).render(ref=lambda name: name))
        columns = [column[0] for column in result.description]
        actual = {
            str(row[0]): (
                dict(zip(columns, row))["journey_suspect"],
                dict(zip(columns, row)).get("journey_suspect_source"),
            )
            for row in result.fetchall()
        }
    assert actual == {
        "2022-04-01": (True, "rule"),
        "2022-10-31": (True, "hourly_override"),
        "2022-11-08": (False, None),
        "2022-11-01": (True, "rule"),
    }


def test_override_validation_rejects_missing_and_non_2022_dates():
    sql = (ROOT / "transform/tests/assert_suspect_overrides_valid.sql").read_text(
        encoding="utf-8"
    )
    with duckdb.connect(":memory:") as con:
        con.execute("CREATE TABLE journey_suspect_overrides (ride_date DATE)")
        con.execute("CREATE TABLE int_journey_quality_daily (ride_date DATE)")
        con.execute(
            "INSERT INTO journey_suspect_overrides VALUES "
            "('2022-10-31'), ('2022-11-01'), ('2023-01-01'), (NULL)"
        )
        con.execute(
            "INSERT INTO int_journey_quality_daily VALUES "
            "('2022-10-31'), ('2023-01-01')"
        )
        rows = con.execute(Template(sql).render(ref=lambda name: name)).fetchall()
    assert {str(row[0]) for row in rows} == {"2022-11-01", "2023-01-01", "None"}
