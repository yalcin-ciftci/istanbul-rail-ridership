"""Load an IBB CKAN datastore resource into the raw DuckDB table."""

import argparse
import json
import os
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
API_ROOT = "https://data.ibb.gov.tr/api/3/action/"
PACKAGE = "rayli-sistemler-istasyon-bazli-yolcu-ve-yolculuk-sayilari"
DEFAULT_RESOURCE = "cce8e138-686a-4b8d-bac0-c2a0ab294a6e"
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
    parser.add_argument("--resource-id", default=DEFAULT_RESOURCE)
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
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            for record in records:
                stream.write(json.dumps(record, ensure_ascii=False) + "\n")
        os.replace(temporary, landing)

        db_path.parent.mkdir(parents=True, exist_ok=True)
        columns_sql = ", ".join(f"{name} {kind}" for name, kind in API_COLUMNS.items())
        names_sql = ", ".join(API_COLUMNS)
        with duckdb.connect(str(db_path)) as connection:
            connection.execute("CREATE SCHEMA IF NOT EXISTS raw")
            connection.execute(
                f"CREATE TABLE IF NOT EXISTS raw.rail_station_daily ({columns_sql}, "
                "resource_id VARCHAR, source_last_modified TIMESTAMP, ingested_at TIMESTAMPTZ)"
            )
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
                connection.execute("COMMIT")
            except Exception:
                connection.execute("ROLLBACK")
                raise
        print(
            f"loaded={loaded} elapsed_seconds={time.perf_counter() - started:.2f} db={db_path}"
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
