"""Create empty ingest source tables for an offline dbt CI build."""

import argparse
import sys
from pathlib import Path
from uuid import uuid4

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "ingest"))

from hourly_transport import create_tables
from rail_station_daily import create_rail_station_daily, create_source_versions


def init_empty_db(db: Path) -> None:
    """Initialize a new database, refusing real database paths and nonempty files."""
    db = db.resolve()
    if db.parent.name.lower() == "data" and db.name.lower() == "ulasim.duckdb":
        raise ValueError(f"Refusing real database path: {db}")
    if db.exists() and db.stat().st_size != 0:
        raise ValueError(f"Database file is not empty: {db}")
    # DuckDB refuses existing zero-byte files; build beside the empty target.
    target = db.with_name(f".{db.name}.{uuid4().hex}.tmp") if db.exists() else db
    with duckdb.connect(str(target)) as connection:
        create_source_versions(connection)
        create_rail_station_daily(connection)
        create_tables(connection)
    if target != db:
        target.replace(db)


def main(argv: list[str] | None = None) -> int:
    """Parse the required output path and report initialization errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        init_empty_db(args.db)
    except (OSError, ValueError, duckdb.Error) as exc:
        print(f"Failed to initialize empty database {args.db}: {exc}", file=sys.stderr)
        return 1
    print(f"Initialized empty database: {args.db}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
