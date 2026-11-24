"""Romanian administrative names and codes shared by the pipeline steps."""

import re
import unicodedata

# ISO 3166-2:RO codes, keyed by the county name without diacritics, upper case.
COUNTIES = {
    "ALBA": "AB", "ARAD": "AR", "ARGES": "AG", "BACAU": "BC", "BIHOR": "BH",
    "BISTRITA-NASAUD": "BN", "BOTOSANI": "BT", "BRASOV": "BV", "BRAILA": "BR", "BUCURESTI": "B",
    "BUZAU": "BZ", "CARAS-SEVERIN": "CS", "CALARASI": "CL", "CLUJ": "CJ", "CONSTANTA": "CT",
    "COVASNA": "CV", "DAMBOVITA": "DB", "DOLJ": "DJ", "GALATI": "GL", "GIURGIU": "GR",
    "GORJ": "GJ", "HARGHITA": "HR", "HUNEDOARA": "HD", "IALOMITA": "IL", "IASI": "IS",
    "ILFOV": "IF", "MARAMURES": "MM", "MEHEDINTI": "MH", "MURES": "MS", "NEAMT": "NT",
    "OLT": "OT", "PRAHOVA": "PH", "SATU MARE": "SM", "SALAJ": "SJ", "SIBIU": "SB",
    "SUCEAVA": "SV", "TELEORMAN": "TR", "TIMIS": "TM", "TULCEA": "TL", "VASLUI": "VS",
    "VALCEA": "VL", "VRANCEA": "VN",
}


def fold(text: str) -> str:
    """Upper case without diacritics: 'Bistrița-Năsăud' -> 'BISTRITA-NASAUD'."""
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if not unicodedata.combining(c)).upper().strip()


def county_code(name: str) -> str:
    key = re.sub(r"\s*-\s*", "-", fold(name))
    if key not in COUNTIES:
        raise ValueError(f"unknown county {name!r}")
    return COUNTIES[key]
