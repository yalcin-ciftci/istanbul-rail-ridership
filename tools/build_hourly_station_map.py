"""Build the reviewed hourly station seed from a read-only DuckDB connection."""

import argparse
import csv
import re
import sys
from collections import defaultdict
from pathlib import Path

import duckdb

FIELDS = (
    "line_code",
    "station_key",
    "station_name_raw",
    "station_group",
    "match_method",
    "note",
)
LINE_MAP = Path(__file__).resolve().parents[1] / "transform/seeds/hourly_line_map.csv"


def normalize_station_key(name: str) -> str:
    """Transliterate Turkish letters, uppercase and retain only ASCII alphanumerics."""
    return re.sub(
        "[^A-Z0-9]",
        "",
        name.translate(str.maketrans("İıŞşĞğÜüÖöÇç", "IiSsGgUuOoCc")).upper(),
    )


def build_rows(
    connection: duckdb.DuckDBPyConnection, line_map: dict[str, str]
) -> list[dict[str, str]]:
    """Resolve unique names first, then disambiguate using daily line membership."""
    candidates = defaultdict(set)
    for name, group in connection.execute(
        "select station_name, station_group from marts.dim_station where station_name is not null and station_group is not null"
    ).fetchall():
        if group:
            candidates[normalize_station_key(name)].add(group)
    daily_groups = defaultdict(set)
    for line, group in connection.execute(
        "select distinct f.line_name, d.station_group from marts.fct_station_daily as f join marts.dim_station as d on f.station_code = d.station_code where d.station_group is not null"
    ).fetchall():
        daily_groups[line].add(group)
    pairs = defaultdict(set)
    for line, name in connection.execute(
        "select distinct trim(line_name), station_poi_desc_cd from raw.rail_hourly where road_type = 'RAYLI' and station_poi_desc_cd is not null"
    ).fetchall():
        pairs[(line, normalize_station_key(name))].add(name)
    rows = []
    for (line, key), names in sorted(pairs.items()):
        groups = candidates[key]
        group, method, note = (
            "",
            "unmapped",
            "Normalleştirilmiş günlük istasyon adı eşleşmedi; güvenilir elle eşleme yok.",
        )
        if len(groups) == 1:
            group, method, note = next(iter(groups)), "auto", ""
        elif len(groups) > 1:
            scoped = groups & daily_groups[line_map.get(line, "")]
            # Only Marmaray prefers "(Marmaray)" groups; other TCDD lines have
            # their own daily line and resolve through daily membership alone.
            if line == "MARMARAY":
                marmaray = {g for g in groups if "(Marmaray)" in g}
                if marmaray:
                    scoped = marmaray
            if len(scoped) == 1:
                group, method, note = (
                    next(iter(scoped)),
                    "auto_line",
                    "Hat bağlamı ile tek gruba indirildi.",
                )
            else:
                method, note = "ambiguous", "Aday gruplar: " + "; ".join(sorted(groups))
        if method == "unmapped" and not line_map.get(line):
            note = "Bu aşamada günlük hat eşlemesi yok; güvenilir elle eşleme yok."
        rows.append(
            dict(zip(FIELDS, (line, key, min(names), group, method, note), strict=True))
        )
    return rows


def write_mapping(
    connection: duckdb.DuckDBPyConnection, line_map: dict[str, str], output: Path
) -> int:
    """Write sorted UTF-8 CSV, retaining existing manually reviewed rows."""
    manual = {}
    previous = {}
    if output.exists():
        with output.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != list(FIELDS):
                raise ValueError(f"Invalid mapping header: {output}")
            for row in reader:
                key = (row["line_code"], row["station_key"])
                previous[key] = row
                if row["match_method"] == "manual":
                    if key in manual:
                        raise ValueError(f"Duplicate manual key: {key}")
                    manual[key] = row
    rows = {
        (r["line_code"], r["station_key"]): r for r in build_rows(connection, line_map)
    }
    # Keep a reviewed note when regeneration reaches the same outcome.
    for key, row in rows.items():
        old = previous.get(key)
        if (
            old
            and old["note"]
            and (old["match_method"], old["station_group"])
            == (row["match_method"], row["station_group"])
        ):
            row["note"] = old["note"]
    rows.update(manual)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows[key] for key in sorted(rows))
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    """Generate the seed without permitting database writes."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        with LINE_MAP.open(encoding="utf-8-sig", newline="") as stream:
            line_map = {
                r["line_code"]: r["daily_line_name"] for r in csv.DictReader(stream)
            }
        with duckdb.connect(str(args.db), read_only=True) as connection:
            count = write_mapping(connection, line_map, args.out)
    except (duckdb.Error, OSError, ValueError, csv.Error) as error:
        print(
            f"Hourly station mapping failed (DB: {args.db}, output: {args.out}): {error}",
            file=sys.stderr,
        )
        return 1
    print(f"Hourly station mapping: {count} pairs written to {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
