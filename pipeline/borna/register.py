"""Phase 1 step 3: parse the HG 540/2000 annexes into borna.register_entry and borna.road.

Input: data/raw/hg540/anexe.html (the consolidated annexes from legislatie.just.ro, see
`make download SOURCES=hg540`). The annexes are HTML tables:
  1    national roads: the national list, then a per-county breakdown
  2.x  county roads (DJ), one annex per county
  3.x  communal roads (DC), one annex per county
  4.x  private roads open to public traffic (DP)
Each annex also prints official length totals, which are used as a QA check.

Outputs: data/out/hg540/register.csv, totals_check.csv, amendments.csv, and the DB tables.
"""

import csv
import re
import sys
from dataclasses import asdict, dataclass, fields
from decimal import Decimal

import lxml.html
import psycopg

from borna import sources
from borna.config import DATA_OUT, DATA_RAW, PG_DSN
from borna.romania import county_code

LIST_BY_ANNEX = {"2": "dj", "3": "dc", "4": "dp"}

# Unambiguous typos in road numbers as published: (annex, county, ref as printed) -> ref.
# `ref_published` keeps the printed form. Add an entry only with a reason that can be checked.
REF_CORRECTIONS = {
    ("1", "AG", "DB 65B"): "DN65B",  # route 'A1 (Pitești) - Geamăna - DN65 (Pitești)'; DN65B in the national list
    ("1", "CT", "N 22"): "DN22",     # 'Limita Jud. Tulcea - Mihai Viteazu - DN 2A (Ovidiu)' continues DN22 from Tulcea
    ("1", "AR", "AA 11"): "A11",     # Arad - Arad, 2.3 km: the A11 (Arad - Oradea) motorway stub
}

HEADING_RE = re.compile(r"^(?:Rețeaua (?:de )?drum.*? )?din (?:județul|municipiul) (.+)$", re.IGNORECASE)
ANNEX_RE = re.compile(r"^Anexa nr\. (\d+(?:\.\d+)?)$")
NOTE_RE = re.compile(r"^\(la (\d\d-\d\d-\d{4}),\s*(.+?)\s*\)$", re.DOTALL)
ACT_RE = re.compile(r"HOTĂRÂREA nr\. ([\d.]+) din \d+ \w+ (\d{4})")
# km positions as printed: '17 + 987', '0+000', '10 600', '21191', plus slips such as '17+3500'
# (= 17.350), '6+00', '2+707,20' and '13.73'.
KM_RE = re.compile(r"^\|?(?:(\d{1,3})\s*\+\s*(\d{2,4})(?:,\d+)?|(\d{1,3}) ?(\d{3})|(\d{1,3})\.(\d{1,2}))$")
KM_MULTI_RE = re.compile(r"(\d{1,3})\s*\+\s*(\d{3})")  # several sections in one cell
REF_RE = re.compile(r"^(A|DX|DN|DJ|DC|DP)\s*(\d+)?\s*([A-ZĂÂÎȘȚ]{0,3}\d?)$")
NR_RE = re.compile(r"^\d+(?:\^\d+)?\.?$")  # '12', '12.', '41^1' (row inserted by an amendment)
REMOVED_RE = re.compile(r"^(Poziție eliminată|Abrogat)", re.IGNORECASE)
PREFIX_BY_LIST = {"dj": "DJ", "dc": "DC", "dp": "DP"}


@dataclass
class Entry:
    annex: str
    list: str
    county: str | None
    nr_crt: str | None
    ref: str
    ref_published: str
    category: str
    route_desc: str
    km_from: Decimal | None
    km_to: Decimal | None
    length_km: Decimal | None
    owner: str | None
    table_no: int
    row_no: int


@dataclass
class Total:
    annex: str
    county: str | None
    label: str
    length_km: Decimal


@dataclass
class Note:
    annex: str
    date: str
    act: str | None
    text: str


@dataclass
class Problem:
    table_no: int
    row_no: int
    annex: str
    reason: str
    cells: list[str]


def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", text.replace("\xa0", " ")).strip()


def parse_km(text: str) -> Decimal | None:
    """'17 + 987', '0+000', '10 600' -> Decimal('17.987')."""
    m = KM_RE.match(_clean(text))
    if not m:
        return None
    km, frac = next((m.group(i), m.group(i + 1)) for i in (1, 3, 5) if m.group(i) is not None)
    return Decimal(f"{km}.{frac}").quantize(Decimal("0.001"))


def parse_length(text: str) -> Decimal | None:
    """'109,086 km', '13.200 km', '67, 639 km', '16.248,30' -> km.

    With both separators the dot groups thousands. With one, it is the decimal mark: no single
    road row is 1000 km or longer, so '13.200' means 13.2 km.
    """
    t = re.sub(r"\s*km$", "", _clean(text), flags=re.IGNORECASE).replace(" ", "")
    if not re.fullmatch(r"\d[\d.,]*", t):
        return None
    if "." in t and "," in t:
        t = t.replace(".", "").replace(",", ".")
    else:
        t = t.replace(",", ".")
    try:
        return Decimal(t)
    except ArithmeticError:
        return None


def normalise_ref(parts: list[str], list_: str) -> tuple[str, str] | None:
    """['DN', '1A'] or ['DJ 103 G'] -> ('DN1A', 'DN'); None if not a road number.

    Annex 1 also lists bypasses under their own codes ('VA', 'CB', 'CALB', '7CC'): those get
    category V and keep the code as their ref.
    """
    text = _clean(" ".join(parts)).upper().replace(".", "").replace("/", "")
    text = re.sub(r"^\d+\s+(?=(?:A|DX|DN|DJ|DC|DP)\b)", "", text)  # nr. repeated: '71 DC 76'
    if list_ in PREFIX_BY_LIST and re.match(r"^\d", text):  # prefix left out: '546 G'
        text = f"{PREFIX_BY_LIST[list_]} {text}"
    if m := REF_RE.match(text):
        return f"{m.group(1)}{m.group(2) or ''}{m.group(3)}", m.group(1)
    if list_ in ("national", "county_dn") and re.fullmatch(r"[A-Z0-9 -]{1,10}", text):
        return text.replace(" ", ""), "V"
    return None


def expand_table(table) -> list[list[str]]:
    """Table rows as a grid of cell texts, with rowspan/colspan expanded."""
    grid, carry = [], {}  # carry: column -> [text, rows remaining]
    for tr in table.xpath("./tr|./tbody/tr"):
        row, col = [], 0
        cells = iter(tr.xpath("./td|./th"))
        while True:
            if col in carry:
                text, left = carry[col]
                row.append(text)
                if left == 1:
                    del carry[col]
                else:
                    carry[col][1] -= 1
                col += 1
                continue
            cell = next(cells, None)
            if cell is None:
                break
            text = _clean(cell.text_content())
            rowspan = int(cell.get("rowspan") or 1)
            for i in range(int(cell.get("colspan") or 1)):
                row.append(text if i == 0 else "")
                if rowspan > 1:
                    carry[col] = [text, rowspan - 1]
                col += 1
        grid.append(row)
    return grid


def parse_row(cells: list[str], list_: str) -> list[dict] | str:
    """One data row -> one entry per road section (usually one), or a reason string."""
    cells = [c for c in cells if c]  # empty cells only pad the layout
    if cells and cells[-1].lower() == "km":  # length printed in two cells: '51,700' 'km'
        cells = cells[:-2] + [cells[-2] + " km"] if len(cells) >= 2 else []
    nr = cells.pop(0).rstrip(".") if cells and NR_RE.match(cells[0]) else None
    if cells and REMOVED_RE.match(cells[0]):
        return "position removed"

    # km_from/km_to: the first two adjacent km cells after the road number and route
    pair = next((i for i in range(1, len(cells) - 1)
                 if parse_km(cells[i]) is not None and parse_km(cells[i + 1]) is not None), None)
    multi = None
    if pair is None:  # several sections in one row: '0 + 000 35 + 178' | '20 + 378 46 + 660'
        multi = next((i for i in range(2, len(cells) - 1)  # route text may contain 'Km 9+502'

                      if len(KM_MULTI_RE.findall(cells[i])) > 1
                      and len(KM_MULTI_RE.findall(cells[i])) == len(KM_MULTI_RE.findall(cells[i + 1]))),
                     None)
    if pair is not None or multi is not None:
        i = pair if pair is not None else multi
        left, right = cells[:i], cells[i + 2:]
    elif len(cells) >= 3 and parse_length(cells[-1]) is not None:  # km missing or misprinted
        i, left, right = None, cells[:-1], cells[-1:]
        while len(left) > 2 and re.fullmatch(r"[\d +.,]+", left[-1]):  # drop e.g. '58 55' '79 900'
            left.pop()
    else:
        return "no km_from/km_to"

    route = left.pop() if len(left) > 1 else ""  # route cell left empty in a few rows
    ref = normalise_ref(left, list_)
    if ref is None:
        return f"unrecognised road number {' '.join(left)!r}"

    if multi is not None:
        froms = [Decimal(a) + Decimal(b) / 1000 for a, b in KM_MULTI_RE.findall(cells[i])]
        tos = [Decimal(a) + Decimal(b) / 1000 for a, b in KM_MULTI_RE.findall(cells[i + 1])]
        lengths = [parse_length(x) for x in re.findall(r"\d+[.,]\d+", right[0])] if right else []
        if len(lengths) != len(froms):  # one combined length printed: use each section's km span
            lengths = [t - f for f, t in zip(froms, tos)]
        sections = list(zip(froms, tos, lengths))
    else:
        length = parse_length(right[0]) if right else None
        sections = [(parse_km(cells[i]) if i is not None else None,
                     parse_km(cells[i + 1]) if i is not None else None, length)]

    owner = right[0] if right and parse_length(right[0]) is None else None
    out = []
    for km_from, km_to, length in sections:
        if length is None and owner and km_from is not None:  # annex 4 prints owner, not length
            length = km_to - km_from
        out.append({
            "nr_crt": nr,
            "ref": ref[0],
            "ref_published": _clean(" ".join(left)),
            "category": ref[1],
            "route_desc": route,
            "km_from": km_from,
            "km_to": km_to,
            "length_km": length,
            "owner": owner,
        })
    return out


def _is_header(cells: list[str]) -> bool:
    text = " ".join(cells)
    return "Nr. crt" in text or "Origine" in text or text.replace(" ", "").isdigit()


def _total_scope(label: str, county: str | None) -> str | None:
    """County a printed total refers to (None = all of Romania), from its own label."""
    if re.search(r"din România", label, re.IGNORECASE):
        return None
    if m := re.search(r"din (?:județul|municipiul) (.+)$", label, re.IGNORECASE):
        return county_code(m.group(1))
    return county


def parse_document(html: str) -> tuple[list[Entry], list[Total], list[Note], list[Problem]]:
    root = lxml.html.fromstring(html)
    entries, totals, notes, problems = [], [], [], []
    state = {"annex": None, "county": None, "table_no": 0}

    def on_table(table):
        annex, county = state["annex"], state["county"]
        table_no = state["table_no"]
        state["table_no"] += 1
        if annex is None:
            return  # the act's header box before Anexa nr. 1
        grid = expand_table(table)
        if any("Nr. MF" in c for row in grid[:2] for c in row):
            return  # public-domain inventory extract inserted by an amending act, not register rows
        top = annex.split(".")[0]
        list_ = LIST_BY_ANNEX.get(top) or ("county_dn" if county else "national")
        for row_no, cells in enumerate(grid):
            filled = [c for c in cells if c]
            if not filled or _is_header(cells):
                continue
            if re.match(r"(Lungime|Totalul)", filled[0]):
                value = filled[-2] + " km" if filled[-1] == "km" and len(filled) > 2 else filled[-1]
                length = parse_length(value)
                if length is not None:
                    totals.append(Total(annex, _total_scope(filled[0], county), filled[0], length))
                continue
            parsed = parse_row(cells, list_)
            if isinstance(parsed, str):
                problems.append(Problem(table_no, row_no, annex, parsed, cells))
                continue
            for p in parsed:
                if fixed := REF_CORRECTIONS.get((annex, county, p["ref_published"])):
                    p["ref"], p["category"] = fixed, re.match(r"[A-Z]+?(?=\d)", fixed).group()
                entries.append(Entry(annex=annex, list=list_, county=county,
                                     table_no=table_no, row_no=row_no, **p))

    def walk(el):
        if not isinstance(el.tag, str):  # comments, processing instructions
            return
        if el.tag == "table":
            on_table(el)
            return
        cls = el.get("class") or ""
        own = _clean(el.text or "")
        if cls == "S_ANX_TTL":
            m = ANNEX_RE.match(_clean(el.text_content()))
            if m:
                state["annex"], state["county"] = m.group(1), None
        elif own and state["annex"]:
            if m := HEADING_RE.match(own):
                state["county"] = county_code(m.group(1))
            elif own.startswith("(la") and (m := NOTE_RE.match(_clean(el.text_content()))):
                act = ACT_RE.search(m.group(2))
                notes.append(Note(state["annex"], m.group(1),
                                  f"HG {act.group(1)}/{act.group(2)}" if act else None, m.group(2)))
        for child in el:
            walk(child)

    walk(root)
    # County tables sometimes write a bypass code with a DN prefix ('DN CB', 'DN 7CC') where the
    # national list has the bare code ('CB', '7CC').
    national = {e.ref: e.category for e in entries if e.list == "national"}
    for e in entries:
        if e.list == "county_dn" and e.ref not in national and national.get(e.ref[2:]) == "V":
            e.ref, e.category = e.ref[2:], "V"
    return entries, totals, notes, problems


def check_totals(entries: list[Entry], totals: list[Total]) -> list[dict]:
    """Compare parsed row sums with the officially printed totals."""
    def total_for(annex, county, label):
        rows = [e for e in entries if e.length_km is not None]
        if county:  # a county table total
            rows = [e for e in rows if e.annex.split(".")[0] == annex.split(".")[0]
                    and e.county == county]
        elif annex == "1":
            rows = [e for e in rows if e.list == "national"]
            if "variante" in label:
                rows = [e for e in rows if e.category == "V"]
            elif "autostr" in label.lower():
                rows = [e for e in rows if e.category == "A"]
            elif "ca DN" in label:
                rows = [e for e in rows if e.category == "DN"]
        else:  # Romania-wide total of annex 2/3/4
            rows = [e for e in rows if e.annex.split(".")[0] == annex.split(".")[0]]
        return sum((e.length_km for e in rows), Decimal(0))

    report = []
    for t in totals:
        parsed = total_for(t.annex, t.county, t.label)
        report.append({"annex": t.annex, "county": t.county or "", "label": t.label,
                       "official_km": t.length_km, "parsed_km": parsed,
                       "diff_km": parsed - t.length_km})
    return report


def _write_csv(path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as f:
        if not rows:
            return
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)


def load(conn, entries: list[Entry], manifest: dict) -> None:
    legal = f"HG 540/2000, anexa {{annex}} (forma consolidată {manifest['version']})"
    sources.register(conn, manifest)
    conn.execute("TRUNCATE borna.register_entry, borna.road RESTART IDENTITY CASCADE")
    cols = [f.name for f in fields(Entry)]
    with conn.cursor().copy(
        f"COPY borna.register_entry ({', '.join(cols)}, legal_source, source_id) FROM STDIN"
    ) as copy:
        for e in entries:
            copy.write_row([getattr(e, c) for c in cols] + [legal.format(annex=e.annex), "hg540"])
    # One road per number: national roads from the national list (county NULL), the rest per
    # county. Roads listed in several sections get their km range and summed length.
    conn.execute("""
        INSERT INTO borna.road (ref, category, county, route_desc, km_from_official,
                                km_to_official, length_official, legal_source, source_id)
        SELECT ref, min(category), county,
               string_agg(route_desc, ' | ' ORDER BY table_no, row_no),
               min(km_from), max(km_to), sum(length_km),
               string_agg(DISTINCT legal_source, '; '), 'hg540'
        FROM borna.register_entry
        WHERE list <> 'county_dn'
        GROUP BY ref, county
    """)


def main() -> int:
    manifest = sources.read_manifest("hg540")
    if not sources.files_present(manifest):
        print("hg540: no download found, run `make download SOURCES=hg540`", file=sys.stderr)
        return 1
    html = (DATA_RAW / "hg540" / "anexe.html").read_text(encoding="utf-8")
    entries, totals, notes, problems = parse_document(html)
    report = check_totals(entries, totals)

    out = DATA_OUT / "hg540"
    _write_csv(out / "register.csv", [asdict(e) for e in entries])
    _write_csv(out / "totals_check.csv", report)
    _write_csv(out / "amendments.csv", [asdict(n) for n in notes])
    _write_csv(out / "problems.csv", [asdict(p) for p in problems])

    with psycopg.connect(PG_DSN) as conn:
        load(conn, entries, manifest)
        n_roads = conn.execute("SELECT count(*) FROM borna.road").fetchone()[0]

    by_list = {}
    for e in entries:
        by_list[e.list] = by_list.get(e.list, 0) + 1
    print(f"hg540 (forma consolidată {manifest['version']}): {len(entries):,} register rows "
          f"{by_list}, {n_roads:,} roads, {len(notes)} amendment notes")
    off = [r for r in report if abs(r["diff_km"]) > Decimal("0.01")]
    print(f"totals: {len(report) - len(off)}/{len(report)} official totals match the parsed rows")
    for r in off:
        print(f"  {r['annex']:>5} {r['county'] or 'RO':<3} official {r['official_km']:>10} "
              f"parsed {r['parsed_km']:>10} diff {r['diff_km']:>+9}  {r['label'][:60]}")
    removed = [p for p in problems if p.reason == "position removed"]
    problems = [p for p in problems if p.reason != "position removed"]
    print(f"{len(removed)} positions marked removed/abrogated in the act (skipped)")
    print(f"problems: {len(problems)} unparsed rows (see {out / 'problems.csv'})")
    for p in problems[:25]:
        print(f"  table {p.table_no} row {p.row_no} (anexa {p.annex}): {p.reason}: {p.cells}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
