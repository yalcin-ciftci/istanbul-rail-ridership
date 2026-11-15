"""Download monthly IBB transport CSVs and retain rail rows in Parquet and DuckDB."""

import argparse
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent
API_ROOT = "https://data.ibb.gov.tr/api/3/action/"
PACKAGE = "hourly-public-transport-data-set"
COLUMNS = (
    "transition_date",
    "transition_hour",
    "transport_type_id",
    "road_type",
    "line",
    "transfer_type",
    "number_of_passage",
    "number_of_passenger",
    "product_kind",
    "transaction_type_desc",
    "town",
    "line_name",
    "station_poi_desc_cd",
)
NAMES_SQL = ", ".join(COLUMNS)  # Identifiers come only from the fixed schema above.
MONTHS = (
    "Ocak",
    "Şubat",
    "Mart",
    "Nisan",
    "Mayıs",
    "Haziran",
    "Temmuz",
    "Ağustos",
    "Eylül",
    "Ekim",
    "Kasım",
    "Aralık",
)


def month_arg(value: str) -> str:
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value) or value[:4] == "0000":
        raise argparse.ArgumentTypeError(
            "month must have format YYYY-MM (valid year/month)"
        )
    return value


def resource_month(resource: dict) -> str:
    match = re.fullmatch(
        r"(" + "|".join(MONTHS) + r") ([0-9]{4}) Toplu Ulaşım Verisi",
        resource["name"].strip(),
    )
    if match is None:
        raise ValueError(f"Cannot parse resource name: {resource['name']!r}")
    return month_arg(f"{match[2]}-{MONTHS.index(match[1]) + 1:02}")


def retryable(exc: httpx.HTTPError) -> bool:
    return isinstance(exc, httpx.TransportError) or (
        isinstance(exc, httpx.HTTPStatusError) and 500 <= exc.response.status_code < 600
    )


def package_resources(client: httpx.Client) -> list[dict]:
    for attempt in range(3):
        try:
            response = client.get(API_ROOT + "package_show", params={"id": PACKAGE})
            response.raise_for_status()
            payload = response.json()
            if payload.get("success") is not True:
                raise ValueError("package_show success is not true")
            return payload["result"]["resources"]
        except httpx.HTTPError as exc:
            if not retryable(exc) or attempt == 2:
                raise
            time.sleep(2 ** (attempt + 1))
    raise RuntimeError("package_show exhausted retries")


def download(client: httpx.Client, resource: dict, target: Path) -> int:
    part = target.with_suffix(".csv.part")
    try:
        for attempt in range(3):
            try:
                downloaded = 0
                with client.stream("GET", resource["url"]) as response:
                    response.raise_for_status()
                    with part.open("wb") as stream:
                        for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                            stream.write(chunk)
                            downloaded += len(chunk)
                    content_length = response.headers.get("Content-Length")
                    if (
                        content_length is not None
                        and "Content-Encoding" not in response.headers
                        and downloaded != int(content_length)
                    ):
                        raise ValueError(
                            f"Content-Length mismatch for {target.name}: "
                            f"downloaded bytes={downloaded} Content-Length={content_length}"
                        )
                expected = resource.get("size")
                if expected is not None and downloaded != expected:
                    print(
                        f"Warning: metadata size {expected} differs from downloaded bytes "
                        f"{downloaded} for {target.name}",
                        file=sys.stderr,
                    )
                os.replace(part, target)
                return downloaded
            except httpx.HTTPError as exc:
                if not retryable(exc) or attempt == 2:
                    raise
                time.sleep(2 ** (attempt + 1))
    finally:
        part.unlink(missing_ok=True)
    raise RuntimeError(f"Download exhausted retries: {target.name}")


def create_tables(connection: duckdb.DuckDBPyConnection) -> None:
    connection.execute("CREATE SCHEMA IF NOT EXISTS raw")
    definitions = ", ".join(f"{name} VARCHAR" for name in COLUMNS)
    connection.execute(
        f"CREATE TABLE IF NOT EXISTS raw.rail_hourly ({definitions}, "
        "source_month VARCHAR, resource_id VARCHAR, source_last_modified TIMESTAMP, "
        "ingested_at TIMESTAMPTZ)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS raw.hourly_transport_months ("
        "month VARCHAR, resource_id VARCHAR, url VARCHAR, url_month VARCHAR, "
        "source_last_modified TIMESTAMP, bytes BIGINT, total_rows BIGINT, "
        "rail_rows BIGINT, min_date VARCHAR, max_date VARCHAR, distinct_dates INTEGER, "
        "status VARCHAR, ingested_at TIMESTAMPTZ)"
    )
    connection.execute(
        "ALTER TABLE raw.hourly_transport_months "
        "ADD COLUMN IF NOT EXISTS metadata_bytes BIGINT"
    )


def load_month(
    connection: duckdb.DuckDBPyConnection,
    month: str,
    resource: dict,
    csv_path: Path,
    parquet: Path,
    downloaded: int,
    url_month: str | None,
) -> tuple[str, int, int, int]:
    temporary = parquet.with_suffix(".parquet.tmp")
    try:
        connection.execute(
            "CREATE OR REPLACE TEMP TABLE hourly_csv AS SELECT * FROM "
            "read_csv(?, header=true, all_varchar=true, delim=',', quote='\"')",
            [str(csv_path)],
        )
        columns = connection.execute("DESCRIBE hourly_csv").fetchall()
        if [column[0] for column in columns] != list(COLUMNS):
            raise ValueError(
                f"Unexpected CSV header for {month}: {[c[0] for c in columns]}"
            )
        total = connection.execute("SELECT count(*) FROM hourly_csv").fetchone()[0]
        invalid_dates = connection.execute(
            "SELECT count(*) FROM hourly_csv WHERE road_type = ? AND "
            "(try_cast(transition_date AS DATE) IS NULL OR "
            "strftime(try_cast(transition_date AS DATE), '%Y-%m') <> ?)",
            ["RAYLI", month],
        ).fetchone()[0]
        if invalid_dates:
            raise ValueError(
                f"Invalid transition_date for {month}: "
                f"{invalid_dates} rail rows have missing, invalid or out-of-month dates"
            )
        invalid_hours = connection.execute(
            "SELECT count(*) FROM hourly_csv WHERE transition_hour IS NULL OR "
            "NOT regexp_full_match(transition_hour, '(0[0-9]|1[0-9]|2[0-3])')"
        ).fetchone()[0]
        if invalid_hours:
            raise ValueError(
                f"Invalid transition_hour for {month}: {invalid_hours} rows outside 00-23"
            )
        rail, minimum, maximum, dates = connection.execute(
            "SELECT count(*), min(transition_date), max(transition_date), "
            "count(DISTINCT transition_date) FROM hourly_csv WHERE road_type = ?",
            ["RAYLI"],
        ).fetchone()
        status = "empty" if total == 0 else "ok"
        if total:
            connection.execute(
                f"COPY (SELECT {NAMES_SQL} FROM hourly_csv WHERE road_type = 'RAYLI') "
                "TO ? (FORMAT parquet, COMPRESSION zstd)",
                [str(temporary)],
            )
            count = connection.execute(
                "SELECT count(*) FROM read_parquet(?)", [str(temporary)]
            ).fetchone()[0]
            if count != rail:
                raise ValueError(
                    f"Parquet count mismatch for {month}: {count} != {rail}"
                )
        ingested_at = datetime.now(UTC)
        connection.execute("BEGIN")
        try:
            connection.execute(
                "DELETE FROM raw.rail_hourly WHERE source_month = ?", [month]
            )
            if total:
                connection.execute(
                    f"INSERT INTO raw.rail_hourly ({NAMES_SQL}, source_month, resource_id, "
                    "source_last_modified, ingested_at) "
                    f"SELECT {NAMES_SQL}, ?, ?, CAST(? AS TIMESTAMP), ? FROM read_parquet(?)",
                    [
                        month,
                        resource["id"],
                        resource["last_modified"],
                        ingested_at,
                        str(temporary),
                    ],
                )
            loaded = connection.execute(
                "SELECT count(*) FROM raw.rail_hourly WHERE source_month = ?", [month]
            ).fetchone()[0]
            if loaded != rail:
                raise ValueError(
                    f"Loaded count mismatch for {month}: {loaded} != {rail}"
                )
            connection.execute(
                "DELETE FROM raw.hourly_transport_months WHERE month = ?", [month]
            )
            connection.execute(
                "INSERT INTO raw.hourly_transport_months (month, resource_id, url, url_month, "
                "source_last_modified, bytes, total_rows, rail_rows, min_date, max_date, "
                "distinct_dates, status, ingested_at, metadata_bytes) "
                "VALUES (?, ?, ?, ?, CAST(? AS TIMESTAMP), ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    month,
                    resource["id"],
                    resource["url"],
                    url_month,
                    resource["last_modified"],
                    downloaded,
                    total,
                    rail,
                    minimum,
                    maximum,
                    dates,
                    status,
                    ingested_at,
                    resource.get("size"),
                ],
            )
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        if total:
            try:
                os.replace(temporary, parquet)
            except OSError as exc:
                raise RuntimeError(
                    f"Database committed for month={month}, but replacing {parquet} "
                    "failed; Parquet remains at its previous version. "
                    f"Rerun --month {month} --force to repair: {exc}"
                ) from exc
        csv_path.unlink()
        return status, total, rail, dates
    finally:
        temporary.unlink(missing_ok=True)
        connection.execute("DROP TABLE IF EXISTS hourly_csv")


def process_month(
    client: httpx.Client,
    connection: duckdb.DuckDBPyConnection,
    month: str,
    resource: dict,
    force: bool,
) -> None:
    started = time.perf_counter()
    match = re.search(r"hourly_transportation_([0-9]{4})([0-9]{2})", resource["url"])
    url_month = f"{match[1]}-{match[2]}" if match else None
    if url_month is not None and url_month != month:
        print(
            f"Warning: month={month} url_month={url_month}; using resource name",
            file=sys.stderr,
        )
    previous = connection.execute(
        "SELECT total_rows, rail_rows, distinct_dates, bytes FROM raw.hourly_transport_months "
        "WHERE month = ? AND resource_id = ? "
        "AND source_last_modified IS NOT DISTINCT FROM CAST(? AS TIMESTAMP) "
        "AND status IN ('ok', 'empty')",
        [month, resource["id"], resource["last_modified"]],
    ).fetchone()
    if previous is not None and not force:
        total, rail, dates, downloaded = previous
        status = "skip"
    else:
        landing = PROJECT_ROOT / "data/landing/hourly_transport"
        landing.mkdir(parents=True, exist_ok=True)
        csv_path = landing / f"{month}.csv"
        downloaded = download(client, resource, csv_path)
        status, total, rail, dates = load_month(
            connection,
            month,
            resource,
            csv_path,
            landing / f"rail_{month}.parquet",
            downloaded,
            url_month,
        )
    print(
        f"month={month} status={status} total={total} rail={rail} dates={dates} "
        f"bytes={downloaded} seconds={time.perf_counter() - started:.2f}",
        flush=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group(required=True)
    selection.add_argument("--month", type=month_arg, action="append")
    selection.add_argument("--all", action="store_true")
    parser.add_argument("--db", type=Path, default=Path("data/ulasim.duckdb"))
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    try:
        with httpx.Client(timeout=60, follow_redirects=True) as client:
            resources = {
                resource_month(item): item for item in package_resources(client)
            }
            selected = (
                sorted(resources) if args.all else list(dict.fromkeys(args.month))
            )
            db_path = (PROJECT_ROOT / args.db).resolve()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            with duckdb.connect(str(db_path)) as connection:
                create_tables(connection)
                for month in selected:
                    if month not in resources:
                        raise ValueError(f"Resource not found for month={month}")
                    process_month(
                        client, connection, month, resources[month], args.force
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
