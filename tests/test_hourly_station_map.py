import csv
import importlib.util
from pathlib import Path

import duckdb
import pytest


def generator():
    path = Path(__file__).resolve().parents[1] / "tools/build_hourly_station_map.py"
    assert path.exists(), "Hourly station mapping is not implemented"
    spec = importlib.util.spec_from_file_location("hourly_map", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("name", "expected"),
    [("İ ı Ş ş Ğ ğ Ü ü Ö ö Ç ç", "IISSGGUUOOCC"), (" Yenikapı-(2). ", "YENIKAPI2")],
)
def test_normalize_turkish_and_punctuation(name, expected):
    assert generator().normalize_station_key(name) == expected


@pytest.fixture
def connection():
    with duckdb.connect(":memory:") as con:
        con.execute("create schema raw")
        con.execute("create schema marts")
        con.execute(
            "create table raw.rail_hourly(line_name varchar, station_poi_desc_cd varchar, road_type varchar)"
        )
        con.execute(
            "create table marts.dim_station(station_code varchar, station_name varchar, station_group varchar)"
        )
        con.execute(
            "create table marts.fct_station_daily(station_code varchar, line_name varchar)"
        )
        con.executemany(
            "insert into marts.dim_station values (?, ?, ?)",
            [
                ("A", "Yenikapı", "Yenikapı"),
                ("B", "Yenimahalle", "Yenimahalle"),
                ("C", "Yenimahalle", "Yenimahalle (Marmaray)"),
                ("D", "Yenimahalle", "Yeni Mahalle"),
            ],
        )
        con.executemany(
            "insert into marts.fct_station_daily values (?, ?)",
            [("B", "daily M1"), ("C", "daily rail"), ("B", "daily tcdd-y")],
        )
        con.executemany(
            "insert into raw.rail_hourly values (?, ?, ?)",
            [
                ("M1", "YENİKAPI", "RAYLI"),
                ("M1", "YENIMAHALLE", "RAYLI"),
                ("TCDD-X", "YENIMAHALLE", "RAYLI"),
                ("TCDD-Y", "YENIMAHALLE", "RAYLI"),
                ("MARMARAY", "YENIMAHALLE", "RAYLI"),
                ("X", "YENIMAHALLE", "RAYLI"),
                ("M9", "UNKNOWN", "RAYLI"),
                ("M1", None, "RAYLI"),
                ("M1", "BUS", "KARAYOLU"),
                ("M1", "YENİKAPI", "RAYLI"),
            ],
        )
        yield con


def test_mapping_methods_line_context_and_null_exclusion(connection):
    rows = generator().build_rows(
        connection,
        {
            "M1": "daily M1",
            "TCDD-X": "daily rail",
            "TCDD-Y": "daily tcdd-y",
            "MARMARAY": "daily marmaray",
        },
    )
    actual = {
        (r["line_code"], r["station_key"]): (r["station_group"], r["match_method"])
        for r in rows
    }
    assert actual == {
        ("M1", "YENIKAPI"): ("Yenikapı", "auto"),
        ("M1", "YENIMAHALLE"): ("Yenimahalle", "auto_line"),
        ("TCDD-X", "YENIMAHALLE"): ("Yenimahalle (Marmaray)", "auto_line"),
        # Non-Marmaray TCDD lines resolve only through their own daily line.
        ("TCDD-Y", "YENIMAHALLE"): ("Yenimahalle", "auto_line"),
        # MARMARAY prefers the "(Marmaray)" group when daily membership is empty.
        ("MARMARAY", "YENIMAHALLE"): ("Yenimahalle (Marmaray)", "auto_line"),
        ("X", "YENIMAHALLE"): ("", "ambiguous"),
        ("M9", "UNKNOWN"): ("", "unmapped"),
    }


def test_manual_survives_and_output_is_deterministic(connection, tmp_path):
    module = generator()
    out = tmp_path / "map.csv"
    manual = dict(
        zip(
            module.FIELDS,
            ["M9", "UNKNOWN", "UNKNOWN", "Reviewed", "manual", "human evidence"],
            strict=True,
        )
    )
    with out.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=module.FIELDS)
        writer.writeheader()
        writer.writerow(manual)
    module.write_mapping(connection, {"M1": "daily M1"}, out)
    before = out.read_bytes()
    module.write_mapping(connection, {"M1": "daily M1"}, out)
    assert out.read_bytes() == before
    with out.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    assert manual in rows
    assert [(r["line_code"], r["station_key"]) for r in rows] == sorted(
        (r["line_code"], r["station_key"]) for r in rows
    )


def test_reviewed_note_survives_when_outcome_unchanged(connection, tmp_path):
    module = generator()
    out = tmp_path / "map.csv"
    module.write_mapping(connection, {"M1": "daily M1"}, out)
    with out.open(encoding="utf-8", newline="") as stream:
        rows = list(csv.DictReader(stream))
    for row in rows:
        if (row["line_code"], row["station_key"]) == ("M9", "UNKNOWN"):
            row["note"] = "Reviewed: no daily counterpart on this line."
        if (row["line_code"], row["station_key"]) == ("M1", "YENIKAPI"):
            row["note"] = "stale note"
            row["station_group"] = "Old group"
    with out.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=module.FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
    module.write_mapping(connection, {"M1": "daily M1"}, out)
    with out.open(encoding="utf-8", newline="") as stream:
        result = {(r["line_code"], r["station_key"]): r for r in csv.DictReader(stream)}
    # Same method and group: the reviewed note is kept.
    assert (
        result["M9", "UNKNOWN"]["note"]
        == "Reviewed: no daily counterpart on this line."
    )
    # Outcome changed: the regenerated row (and its note) wins.
    assert result["M1", "YENIKAPI"]["station_group"] == "Yenikapı"
    assert result["M1", "YENIKAPI"]["note"] == ""


def test_cli_reports_missing_db_without_creating_it(tmp_path, capsys):
    db = tmp_path / "missing.duckdb"
    assert generator().main(["--db", str(db), "--out", str(tmp_path / "out.csv")]) == 1
    assert not db.exists()
    assert "failed" in capsys.readouterr().err.lower()


def render_model(name):
    from jinja2 import Environment

    root = Path(__file__).resolve().parents[1] / "transform"
    path = root / "models/hourly" / f"{name}.sql"
    assert path.exists(), f"Hourly model not implemented: {name}"
    env = Environment()
    env.globals.update(
        ref=lambda name: name,
        source=lambda schema, name: (
            f"{'raw' if schema == 'raw_hourly' else schema}.{name}"
        ),
        config=lambda **kw: "",
    )
    macro = root / "macros/normalize_station_key.sql"
    if macro.exists():
        env.globals["normalize_station_key"] = env.from_string(
            macro.read_text(encoding="utf-8-sig")
        ).module.normalize_station_key
    return env.from_string(path.read_text(encoding="utf-8-sig")).render()


def test_staging_keeps_failed_numeric_casts_and_normalizes_names():
    with duckdb.connect(":memory:") as con:
        con.execute("create schema raw")
        con.execute(
            "create table raw.rail_hourly as select '2023-05-01' as transition_date, '07' as transition_hour, ' M1 ' as line_name, 'route' as line, ' Şişli - İı. ' as station_poi_desc_cd, 'x' as transfer_type, 'x' as product_kind, 'x' as transaction_type_desc, 'x' as town, 'broken' as number_of_passage, '5' as number_of_passenger, '2023-05' as source_month, 'r' as resource_id, 'RAYLI' as road_type union all select '2023-05-01','07','BUS','r',NULL,'x','x','x','x','1','1','2023-05','r','KARAYOLU'"
        )
        rows = con.execute(render_model("stg_rail_hourly")).fetchall()
        assert len(rows) == 1
        assert rows[0][1:3] == (7, "M1")
        assert rows[0][5] == "SISLIII"
        assert rows[0][10:12] == (None, 5)


def test_hourly_fact_preserves_line_grain_and_excludes_unmapped():
    with duckdb.connect(":memory:") as con:
        con.execute(
            "create table stg_rail_hourly as select date '2023-05-01' as ride_date, 7::smallint as ride_hour, 'M1' as line_code, 'A' as station_key, 10::bigint as passage_count, 8::bigint as passenger_count union all select '2023-05-01',7,'M1','B',20,15 union all select '2023-05-01',7,'M2','A',5,4 union all select '2023-05-01',7,'M1','X',100,90"
        )
        con.execute(
            "create table hourly_station_map as select 'M1' as line_code, 'A' as station_key, 'Shared' as station_group union all select 'M1','B','Shared' union all select 'M2','A','Shared' union all select 'M1','X',NULL"
        )
        rows = con.execute(render_model("fct_station_group_hourly")).fetchall()
        assert sorted((r[2], r[3], r[4], r[5], r[6]) for r in rows) == [
            ("M1", "Shared", 30, 23, 2),
            ("M2", "Shared", 5, 4, 1),
        ]


def test_coverage_priority_history_mapping_and_empty_month():
    with duckdb.connect(":memory:") as con:
        con.execute("create schema raw")
        con.execute(
            "create table raw.hourly_transport_months as select '2024-05' as month, 'empty' as status"
        )
        con.execute(
            "create table hourly_line_map as select 'M1' as line_code, 'daily' as daily_line_name"
        )
        con.execute(
            "create table hourly_station_map as select 'M1' as line_code, 'A' as station_key, 'Group' as station_group, 'auto' as match_method union all select 'M1','B',NULL,'ambiguous' union all select 'M1','U',NULL,'unmapped'"
        )
        con.execute("""create table stg_rail_hourly as
            select d::date as ride_date, 7::smallint as ride_hour, 'M1' as line_code,
                'A' as station_key, 'A' as station_name_raw, 100::bigint as passage_count
            from generate_series(date '2024-01-01', date '2024-01-31', interval 1 day) as t(d)
            union all select '2024-02-01',7,'M1','A','A',100000
            union all select d::date,7,'M1','A','A',10 from generate_series(date '2024-03-01',date '2024-03-31',interval 1 day) as t(d)
            union all select '2024-04-01',7,'M1','B','B',2
            union all select '2024-04-01',8,'M1','U','U',3
            union all select '2024-04-01',8,'M1',NULL,NULL,5
            union all select d::date,7,'M1','A','A',50 from generate_series(date '2024-06-01',date '2024-06-30',interval 1 day) as t(d)""")
        con.execute("alter table stg_rail_hourly alter ride_date type date")
        result = con.execute(render_model("fct_hourly_coverage_month"))
        cols = [c[0] for c in result.description]
        rows = {
            (r["month"], r["line_code"]): r
            for r in [dict(zip(cols, row, strict=True)) for row in result.fetchall()]
        }
        assert rows["2024-01", "M1"]["quality_flag"] == "ok"
        assert rows["2024-02", "M1"]["quality_flag"] == "partial_days"
        assert rows["2024-03", "M1"]["quality_flag"] == "low_volume"
        april = rows["2024-04", "M1"]
        assert (
            april["hours_present"],
            april["null_station_rows"],
            april["unmapped_passage"],
            april["ambiguous_passage"],
            april["mapped_share"],
        ) == (2, 1, 3, 2, 0)
        assert april["quality_flag"] == "partial_days"
        assert rows["2024-03", "ALL"]["quality_flag"] == "low_volume"
        assert rows["2024-05", "*"]["quality_flag"] == "empty"
        assert ("2024-05", "ALL") not in rows
        assert rows["2024-06", "M1"]["quality_flag"] == "ok"


def test_coverage_counts_are_bigint_and_zero_when_all_casts_failed():
    with duckdb.connect(":memory:") as con:
        con.execute("create schema raw")
        con.execute(
            "create table raw.hourly_transport_months as select '2024-05' as month, 'empty' as status"
        )
        con.execute(
            "create table hourly_line_map as select 'M1' as line_code, 'daily' as daily_line_name"
        )
        con.execute(
            "create table hourly_station_map as select 'M1' as line_code, 'A' as station_key, 'Group' as station_group, 'auto' as match_method"
        )
        con.execute("""create table stg_rail_hourly as
            select d::date as ride_date, 7::smallint as ride_hour, 'M1' as line_code,
                'A' as station_key, 'A' as station_name_raw, NULL::bigint as passage_count
            from generate_series(date '2024-01-01', date '2024-01-31', interval 1 day) as t(d)""")
        result = con.execute(render_model("fct_hourly_coverage_month"))
        types = {c[0]: str(c[1]) for c in result.description}
        cols = [c[0] for c in result.description]
        rows = {
            (r["month"], r["line_code"]): r
            for r in [dict(zip(cols, row, strict=True)) for row in result.fetchall()]
        }
        assert rows["2024-01", "M1"]["passage_count"] == 0
        for name in (
            "passage_count",
            "mapped_passage",
            "unmapped_passage",
            "ambiguous_passage",
            "source_rows",
            "null_station_rows",
        ):
            assert types[name] == "BIGINT", (name, types[name])


def test_line_seed_loads_with_duckdb_strict_csv_parser():
    path = Path(__file__).resolve().parents[1] / "transform/seeds/hourly_line_map.csv"
    with duckdb.connect(":memory:") as con:
        assert con.execute(
            "select count(*) from read_csv(?, header=true, all_varchar=true)",
            [str(path)],
        ).fetchone() == (28,)
