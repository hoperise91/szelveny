from decimal import Decimal as D

import lxml.html
import pytest

from borna.register import (
    expand_table,
    normalise_ref,
    parse_document,
    parse_km,
    parse_length,
    parse_row,
)
from borna.romania import county_code


@pytest.mark.parametrize("text,km", [
    ("17 + 987", "17.987"), ("0+000", "0.000"), ("10 600", "10.600"), ("21191", "21.191"),
    ("0000", "0.000"), ("17+3500", "17.350"), ("6+00", "6.000"), ("2+707,20", "2.707"),
    ("13.73", "13.730"), ("|0+000", "0.000"),
])
def test_parse_km(text, km):
    assert parse_km(text) == D(km)


@pytest.mark.parametrize("text", ["DJ 103", "58 55", "Origine", "", "5"])
def test_parse_km_rejects(text):
    assert parse_km(text) is None


@pytest.mark.parametrize("text,km", [
    ("109,086 km", "109.086"), ("13.200 km", "13.200"), ("67, 639 km", "67.639"),
    ("16.248,30", "16248.30"), ("1.280,09 km", "1280.09"), ("0,472", "0.472"),
])
def test_parse_length(text, km):
    assert parse_length(text) == D(km)


@pytest.mark.parametrize("parts,list_,expected", [
    (["DN", "1A"], "national", ("DN1A", "DN")),
    (["A", "1"], "national", ("A1", "A")),
    (["DJ 103 G"], "dj", ("DJ103G", "DJ")),
    (["546 G"], "dj", ("DJ546G", "DJ")),          # prefix left out
    (["71 DC 76"], "dc", ("DC76", "DC")),         # nr. crt. repeated
    (["DC 38/A"], "dc", ("DC38A", "DC")),
    (["DC 88Ț"], "dc", ("DC88Ț", "DC")),
    (["DJ 106A1"], "dj", ("DJ106A1", "DJ")),
    (["CALB"], "national", ("CALB", "V")),        # bypass code
    (["Abrogată"], "dc", None),
])
def test_normalise_ref(parts, list_, expected):
    assert normalise_ref(parts, list_) == expected


def test_county_code():
    assert county_code("BISTRIȚA-NĂSĂUD") == "BN"
    assert county_code("Iasi") == "IS"
    assert county_code("BUCUREȘTI") == "B"


def test_parse_row_annex1_split_cells():
    [r] = parse_row(["1", "", "A", "", "3", "", "Câmpia Turzii - Gilău", "0 000", "", "51 700", "",
                     "51,700", "km"], "county_dn")
    assert (r["nr_crt"], r["ref"], r["km_from"], r["km_to"], r["length_km"]) == \
        ("1", "A3", D(0), D("51.700"), D("51.700"))


def test_parse_row_two_sections_in_one_row():
    rows = parse_row(["33", "DJ 280", "Strunga - Lim. Jud. Neamț (Km 20 + 378) - Dagita",
                      "0 + 000 35 + 178", "20 + 378 46 + 660", "20.378 km 11.482 km"], "dj")
    assert [(r["km_from"], r["km_to"], r["length_km"]) for r in rows] == [
        (D(0), D("20.378"), D("20.378")), (D("35.178"), D("46.660"), D("11.482"))]


def test_parse_row_owner_instead_of_length():
    [r] = parse_row(["2", "DP8", "Stânceni (DN 15) - Zebrac", "0+900", "2+000", "R.N.P - Romsilva"], "dp")
    assert (r["owner"], r["length_km"]) == ("R.N.P - Romsilva", D("1.100"))


def test_parse_row_without_km():
    [r] = parse_row(["44", "DN", "3F", "Intravilan Constanța", "", "", "0,472 km"], "national")
    assert (r["ref"], r["km_from"], r["length_km"]) == ("DN3F", None, D("0.472"))


def test_parse_row_removed_position():
    assert parse_row(["13", "Poziție eliminată potrivit art. 4", "", ""], "dj") == "position removed"


def test_expand_table_rowspan():
    t = lxml.html.fromstring("<table><tr><td rowspan='2'>A</td><td>1</td></tr><tr><td>2</td></tr></table>")
    assert expand_table(t) == [["A", "1"], ["A", "2"]]


DOC = """<html><body>
<span class="S_ANX_TTL">Anexa nr. 2.15</span>
<span class="S_PAR">Rețeaua de drumuri județene din județul COVASNA<table>
<tr><td rowspan="2">Nr. crt.</td><td rowspan="2">Denumirea drumului</td><td>Traseul drumului</td>
<td colspan="2">Poziția kilometrică</td><td>Lungimea reală</td></tr>
<tr><td>Origine</td><td>Destinație</td></tr>
<tr><td>1</td><td>DJ 103</td><td>Limită județ Brașov - Araci (DN 13E)</td><td>17 + 987</td><td>19 + 213</td><td>1.226 km</td></tr>
<tr><td colspan="5">Lungimea totală a rețelei de drumuri județene din județul COVASNA</td><td>1.226 km</td></tr>
</table></span>
<span class="S_PAR">(la 28-07-2022, Anexa nr. 2.15 a fost modificată de HOTĂRÂREA nr. 942 din 20 iulie 2022, publicată în MONITORUL OFICIAL nr. 758 )</span>
</body></html>"""


def test_parse_document():
    entries, totals, notes, problems = parse_document(DOC)
    assert problems == []
    [e] = entries
    assert (e.annex, e.list, e.county, e.ref, e.km_from, e.km_to) == \
        ("2.15", "dj", "CV", "DJ103", D("17.987"), D("19.213"))
    assert [(t.county, t.length_km) for t in totals] == [("CV", D("1.226"))]
    assert [(n.date, n.act) for n in notes] == [("28-07-2022", "HG 942/2022")]
