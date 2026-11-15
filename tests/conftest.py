import sys
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import httpx
import pytest
import rail_station_daily


def _os_user() -> str:
    """Real account name of this process (not the USERNAME env var, which the Codex sandbox inherits)."""
    if sys.platform == "win32":
        import ctypes

        size = ctypes.c_ulong(256)
        buf = ctypes.create_unicode_buffer(size.value)
        if ctypes.windll.advapi32.GetUserNameW(buf, ctypes.byref(size)):
            return buf.value
    import getpass

    return getpass.getuser()


def pytest_configure(config: pytest.Config) -> None:
    # pytest creates its temp dirs owner-only (Python 3.13 on Windows applies mode 0o700 as an ACL).
    # The main user and the Codex sandbox user are different accounts, so each gets its own basetemp.
    if config.option.basetemp is None:
        parent = Path(__file__).resolve().parent.parent / ".pytest-tmp"
        parent.mkdir(exist_ok=True)
        config.option.basetemp = str(parent / _os_user())


@dataclass
class FakeServer:
    root: Path
    monkeypatch: pytest.MonkeyPatch
    resources: dict = field(default_factory=dict)
    totals: dict = field(default_factory=dict)
    requests: list = field(default_factory=list)
    sleeps: list = field(default_factory=list)
    statuses: list = field(default_factory=list)
    connect_error: bool = False
    success: bool = True

    @property
    def db(self) -> Path:
        return self.root / "test.duckdb"

    def add(self, resource_id: str = "resource-a", count: int = 5) -> None:
        self.resources[resource_id] = [
            {
                "_id": index,
                "transaction_year": "2026",
                "transaction_month": "02",
                "transaction_day": "03",
                "line": "M1",
                "station_name": "İstasyon",
                "station_number": str(index),
                "town": "EyüpSultan" if index % 2 else "Bakırköy",
                "longitude": "28.95",
                "latitude": "41.01",
                "passage_cnt": "100",
                "passanger_cnt": "90",
            }
            for index in range(1, count + 1)
        ]
        self.totals[resource_id] = count

    def handle(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/3/action/package_show":
            assert request.url.params["id"] == rail_station_daily.PACKAGE
            return httpx.Response(
                200,
                json={
                    "success": True,
                    "result": {
                        "resources": [
                            {
                                "id": resource_id,
                                "last_modified": "2026-02-03T10:41:33.518571",
                            }
                            for resource_id in self.resources
                        ]
                    },
                },
            )
        assert request.url.path == "/api/3/action/datastore_search"
        assert request.url.params["limit"] == "32000"
        resource_id = request.url.params["resource_id"]
        offset = int(request.url.params["offset"])
        self.requests.append((resource_id, offset))
        if self.connect_error:
            raise httpx.ConnectError("Simulated connection failure", request=request)
        if self.statuses:
            status = self.statuses.pop(0)
            if status != 200:
                return httpx.Response(status)
        return httpx.Response(
            200,
            json={
                "success": self.success,
                "result": {
                    "total": self.totals[resource_id],
                    "records": self.resources[resource_id][offset : offset + 2],
                },
            },
        )

    def run(self, resource_id: str = "resource-a") -> int:
        self.monkeypatch.setattr(
            sys,
            "argv",
            ["rail_station_daily", "--resource-id", resource_id, "--db", str(self.db)],
        )
        return rail_station_daily.main()

    def rows(self) -> list:
        with duckdb.connect(str(self.db), read_only=True) as connection:
            return connection.execute(
                "SELECT _id, transaction_year, transaction_month, transaction_day, "
                "line, station_name, station_number, town, longitude, latitude, "
                "passage_cnt, passanger_cnt, resource_id, source_last_modified, "
                "ingested_at FROM raw.rail_station_daily ORDER BY resource_id, _id"
            ).fetchall()


@pytest.fixture
def server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> FakeServer:
    fake = FakeServer(tmp_path, monkeypatch)
    fake.add()
    real_client = httpx.Client

    def client_factory(**kwargs: object) -> httpx.Client:
        return real_client(transport=httpx.MockTransport(fake.handle), timeout=60)

    monkeypatch.setattr(rail_station_daily.httpx, "Client", client_factory)
    monkeypatch.setattr(rail_station_daily, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(rail_station_daily.time, "sleep", fake.sleeps.append)
    return fake
