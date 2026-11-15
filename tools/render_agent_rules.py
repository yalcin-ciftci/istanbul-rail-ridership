"""Render the agent's metric rule block from the shared TOML contract."""

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ulasim_mcp.metrics import load_contract

AGENT_PATH = ROOT / "examples" / "claude-code" / "ulasim.md"
BEGIN = "<!-- metrics:begin -->"
END = "<!-- metrics:end -->"


def main(argv: list[str] | None = None) -> int:
    """Render only the marked block, or check it without writing."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    try:
        source = AGENT_PATH.read_text(encoding="utf-8")
        if source.count(BEGIN) != 1 or source.count(END) != 1:
            raise ValueError("Ajan dosyasında birer metrics:begin/end işareti gerekli.")
        before, rest = source.split(BEGIN)
        _, after = rest.split(END)
        numbers = re.findall(r"^(\d+)\. ", before, flags=re.MULTILINE)
        start = int(numbers[-1]) + 1 if numbers else 1
        rules = load_contract()["rule"]
        block = (
            "\n"
            + "\n".join(
                f"{index}. {rule['text_tr']}" for index, rule in enumerate(rules, start)
            )
            + "\n"
        )
        rendered = before + BEGIN + block + END + after
        if rendered != source:
            if args.check:
                print(
                    "Ajan ölçü kuralları güncel değil; üreticiyi çalıştırın.",
                    file=sys.stderr,
                )
                return 1
            AGENT_PATH.write_text(rendered, encoding="utf-8")
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
