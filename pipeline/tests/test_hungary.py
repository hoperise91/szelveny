import pytest

from szelveny.hungary import (
    COUNTIES,
    county_code,
    fold,
    format_position,
    normalise_ref,
    parse_position,
    parse_szelveny,
)


def test_counties_are_19_plus_budapest():
    assert len(COUNTIES) == 20
    assert len(set(COUNTIES.values())) == 20


@pytest.mark.parametrize("name,code", [
    ("Győr-Moson-Sopron", "GS"),
    ("Borsod-Abaúj-Zemplén vármegye", "BZ"),
    ("Csongrád-Csanád", "CS"),
    ("Csongrád megye", "CS"),        # pre-2020 name
    ("Budapest", "BU"),
    ("Jász-Nagykun-Szolnok", "JN"),
    ("Szabolcs - Szatmár - Bereg", "SZ"),
])
def test_county_code(name, code):
    assert county_code(name) == code


def test_county_code_unknown():
    with pytest.raises(ValueError):
        county_code("Kovászna")


def test_fold_double_acute():
    assert fold("Győr, Nyíregyháza, Tűzoltó") == "GYOR, NYIREGYHAZA, TUZOLTO"


@pytest.mark.parametrize("token,expected", [
    ("M1", ("M1", "M")),
    ("M 0", ("M0", "M")),
    ("M30", ("M30", "M")),
    ("8", ("8", "F")),
    ("86", ("86", "F")),
    ("471", ("471", "F")),
    ("4512", ("4512", "OK")),
    ("81101", ("81101", "BK")),
    ("8.", ("8", "F")),
    ("E60", None),
    ("0", None),
    ("123456", None),
    ("M100", None),
    ("Fő utca", None),
])
def test_normalise_ref(token, expected):
    assert normalise_ref(token) == expected


def test_szelveny_roundtrip():
    assert parse_szelveny("2 + 548") == pytest.approx(2.548)
    assert parse_szelveny("145+020") == pytest.approx(145.02)
    assert parse_szelveny("0 + 1064") == pytest.approx(1.064)   # km section longer than 1 km
    assert format_position("M1", 12.3) == "M1 12+300"
    assert format_position("4512", 3.4) == "4512 3+400"
    assert format_position("8", 145.02) == "8 145+020"


@pytest.mark.parametrize("query,expected", [
    ("M1 12+300", ("M1", 12.3)),
    ("m1 12 + 300", ("M1", 12.3)),
    ("8 145+020", ("8", 145.02)),
    ("4512 3,4", ("4512", 3.4)),
    ("M1", None),
    ("Budapest", None),
])
def test_parse_position(query, expected):
    assert parse_position(query) == expected
