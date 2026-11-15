"""Publish a separate DuckDB database containing only materialized marts objects."""

import argparse
import os
import sys
from pathlib import Path

import duckdb


def _identifier(name: str) -> str:
    # Object names come from the source catalog, but still require SQL identifier
    # quoting: parameters cannot represent identifiers, and names may contain '"'.
    return '"' + name.replace('"', '""') + '"'


def publish(source: Path, target: Path) -> int:
    """Copy and verify marts in a temporary database, then replace the target."""
    temporary = Path(str(target) + ".tmp")
    try:
        if not source.is_file():
            raise ValueError(f"Source database does not exist: {source}")
        temporary.unlink(missing_ok=True)
        with duckdb.connect(str(temporary)) as con:
            con.execute("ATTACH ? AS src (READ_ONLY)", [str(source)])
            objects = con.execute(
                """
                SELECT table_name AS name FROM duckdb_tables()
                WHERE database_name = ? AND schema_name = ? AND NOT internal
                UNION ALL
                SELECT view_name AS name FROM duckdb_views()
                WHERE database_name = ? AND schema_name = ? AND NOT internal
                ORDER BY name
                """,
                ["src", "marts", "src", "marts"],
            ).fetchall()
            if not objects:
                raise ValueError(
                    f"No marts tables or views in source database: {source}"
                )
            con.execute("CREATE SCHEMA marts")
            for (name,) in objects:
                identifier = _identifier(name)
                con.execute(
                    f"CREATE TABLE marts.{identifier} AS SELECT * FROM src.marts.{identifier}"
                )
                source_count = con.execute(
                    f"SELECT count(*) FROM src.marts.{identifier}"
                ).fetchone()[0]
                target_count = con.execute(
                    f"SELECT count(*) FROM marts.{identifier}"
                ).fetchone()[0]
                if source_count != target_count:
                    raise ValueError(
                        f"Row count mismatch for marts.{name}: "
                        f"source={source_count} target={target_count}"
                    )
                print(f"table=marts.{name} rows={target_count}")
            con.execute("DETACH src")
            con.execute("CHECKPOINT")
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    print(f"published={target} tables={len(objects)}")
    return len(objects)


def main(argv: list[str] | None = None) -> int:
    """Run the publisher; errors leave the previous target intact and return 1."""
    root = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=root / "data" / "ulasim.duckdb")
    parser.add_argument(
        "--target", type=Path, default=root / "data" / "ulasim_analysis.duckdb"
    )
    args = parser.parse_args(argv)
    try:
        publish(args.source, args.target)
    except (duckdb.Error, OSError, ValueError) as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
