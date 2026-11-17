"""Load an IBB CKAN datastore resource into the raw DuckDB table."""

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
API_ROOT = "https://data.ibb.gov.tr/api/3/action/"
PACKAGE = "rayli-sistemler-istasyon-bazli-yolcu-ve-yolculuk-sayilari"
DEFAULT_RESOURCE = "cce8e138-686a-4b8d-bac0-c2a0ab294a6e"
RESOURCE_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}")
API_COLUMNS = {
    "_id": "BIGINT",
    "transaction_year": "VARCHAR",
    "transaction_month": "VARCHAR",
    "transaction_day": "VARCHAR",
    "line": "VARCHAR",
    "station_name": "VARCHAR",
    "station_number": "VARCHAR",
    "town": "VARCHAR",
    "longitude": "VARCHAR",
    "latitude": "VARCHAR",
    "passage_cnt": "VARCHAR",
    "passanger_cnt": "VARCHAR",
}


def create_rail_station_daily(connection: duckdb.DuckDBPyConnection) -> None:
    """Create the daily raw table using the ingest column definitions."""
    connection.execute("CREATE SCHEMA IF NOT EXISTS raw")
    columns_sql = ", ".join(f"{name} {kind}" for name, kind in API_COLUMNS.items())
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS raw.rail_station_daily ({columns_sql}, "
        "resource_id VARCHAR, source_last_modified TIMESTAMP, ingested_at TIMESTAMPTZ)"
    )


def create_source_versions(connection: duckdb.DuckDBPyConnection) -> None:
    """Create the shared source fingerprint registry for both ingest scripts."""
    connection.execute("CREATE SCHEMA IF NOT EXISTS raw")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS raw.source_versions ("
        "dataset VARCHAR NOT NULL, resource_id VARCHAR NOT NULL, source_month VARCHAR, "
        "source_last_modified TIMESTAMP, source_sha256 VARCHAR, "
        "stored_sha256 VARCHAR NOT NULL, stored_path VARCHAR NOT NULL, "
        "row_count BIGINT NOT NULL, ingested_at TIMESTAMPTZ NOT NULL, hash_note VARCHAR)"
    )


def delete_source_version(
    connection: duckdb.DuckDBPyConnection,
    dataset: str,
    resource_id: str,
    source_month: str | None,
) -> None:
    """Remove one logical key inside the caller's transaction."""
    connection.execute(
        "DELETE FROM raw.source_versions WHERE dataset = ? AND resource_id = ? "
        "AND source_month IS NOT DISTINCT FROM ?",
        [dataset, resource_id, source_month],
    )


def write_source_version(
    connection: duckdb.DuckDBPyConnection,
    dataset: str,
    resource_id: str,
    source_month: str | None,
    source_last_modified: str | datetime | None,
    source_sha256: str | None,
    stored_sha256: str,
    stored_path: str,
    row_count: int,
    ingested_at: datetime,
    hash_note: str | None = None,
) -> None:
    """Replace one fingerprint inside the caller's data transaction."""
    delete_source_version(connection, dataset, resource_id, source_month)
    connection.execute(
        "INSERT INTO raw.source_versions (dataset, resource_id, source_month, "
        "source_last_modified, source_sha256, stored_sha256, stored_path, row_count, "
        "ingested_at, hash_note) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            dataset,
            resource_id,
            source_month,
            source_last_modified,
            source_sha256,
            stored_sha256,
            stored_path,
            row_count,
            ingested_at,
            hash_note,
        ],
    )


def file_sha256(path: Path) -> str:
    """Hash a stored file with bounded memory."""
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_lines(records: list[dict]) -> Iterator[bytes]:
    """Yield the canonical daily source bytes in numeric source-ID order."""
    for record in sorted(records, key=lambda record: int(record["_id"])):
        yield (
            json.dumps(
                record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
            + "\n"
        ).encode("utf-8")


def resource_id_arg(value: str) -> str:
    if not RESOURCE_ID_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            "resource id may contain only letters, digits, '-' and '_'"
        )
    return value


def request_result(client, action, params, offset):
    url = API_ROOT + action
    for attempt in range(3):
        try:
            response = client.get(url, params=params)
            response.raise_for_status()
        except (httpx.TransportError, httpx.HTTPStatusError) as exc:
            retryable = isinstance(exc, httpx.TransportError) or (
                500 <= exc.response.status_code < 600
            )
            if retryable and attempt < 2:
                time.sleep(2 ** (attempt + 1))
                continue
            raise RuntimeError(
                f"Request failed: url={url} offset={offset}: {exc}"
            ) from exc
        try:
            payload = response.json()
            if payload.get("success") is not True:
                raise ValueError(f"success is not true: {payload.get('error')}")
            return payload["result"]
        except (ValueError, KeyError, TypeError) as exc:
            raise RuntimeError(
                f"Invalid response: url={url} offset={offset}: {exc}"
            ) from exc


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource-id", type=resource_id_arg, default=DEFAULT_RESOURCE)
    parser.add_argument("--db", type=Path, default=Path("data/ulasim.duckdb"))
    args = parser.parse_args()
    started = time.perf_counter()
    ingested_at = datetime.now(UTC)
    db_path = (PROJECT_ROOT / args.db).resolve()

    try:
        with httpx.Client(timeout=60) as client:
            package = request_result(client, "package_show", {"id": PACKAGE}, "n/a")
            resource = next(
                (
                    item
                    for item in package["resources"]
                    if item["id"] == args.resource_id
                ),
                None,
            )
            if resource is None:
                raise RuntimeError(
                    f"Resource not found in package {PACKAGE}: {args.resource_id}"
                )
            last_modified = resource["last_modified"]
            print(
                f"resource_id={args.resource_id} last_modified={last_modified}",
                flush=True,
            )
            records = []
            offset = 0
            total = None
            while True:
                result = request_result(
                    client,
                    "datastore_search",
                    {"resource_id": args.resource_id, "limit": 32000, "offset": offset},
                    offset,
                )
                if total is None:
                    total = result["total"]
                page = result["records"]
                records.extend(page)
                offset += len(page)
                print(f"offset={offset}/{total}", flush=True)
                if not page or offset >= total:
                    break

        if len(records) != total:
            raise RuntimeError(
                f"Row count mismatch: fetched={len(records)} total={total}"
            )
        if len({record["_id"] for record in records}) != len(records):
            raise RuntimeError(f"Duplicate _id values in resource {args.resource_id}")

        landing = (
            PROJECT_ROOT
            / "data/landing/rail_station_daily"
            / f"{args.resource_id}.jsonl"
        )
        landing.parent.mkdir(parents=True, exist_ok=True)
        temporary = landing.with_suffix(".jsonl.tmp")
        digest = hashlib.sha256()
        with temporary.open("wb") as stream:
            for line in canonical_lines(records):
                stream.write(line)
                digest.update(line)
        source_hash = digest.hexdigest()
        os.replace(temporary, landing)

        db_path.parent.mkdir(parents=True, exist_ok=True)
        names_sql = ", ".join(API_COLUMNS)
        with duckdb.connect(str(db_path)) as connection:
            create_source_versions(connection)
            create_rail_station_daily(connection)
            connection.execute("BEGIN")
            try:
                connection.execute(
                    "DELETE FROM raw.rail_station_daily WHERE resource_id = ?",
                    [args.resource_id],
                )
                connection.execute(
                    f"INSERT INTO raw.rail_station_daily SELECT {names_sql}, "
                    "? AS resource_id, CAST(? AS TIMESTAMP), ? "
                    "FROM read_json(?, format='newline_delimited', columns=?)",
                    [
                        args.resource_id,
                        last_modified,
                        ingested_at,
                        str(landing),
                        API_COLUMNS,
                    ],
                )
                loaded = connection.execute(
                    "SELECT count(*) FROM raw.rail_station_daily WHERE resource_id = ?",
                    [args.resource_id],
                ).fetchone()[0]
                if loaded != total:
                    raise RuntimeError(
                        f"Loaded row count mismatch: loaded={loaded} total={total}"
                    )
                write_source_version(
                    connection,
                    "rail_station_daily",
                    args.resource_id,
                    None,
                    last_modified,
                    source_hash,
                    source_hash,
                    landing.relative_to(PROJECT_ROOT).as_posix(),
                    loaded,
                    ingested_at,
                )
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        print(
            f"loaded={loaded} elapsed_seconds={time.perf_counter() - started:.2f} "
            f"db={db_path} sha256={source_hash[:12]}"
        )
        return 0
    except (
        RuntimeError,
        httpx.HTTPError,
        duckdb.Error,
        OSError,
        ValueError,
        KeyError,
        TypeError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
