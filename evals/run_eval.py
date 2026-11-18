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

from evals.scoring import pass_hat_k, score_answer


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
        for field in (
            "refusal",
            "forbid_large_numbers",
            "signed_by_words",
            "require_hourly_version",
        ):
            if field in case and not isinstance(case[field], bool):
                raise TypeError(f"Geçersiz {field}: {case_id}")
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
                "saatlik_veri_surumu": version.get("saatlik_kimlik"),
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


def select_cases(cases: list[dict], only: str | None) -> list[dict]:
    """Filter case IDs while retaining their declared order."""
    selected = (
        set(only.split(",")) if only is not None else {case["id"] for case in cases}
    )
    unknown = selected - {case["id"] for case in cases}
    if unknown:
        raise ValueError(f"Bilinmeyen vaka: {', '.join(sorted(unknown))}")
    return [case for case in cases if case["id"] in selected]


def _answer_checks(case: dict, path: Path, reference: dict) -> list[dict]:
    if not path.exists():
        return [{"check": "answer", "ok": False, "detail": "cevap yok"}]
    if case["id"] not in reference["cases"]:
        return [{"check": "reference", "ok": False, "detail": "golden içinde vaka yok"}]
    return score_answer(
        case,
        path.read_text("utf-8"),
        reference["cases"][case["id"]],
        reference["veri_surumu"],
        reference.get("saatlik_veri_surumu"),
    )


def score(
    cases: list[dict], answers: Path, golden_file: Path | None, only: str | None = None
) -> int:
    """Write machine-readable and human-readable results, including missing answers."""
    cases = select_cases(cases, only)
    if golden_file is None:
        files = list(GOLDEN_DIR.glob("*.json"))
        if not files:
            raise ValueError("golden dosyası yok")
        golden_file = max(files, key=lambda p: (p.stat().st_mtime_ns, p.name))
    reference = json.loads(golden_file.read_text("utf-8"))
    results = {}
    repeated_paths = {
        case["id"]: sorted(
            (
                p
                for p in answers.glob(f"{case['id']}__r*.md")
                if re.fullmatch(rf"{case['id']}__r[1-9]\d*\.md", p.name)
            ),
            key=lambda p: int(p.stem.rsplit("__r", 1)[1]),
        )
        for case in cases
    }
    repeated = any(repeated_paths.values())
    for case in cases:
        case_id = case["id"]
        paths = repeated_paths[case_id] or [answers / f"{case_id}.md"]
        run_results = []
        for path in paths:
            checks = _answer_checks(case, path, reference)
            run_results.append(
                {
                    "file": path.name,
                    "ok": all(c["ok"] for c in checks),
                    "checks": checks,
                }
            )
        checks = [
            {**check, **({"run": run["file"]} if repeated else {})}
            for run in run_results
            for check in run["checks"]
        ]
        results[case_id] = {
            "ok": all(run["ok"] for run in run_results),
            "checks": checks,
        }
        if repeated:
            n = len(run_results)
            c = sum(run["ok"] for run in run_results)
            results[case_id].update(
                runs=n,
                passes=c,
                pass_rate=c / n,
                pass_hat_k=pass_hat_k(c, n, n),
                run_results=run_results,
            )
    passed = sum(result["ok"] for result in results.values())
    report = {
        "veri_surumu": reference["veri_surumu"],
        "passed": passed,
        "total": len(cases),
        "cases": results,
    }
    if repeated:
        report.update(
            mean_pass_at_1=sum(r["pass_rate"] for r in results.values()) / len(cases),
            mean_pass_hat_k=sum(r["pass_hat_k"] for r in results.values()) / len(cases),
        )
    answers.mkdir(parents=True, exist_ok=True)
    _write_json(answers / "score.json", report)
    lines = [
        f"passed={passed}/{len(cases)}",
        f"Veri sürümü: {reference['veri_surumu']}",
        "",
    ]
    if repeated:
        lines.extend(
            [
                f"Ortalama pass@1: {report['mean_pass_at_1']:.6f}",
                f"Ortalama Pass^k: {report['mean_pass_hat_k']:.6f}",
                "",
                "| Vaka | Sonuç | c/n |",
                "| --- | --- | --- |",
            ]
        )
        for case_id, result in results.items():
            lines.append(
                f"| {case_id} | {'geçti' if result['ok'] else 'kaldı'} | "
                f"{result['passes']}/{result['runs']} |"
            )
        lines.append("")
    for case_id, result in results.items():
        lines.append(f"- {case_id}: {'geçti' if result['ok'] else 'kaldı'}")
        for check in result["checks"]:
            if not check["ok"]:
                run_label = f" [{check['run']}]" if repeated else ""
                lines.append(f"  - {check['check']}{run_label}: {check['detail']}")
    (answers / "score.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"passed={passed}/{len(cases)}")
    if repeated:
        print(
            f"mean_pass_at_1={report['mean_pass_at_1']:.6f} "
            f"mean_pass_hat_k={report['mean_pass_hat_k']:.6f}"
        )
    return 0 if passed == len(cases) else 2


# Headless runs cannot answer permission prompts; allow only the agent's MCP tools.
# The flag is variadic, so it must use the '=' form or it would swallow the question.
AGENT_TOOLS = "mcp__ulasim__sema,mcp__ulasim__sorgu,mcp__ulasim__istasyon_bul"


EXAMPLES = ROOT / "examples/claude-code"


def prepare_workdir(workdir: Path) -> list[str]:
    """Write the example agent and MCP config into an isolated directory.

    Running from a directory outside any configured project keeps project hooks,
    CLAUDE.md files and other MCP servers out of the agent's context. Returns the
    extra CLI arguments that load only these two files.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    root = ROOT.as_posix()
    mcp = (EXAMPLES / "mcp.json").read_text(encoding="utf-8")
    (workdir / "mcp.json").write_text(
        mcp.replace("<path-to-repo>", root), encoding="utf-8"
    )
    text = (EXAMPLES / "ulasim.md").read_text(encoding="utf-8")
    text = text.replace("<path-to-repo>", root).replace("\r\n", "\n")
    match = re.match(r"---\n(.*?)\n---\n(.*)", text, re.DOTALL)
    if match is None:
        raise ValueError("examples/claude-code/ulasim.md frontmatter okunamadı")
    front = dict(line.split(": ", 1) for line in match[1].splitlines())
    agents = {
        front["name"]: {
            "description": front["description"],
            "prompt": match[2].strip(),
            "tools": [tool.strip() for tool in front["tools"].split(",")],
            "model": front["model"],
        }
    }
    (workdir / "agents.json").write_text(
        json.dumps(agents, ensure_ascii=False), encoding="utf-8"
    )
    return [
        "--strict-mcp-config",
        "--mcp-config",
        str(workdir / "mcp.json"),
        "--agents",
        str(workdir / "agents.json"),
    ]


def agent_command(
    agent: str, question: str, extra: list[str] | None = None
) -> list[str]:
    """Build arguments without invoking a shell."""
    return [
        "claude",
        "-p",
        *(extra or []),
        "--agent",
        agent,
        f"--allowedTools={AGENT_TOOLS}",
        question,
    ]


def ask(
    cases: list[dict],
    answers: Path,
    agent: str,
    only: str | None,
    workdir: Path | None = None,
    repeat: int = 1,
) -> int:
    """Collect answers; record per-case process failures and continue."""
    cases = select_cases(cases, only)
    if repeat < 1:
        raise ValueError("repeat en az 1 olmalı")
    answers.mkdir(parents=True, exist_ok=True)
    extra = prepare_workdir(workdir) if workdir is not None else None
    for case in cases:
        for run in range(1, repeat + 1):
            suffix = "" if repeat == 1 else f"__r{run}"
            _ask_once(case, answers / f"{case['id']}{suffix}.md", agent, extra, workdir)
    return 0


def _ask_once(
    case: dict,
    destination: Path,
    agent: str,
    extra: list[str] | None,
    workdir: Path | None,
) -> None:
    command = agent_command(agent, case["question_tr"], extra)
    try:
        result = subprocess.run(
            command,
            cwd=workdir,
            stdin=subprocess.DEVNULL,
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
    destination.write_text(answer, encoding="utf-8")


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
            sub.add_argument("--only")
        if name == "ask":
            sub.add_argument("--agent", default="ulasim")
            sub.add_argument("--only")
            sub.add_argument("--repeat", type=int, default=1)
            sub.add_argument(
                "--workdir",
                help="isolated directory outside any Claude project; loads only "
                "the example agent and MCP config (recommended)",
            )
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
                args.only,
            )
        return ask(
            cases,
            repo_path(args.answers),
            args.agent,
            args.only,
            Path(args.workdir).resolve() if args.workdir else None,
            args.repeat,
        )
    except (OSError, ValueError, KeyError, TypeError, duckdb.Error) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
