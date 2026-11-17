"""Shared, repository-relative metric contract loader."""

import tomllib
from pathlib import Path
from typing import Any

CONTRACT_PATH = Path(__file__).resolve().parents[1] / "metrics" / "metrics.toml"


def load_contract() -> dict[str, Any]:
    """Read the contract afresh; report unreadable or invalid metadata as ValueError."""
    try:
        with CONTRACT_PATH.open("rb") as stream:
            contract = tomllib.load(stream)
        if type(contract["version"]) is not int:
            raise ValueError("version must be an integer")
        for kind, fields in (
            ("metric", ("id", "role", "label_tr", "definition_tr", "description_en")),
            ("rule", ("id", "text_tr")),
        ):
            entries = contract[kind]
            if not isinstance(entries, list) or not entries:
                raise ValueError(f"{kind} must be a nonempty list")
            seen = set()
            for entry in entries:
                if not all(
                    isinstance(entry[field], str) and entry[field].strip()
                    for field in fields
                ):
                    raise ValueError(f"{kind} has empty or invalid fields")
                if entry["id"] in seen:
                    raise ValueError(f"duplicate {kind} id: {entry['id']}")
                seen.add(entry["id"])
        for metric in contract["metric"]:
            if metric["role"] not in {
                "default",
                "source",
                "restricted",
                "analysis",
                "interpretation",
            }:
                raise ValueError("invalid metric role")
            tables = metric["tables"]
            if (
                not isinstance(tables, list)
                or not tables
                or not all(isinstance(table, str) and table.strip() for table in tables)
            ):
                raise ValueError("metric tables must be a nonempty list of names")
        if sum(m["role"] == "default" for m in contract["metric"]) != 1:
            raise ValueError("exactly one default metric is required")
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise ValueError(f"Ölçü sözleşmesi okunamadı ({CONTRACT_PATH}): {exc}") from exc
    return contract
