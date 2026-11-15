"""İstanbul ulaşım verileri için salt okunur, stdio tabanlı MCP sunucusu."""

import math
import os
import sys
import threading
import time
from collections.abc import Callable
from datetime import date, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

import duckdb
from mcp.server.mcpserver import MCPServer

SORGU_BELLEK_SINIRI = "2GiB"

# Direct stdio execution must also find the shared repository-local loader.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ulasim_mcp.metrics import load_contract


def database_path() -> Path:
    """ULASIM_DB yoksa çalışma dizininden bağımsız depo yolunu döndürür."""
    configured = os.environ.get("ULASIM_DB")
    return (
        Path(configured)
        if configured is not None
        else Path(__file__).resolve().parents[1] / "data" / "ulasim_analysis.duckdb"
    )


def _with_connection(
    action: Callable[[duckdb.DuckDBPyConnection], dict[str, Any]],
) -> dict[str, Any]:
    try:
        con = duckdb.connect(
            str(database_path()),
            read_only=True,
            config={
                "enable_external_access": False,
                "lock_configuration": True,
                "threads": 2,
                "memory_limit": SORGU_BELLEK_SINIRI,
            },
        )
    except (duckdb.Error, OSError, ValueError) as exc:
        return {
            "hata": "Veritabanı şu an kullanılamıyor (güncelleniyor olabilir): "
            + str(exc)
            + ". Analiz kopyasını üretin: uv run python tools/publish_analysis_db.py"
        }
    try:
        with con:
            outside_marts = con.execute(
                """
                SELECT table_name AS name FROM duckdb_tables()
                WHERE database_name = current_database()
                  AND schema_name <> ? AND NOT internal
                UNION ALL
                SELECT view_name AS name FROM duckdb_views()
                WHERE database_name = current_database()
                  AND schema_name <> ? AND NOT internal
                LIMIT 1
                """,
                ["marts", "marts"],
            ).fetchone()
            if outside_marts is not None:
                return {
                    "hata": "Bu veritabanı yalnız marts şemasını içermelidir. "
                    "Analiz kopyasını üretin: uv run python tools/publish_analysis_db.py"
                }
            return action(con)
    except (duckdb.Error, ValueError) as exc:
        return {"hata": str(exc)}


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        value = float(value)
    if isinstance(value, float) and not math.isfinite(value):
        return str(value)
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    return str(value)


def _schema(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    contract = load_contract()
    tables = con.execute(
        """
        SELECT table_name, comment
        FROM duckdb_tables()
        WHERE schema_name = ? AND database_name = current_database()
        ORDER BY table_name
        """,
        ["marts"],
    ).fetchall()
    columns = con.execute(
        """
        SELECT table_name, column_name, data_type, comment
        FROM duckdb_columns()
        WHERE schema_name = ? AND database_name = current_database()
        ORDER BY table_name, column_index
        """,
        ["marts"],
    ).fetchall()
    by_table = {
        name: {"tablo": f"marts.{name}", "aciklama": comment, "kolonlar": []}
        for name, comment in tables
    }
    for table, column, data_type, comment in columns:
        if table in by_table:
            by_table[table]["kolonlar"].append(
                {"kolon": column, "tur": data_type, "aciklama": comment}
            )
    scope = {"min": None, "max": None}
    if "fct_station_daily" in by_table:
        first, last = con.execute(
            "SELECT min(ride_date), max(ride_date) FROM marts.fct_station_daily"
        ).fetchone()
        scope = {"min": _json_value(first), "max": _json_value(last)}
    return {
        "tablolar": list(by_table.values()),
        "kapsam": scope,
        "uyarilar": [rule["text_tr"] for rule in contract["rule"]],
        "olcu_sozlesmesi": {
            "surum": contract["version"],
            "olculer": [
                {
                    "id": metric["id"],
                    "rol": metric["role"],
                    "ad": metric["label_tr"],
                    "tanim": metric["definition_tr"],
                }
                for metric in contract["metric"]
            ],
        },
    }


def sema() -> dict[str, Any]:
    """Veriyi sorgulamadan önce marts tablo ve sütunlarını, kapsamı ve uyarıları öğrenin."""
    return _with_connection(_schema)


def sorgu(sql: str, satir_siniri: int = 200) -> dict[str, Any]:
    """Veri analizi için tek bir SELECT çalıştırın; sınırlı satır ve kesilme bilgisini alın."""
    started = time.perf_counter()

    def execute(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
        statements = con.extract_statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            return {"hata": "Sorgu tek bir SELECT ifadesi içermelidir."}
        limit = max(1, min(1000, satir_siniri))
        try:
            timeout = float(os.environ.get("ULASIM_SORGU_ZAMAN_ASIMI", "30"))
        except ValueError:
            return {
                "hata": "ULASIM_SORGU_ZAMAN_ASIMI pozitif, sonlu bir saniye olmalıdır."
            }
        if not math.isfinite(timeout) or timeout <= 0:
            return {
                "hata": "ULASIM_SORGU_ZAMAN_ASIMI pozitif, sonlu bir saniye olmalıdır."
            }
        timed_out = threading.Event()

        def interrupt() -> None:
            timed_out.set()
            con.interrupt()

        timer = threading.Timer(timeout, interrupt)
        timer.daemon = True
        timer.start()
        try:
            con.execute(statements[0])
            columns = [column[0] for column in con.description]
            rows = con.fetchmany(limit + 1)
        except duckdb.Error:
            if not timed_out.is_set():
                raise
        finally:
            # Wait for a running callback before closing its connection.
            timer.cancel()
            timer.join()
        if timed_out.is_set():
            return {"hata": f"Sorgu zaman aşımı: {timeout:g} saniye sınırı aşıldı."}
        return {
            "kolonlar": columns,
            "satirlar": [[_json_value(value) for value in row] for row in rows[:limit]],
            "satir_sayisi": min(len(rows), limit),
            "kesildi": len(rows) > limit,
            "sure_ms": round((time.perf_counter() - started) * 1000, 3),
        }

    return _with_connection(execute)


def _match_key(name: str) -> str:
    lowered = name.translate(str.maketrans({"İ": "i", "I": "ı"})).lower()
    folded = lowered.translate(str.maketrans("ığüşöç", "igusoc"))
    return " ".join(folded.split())


def _score(query: str, name: str | None) -> float:
    if not name:
        return 0.0
    key = _match_key(name)
    if query == key:
        return 1.0
    if key.startswith(query):
        return 0.9
    if query in key:
        return 0.8
    return SequenceMatcher(None, query, key).ratio()


def _stations(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    has_group = con.execute(
        """
        SELECT count(*) FROM duckdb_columns()
        WHERE database_name = current_database() AND schema_name = ?
          AND table_name = ? AND column_name = ?
        """,
        ["marts", "dim_station", "station_group"],
    ).fetchone()[0]
    if has_group:
        return con.execute(
            """
            SELECT station_code, station_name, station_group, district,
                   first_seen_date, last_seen_date
            FROM marts.dim_station
            """
        ).fetchall()
    return con.execute(
        """
        SELECT station_code, station_name, station_name AS station_group, district,
               first_seen_date, last_seen_date
        FROM marts.dim_station
        """
    ).fetchall()


def _find_stations(
    con: duckdb.DuckDBPyConnection, key: str, limit: int
) -> dict[str, Any]:
    lines_by_code: dict[str, set[str]] = {}
    for code, line in con.execute(
        """
        SELECT DISTINCT station_code, line_name FROM marts.fct_station_daily
        WHERE line_name IS NOT NULL
        """
    ).fetchall():
        lines_by_code.setdefault(code, set()).add(line)
    groups: dict[str | None, dict[str, Any]] = {}
    for code, name, group, district, first, last in _stations(con):
        entry = groups.setdefault(
            group,
            {
                "station_group": group,
                "skor": 0.0,
                "istasyon_kodlari": set(),
                "adlar": set(),
                "hatlar": set(),
                "ilceler": set(),
                "ilk_tarih": None,
                "son_tarih": None,
            },
        )
        entry["skor"] = max(entry["skor"], _score(key, name), _score(key, group))
        if code is not None:
            entry["istasyon_kodlari"].add(code)
        if name is not None:
            entry["adlar"].add(name)
        if district is not None:
            entry["ilceler"].add(district)
        entry["hatlar"].update(lines_by_code.get(code, ()))
        if first is not None and (
            entry["ilk_tarih"] is None or first < entry["ilk_tarih"]
        ):
            entry["ilk_tarih"] = first
        if last is not None and (
            entry["son_tarih"] is None or last > entry["son_tarih"]
        ):
            entry["son_tarih"] = last
    matches = sorted(
        (entry for entry in groups.values() if entry["skor"] >= 0.6),
        key=lambda entry: (-entry["skor"], entry["station_group"] or ""),
    )[:limit]
    for entry in matches:
        for field in ("istasyon_kodlari", "adlar", "hatlar", "ilceler"):
            entry[field] = sorted(entry[field])
    return {"sonuclar": _json_value(matches)}


def istasyon_bul(ad: str, en_fazla: int = 10) -> dict[str, Any]:
    """İstasyon adını veya yazım varyantını arayarak grup, kod, hat ve tarihleri bulun."""
    key = _match_key(ad)
    if not key:
        return {"hata": "İstasyon adı boş olamaz."}
    limit = max(1, min(50, en_fazla))
    return _with_connection(lambda con: _find_stations(con, key, limit))


server = MCPServer("ulasim")
for function in (sema, sorgu, istasyon_bul):
    server.tool()(function)


if __name__ == "__main__":
    try:
        server.run()
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"MCP sunucusu başlatılamadı: {exc}", file=sys.stderr)
        sys.exit(1)
