"""Generate versioned references, collect agent answers, and score text files."""

import argparse
import importlib.util
import json
import re
import subprocess
import sys
import tomllib
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import duckdb

ROOT = Path(__file__).resolve().parents[1]
GOLDEN_DIR = ROOT / "evals/golden"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from evals.scoring import score_answer


def repo_path(value: str | Path) -> Path:
    """Resolve relative arguments against the repository, regardless of cwd."""
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def validate_sql(sql: str) -> None:
    """Parse SQL without executing it; allow one SELECT over marts and CTEs."""
    with duckdb.connect(":memory:") as con:
        statements = con.extract_statements(sql)
        if len(statements) != 1 or statements[0].type != duckdb.StatementType.SELECT:
            raise ValueError("reference_sql tek SELECT olmalı")
        parsed = json.loads(
            con.execute("SELECT json_serialize_sql(?)", [sql]).fetchone()[0]
        )
    if parsed.get("error"):
        raise ValueError("reference_sql ayrıştırılamadı")

    def walk(node: Any, ctes: frozenset = frozenset()) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item, ctes)
        elif isinstance(node, dict):
            ctes = ctes | {
                item["key"] for item in node.get("cte_map", {}).get("map", [])
            }
            if node.get("type") == "BASE_TABLE":
                schema = node.get("schema_name", "")
                table = node["table_name"]
                if node.get("catalog_name") or not (
                    schema.lower() == "marts" or (not schema and table in ctes)
                ):
                    raise ValueError("reference_sql yalnız marts.* kullanmalı")
            if node.get("type") == "TABLE_FUNCTION":
                raise ValueError("reference_sql tablo fonksiyonu kullanamaz")
            for value in node.values():
                walk(value, ctes)

    walk(parsed)


def load_cases(path: Path) -> list[dict]:
    """Load validated cases, rejecting duplicate IDs and malformed controls."""
    cases = tomllib.loads(path.read_text(encoding="utf-8"))["case"]
    seen = set()
    required = {
        "id",
        "question_tr",
        "reference_sql",
        "numbers",
        "must_mention",
        "must_not_mention",
        "rules",
        "refusal",
    }
    for case in cases:
        if not required <= case.keys():
            raise ValueError("Vaka zorunlu alanları eksik")
        case_id = case["id"]
        if not re.fullmatch(r"[a-z0-9_]+", case_id) or case_id in seen:
            raise ValueError(f"Geçersiz veya tekrarlanan vaka id: {case_id}")
        seen.add(case_id)
        for field in ("numbers", "must_mention", "must_not_mention", "rules"):
            if not isinstance(case[field], list) or not all(
                isinstance(x, str) for x in case[field]
            ):
                raise ValueError(f"Geçersiz alan: {case_id}.{field}")
        if not isinstance(case["refusal"], bool):
            raise TypeError(f"Geçersiz refusal: {case_id}")
        if "number_rows" in case and (
            type(case["number_rows"]) is not int or case["number_rows"] < 1
        ):
            raise ValueError(f"Geçersiz number_rows: {case_id}")
        for pattern in case["must_mention"] + case["must_not_mention"]:
            re.compile(pattern)
        validate_sql(case["reference_sql"])
    if not cases:
        raise ValueError("Vaka listesi boş")
    return cases


def _json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral_value() else float(value)
    raise TypeError(f"JSON'a dönüştürülemeyen değer: {type(value).__name__}")


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=_json_value) + "\n",
        encoding="utf-8",
    )


def golden(cases: list[dict], db: Path) -> int:
    """Evaluate references through a read-only connection and shared version ID."""
    spec = importlib.util.spec_from_file_location(
        "eval_reference_server", ROOT / "ulasim_mcp/server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    with duckdb.connect(
        str(db), read_only=True, config={"enable_external_access": False}
    ) as con:
        version = module._data_version(con)
        if version is None:
            raise ValueError("veri sürümü yok")
        references = {}
        for case in cases:
            validate_sql(case["reference_sql"])
            cursor = con.execute(case["reference_sql"])
            references[case["id"]] = {
                "columns": [column[0] for column in cursor.description],
                "rows": [list(row) for row in cursor.fetchall()],
            }
    payload = json.loads(
        json.dumps(
            {
                "veri_surumu": version["kimlik"],
                "olusturma": datetime.now(UTC).isoformat(),
                "cases": references,
            },
            default=_json_value,
        )
    )
    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    destination = GOLDEN_DIR / f"{version['kimlik']}.json"
    if destination.exists():
        previous = json.loads(destination.read_text("utf-8"))
        for case_id in sorted(previous["cases"].keys() | references.keys()):
            if previous["cases"].get(case_id) != payload["cases"].get(case_id):
                print(f"Warning: değişen vaka: {case_id}")
    _write_json(destination, payload)
    print(f"golden={destination}")
    return 0


def score(cases: list[dict], answers: Path, golden_file: Path | None) -> int:
    """Write machine-readable and human-readable results, including missing answers."""
    if golden_file is None:
        files = list(GOLDEN_DIR.glob("*.json"))
        if not files:
            raise ValueError("golden dosyası yok")
        golden_file = max(files, key=lambda p: (p.stat().st_mtime_ns, p.name))
    reference = json.loads(golden_file.read_text("utf-8"))
    results = {}
    for case in cases:
        case_id = case["id"]
        path = answers / f"{case_id}.md"
        if not path.exists():
            checks = [{"check": "answer", "ok": False, "detail": "cevap yok"}]
        elif case_id not in reference["cases"]:
            checks = [
                {"check": "reference", "ok": False, "detail": "golden içinde vaka yok"}
            ]
        else:
            checks = score_answer(
                case,
                path.read_text("utf-8"),
                reference["cases"][case_id],
                reference["veri_surumu"],
            )
        results[case_id] = {
            "ok": all(check["ok"] for check in checks),
            "checks": checks,
        }
    passed = sum(result["ok"] for result in results.values())
    report = {
        "veri_surumu": reference["veri_surumu"],
        "passed": passed,
        "total": len(cases),
        "cases": results,
    }
    answers.mkdir(parents=True, exist_ok=True)
    _write_json(answers / "score.json", report)
    lines = [
        f"passed={passed}/{len(cases)}",
        f"Veri sürümü: {reference['veri_surumu']}",
        "",
    ]
    for case_id, result in results.items():
        lines.append(f"- {case_id}: {'geçti' if result['ok'] else 'kaldı'}")
        for check in result["checks"]:
            if not check["ok"]:
                lines.append(f"  - {check['check']}: {check['detail']}")
    (answers / "score.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"passed={passed}/{len(cases)}")
    return 0 if passed == len(cases) else 2


# Headless runs cannot answer permission prompts; allow only the agent's MCP tools.
# The flag is variadic, so it must use the '=' form or it would swallow the question.
AGENT_TOOLS = "mcp__ulasim__sema,mcp__ulasim__sorgu,mcp__ulasim__istasyon_bul"


def agent_command(agent: str, question: str) -> list[str]:
    """Build arguments without invoking a shell."""
    return ["claude", "-p", "--agent", agent, f"--allowedTools={AGENT_TOOLS}", question]


def ask(cases: list[dict], answers: Path, agent: str, only: str | None) -> int:
    """Collect answers; record per-case process failures and continue."""
    selected = set(only.split(",")) if only else {case["id"] for case in cases}
    unknown = selected - {case["id"] for case in cases}
    if unknown:
        raise ValueError(f"Bilinmeyen vaka: {', '.join(sorted(unknown))}")
    answers.mkdir(parents=True, exist_ok=True)
    for case in cases:
        if case["id"] not in selected:
            continue
        command = agent_command(agent, case["question_tr"])
        try:
            result = subprocess.run(
                command,
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=300,
                check=False,
            )
            answer = (
                result.stdout
                if result.returncode == 0
                else f"ERROR: exit={result.returncode}: {result.stderr}"
            )
        except (OSError, subprocess.SubprocessError) as exc:
            answer = f"ERROR: {exc}"
        (answers / f"{case['id']}.md").write_text(answer, encoding="utf-8")
    return 0


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; operational failures return 1, scoring failures return 2."""
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("golden", "score", "ask"):
        sub = commands.add_parser(name)
        sub.add_argument("--cases", default="evals/cases.toml")
        if name == "golden":
            sub.add_argument("--db", default="data/ulasim_analysis.duckdb")
        else:
            sub.add_argument("--answers", required=True)
        if name == "score":
            sub.add_argument("--golden")
        if name == "ask":
            sub.add_argument("--agent", default="ulasim")
            sub.add_argument("--only")
    args = parser.parse_args(argv)
    try:
        cases = load_cases(repo_path(args.cases))
        if args.command == "golden":
            return golden(cases, repo_path(args.db))
        if args.command == "score":
            return score(
                cases,
                repo_path(args.answers),
                repo_path(args.golden) if args.golden else None,
            )
        return ask(cases, repo_path(args.answers), args.agent, args.only)
    except (OSError, ValueError, KeyError, TypeError, duckdb.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
