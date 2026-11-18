"""Pure answer checks with optional word-based numeric direction."""

import re
from dataclasses import dataclass
from datetime import date
from math import comb
from typing import Any


def pass_hat_k(c: int, n: int, k: int) -> float:
    """Estimate success on all k independent attempts from n observed runs."""
    if not 0 <= c <= n or not 1 <= k <= n:
        raise ValueError("Pass^k için 0 <= c <= n ve 1 <= k <= n olmalı")
    return comb(c, k) / comb(n, k) if c >= k else 0.0


@dataclass(frozen=True)
class Number:
    value: float
    precision: float = 0
    year: bool = False
    date_part: bool = False


TOKEN = re.compile(
    r"(?<![\w])[-+]?\d+(?:(?:[.,]\d+)+|(?:[ \u00a0\u202f]\d{3})+(?:[.,]\d+)?)?"
    r"(?:\s*(?:milyon|million|bin|k)\b)?(?!\w)",
    re.IGNORECASE,
)


def extract_numbers(answer: str) -> list[Number]:
    """Read localized quantities, retaining the precision of scaled forms."""
    result = []
    date_spans = [
        match.span()
        for match in re.finditer(
            r"\b(?:\d{4}-\d{2}-\d{2}|\d{4}\.\d{2}\.\d{2}|\d{1,2}\.\d{1,2}\.\d{4})\b",
            answer,
        )
    ]
    # Replace separators without shifting offsets, retaining date provenance.
    answer = re.sub(
        r"\b(?:\d{4}-\d{2}-\d{2}|\d{4}\.\d{2}\.\d{2}|\d{1,2}\.\d{1,2}\.\d{4})\b",
        lambda match: re.sub(r"[.-]", "/", match.group()),
        answer,
    )
    for match in TOKEN.finditer(answer):
        text = match.group()
        scale_match = re.search(r"(milyon|million|bin|k)$", text, re.IGNORECASE)
        scale = 1
        if scale_match:
            scale = (
                1_000_000 if scale_match[1].lower() in {"milyon", "million"} else 1000
            )
            text = text[: scale_match.start()].strip()
        text = re.sub(r"\s", "", text)
        separators = re.findall(r"[.,]", text)
        percentage = (
            re.search(r"(?:%|\byüzde)\s*$", answer[: match.start()], re.IGNORECASE)
            is not None
            or re.match(r"\s*%", answer[match.end() :]) is not None
        )
        decimals = 0
        if separators:
            last = max(text.rfind("."), text.rfind(","))
            tail = len(text) - last - 1
            # A single non-three-digit suffix is decimal. With mixed separators,
            # the final separator determines decimal vs grouping by the same rule.
            if tail != 3 or (percentage and len(separators) == 1):
                decimals = tail
                text = re.sub(r"[.,]", "", text[:last]) + "." + text[last + 1 :]
            else:
                text = re.sub(r"[.,]", "", text)
        value = float(text) * scale
        sign = re.search(r"(?<![\w%])([-+])\s*%\s*$", answer[: match.start()])
        if sign and not text.startswith(("-", "+")):
            value *= -1 if sign[1] == "-" else 1
        result.append(
            Number(
                value,
                scale * 10 ** (-decimals) / 2 if scale_match else 0,
                not scale_match and not separators and 1900 <= value <= 2100,
                any(start <= match.start() < end for start, end in date_spans),
            )
        )
    return result


def number_matches(answer: str, expected: float, signed_by_words: bool = False) -> bool:
    """Use half-percent tolerance, widened by written scaled precision."""
    if signed_by_words:
        prose = re.sub(r"```.*?```", "\n", answer, flags=re.DOTALL)
        # A dot between digits belongs to a decimal, grouping or date token.
        sentences = re.split(r"[!?\r\n]|(?<!\d)\.|\.(?!\d)", prose)
        return any(_number_matches_sentence(s, expected, True) for s in sentences)
    return _number_matches_sentence(answer, expected, False)


def _number_matches_sentence(
    answer: str, expected: float, signed_by_words: bool
) -> bool:
    decreasing = (
        re.search(r"\b(?:azal|düş(?!ün|ük)|geril|eksi\b)", answer, re.IGNORECASE)
        is not None
    )
    # "Artık yıl" describes leap-year coverage, not an increase.
    increasing = re.search(r"\bart(?!ık\b)|\byüksel", answer, re.IGNORECASE) is not None
    for number in extract_numbers(answer):
        if number.date_part:
            continue
        if number.year and not 1900 <= expected <= 2100:
            continue
        tolerance = max(0.005 * abs(expected), 0.5, number.precision)
        value = number.value
        if signed_by_words and value > 0 and expected != 0:
            direction_matches = (
                decreasing and not increasing
                if expected < 0
                else increasing and not decreasing
            )
            if not direction_matches:
                continue
            value = -value if expected < 0 else value
        if abs(value - expected) <= tolerance:
            return True
    return False


def _check(name: str, ok: bool, detail: str) -> dict[str, Any]:
    return {"check": name, "ok": bool(ok), "detail": detail}


def _no_large_numbers(answer: str) -> bool:
    return not any(abs(n.value) >= 100000 for n in extract_numbers(answer))


def _refusal_checks(
    answer: str, reference: dict, allow_numbers: bool = False
) -> list[dict]:
    heading = re.search(r"\*\*Nasıl hesaplandı", answer, re.IGNORECASE)
    answer_section = answer[: heading.start()] if heading else answer[:600]
    dates = []
    for row in reference["rows"]:
        for value in row:
            if isinstance(value, str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
                dates.append(date.fromisoformat(value))
    last = max(dates) if dates else None
    months = [
        "Ocak",
        "Şubat",
        "Mart",
        "Nisan",
        "Mayıs",
        "Haziran",
        "Temmuz",
        "Ağustos",
        "Eylül",
        "Ekim",
        "Kasım",
        "Aralık",
    ]
    forms = (
        []
        if last is None
        else [
            last.isoformat(),
            last.strftime("%d.%m.%Y"),
            f"{last.day} {months[last.month - 1]} {last.year}",
        ]
    )
    checks = [
        _check(
            "refusal_date",
            any(form.casefold() in answer.casefold() for form in forms),
            f"Kapsam son tarihi: {last}",
        ),
        _check(
            "refusal_text",
            re.search(
                r"(?i)kapsam dışı|kapsamında değil|kapsamın(ın)? dışında|yok|bulunmuyor|bulunmamakta|içermiyor|söyleyem|verem",
                answer_section,
            )
            is not None,
            "Kapsam dışı red ifadesi",
        ),
    ]
    if not allow_numbers:
        checks.append(
            _check(
                "refusal_number",
                _no_large_numbers(answer),
                "Red cevabında altı veya daha çok basamaklı sayı olmamalı",
            )
        )
    return checks


def score_answer(
    case: dict,
    answer: str,
    reference: dict,
    data_version: str,
    hourly_version: str | None = None,
) -> list[dict]:
    """Return a separate result for every required number, regex and rule check."""
    checks = []
    for column in case.get("numbers", []):
        index = reference["columns"].index(column)
        rows = reference["rows"][: case.get("number_rows")]
        if not rows:
            checks.append(_check(f"number:{column}", False, "Referans satırı yok"))
        for row_index, row in enumerate(rows):
            value = row[index]
            ok = isinstance(value, (int, float)) and number_matches(
                answer, value, signed_by_words=case.get("signed_by_words", False)
            )
            checks.append(
                _check(f"number:{column}:{row_index + 1}", ok, f"Beklenen: {value}")
            )
    for field, positive in (("must_mention", True), ("must_not_mention", False)):
        for pattern in case.get(field, []):
            found = re.search(pattern, answer) is not None
            checks.append(_check(f"{field}:{pattern}", found == positive, pattern))
    if case.get("refusal", False):
        checks.extend(
            _refusal_checks(
                answer, reference, case.get("refusal_allows_numbers", False)
            )
        )
    if case.get("forbid_large_numbers", False):
        checks.append(
            _check(
                "forbid_large_numbers",
                _no_large_numbers(answer),
                "Altı veya daha çok basamaklı sayı olmamalı",
            )
        )
    if "data_version" in case.get("rules", []):
        checks.append(
            _check(
                "data_version",
                bool(data_version) and data_version in answer,
                f"Beklenen veri sürümü: {data_version}",
            )
        )
    if case.get("require_hourly_version", False):
        checks.append(
            _check(
                "hourly_version",
                bool(hourly_version) and hourly_version in answer,
                f"Beklenen saatlik veri sürümü: {hourly_version}"
                if hourly_version
                else "golden'da saatlik_veri_surumu yok",
            )
        )
    return checks
