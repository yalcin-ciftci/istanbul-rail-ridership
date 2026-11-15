import importlib.util
import json
import os
import subprocess
import time
from pathlib import Path

import anyio
import duckdb
import pytest
from mcp.client.stdio import StdioServerParameters, stdio_client

from mcp import ClientSession

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "ulasim_mcp" / "server.py"


def load_server():
    assert SERVER.is_file(), "MCP sunucusu henüz uygulanmadı"
    spec = importlib.util.spec_from_file_location("ulasim_server_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "fixture.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE SCHEMA marts")
        con.execute("CREATE SCHEMA staging")
        con.execute("CREATE TABLE staging.secret (hidden INTEGER)")
        con.execute("""
            CREATE TABLE marts.dim_station (
                station_code VARCHAR, station_name VARCHAR, station_group VARCHAR,
                district VARCHAR, first_seen_date DATE, last_seen_date DATE
            )
        """)
        con.executemany(
            "INSERT INTO marts.dim_station VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "YNK-MR1",
                    "Yenikapı-1",
                    "Yenikapı",
                    "Fatih",
                    "2022-01-01",
                    "2024-01-03",
                ),
                (
                    "YNK-YN1",
                    "Yenikapı Güney",
                    "Yenikapı",
                    "Fatih",
                    "2022-01-02",
                    "2024-01-02",
                ),
                (
                    "USE-US1",
                    "Üsküdar",
                    "Üsküdar",
                    "Üsküdar",
                    "2022-01-02",
                    "2024-01-03",
                ),
                ("TKS-TK1", "Taksim", "Taksim", "Beyoğlu", "2022-01-01", "2024-01-01"),
            ],
        )
        con.execute("""
            CREATE TABLE marts.dim_station_group AS
            SELECT station_group, count(*) AS station_code_count
            FROM marts.dim_station GROUP BY station_group
        """)
        con.execute("""
            CREATE TABLE marts.fct_station_daily (
                ride_date DATE, station_code VARCHAR, line_name VARCHAR,
                journey_count BIGINT
            )
        """)
        con.executemany(
            "INSERT INTO marts.fct_station_daily VALUES (?, ?, ?, ?)",
            [
                ("2022-01-01", "YNK-MR1", "Marmaray", 100),
                ("2024-01-03", "YNK-MR1", "Marmaray", 200),
                ("2024-01-02", "YNK-YN1", "M2", 150),
                ("2024-01-03", "USE-US1", "M5", 300),
                ("2024-01-01", "TKS-TK1", "M2", 50),
            ],
        )
        con.execute("COMMENT ON TABLE marts.dim_station IS 'İstasyon kodları'")
        con.execute(
            "COMMENT ON COLUMN marts.dim_station.station_name IS 'İstasyon adı'"
        )
    monkeypatch.setenv("ULASIM_DB", str(path))
    return path


@pytest.fixture
def legacy_db(db):
    with duckdb.connect(str(db)) as con:
        con.execute("ALTER TABLE marts.dim_station DROP COLUMN station_group")
    return db


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT sum(journey_count) AS toplam FROM marts.fct_station_daily",
        (
            "WITH x AS (SELECT journey_count FROM marts.fct_station_daily) "
            "SELECT sum(journey_count) AS toplam FROM x"
        ),
    ],
)
def test_sorgu_select_and_cte(db, sql):
    result = load_server().sorgu(sql)
    assert result["kolonlar"] == ["toplam"]
    assert result["satirlar"] == [[800]]
    assert result["satir_sayisi"] == 1
    assert result["kesildi"] is False
    assert result["sure_ms"] >= 0


@pytest.mark.parametrize(
    ("limit", "count", "cut"),
    [
        (2, 2, True),
        (0, 1, True),
        (-20, 1, True),
        (2000, 1000, True),
        (1000, 1000, True),
    ],
)
def test_sorgu_row_limit_and_clamping(db, limit, count, cut):
    result = load_server().sorgu("SELECT i FROM range(1001) AS r(i)", limit)
    assert result["satir_sayisi"] == count
    assert len(result["satirlar"]) == count
    assert result["kesildi"] is cut


def test_sorgu_exact_limit_is_not_truncated(db):
    result = load_server().sorgu("SELECT i FROM range(2) AS r(i)", 2)
    assert result["satirlar"] == [[0], [1]]
    assert result["kesildi"] is False


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO marts.fct_station_daily VALUES ('2024-01-01', 'X', 'M1', 1)",
        "CREATE TABLE marts.bad (x INTEGER)",
        "DROP TABLE marts.dim_station",
        "COPY marts.dim_station TO 'forbidden.csv'",
        "ATTACH ':memory:' AS other",
        "SET enable_external_access=true",
        "PRAGMA version",
        "INSTALL httpfs",
        "LOAD httpfs",
        "SELECT 1; SELECT 2",
        "SELECT 1; DROP TABLE marts.dim_station",
        "",
        "  -- only a comment\n",
    ],
)
def test_sorgu_rejects_non_single_select_before_execution(db, sql):
    result = load_server().sorgu(sql)
    assert "hata" in result
    assert "tek bir SELECT" in result["hata"]
    with duckdb.connect(str(db)) as con:
        assert con.execute("SELECT count(*) FROM marts.dim_station").fetchone()[0] == 4
        assert (
            con.execute("SELECT count(*) FROM marts.fct_station_daily").fetchone()[0]
            == 5
        )


def test_sorgu_external_file_access_is_disabled(db, tmp_path):
    csv = tmp_path / "secret.csv"
    csv.write_text("secret\n123\n", encoding="utf-8")
    sql = "SELECT * FROM read_csv('" + csv.as_posix().replace("'", "''") + "')"
    result = load_server().sorgu(sql)
    assert "hata" in result
    assert "external" in result["hata"].lower()
    assert "123" not in result["hata"]


def test_sorgu_security_configuration(db):
    result = load_server().sorgu("""
        SELECT current_setting('access_mode'),
               current_setting('enable_external_access'),
               current_setting('lock_configuration'), current_setting('threads')
    """)
    assert result["satirlar"] == [["read_only", False, True, 2]]


def test_sorgu_timeout_interrupts_and_releases_connection(db, monkeypatch):
    module = load_server()
    monkeypatch.setenv("ULASIM_SORGU_ZAMAN_ASIMI", "1")
    start = time.monotonic()
    result = module.sorgu("SELECT sum(sin(i)) FROM range(1000000000000) AS r(i)")
    assert time.monotonic() - start < 6
    assert "zaman aşımı" in result["hata"].lower()
    assert module.sorgu("SELECT 42")["satirlar"] == [[42]]
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE marts.after_timeout (x INTEGER)")


def test_sorgu_json_types(db):
    result = load_server().sorgu("""
        SELECT DATE '2024-01-02' AS d, TIMESTAMP '2024-01-02 03:04:05' AS dt,
               12.34::DECIMAL(8, 2) AS dec, NULL AS n, true AS b,
               from_hex('4142') AS blob, INTERVAL '2 days' AS duration,
               [DATE '2024-01-02'] AS nested,
               'NaN'::DOUBLE AS nan, 'Infinity'::DOUBLE AS inf
    """)
    row = result["satirlar"][0]
    assert row[:5] == ["2024-01-02", "2024-01-02T03:04:05", 12.34, None, True]
    assert isinstance(row[5], str)
    assert isinstance(row[6], str)
    assert isinstance(row[7], (str, list))
    assert isinstance(row[8], str)
    assert isinstance(row[9], str)
    json.dumps(result, ensure_ascii=False, allow_nan=False)


def test_sorgu_sql_error_returns_message_and_releases_connection(db):
    result = load_server().sorgu("SELECT nonexistent FROM marts.dim_station")
    assert "nonexistent" in result["hata"]
    assert "Traceback" not in result["hata"]
    with duckdb.connect(str(db)) as con:
        con.execute("CREATE TABLE marts.after_error (x INTEGER)")


@pytest.mark.parametrize(
    "tool,args",
    [("sema", ()), ("sorgu", ("SELECT 1",)), ("istasyon_bul", ("yenikapi",))],
)
def test_missing_database_is_turkish_error(tmp_path, monkeypatch, tool, args):
    missing = tmp_path / "missing.duckdb"
    monkeypatch.setenv("ULASIM_DB", str(missing))
    result = getattr(load_server(), tool)(*args)
    assert result["hata"].startswith(
        "Veritabanı şu an kullanılamıyor (güncelleniyor olabilir):"
    )
    assert not missing.exists()


@pytest.mark.parametrize(
    "tool,args",
    [("sema", ()), ("sorgu", ("SELECT 1",)), ("istasyon_bul", ("yenikapi",))],
)
def test_writer_lock_is_turkish_error(db, tool, args):
    module = load_server()
    code = (
        "import duckdb, sys; "
        "con=duckdb.connect(sys.argv[1]); "
        "print('locked', flush=True); sys.stdin.readline(); con.close()"
    )
    with subprocess.Popen(
        ["uv", "run", "python", "-c", code, str(db)],
        cwd=ROOT,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
    ) as process:
        try:
            assert process.stdout.readline().strip() == "locked"
            result = getattr(module, tool)(*args)
            assert result["hata"].startswith(
                "Veritabanı şu an kullanılamıyor (güncelleniyor olabilir):"
            )
        finally:
            process.communicate("\n", timeout=10)


def test_default_database_path_is_relative_to_source(tmp_path, monkeypatch):
    module = load_server()
    monkeypatch.delenv("ULASIM_DB", raising=False)
    monkeypatch.chdir(tmp_path)
    # Inspect path resolution only; never open the real database.
    assert module.database_path() == ROOT / "data" / "ulasim.duckdb"


@pytest.mark.parametrize("name", ["yenikapi", "YENİKAPI", "yenikpı", "  YENİKAPI  "])
def test_station_search_groups_turkish_variants_and_typo(db, name):
    result = load_server().istasyon_bul(name)
    station = result["sonuclar"][0]
    assert station["station_group"] == "Yenikapı"
    assert station["skor"] >= 0.6
    assert station["istasyon_kodlari"] == ["YNK-MR1", "YNK-YN1"]
    assert station["adlar"] == ["Yenikapı Güney", "Yenikapı-1"]
    assert station["hatlar"] == ["M2", "Marmaray"]
    assert station["ilceler"] == ["Fatih"]
    assert station["ilk_tarih"] == "2022-01-01"
    assert station["son_tarih"] == "2024-01-03"
    assert len([s for s in result["sonuclar"] if s["station_group"] == "Yenikapı"]) == 1
    json.dumps(result, ensure_ascii=False, allow_nan=False)


@pytest.mark.parametrize(
    ("name", "score"), [("yenikapi", 1.0), ("yeni", 0.9), ("nikap", 0.8)]
)
def test_station_search_scoring(db, name, score):
    result = load_server().istasyon_bul(name)
    assert result["sonuclar"][0]["skor"] == score


def test_station_search_uskudar_and_no_match(db):
    module = load_server()
    assert module.istasyon_bul("uskudar")["sonuclar"][0]["station_group"] == "Üsküdar"
    assert module.istasyon_bul("zzzzzzzz")["sonuclar"] == []


@pytest.mark.parametrize("name", ["", " \t\n"])
def test_station_search_blank_is_error(db, name):
    assert "hata" in load_server().istasyon_bul(name)


def test_station_search_legacy_column_fallback(legacy_db):
    result = load_server().istasyon_bul("yenikapi")
    assert [s["station_group"] for s in result["sonuclar"]] == [
        "Yenikapı Güney",
        "Yenikapı-1",
    ]
    assert {tuple(s["istasyon_kodlari"]) for s in result["sonuclar"]} == {
        ("YNK-MR1",),
        ("YNK-YN1",),
    }


@pytest.mark.parametrize(("limit", "count"), [(0, 1), (-2, 1), (100, 50)])
def test_station_search_limit_and_tie_order(db, limit, count):
    with duckdb.connect(str(db)) as con:
        con.executemany(
            "INSERT INTO marts.dim_station VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    f"X-{i}",
                    f"Metro {i:02}",
                    f"Metro {i:02}",
                    None,
                    "2024-01-01",
                    "2024-01-02",
                )
                for i in range(55)
            ],
        )
    results = load_server().istasyon_bul("metro", limit)["sonuclar"]
    assert len(results) == count
    assert [s["station_group"] for s in results] == [
        f"Metro {i:02}" for i in range(count)
    ]


def test_sema_only_marts_comments_scope_and_caveats(db):
    result = load_server().sema()
    tables = {table["tablo"]: table for table in result["tablolar"]}
    assert set(tables) == {"dim_station", "dim_station_group", "fct_station_daily"}
    assert tables["dim_station"]["aciklama"] == "İstasyon kodları"
    columns = {col["kolon"]: col for col in tables["dim_station"]["kolonlar"]}
    assert columns["station_name"]["aciklama"] == "İstasyon adı"
    assert columns["station_name"]["tur"] == "VARCHAR"
    assert result["kapsam"] == {"min": "2022-01-01", "max": "2024-01-03"}
    caveats = " ".join(result["uyarilar"])
    for text in [
        "passenger_count",
        "journey_count",
        "2022-03-28",
        "2022-11-02",
        "journey_count_estimated",
        "tahmin",
        "fct_line_daily",
        "eşleme",
        "İBB",
        "CC BY 4.0",
    ]:
        assert text in caveats
    json.dumps(result, ensure_ascii=False, allow_nan=False)


def test_sema_missing_fact_table_has_null_scope(db):
    with duckdb.connect(str(db)) as con:
        con.execute("DROP TABLE marts.fct_station_daily")
    assert load_server().sema()["kapsam"] == {"min": None, "max": None}


@pytest.mark.parametrize(
    "tool,args",
    [("sema", ()), ("sorgu", ("SELECT 1",)), ("istasyon_bul", ("yenikapi",))],
)
def test_tools_release_connections_after_success(db, tool, args):
    module = load_server()
    for _ in range(2):
        assert "hata" not in getattr(module, tool)(*args)
        with duckdb.connect(str(db)) as con:
            assert con.execute("SELECT 1").fetchone() == (1,)


def test_stdio_integration_lists_tools_and_calls_sorgu(db):
    assert SERVER.is_file(), "MCP sunucusu henüz uygulanmadı"

    async def exercise():
        params = StdioServerParameters(
            command="uv",
            args=["run", "python", "ulasim_mcp/server.py"],
            cwd=ROOT,
            env={**os.environ, "ULASIM_DB": str(db), "PYTHONUTF8": "1"},
        )
        with anyio.fail_after(20):
            async with (
                stdio_client(params) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                listing = await session.list_tools()
                assert {tool.name for tool in listing.tools} == {
                    "sema",
                    "sorgu",
                    "istasyon_bul",
                }
                assert len(listing.tools) == 3
                for tool in sorted(listing.tools, key=lambda tool: tool.name):
                    assert tool.description
                    print(ascii((tool.name, tool.description)))
                result = await session.call_tool(
                    "sorgu",
                    {
                        "sql": "SELECT sum(journey_count) AS toplam FROM marts.fct_station_daily"
                    },
                )
                assert not result.is_error
                assert "hata" not in result.structured_content, (
                    result.structured_content
                )
                assert result.structured_content["satirlar"] == [[800]]

    anyio.run(exercise)
