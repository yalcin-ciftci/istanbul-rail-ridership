import csv
import importlib.util
from pathlib import Path

import duckdb
import pytest


def load_draft():
    path = Path(__file__).resolve().parents[1] / "tools/draft_station_groups.py"
    assert path.is_file(), "Station group generator has not been implemented"
    spec = importlib.util.spec_from_file_location("draft_station_groups", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_db(path, rows):
    with duckdb.connect(str(path)) as con:
        con.execute("create schema marts")
        con.execute(
            "create table marts.dim_station "
            "(station_code varchar, station_name varchar)"
        )
        if rows:
            con.executemany("insert into marts.dim_station values (?, ?)", rows)


def read_rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("Yenikapı-2", "yenikapı"),
        ("MAHMUTBEY M3 HOL 3", "mahmutbey"),
        ("4 Levent 2 Güney", "4 levent"),
        ("Seyrantepe 1 Batı", "seyrantepe"),
        ("M7 YILDIZ 1", "yıldız"),
        ("Acıbadem (Doğu)", "acıbadem"),
        ("Levent Batı konkors", "levent"),
        ("  İSTİNYE\t  IHLAMUR  ", "istinye ıhlamur"),
        ("M2 Levent M2 1 Batı", "levent"),
        ("Kartal (Batı)", "kartal"),
        ("Halkalı GÜNEY", "halkalı"),
        ("M7 FULYA", "fulya"),
    ],
)
def test_normalisation_examples(name, key):
    draft = load_draft()
    assert draft.normalize_key(name) == key


@pytest.mark.parametrize(
    ("name", "key"),
    [
        ("boğaz köprüsü 2", "boğaz köprüsü"),
        ("bakırköy-1", "bakırköy"),
        ("bakırköy - 1", "bakırköy"),
        ("Mahmutbey M3", "mahmutbey"),
        ("Mahmutbey M3 Güney 2", "mahmutbey"),
        ("Mahmutbeym3", "mahmutbeym3"),
        ("İstasyon2", "istasyon2"),
        ("İstasyon t2 Batı f1 3", "istasyon"),
    ],
)
def test_revised_suffix_order_and_separated_numbers(name, key):
    draft = load_draft()
    assert draft.normalize_key(name) == key


@pytest.mark.parametrize(
    ("key", "display"),
    [
        ("yenikapı", "Yenikapı"),
        ("4 levent", "4 Levent"),
        ("ıhlamur", "Ihlamur"),
        ("istinye şişli", "İstinye Şişli"),
    ],
)
def test_turkish_title_case(key, display):
    draft = load_draft()
    assert draft.title_case(key) == display


def test_multiple_prefixes_flags_every_group_member(tmp_path):
    draft = load_draft()
    db, output = tmp_path / "test.duckdb", tmp_path / "map.csv"
    make_db(db, [("AKR-KBT", "Aksaray"), ("AKE-AKS", "Aksaray")])
    draft.generate(db, output)
    rows = read_rows(output)
    assert [row["station_code"] for row in rows] == ["AKE-AKS", "AKR-KBT"]
    assert {row["station_group"] for row in rows} == {"Aksaray"}
    assert {row["status"] for row in rows} == {"flagged"}
    assert all("prefix" in row["note"] for row in rows)


def test_split_prefix_flags_all_rows_in_affected_groups(tmp_path):
    draft = load_draft()
    db, output = tmp_path / "test.duckdb", tmp_path / "map.csv"
    make_db(
        db,
        [
            ("ALK-ALM", "Alibeyköy Metro"),
            ("ALK-AL2", "Alibeyköy"),
            ("OTHER-X", "Alibeyköy"),
        ],
    )
    draft.generate(db, output)
    assert all(
        row["status"] == "flagged" and "multiple groups" in row["note"]
        for row in read_rows(output)
    )
    metro = next(row for row in read_rows(output) if row["station_code"] == "ALK-ALM")
    assert "; " in metro["note"]


def test_null_name_has_empty_group_and_flag(tmp_path):
    draft = load_draft()
    db, output = tmp_path / "test.duckdb", tmp_path / "map.csv"
    make_db(db, [("EMI-EMN", None)])
    draft.generate(db, output)
    row = read_rows(output)[0]
    assert row["station_name"] == row["station_group"] == ""
    assert row["status"] == "flagged"
    assert "NULL" in row["note"]


@pytest.mark.parametrize(
    "name",
    [
        "4 Levent",
        "İstasyon giriş",
        "İstasyon girişi",
        "İstasyon stad",
        "İstasyon metro",
        "İstasyon mah",
        "İstasyon mah.",
    ],
)
def test_residual_digits_and_review_words_are_flagged(tmp_path, name):
    draft = load_draft()
    db, output = tmp_path / "test.duckdb", tmp_path / "map.csv"
    make_db(db, [("AAA-A", name)])
    draft.generate(db, output)
    row = read_rows(output)[0]
    assert row["status"] == "flagged"
    assert row["note"]


def test_unambiguous_groups_are_rule_assigned(tmp_path):
    draft = load_draft()
    db, output = tmp_path / "test.duckdb", tmp_path / "seeds/map.csv"
    make_db(db, [("YNK-2", "Yenikapı-2"), ("YNK-1", "Yenikapı")])
    before = db.read_bytes()
    draft.generate(db, output)
    assert db.read_bytes() == before
    assert {row["status"] for row in read_rows(output)} == {"rule"}
    assert {row["station_group"] for row in read_rows(output)} == {"Yenikapı"}
    assert {row["note"] for row in read_rows(output)} == {""}


def test_existing_checked_records_survive_byte_for_byte_and_new_code_is_sorted(
    tmp_path,
):
    draft = load_draft()
    db, output = tmp_path / "test.duckdb", tmp_path / "map.csv"
    make_db(db, [("ZZZ-1", "New source name"), ("AAA-1", "İstinye")])
    header = b"station_code,station_name,station_group,status,note\r\n"
    checked = 'ZZZ-1,"Eski, ad",Özel,checked,"İnsan\r\nnotu"\r\n'.encode()
    retired = b"YYY-1,Retired,Retired,checked,keep\n"
    output.write_bytes(header + retired + checked)
    draft.generate(db, output)
    content = output.read_bytes()
    assert content.startswith(header)
    assert checked in content and retired in content
    rows = read_rows(output)
    assert [row["station_code"] for row in rows] == ["AAA-1", "YYY-1", "ZZZ-1"]
    assert rows[-1]["station_name"] == "Eski, ad"
    assert rows[-1]["station_group"] == "Özel"
    assert rows[-1]["status"] == "checked"
    draft.generate(db, output)
    assert output.read_bytes() == content


def test_cli_defaults_and_overrides(tmp_path, monkeypatch):
    draft = load_draft()
    monkeypatch.chdir(tmp_path)
    (tmp_path / "data").mkdir()
    make_db(tmp_path / "data/ulasim.duckdb", [("ABC-1", "İstinye")])
    assert draft.main([]) == 0
    assert (
        read_rows(tmp_path / "transform/seeds/station_group_map.csv")[0][
            "station_group"
        ]
        == "İstinye"
    )
    other_db, other_csv = tmp_path / "other.duckdb", tmp_path / "other.csv"
    make_db(other_db, [("DEF-1", "Yenikapı")])
    assert draft.main(["--db", str(other_db), "--csv", str(other_csv)]) == 0
    assert read_rows(other_csv)[0]["station_group"] == "Yenikapı"


@pytest.mark.parametrize(
    "failure", ["missing_db", "missing_table", "bad_csv", "output"]
)
def test_failures_return_one_without_overwriting_csv(tmp_path, capsys, failure):
    draft = load_draft()
    db, output = tmp_path / "test.duckdb", tmp_path / "map.csv"
    original = b"wrong,header\nleave,alone\n"
    if failure == "missing_table":
        with duckdb.connect(str(db)):
            pass
    elif failure != "missing_db":
        make_db(db, [("ABC-1", "İstinye")])
    if failure == "bad_csv":
        output.write_bytes(original)
    if failure == "output":
        output.mkdir()
    assert draft.main(["--db", str(db), "--csv", str(output)]) == 1
    error = capsys.readouterr().err
    assert error and (str(db) in error or str(output) in error)
    if failure == "bad_csv":
        assert output.read_bytes() == original
    if failure == "missing_db":
        assert not db.exists()
