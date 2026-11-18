"""Calendar mart SQL on synthetic inputs, without production data."""

from datetime import date
from pathlib import Path

import duckdb
from jinja2 import Environment

ROOT = Path(__file__).resolve().parents[1]


def test_calendar_spine_and_flags():
    path = ROOT / "transform/models/marts/dim_calendar_day.sql"
    assert path.exists(), "Calendar mart is missing"
    sql = (
        Environment()
        .from_string(path.read_text("utf-8"))
        .render(
            config=lambda **kwargs: "",
            ref=lambda name: name,
            var=lambda name, default: default,
        )
    )
    with duckdb.connect(":memory:") as con:
        con.execute("""CREATE TABLE int_calendar_day (
            ride_date DATE, event_types VARCHAR, labels VARCHAR,
            is_holiday BOOLEAN, is_half_day_holiday BOOLEAN,
            is_covid_restriction BOOLEAN, is_partial_covid_restriction BOOLEAN,
            has_special_event BOOLEAN)""")
        con.execute("""INSERT INTO int_calendar_day VALUES
            ('2024-01-01', 'public_holiday', 'Full', true, false, false, false, false),
            ('2024-01-02', 'religious_holiday', 'Half', false, true, false, false, false),
            ('2024-01-03', 'covid_restriction', 'Full', false, false, true, false, false),
            ('2024-01-04', 'covid_restriction,special_event', 'Partial', false, false, false, true, true)
        """)
        con.execute("""CREATE TABLE calendar_events (
            start_date DATE, end_date DATE, source_url VARCHAR)""")
        con.execute("""INSERT INTO calendar_events VALUES
            ('2024-01-01', '2024-01-02', 'https://example.org/b'),
            ('2024-01-01', '2024-01-01', 'https://example.org/a'),
            ('2024-01-01', '2024-01-01', 'https://example.org/a')""")
        con.execute("CREATE TABLE result AS " + sql)
        assert con.execute(
            "SELECT count(*), count(DISTINCT ride_date), min(ride_date), max(ride_date) FROM result"
        ).fetchone() == (1826, 1826, date(2021, 1, 1), date(2025, 12, 31))
        assert con.execute("""SELECT day_of_week_iso, is_weekend,
            is_holiday_full_day, is_holiday_half_day, is_restriction_full_day,
            is_restriction_partial, is_regular_workday FROM result
            WHERE ride_date BETWEEN '2024-01-01' AND '2024-01-07'
            ORDER BY ride_date""").fetchall() == [
            (1, False, True, False, False, False, False),
            (2, False, False, True, False, False, True),
            (3, False, False, False, True, False, False),
            (4, False, False, False, False, True, True),
            (5, False, False, False, False, False, True),
            (6, True, False, False, False, False, False),
            (7, True, False, False, False, False, False),
        ]
        assert con.execute(
            "SELECT is_public_holiday, is_religious_holiday, labels_tr, source_urls FROM result WHERE ride_date = '2024-01-01'"
        ).fetchone() == (
            True,
            False,
            "Full",
            "https://example.org/a | https://example.org/b",
        )
        assert con.execute(
            "SELECT is_religious_holiday, is_special_event FROM result WHERE ride_date = '2024-01-02'"
        ).fetchone() == (True, False)
        assert con.execute(
            "SELECT labels_tr, source_urls FROM result WHERE ride_date = '2024-01-05'"
        ).fetchone() == (None, None)
