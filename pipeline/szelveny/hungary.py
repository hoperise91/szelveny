"""Hungarian administrative codes, road numbers and szelvény (chainage) notation shared by the
pipeline steps."""

import re
import unicodedata

# ISO 3166-2:HU codes of the 19 vármegye + Budapest, keyed by the name without diacritics,
# upper case (the code is stored without the 'HU-' prefix).
COUNTIES = {
    "BACS-KISKUN": "BK", "BARANYA": "BA", "BEKES": "BE", "BORSOD-ABAUJ-ZEMPLEN": "BZ",
    "BUDAPEST": "BU", "CSONGRAD-CSANAD": "CS", "FEJER": "FE", "GYOR-MOSON-SOPRON": "GS",
    "HAJDU-BIHAR": "HB", "HEVES": "HE", "JASZ-NAGYKUN-SZOLNOK": "JN", "KOMAROM-ESZTERGOM": "KE",
    "NOGRAD": "NO", "PEST": "PE", "SOMOGY": "SO", "SZABOLCS-SZATMAR-BEREG": "SZ", "TOLNA": "TO",
    "VAS": "VA", "VESZPREM": "VE", "ZALA": "ZA",
}
COUNTY_ALIASES = {"CSONGRAD": "CS"}  # renamed Csongrád-Csanád in 2020; older datasets keep it

# Road categories of the national network, derived from the number (see CLAUDE.md):
#   M   gyorsforgalmi út (motorway or expressway)   'M1', 'M0', 'M30'
#   F   főút (1-3 digits)                           '1', '86', '471'
#   OK  összekötő út (4 digits)                     '4512'
#   BK  bekötő út and other 5-digit roads           '81101'
# The number can't tell first- from second-class main roads (2-digit roads are both, checked
# against NAP on 2026-11-24), nor a bekötő út from a junction ramp or a rest-area road (also 5
# digits). NAP's own category (kutka) is authoritative; this is the fallback for OSM.
CATEGORIES = ("M", "F", "OK", "BK")
_CATEGORY_BY_DIGITS = {1: "F", 2: "F", 3: "F", 4: "OK", 5: "BK"}

_REF_RE = re.compile(r"^(?:M(\d{1,2})|([1-9]\d{0,4}))\.?$")
_SZELVENY_RE = re.compile(r"^\s*(\d+)\s*\+\s*(\d+)\s*$")
_POSITION_RE = re.compile(r"^\s*(M\s?\d{1,2}|\d{1,5})\.?\s+(\d+\s*\+\s*\d+|\d+(?:[.,]\d+)?)\s*$",
                          re.IGNORECASE)


def fold(text: str) -> str:
    """Upper case without diacritics: 'Győr-Moson-Sopron' -> 'GYOR-MOSON-SOPRON'."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).upper().strip()


def county_code(name: str) -> str:
    """'Győr-Moson-Sopron vármegye' -> 'GS', 'Budapest' -> 'BU'."""
    key = re.sub(r"\s+(VARMEGYE|MEGYE)$", "", re.sub(r"\s*-\s*", "-", fold(name)))
    if key in COUNTIES:
        return COUNTIES[key]
    if key in COUNTY_ALIASES:
        return COUNTY_ALIASES[key]
    raise ValueError(f"unknown county {name!r}")


def normalise_ref(token: str) -> tuple[str, str] | None:
    """One road-number token -> (ref, category), or None if it isn't a national road number.

    'M 1' -> ('M1', 'M'), '8' -> ('8', 'F'), '4512' -> ('4512', 'OK'). E-road numbers
    ('E60'), 6+ digit numbers and names are left out.
    """
    t = re.sub(r"[\s]", "", token.upper())
    m = _REF_RE.match(t)
    if not m:
        return None
    if m.group(1) is not None:
        return f"M{int(m.group(1))}", "M"
    return m.group(2), _CATEGORY_BY_DIGITS[len(m.group(2))]


def parse_szelveny(text: str) -> float:
    """NAP chainage '12 + 300' -> 12.3 (km).

    The metre part can exceed 999 where a km section is longer than 1 km (after a szelvény
    jump): '0 + 1064' -> 1.064. That is not the same place as '1 + 064' on such a road, so
    keep the original string for display.
    """
    m = _SZELVENY_RE.match(text)
    if not m:
        raise ValueError(f"not a szelvény: {text!r}")
    return int(m.group(1)) + int(m.group(2)) / 1000


def format_szelveny(km: float) -> str:
    """12.3 -> '12+300'."""
    metres = round(km * 1000)
    return f"{metres // 1000}+{metres % 1000:03d}"


def format_position(ref: str, km: float) -> str:
    """('M1', 12.3) -> 'M1 12+300'."""
    return f"{ref} {format_szelveny(km)}"


def parse_position(query: str) -> tuple[str, float] | None:
    """'M1 12+300' -> ('M1', 12.3); also '8 145.02' and 'm 1 12+300'. None if not a position."""
    m = _POSITION_RE.match(query)
    if not m or not (ref := normalise_ref(m.group(1))):
        return None
    pos = m.group(2)
    km = parse_szelveny(pos) if "+" in pos else float(pos.replace(",", "."))
    return ref[0], round(km, 3)
