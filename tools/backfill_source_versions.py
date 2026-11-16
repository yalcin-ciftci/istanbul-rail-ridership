"""Backfill source fingerprints from existing local landing files without network access."""

import argparse
import hashlib
import json
import sys
from pathlib import Path

import duckdb

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from ingest.rail_station_daily import (
    canonical_lines,
    create_source_versions,
    file_sha256,
    write_source_version,
)

HOURLY_HASH_NOTE = (
    "kaynak CSV 2b öncesi alındı ve saklanmadı; yalnız Parquet parmak izi var"
)


def _has_table(con: duckdb.DuckDBPyConnection, name: str) -> bool:
    return bool(
        con.execute(
            "SELECT count(*) FROM information_schema.tables "
            "WHERE table_catalog = current_database() AND table_schema = ? AND table_name = ?",
            ["raw", name],
        ).fetchone()[0]
    )


def backfill(db: Path) -> None:
    """Write all available legacy fingerprints atomically, preserving known sources."""
    if not db.is_file():
        raise ValueError(f"Database does not exist: {db}")
    written = skipped = missing = 0
    messages = []
    with duckdb.connect(str(db)) as con:
        con.execute("BEGIN")
        try:
            create_source_versions(con)
            sources = []
            if _has_table(con, "rail_station_daily"):
                for resource, modified, count, ingested in con.execute(
                    "SELECT resource_id, max(source_last_modified), count(*), "
                    "max(ingested_at) FROM raw.rail_station_daily GROUP BY resource_id "
                    "ORDER BY resource_id"
                ).fetchall():
                    sources.append(
                        (
                            "rail_station_daily",
                            resource,
                            None,
                            modified,
                            count,
                            ingested,
                            f"data/landing/rail_station_daily/{resource}.jsonl",
                        )
                    )
            if _has_table(con, "hourly_transport_months"):
                for month, resource, modified, ingested in con.execute(
                    "SELECT month, resource_id, source_last_modified, ingested_at "
                    "FROM raw.hourly_transport_months WHERE status = ? ORDER BY month",
                    ["ok"],
                ).fetchall():
                    count = con.execute(
                        "SELECT count(*) FROM raw.rail_hourly WHERE source_month = ?",
                        [month],
                    ).fetchone()[0]
                    sources.append(
                        (
                            "rail_hourly",
                            resource,
                            month,
                            modified,
                            count,
                            ingested,
                            f"data/landing/hourly_transport/rail_{month}.parquet",
                        )
                    )
            for (
                dataset,
                resource,
                month,
                modified,
                count,
                ingested,
                relative,
            ) in sources:
                existing = con.execute(
                    "SELECT source_sha256 FROM raw.source_versions "
                    "WHERE dataset = ? AND resource_id = ? "
                    "AND source_month IS NOT DISTINCT FROM ? AND source_sha256 IS NOT NULL",
                    [dataset, resource, month],
                ).fetchone()
                if existing is not None:
                    skipped += 1
                    continue
                path = PROJECT_ROOT / relative
                if not path.is_file():
                    missing += 1
                    print(f"Warning: missing landing file: {path}", file=sys.stderr)
                    continue
                stored_hash = file_sha256(path)
                source_hash = None
                note = HOURLY_HASH_NOTE
                if dataset == "rail_station_daily":
                    with path.open(encoding="utf-8") as stream:
                        records = [json.loads(line) for line in stream if line.strip()]
                    digest = hashlib.sha256()
                    for line in canonical_lines(records):
                        digest.update(line)
                    source_hash = digest.hexdigest()
                    note = None
                write_source_version(
                    con,
                    dataset,
                    resource,
                    month,
                    modified,
                    source_hash,
                    stored_hash,
                    relative,
                    count,
                    ingested,
                    note,
                )
                written += 1
                messages.append(
                    f"dataset={dataset} resource_id={resource} source_month={month or ''}"
                )
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    for message in messages:
        print(message)
    print(f"written={written} skipped_existing={skipped} missing_files={missing}")


def main(argv: list[str] | None = None) -> int:
    """Resolve the database against the repository and report backfill errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/ulasim.duckdb"))
    args = parser.parse_args(argv)
    try:
        backfill((PROJECT_ROOT / args.db).resolve())
    except (duckdb.Error, OSError, ValueError, KeyError, TypeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
