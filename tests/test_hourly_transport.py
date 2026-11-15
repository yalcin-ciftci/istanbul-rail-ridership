import csv
import gzip
import hashlib
import importlib
import io
import sys
from dataclasses import dataclass, field

import duckdb
import httpx
import pytest

HEADER = (
    "transition_date,transition_hour,transport_type_id,road_type,line,transfer_type,"
    "number_of_passage,number_of_passenger,product_kind,transaction_type_desc,"
    "town,line_name,station_poi_desc_cd"
)


def csv_bytes(month, roads):
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(HEADER.split(","))
    for index, road in enumerate(roads, 1):
        writer.writerow(
            [
                f"{month}-{index:02}",
                "00",
                "1",
                road,
                "Üsküdar",
                "Normal",
                "3",
                "2",
                "TAM",
                "Tam Kontur",
                "Kadıköy",
                "M5",
                "İstasyon",
            ]
        )
    return output.getvalue().encode("utf-8")


@dataclass
class HourlyServer:
    module: object
    root: object
    monkeypatch: object
    resources: list = field(default_factory=list)
    bodies: dict = field(default_factory=dict)
    requests: list = field(default_factory=list)
    sleeps: list = field(default_factory=list)
    statuses: list = field(default_factory=list)
    network_error: bool = False
    package_statuses: list = field(default_factory=list)
    headers: dict = field(default_factory=dict)

    @property
    def db(self):
        return self.root / "test.duckdb"

    @property
    def landing(self):
        return self.root / "data/landing/hourly_transport"

    def handle(self, request):
        self.requests.append(request.url.path)
        if request.url.path.endswith("package_show"):
            assert request.url.params["id"] == "hourly-public-transport-data-set"
            status = self.package_statuses.pop(0) if self.package_statuses else 200
            return httpx.Response(
                status, json={"success": True, "result": {"resources": self.resources}}
            )
        if self.network_error:
            raise httpx.ConnectError("simulated failure", request=request)
        status = self.statuses.pop(0) if self.statuses else 200
        return httpx.Response(
            status, content=self.bodies[request.url.path], headers=self.headers
        )

    def run(self, *args):
        self.monkeypatch.setattr(
            sys,
            "argv",
            [
                "hourly_transport",
                "--db",
                str(self.db),
                *(args or ("--month", "2024-01")),
            ],
        )
        return self.module.main()

    def query(self, sql, params=None):
        with duckdb.connect(str(self.db), read_only=True) as connection:
            return connection.execute(sql, params or []).fetchall()

    def snapshot(self):
        return (
            self.query(
                "SELECT * FROM raw.rail_hourly ORDER BY source_month, transition_date"
            ),
            self.query("SELECT * FROM raw.hourly_transport_months ORDER BY month"),
        )


@pytest.fixture
def hourly(tmp_path, monkeypatch):
    module = importlib.import_module("hourly_transport")
    fake = HourlyServer(module, tmp_path, monkeypatch)
    for month, name, url_month, roads in [
        ("2024-01", "Ocak", "202401", ["RAYLI", "OTOYOL", "DENİZ", "RAYLI"]),
        ("2024-11", "Kasım", "202411", []),
        ("2024-12", "Aralık", "202512", ["RAYLI", "RAYLI"]),
    ]:
        path = f"/download/hourly_transportation_{url_month}.csv"
        body = csv_bytes(month, roads)
        fake.bodies[path] = body
        fake.resources.append(
            {
                "id": f"resource-{month}",
                "name": f"{name} 2024 Toplu Ulaşım Verisi",
                "url": f"https://data.ibb.gov.tr{path}",
                "size": len(body),
                "last_modified": "2026-11-15T10:00:00",
                "format": "CSV",
            }
        )
    real_client = httpx.Client
    monkeypatch.setattr(
        module.httpx,
        "Client",
        lambda **kwargs: real_client(
            transport=httpx.MockTransport(fake.handle), **kwargs
        ),
    )
    monkeypatch.setattr(module, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(module.time, "sleep", fake.sleeps.append)
    return fake


def test_success_preserves_rail_text_and_metrics(hourly, capsys):
    assert hourly.run() == 0
    rows, manifest = hourly.snapshot()
    assert len(rows) == 2
    assert {row[3] for row in rows} == {"RAYLI"}
    assert {row[4] for row in rows} == {"Üsküdar"}
    assert {row[10] for row in rows} == {"Kadıköy"}
    assert {row[1] for row in rows} == {"00"}
    assert all(row[15] is not None and row[16] is not None for row in rows)
    assert manifest[0][0:2] == ("2024-01", "resource-2024-01")
    assert manifest[0][5:12] == (
        len(hourly.bodies["/download/hourly_transportation_202401.csv"]),
        4,
        2,
        "2024-01-01",
        "2024-01-04",
        2,
        "ok",
    )
    parquet = hourly.landing / "rail_2024-01.parquet"
    assert parquet.exists()
    assert not (hourly.landing / "2024-01.csv").exists()
    assert hourly.query("SELECT count(*) FROM read_parquet(?)", [str(parquet)]) == [
        (2,)
    ]
    schema = hourly.query("DESCRIBE SELECT * FROM read_parquet(?)", [str(parquet)])
    assert [row[0] for row in schema] == HEADER.split(",")
    assert {row[1] for row in schema} == {"VARCHAR"}
    assert (
        "month=2024-01 status=ok total=4 rail=2 dates=2 bytes="
        in capsys.readouterr().out
    )


def test_skip_and_force_do_not_duplicate(hourly, capsys):
    assert hourly.run() == 0
    before = hourly.snapshot()
    hourly.requests.clear()
    assert hourly.run() == 0
    assert "skip" in capsys.readouterr().out
    assert hourly.snapshot() == before
    assert len(hourly.requests) == 1
    assert hourly.run("--month", "2024-01", "--force") == 0
    assert len(hourly.snapshot()[0]) == 2
    assert len(hourly.requests) == 3


@pytest.mark.parametrize("changed", ["id", "last_modified"])
def test_changed_source_reloads(hourly, changed):
    assert hourly.run() == 0
    hourly.resources[0][changed] = (
        "replacement" if changed == "id" else "2026-11-16T10:00:00"
    )
    hourly.requests.clear()
    assert hourly.run() == 0
    assert len(hourly.requests) == 2
    assert len(hourly.snapshot()[0]) == 2


def test_other_month_is_preserved_and_url_month_recorded(hourly, capsys):
    assert hourly.run("--month", "2024-01", "--month", "2024-12") == 0
    before = hourly.query(
        "SELECT * FROM raw.rail_hourly WHERE source_month = '2024-12'"
    )
    assert hourly.run("--month", "2024-01", "--force") == 0
    assert (
        hourly.query("SELECT * FROM raw.rail_hourly WHERE source_month = '2024-12'")
        == before
    )
    assert hourly.query(
        "SELECT month, url_month FROM raw.hourly_transport_months "
        "WHERE month = '2024-12'"
    ) == [("2024-12", "2025-12")]
    assert "Warning:" in capsys.readouterr().err


def test_empty_month_and_empty_skip(hourly, capsys):
    assert hourly.run("--month", "2024-11") == 0
    assert hourly.snapshot()[0] == []
    assert hourly.query(
        "SELECT total_rows, rail_rows, min_date, max_date, distinct_dates, "
        "status FROM raw.hourly_transport_months"
    ) == [(0, 0, None, None, 0, "empty")]
    assert not (hourly.landing / "rail_2024-11.parquet").exists()
    assert not (hourly.landing / "2024-11.csv").exists()
    hourly.requests.clear()
    assert hourly.run("--month", "2024-11") == 0
    assert "skip" in capsys.readouterr().out
    assert len(hourly.requests) == 1


def test_metadata_size_mismatch_warns_and_loads(hourly, capsys):
    hourly.resources[0]["size"] += 1
    assert hourly.run() == 0
    assert "Warning: metadata size" in capsys.readouterr().err
    actual = len(hourly.bodies["/download/hourly_transportation_202401.csv"])
    assert hourly.query(
        "SELECT metadata_bytes, bytes FROM raw.hourly_transport_months"
    ) == [(actual + 1, actual)]
    assert len(hourly.snapshot()[0]) == 2
    assert not list(hourly.landing.glob("*.part"))


@pytest.mark.parametrize("status, expected, sleeps", [(503, 0, [2]), (404, 1, [])])
def test_http_retry_policy(hourly, status, expected, sleeps):
    hourly.statuses = [status, 200]
    assert hourly.run() == expected
    assert hourly.sleeps == sleeps
    assert len(hourly.requests) == (3 if status == 503 else 2)
    if expected == 0:
        assert len(hourly.snapshot()[0]) == 2


def test_persistent_network_failure_preserves_database(hourly):
    assert hourly.run() == 0
    before = hourly.snapshot()
    hourly.requests.clear()
    hourly.network_error = True
    assert hourly.run("--month", "2024-01", "--force") == 1
    assert hourly.sleeps == [2, 4]
    assert len(hourly.requests) == 4
    assert hourly.snapshot() == before
    assert not list(hourly.landing.glob("*.part"))


def test_bad_header_retains_csv_and_database(hourly):
    assert hourly.run() == 0
    before = hourly.snapshot()
    body = b"transition_date,road_type\n2024-01-01,RAYLI\n"
    hourly.bodies["/download/hourly_transportation_202401.csv"] = body
    hourly.resources[0]["size"] = len(body)
    assert hourly.run("--month", "2024-01", "--force") == 1
    assert hourly.snapshot() == before
    assert (hourly.landing / "2024-01.csv").read_bytes() == body
    assert not list(hourly.landing.glob("*.tmp"))


def test_unknown_month_fails(hourly):
    assert hourly.run("--month", "2019-01") == 1
    assert len(hourly.requests) == 1


@pytest.mark.parametrize(
    "args",
    [
        ["--month", "2024-1"],
        ["--month", "../x"],
        ["--month", "2024-13"],
        ["--month", "2024-00"],
        ["--month", "0000-01"],
        ["--month", "2024-01", "--all"],
        [],
    ],
)
def test_invalid_cli_never_contacts_network(hourly, args):
    hourly.monkeypatch.setattr(sys, "argv", ["hourly_transport", *args])
    with pytest.raises(SystemExit) as exc:
        hourly.module.main()
    assert exc.value.code == 2
    assert hourly.requests == []


def test_unparseable_resource_name_fails_all(hourly):
    hourly.resources[0]["name"] = "Unparseable resource"
    assert hourly.run("--all") == 1
    assert len(hourly.requests) == 1


def test_all_loads_each_month_in_order(hourly):
    hourly.resources.reverse()
    assert hourly.run("--all") == 0
    assert hourly.query(
        "SELECT month, status FROM raw.hourly_transport_months ORDER BY month"
    ) == [("2024-01", "ok"), ("2024-11", "empty"), ("2024-12", "ok")]


def test_no_rail_rows_still_produces_empty_parquet(hourly):
    body = csv_bytes("2024-01", ["OTOYOL"])
    hourly.bodies["/download/hourly_transportation_202401.csv"] = body
    hourly.resources[0]["size"] = None
    assert hourly.run() == 0
    assert hourly.snapshot()[0] == []
    assert (hourly.landing / "rail_2024-01.parquet").exists()
    assert hourly.query(
        "SELECT total_rows, rail_rows, status FROM raw.hourly_transport_months"
    ) == [(1, 0, "ok")]


def test_transaction_failure_rolls_back_and_retains_artifacts(hourly):
    december = csv_bytes("2024-12", ["RAYLI"] * 3)
    hourly.bodies["/download/hourly_transportation_202512.csv"] = december
    hourly.resources[2]["size"] = len(december)
    assert hourly.run("--month", "2024-01", "--month", "2024-12") == 0
    before = hourly.snapshot()
    parquet = hourly.landing / "rail_2024-01.parquet"
    digest = hashlib.sha256(parquet.read_bytes()).digest()
    with duckdb.connect(str(hourly.db)) as connection:
        connection.execute(
            "CREATE UNIQUE INDEX reject_reload ON raw.hourly_transport_months "
            "(rail_rows)"
        )
    body = csv_bytes("2024-01", ["RAYLI"] * 3)
    hourly.bodies["/download/hourly_transportation_202401.csv"] = body
    hourly.resources[0]["size"] = len(body)
    assert hourly.run("--month", "2024-01", "--force") == 1
    assert hourly.snapshot() == before
    assert hashlib.sha256(parquet.read_bytes()).digest() == digest
    assert (hourly.landing / "2024-01.csv").exists()
    assert (hourly.landing / "rail_2024-01.parquet").exists()
    assert not list(hourly.landing.glob("*.tmp"))


def test_success_replaces_parquet_with_new_content(hourly):
    assert hourly.run() == 0
    parquet = hourly.landing / "rail_2024-01.parquet"
    digest = hashlib.sha256(parquet.read_bytes()).digest()
    hourly.bodies["/download/hourly_transportation_202401.csv"] = csv_bytes(
        "2024-01", ["RAYLI"] * 3
    )
    hourly.resources[0]["size"] = 430
    assert hourly.run("--month", "2024-01", "--force") == 0
    assert hashlib.sha256(parquet.read_bytes()).digest() != digest
    assert hourly.query("SELECT count(*) FROM read_parquet(?)", [str(parquet)]) == [
        (3,)
    ]
    assert len(hourly.snapshot()[0]) == 3


def test_replace_failure_after_commit_reports_force_recovery(hourly, capsys):
    assert hourly.run() == 0
    parquet = hourly.landing / "rail_2024-01.parquet"
    old = parquet.read_bytes()
    hourly.bodies["/download/hourly_transportation_202401.csv"] = csv_bytes(
        "2024-01", ["RAYLI"] * 3
    )
    hourly.resources[0]["size"] = 430
    replace = hourly.module.os.replace

    def fail_parquet(source, target):
        if target == parquet:
            raise PermissionError("file is open")
        replace(source, target)

    with hourly.monkeypatch.context() as patch:
        patch.setattr(hourly.module.os, "replace", fail_parquet)
        assert hourly.run("--month", "2024-01", "--force") == 1
    error = capsys.readouterr().err
    assert "Error:" in error and "2024-01" in error
    assert "committed" in error and "--force" in error
    assert len(hourly.snapshot()[0]) == 3
    assert hourly.query("SELECT rail_rows FROM raw.hourly_transport_months") == [(3,)]
    assert parquet.read_bytes() == old
    assert (hourly.landing / "2024-01.csv").exists()
    assert not list(hourly.landing.glob("*.tmp"))
    assert hourly.run("--month", "2024-01", "--force") == 0
    assert hourly.query("SELECT count(*) FROM read_parquet(?)", [str(parquet)]) == [
        (3,)
    ]


def test_short_content_length_preserves_database(hourly, capsys):
    assert hourly.run() == 0
    before = hourly.snapshot()
    body = hourly.bodies["/download/hourly_transportation_202401.csv"]
    hourly.headers = {"Content-Length": str(len(body) + 100)}
    assert hourly.run("--month", "2024-01", "--force") == 1
    assert "Content-Length" in capsys.readouterr().err
    assert hourly.snapshot() == before
    assert not list(hourly.landing.glob("*.part"))


def test_encoded_response_uses_decoded_bytes(hourly):
    path = "/download/hourly_transportation_202401.csv"
    body = hourly.bodies[path]
    compressed = gzip.compress(body)
    hourly.bodies[path] = compressed
    hourly.headers = {
        "Content-Encoding": "gzip",
        "Content-Length": str(len(compressed)),
    }
    assert hourly.run() == 0
    assert hourly.query("SELECT bytes FROM raw.hourly_transport_months") == [
        (len(body),)
    ]


@pytest.mark.parametrize(
    "original, replacement, field",
    [
        (b"2024-01-01", b"2023-12-31", "transition_date"),
        (b"2024-01-01", b"2024-02-01", "transition_date"),
        (b"2024-01-01", b"2024-01-32", "transition_date"),
        (b"2024-01-01", b"", "transition_date"),
        (b",00,", b",24,", "transition_hour"),
        (b",00,", b",-1,", "transition_hour"),
        (b",00,", b",,", "transition_hour"),
    ],
)
def test_invalid_rail_content_preserves_database(
    hourly, capsys, original, replacement, field
):
    assert hourly.run() == 0
    before = hourly.snapshot()
    parquet = hourly.landing / "rail_2024-01.parquet"
    old = parquet.read_bytes()
    path = "/download/hourly_transportation_202401.csv"
    body = hourly.bodies[path].replace(original, replacement, 1)
    hourly.bodies[path] = body
    hourly.resources[0]["size"] = len(body)
    assert hourly.run("--month", "2024-01", "--force") == 1
    assert field in capsys.readouterr().err
    assert hourly.snapshot() == before
    assert parquet.read_bytes() == old
    assert (hourly.landing / "2024-01.csv").read_bytes() == body
    assert not list(hourly.landing.glob("*.tmp"))


def test_legacy_manifest_migration_preserves_old_rows(hourly):
    assert hourly.run() == 0
    with duckdb.connect(str(hourly.db)) as connection:
        connection.execute(
            "ALTER TABLE raw.hourly_transport_months DROP COLUMN IF EXISTS metadata_bytes"
        )
    before = hourly.snapshot()
    assert hourly.run("--month", "2024-12") == 0
    assert hourly.query(
        "SELECT metadata_bytes FROM raw.hourly_transport_months WHERE month = '2024-01'"
    ) == [(None,)]
    assert (
        hourly.query(
            "SELECT * EXCLUDE (metadata_bytes) FROM raw.hourly_transport_months "
            "WHERE month = '2024-01'"
        )
        == before[1]
    )
    assert hourly.query(
        "SELECT metadata_bytes, bytes FROM raw.hourly_transport_months WHERE month = '2024-12'"
    ) == [(hourly.resources[2]["size"], hourly.resources[2]["size"])]
