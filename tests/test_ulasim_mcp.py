import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path

import anyio
import duckdb
import pytest
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "ulasim_mcp" / "server.py"


def test_sema_metric_contract(db, tmp_path, monkeypatch):
    contract_path = ROOT / "metrics" / "metrics.toml"
    assert contract_path.is_file(), "Metric contract is missing"
    contract = tomllib.loads(contract_path.read_text(encoding="utf-8"))
    monkeypatch.chdir(tmp_path)
    result = load_server().sema()
    assert result["uyarilar"] == [rule["text_tr"] for rule in contract["rule"]] + [
        "Veri sürümü tablosu yok; analiz kopyasını yeniden üretin."
    ]
    assert result["olcu_sozlesmesi"] == {
        "surum": contract["version"],
        "olculer": [
            {
                "id": m["id"],
                "rol": m["role"],
                "ad": m["label_tr"],
                "tanim": m["definition_tr"],
            }
            for m in contract["metric"]
        ],
    }


@pytest.mark.parametrize("content", [None, "invalid = [", "version = 1"])
def test_sema_unreadable_contract_returns_error(db, tmp_path, monkeypatch, content):
    module = load_server()
    assert hasattr(module, "load_contract"), "Server does not load metric contract"
    loader = sys.modules[module.load_contract.__module__]
    path = tmp_path / "unavailable.toml"
    if content is not None:
        path.write_text(content, encoding="utf-8")
    monkeypatch.setattr(loader, "CONTRACT_PATH", path)
    result = module.sema()
    assert "hata" in result
    assert "sözleşme" in result["hata"].lower()


def load_server():
    assert SERVER.is_file(), "MCP sunucusu henüz uygulanmadı"
    spec = importlib.util.spec_from_file_location("ulasim_server_under_test", SERVER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_data_version_missing_or_empty(db):
    module = load_server()
    assert module.sema()["veri_surumu"] is None
    assert module.sorgu("SELECT 1")["veri_surumu"] is None
    with duckdb.connect(str(db)) as con:
        con.execute(
            "CREATE TABLE marts.dim_source_version (dataset VARCHAR, resource_id VARCHAR, source_month VARCHAR, source_sha256 VARCHAR, stored_sha256 VARCHAR, ingested_at TIMESTAMPTZ)"
        )
    assert module.sema()["veri_surumu"] is None
    assert module.sorgu("SELECT 1")["veri_surumu"] is None


def test_data_version_formula_and_hourly_exclusion(db):
    module = load_server()
    with duckdb.connect(str(db)) as con:
        con.execute(
            "CREATE TABLE marts.dim_source_version (dataset VARCHAR, resource_id VARCHAR, source_month VARCHAR, source_sha256 VARCHAR, stored_sha256 VARCHAR, ingested_at TIMESTAMPTZ)"
        )
        con.executemany(
            "INSERT INTO marts.dim_source_version VALUES (?, ?, ?, ?, ?, ?)",
            [
                (
                    "rail_station_daily",
                    "b",
                    None,
                    None,
                    "b" * 64,
                    "2026-11-15T10:00:00Z",
                ),
                (
                    "rail_station_daily",
                    "a",
                    None,
                    "a" * 64,
                    "c" * 64,
                    "2026-11-16T10:00:00Z",
                ),
            ],
        )
    expected = hashlib.sha256(
        ("a||" + "a" * 64 + "\nb||stored:" + "b" * 64).encode()
    ).hexdigest()[:12]
    version = module.sema()["veri_surumu"]
    assert {k: v for k, v in version.items() if k != "son_alim"} == {
        "kimlik": expected,
        "kaynak_sayisi": 2,
        "kaynak_hash_eksik": 1,
    }
    assert datetime.fromisoformat(version["son_alim"]) == datetime.fromisoformat(
        "2026-11-16T10:00:00+00:00"
    )
    assert module.sorgu("SELECT 1")["veri_surumu"] == expected
    with duckdb.connect(str(db)) as con:
        con.execute(
            "INSERT INTO marts.dim_source_version VALUES ('rail_hourly', 'hour', '2024-01', ?, ?, now())",
            ["d" * 64, "e" * 64],
        )
    assert module.sema()["veri_surumu"] == version
    assert module.sorgu("SELECT 1")["veri_surumu"] == expected


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = tmp_path / "fixture.duckdb"
    with duckdb.connect(str(path)) as con:
        con.execute("CREATE SCHEMA marts")
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
        "/* gizli */ DROP TABLE marts.dim_station",
        "SELECT 1; -- yorum\nDROP TABLE marts.dim_station",
        "SELECT 1 /* ; */; DROP TABLE marts.dim_station",
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


def test_sorgu_comment_inside_single_select_runs_safely(db):
    result = load_server().sorgu("SELECT 1 AS x /* ; DROP TABLE marts.dim_station */")
    assert result["satirlar"] == [[1]]
    with duckdb.connect(str(db)) as con:
        assert con.execute("SELECT count(*) FROM marts.dim_station").fetchone()[0] == 4


@pytest.mark.parametrize(
    "sql_template",
    [
        "SELECT * FROM read_text('{p}')",
        "SELECT * FROM read_blob('{p}')",
        "SELECT * FROM read_json('{p}')",
        "SELECT * FROM read_parquet('{p}')",
        "SELECT * FROM glob('{d}')",
        "SELECT getenv('ULASIM_TEST_GIZLI')",
        "SELECT * FROM read_csv('https://127.0.0.1:9/x.csv')",
    ],
)
def test_sorgu_file_and_network_functions_are_blocked(
    db, tmp_path, monkeypatch, sql_template
):
    secret = tmp_path / "secret.txt"
    secret.write_text("GIZLI-DEGER-123", encoding="utf-8")
    monkeypatch.setenv("ULASIM_TEST_GIZLI", "GIZLI-ORTAM-456")
    sql = sql_template.format(
        p=secret.as_posix().replace("'", "''"),
        d=(tmp_path.as_posix() + "/*").replace("'", "''"),
    )
    result = load_server().sorgu(sql)
    assert "hata" in result, (sql_template, result)
    assert "satirlar" not in result
    serialized = json.dumps(result, ensure_ascii=False)
    assert "GIZLI-DEGER-123" not in serialized
    assert "GIZLI-ORTAM-456" not in serialized


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
               current_setting('lock_configuration'), current_setting('threads'),
               current_setting('memory_limit')
    """)
    assert result["satirlar"] == [["read_only", False, True, 2, "2.0 GiB"]]


@pytest.mark.parametrize("value", ["abc", "0", "-1", "inf", "nan"])
def test_sorgu_invalid_timeout_setting_is_error(db, monkeypatch, value):
    monkeypatch.setenv("ULASIM_SORGU_ZAMAN_ASIMI", value)
    result = load_server().sorgu("SELECT 1")
    assert "ULASIM_SORGU_ZAMAN_ASIMI" in result["hata"]


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
    assert "uv run python tools/publish_analysis_db.py" in result["hata"]
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
    assert module.database_path() == ROOT / "data" / "ulasim_analysis.duckdb"


@pytest.mark.parametrize("schema", ["raw", "staging", "main"])
@pytest.mark.parametrize("kind", ["TABLE", "VIEW"])
@pytest.mark.parametrize(
    "tool,args",
    [
        ("sema", ()),
        ("sorgu", ("SELECT * FROM raw.x",)),
        ("istasyon_bul", ("yenikapi",)),
    ],
)
def test_tools_reject_database_with_non_marts_objects(db, schema, kind, tool, args):
    with duckdb.connect(str(db)) as con:
        con.execute(f"CREATE SCHEMA IF NOT EXISTS {schema}")
        con.execute(f"CREATE {kind} {schema}.x AS SELECT 42 AS hidden")
    assert getattr(load_server(), tool)(*args) == {
        "hata": "Bu veritabanı yalnız marts şemasını içermelidir. Analiz kopyasını üretin: "
        "uv run python tools/publish_analysis_db.py",
        "hata_turu": "sozlesme",
        "ipucu": None,
    }
    # Rejection also releases the connection.
    with duckdb.connect(str(db)) as con:
        assert con.execute(f"SELECT hidden FROM {schema}.x").fetchone() == (42,)


def test_sorgu_missing_raw_table_is_readable_error(db):
    module = load_server()
    result = module.sorgu("SELECT * FROM raw.x")
    assert "hata" in result and "x" in result["hata"]
    assert "Traceback" not in result["hata"]
    assert module.sorgu("SELECT 42")["satirlar"] == [[42]]


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
    assert set(tables) == {
        "marts.dim_station",
        "marts.dim_station_group",
        "marts.fct_station_daily",
    }
    assert tables["marts.dim_station"]["aciklama"] == "İstasyon kodları"
    columns = {col["kolon"]: col for col in tables["marts.dim_station"]["kolonlar"]}
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


def test_sema_table_names_are_directly_queryable(db):
    server = load_server()
    schema = server.sema()
    assert schema["tablolar"]
    for table in schema["tablolar"]:
        result = server.sorgu(f"SELECT * FROM {table['tablo']} LIMIT 1")
        assert "hata" not in result, result
        assert len(result["satirlar"]) == 1


def test_sema_warns_about_unreliable_district(db):
    result = load_server().sema()
    assert any("ilçe" in warning for warning in result["uyarilar"])


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


SIZE_HINT = "Sonuç karakter bütçesini aştı; sütunları azaltın, toplulaştırın ya da filtreyi daraltın."
SQL_HINT = "Tek bir DuckDB SELECT ifadesi yazın; noktalı virgülle birden çok ifade göndermeyin."
BIND_HINT = "Tablo ve kolon adlarını önce `sema` ile kontrol edin; yalnız `marts` şeması vardır."
TIME_HINT = "Tarih aralığını daraltın ya da önce toplulaştırın."


@pytest.mark.parametrize("limit", [3, 10])
def test_character_budget_keeps_longest_unicode_prefix(db, monkeypatch, limit):
    monkeypatch.setenv("ULASIM_YANIT_KARAKTER_SINIRI", "16")
    result = load_server().sorgu(
        "SELECT value FROM (VALUES (1, 'İ'), (2, 'ş'), (3, 'uzun'), (4, 'son')) t(i, value) ORDER BY i",
        limit,
    )
    assert result["satirlar"] == [["İ"], ["ş"]]
    assert len(json.dumps(result["satirlar"], ensure_ascii=False)) == 14
    assert result["satir_sayisi"] == 2
    assert result["kesildi"] is True
    assert result["kesilme_nedeni"] == "karakter_siniri"
    assert result["not"] == SIZE_HINT


@pytest.mark.parametrize(
    "sql,limit,reason",
    [
        ("SELECT i FROM range(3) t(i)", 2, "satir_siniri"),
        ("SELECT i FROM range(2) t(i)", 2, None),
        ("SELECT 1 WHERE false", 2, None),
    ],
)
def test_truncation_reason_and_existing_field_order(db, sql, limit, reason):
    result = load_server().sorgu(sql, limit)
    assert result["kesilme_nedeni"] == reason
    assert list(result) == [
        "veri_surumu",
        "kolonlar",
        "satirlar",
        "satir_sayisi",
        "kesildi",
        "sure_ms",
        "kesilme_nedeni",
    ]
    assert "not" not in result


@pytest.mark.parametrize("budget,count", [(14, 2), (13, 1)])
def test_character_budget_exact_json_boundary(db, monkeypatch, budget, count):
    monkeypatch.setenv("ULASIM_YANIT_KARAKTER_SINIRI", str(budget))
    result = load_server().sorgu("SELECT 'İ' FROM range(2)")
    assert result["satir_sayisi"] == count
    assert result["kesilme_nedeni"] == (None if count == 2 else "karakter_siniri")


@pytest.mark.parametrize("configured", [False, True])
def test_first_row_over_budget_is_structured_error(db, monkeypatch, configured):
    monkeypatch.delenv("ULASIM_YANIT_KARAKTER_SINIRI", raising=False)
    if configured:
        monkeypatch.setenv("ULASIM_YANIT_KARAKTER_SINIRI", "10")
    result = load_server().sorgu("SELECT repeat('x', 50000)")
    assert result["hata_turu"] == "yanit_boyutu"
    assert result["ipucu"] == SIZE_HINT
    assert "hata" in result and "satirlar" not in result


@pytest.mark.parametrize("value", ["abc", "", "0", "-1", "inf", "nan", "1.5", "1e3"])
def test_invalid_character_budget_is_input_error(db, monkeypatch, value):
    monkeypatch.setenv("ULASIM_YANIT_KARAKTER_SINIRI", value)
    result = load_server().sorgu("SELECT 1")
    assert "ULASIM_YANIT_KARAKTER_SINIRI" in result["hata"]
    assert result["hata_turu"] == "girdi"
    assert "ipucu" in result


@pytest.mark.parametrize(
    "sql,kind,hint",
    [
        ("SELEC 1", "sql_ayristirma", SQL_HINT),
        ("SELECT nonexistent FROM marts.dim_station", "sql_baglama", BIND_HINT),
        ("SELECT * FROM marts.nonexistent", "sql_baglama", BIND_HINT),
        ("SELECT 1; SELECT 2", "girdi", None),
    ],
)
def test_query_structured_errors(db, sql, kind, hint):
    result = load_server().sorgu(sql)
    assert result["hata_turu"] == kind
    assert result["ipucu"] == hint
    assert result["hata"]


@pytest.mark.parametrize(
    "tool,args",
    [("sema", ()), ("sorgu", ("SELECT 1",)), ("istasyon_bul", ("yenikapi",))],
)
@pytest.mark.parametrize("failure", ["missing", "outside"])
def test_all_tools_connection_and_contract_error_types(
    db, tmp_path, monkeypatch, tool, args, failure
):
    if failure == "missing":
        monkeypatch.setenv("ULASIM_DB", str(tmp_path / "missing.duckdb"))
    else:
        with duckdb.connect(str(db)) as con:
            con.execute("CREATE TABLE main.outside AS SELECT 1 AS x")
    result = getattr(load_server(), tool)(*args)
    assert result["hata_turu"] == ("baglanti" if failure == "missing" else "sozlesme")
    assert "ipucu" in result


def test_timeout_has_structured_type(db, monkeypatch):
    monkeypatch.setenv("ULASIM_SORGU_ZAMAN_ASIMI", "0.1")
    result = load_server().sorgu("SELECT sum(sin(i)) FROM range(1000000000000) t(i)")
    assert result["hata_turu"] == "zaman_asimi"
    assert result["ipucu"] == TIME_HINT


def test_invalid_timeout_has_input_type(db, monkeypatch):
    monkeypatch.setenv("ULASIM_SORGU_ZAMAN_ASIMI", "nan")
    assert load_server().sorgu("SELECT 1")["hata_turu"] == "girdi"


def test_blank_station_has_input_type():
    assert load_server().istasyon_bul(" ") == {
        "hata": "İstasyon adı boş olamaz.",
        "hata_turu": "girdi",
        "ipucu": None,
    }


@pytest.mark.parametrize(
    "exception,kind,hint",
    [(duckdb.OutOfMemoryException, "bellek", TIME_HINT), (duckdb.Error, "diger", None)],
)
@pytest.mark.parametrize(
    "tool,args,action",
    [
        ("sema", (), "_schema"),
        ("sorgu", ("SELECT 1",), "_data_version"),
        ("istasyon_bul", ("yenikapi",), "_find_stations"),
    ],
)
def test_shared_error_classifier_uses_exception_class(
    db, monkeypatch, exception, kind, hint, tool, args, action
):
    module = load_server()

    def fail(*args):
        raise exception("Parser Binder Out of Memory")

    monkeypatch.setattr(module, action, fail)
    assert getattr(module, tool)(*args) == {
        "hata": "Parser Binder Out of Memory",
        "hata_turu": kind,
        "ipucu": hint,
    }
