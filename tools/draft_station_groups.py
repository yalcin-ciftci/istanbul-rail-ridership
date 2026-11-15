"""Draft station groups for human review without changing existing CSV records."""

import argparse
import csv
import io
import os
import re
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

import duckdb

FIELDS = ("station_code", "station_name", "station_group", "status", "note")


def normalize_key(name: str) -> str:
    """Apply the ordered M3 rules, including Turkish dotted/dotless I."""
    key = " ".join(name.translate(str.maketrans({"İ": "i", "I": "ı"})).lower().split())
    key = re.sub(r"\s*\(.*\)$", "", key)
    key = re.sub(r"\s+konkors$", "", key)
    key = re.sub(r"\s+hol\s+\d+$", "", key)
    while True:
        previous = key
        key = re.sub(r"\s+[mtf]\d+$", "", key)
        key = re.sub(r"\s+(kuzey|güney|guney|doğu|dogu|batı|bati)$", "", key)
        key = re.sub(r"(?:\s+|\s*-\s*)\d+$", "", key)
        if key == previous:
            break
    return re.sub(r"^[mtf]\d+\s+", "", key)


def title_case(key: str) -> str:
    """Display a normalised key using Turkish title case per word."""
    upper = str.maketrans({"i": "İ", "ı": "I"})
    return " ".join(word[0].translate(upper).upper() + word[1:] for word in key.split())


def _draft_rows(stations: list[tuple[str, str | None]]) -> list[dict[str, str]]:
    keys = {
        code: normalize_key(name) if name is not None else "" for code, name in stations
    }
    group_prefixes: dict[str, set[str]] = defaultdict(set)
    prefix_groups: dict[str, set[str]] = defaultdict(set)
    for code, key in keys.items():
        if key:
            prefix = code.split("-", 1)[0]
            group_prefixes[key].add(prefix)
            prefix_groups[prefix].add(key)
    split_groups: dict[str, set[str]] = defaultdict(set)
    for prefix, groups in prefix_groups.items():
        if len(groups) > 1:
            for key in groups:
                split_groups[key].add(prefix)

    rows = []
    for code, name in stations:
        key = keys[code]
        reasons = []
        if len(group_prefixes[key]) > 1:
            reasons.append(
                "multiple code prefixes: " + ", ".join(sorted(group_prefixes[key]))
            )
        if split_groups[key]:
            reasons.append(
                "code prefix in multiple groups: "
                + ", ".join(sorted(split_groups[key]))
            )
        if name is None:
            reasons.append("station_name is NULL")
        if re.search(r"\d|\b(?:giriş|girişi|stad|metro|mah\.?)(?!\w)", key):
            reasons.append("normalised key contains a digit or review word")
        rows.append(
            dict(
                zip(
                    FIELDS,
                    (
                        code,
                        name or "",
                        title_case(key),
                        "flagged" if reasons else "rule",
                        "; ".join(reasons),
                    ),
                    strict=True,
                )
            )
        )
    return rows


def _csv_record(values: list[str] | tuple[str, ...]) -> bytes:
    stream = io.StringIO(newline="")
    csv.writer(stream).writerow(values)
    return stream.getvalue().encode("utf-8")


def _existing_records(path: Path) -> tuple[bytes, dict[str, bytes]]:
    if not path.exists():
        return _csv_record(FIELDS), {}
    # csv.reader.line_num also handles quoted notes containing physical newlines.
    text = path.read_bytes().decode("utf-8")
    lines = text.splitlines(keepends=True)
    reader = csv.reader(io.StringIO(text, newline=""), strict=True)
    if next(reader, None) != list(FIELDS):
        raise ValueError(f"Invalid CSV header in {path}; expected {','.join(FIELDS)}")
    end = reader.line_num
    header = "".join(lines[:end]).encode("utf-8")
    records = {}
    for row in reader:
        start, end = end, reader.line_num
        if len(row) != len(FIELDS) or not row[0] or row[0] in records:
            raise ValueError(f"Invalid or duplicate station_code at {path}:{end}")
        records[row[0]] = "".join(lines[start:end]).encode("utf-8")
    return header, records


def _write_atomic(path: Path, content: bytes) -> None:
    descriptor, temporary = tempfile.mkstemp(
        dir=path.parent, prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise


def generate(db: Path, output: Path) -> int:
    """Read dim_station read-only and add only codes absent from the mapping CSV."""
    with duckdb.connect(str(db), read_only=True) as connection:
        stations = connection.execute(
            "select station_code, station_name from marts.dim_station order by station_code"
        ).fetchall()
    header, records = _existing_records(output)
    added = 0
    for row in _draft_rows(stations):
        code = row["station_code"]
        if code not in records:
            records[code] = _csv_record([row[field] for field in FIELDS])
            added += 1
    chunks = [header] + [records[code] for code in sorted(records)]
    content = b""
    for chunk in chunks:
        if content and not content.endswith((b"\n", b"\r")):
            content += b"\r\n"
        content += chunk
    if not output.exists() or output.read_bytes() != content:
        output.parent.mkdir(parents=True, exist_ok=True)
        _write_atomic(output, content)
    return added


def main(argv: list[str] | None = None) -> int:
    """CLI entry point; failures leave a clear diagnostic and exit status 1."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=Path("data/ulasim.duckdb"))
    parser.add_argument(
        "--csv", type=Path, default=Path("transform/seeds/station_group_map.csv")
    )
    args = parser.parse_args(argv)
    try:
        added = generate(args.db, args.csv)
    except (duckdb.Error, OSError, ValueError, csv.Error) as error:
        print(
            f"Station group generation failed (DB: {args.db}, CSV: {args.csv}): {error}",
            file=sys.stderr,
        )
        return 1
    print(f"Station group mapping: added {added} rows to {args.csv}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
