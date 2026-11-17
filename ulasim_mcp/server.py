"""İstanbul ulaşım verileri için salt okunur, stdio tabanlı MCP sunucusu."""

import hashlib
import json
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
YANIT_BOYUTU_IPUCU = "Sonuç karakter bütçesini aştı; sütunları azaltın, toplulaştırın ya da filtreyi daraltın."
KAYNAK_IPUCU = "Tarih aralığını daraltın ya da önce toplulaştırın."

# Direct stdio execution must also find the shared repository-local loader.
if not __package__:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ulasim_mcp.metrics import load_contract


def _error(message: str, kind: str, hint: str | None = None) -> dict[str, Any]:
    return {"hata": message, "hata_turu": kind, "ipucu": hint}


def _exception_error(exc: Exception) -> dict[str, Any]:
    if isinstance(exc, duckdb.ParserException):
        return _error(
            str(exc),
            "sql_ayristirma",
            "Tek bir DuckDB SELECT ifadesi yazın; noktalı virgülle birden çok ifade göndermeyin.",
        )
    if isinstance(exc, (duckdb.BinderException, duckdb.CatalogException)):
        return _error(
            str(exc),
            "sql_baglama",
            "Tablo ve kolon adlarını önce `sema` ile kontrol edin; yalnız `marts` şeması vardır.",
        )
    if isinstance(exc, duckdb.OutOfMemoryException):
        return _error(str(exc), "bellek", KAYNAK_IPUCU)
    return _error(str(exc), "diger")


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
        return _error(
            "Veritabanı şu an kullanılamıyor (güncelleniyor olabilir): "
            + str(exc)
            + ". Analiz kopyasını üretin: uv run python tools/publish_analysis_db.py",
            "baglanti",
        )
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
                return _error(
                    "Bu veritabanı yalnız marts şemasını içermelidir. "
                    "Analiz kopyasını üretin: uv run python tools/publish_analysis_db.py",
                    "sozlesme",
                )
            return action(con)
    except (duckdb.Error, ValueError) as exc:
        return _exception_error(exc)


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


def _version_identity(rows: list[tuple]) -> str:
    keys = sorted(
        f"{resource}|{month or ''}|{source or 'stored:' + stored}"
        for resource, month, source, stored, _ in rows
    )
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:12]


def _data_version(con: duckdb.DuckDBPyConnection) -> dict[str, Any] | None:
    exists = con.execute(
        "SELECT count(*) FROM information_schema.tables "
        "WHERE table_catalog = current_database() AND table_schema = ? AND table_name = ?",
        ["marts", "dim_source_version"],
    ).fetchone()[0]
    if not exists:
        return None
    rows = con.execute(
        "SELECT resource_id, source_month, source_sha256, stored_sha256, ingested_at "
        "FROM marts.dim_source_version WHERE dataset = ?",
        ["rail_station_daily"],
    ).fetchall()
    if not rows:
        return None
    hourly_rows = con.execute(
        "SELECT resource_id, source_month, source_sha256, stored_sha256, ingested_at "
        "FROM marts.dim_source_version WHERE dataset = ?",
        ["rail_hourly"],
    ).fetchall()
    return {
        "kimlik": _version_identity(rows),
        "kaynak_sayisi": len(rows),
        "kaynak_hash_eksik": sum(row[2] is None for row in rows),
        "son_alim": _json_value(max(row[4] for row in rows)),
        "saatlik_kimlik": _version_identity(hourly_rows) if hourly_rows else None,
        "saatlik_kaynak_sayisi": len(hourly_rows) if hourly_rows else None,
    }


def _schema(con: duckdb.DuckDBPyConnection) -> dict[str, Any]:
    try:
        contract = load_contract()
    except ValueError as exc:
        return _error(str(exc), "sozlesme")
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
    version = _data_version(con)
    warnings = [rule["text_tr"] for rule in contract["rule"]]
    if version is None:
        warnings.append("Veri sürümü tablosu yok; analiz kopyasını yeniden üretin.")
    return {
        "tablolar": list(by_table.values()),
        "kapsam": scope,
        "uyarilar": warnings,
        "veri_surumu": version,
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
            return _error("Sorgu tek bir SELECT ifadesi içermelidir.", "girdi")
        limit = max(1, min(1000, satir_siniri))
        try:
            timeout = float(os.environ.get("ULASIM_SORGU_ZAMAN_ASIMI", "30"))
        except ValueError:
            return _error(
                "ULASIM_SORGU_ZAMAN_ASIMI pozitif, sonlu bir saniye olmalıdır.", "girdi"
            )
        if not math.isfinite(timeout) or timeout <= 0:
            return _error(
                "ULASIM_SORGU_ZAMAN_ASIMI pozitif, sonlu bir saniye olmalıdır.", "girdi"
            )
        try:
            character_limit = int(
                os.environ.get("ULASIM_YANIT_KARAKTER_SINIRI", "50000")
            )
            if character_limit <= 0:
                raise ValueError
        except ValueError:
            return _error(
                "ULASIM_YANIT_KARAKTER_SINIRI pozitif, sonlu bir tam sayı olmalıdır.",
                "girdi",
            )
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
            return _error(
                f"Sorgu zaman aşımı: {timeout:g} saniye sınırı aşıldı.",
                "zaman_asimi",
                KAYNAK_IPUCU,
            )
        result_rows = [[_json_value(value) for value in row] for row in rows[:limit]]
        reason = "satir_siniri" if len(rows) > limit else None
        if (
            result_rows
            and len(json.dumps(result_rows, ensure_ascii=False)) > character_limit
        ):
            if (
                result_rows
                and len(json.dumps(result_rows[:1], ensure_ascii=False))
                > character_limit
            ):
                return _error(
                    "İlk satır yanıt karakter bütçesini aşıyor.",
                    "yanit_boyutu",
                    YANIT_BOYUTU_IPUCU,
                )
            low, high = 0, len(result_rows)
            while low < high:
                middle = (low + high + 1) // 2
                if (
                    len(json.dumps(result_rows[:middle], ensure_ascii=False))
                    <= character_limit
                ):
                    low = middle
                else:
                    high = middle - 1
            result_rows = result_rows[:low]
            reason = "karakter_siniri"
        version = _data_version(con)
        result = {
            "veri_surumu": version["kimlik"] if version is not None else None,
            "kolonlar": columns,
            "satirlar": result_rows,
            "satir_sayisi": len(result_rows),
            "kesildi": reason is not None,
            "sure_ms": round((time.perf_counter() - started) * 1000, 3),
            "kesilme_nedeni": reason,
        }
        if "fct_station_group_hour_profile" in sql.lower():
            result["saatlik_veri_surumu"] = (
                version["saatlik_kimlik"] if version is not None else None
            )
        if reason == "karakter_siniri":
            result["not"] = YANIT_BOYUTU_IPUCU
        return result

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
        return _error("İstasyon adı boş olamaz.", "girdi")
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
