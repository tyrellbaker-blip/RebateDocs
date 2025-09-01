# app/services/extract.py
"""
Extraction rules:
- Build a TOC from the front-matter table:
    Program ID Program Name Updated Page(s)
    V25URC08 Retail Customer Bonus 8/1/2025 10-11
  On each page start, preload its program context (program_id + published date)
  and set the rebate_type from the TOC program name.

- Program header fields (values on the NEXT line):
    Program ID -> <V25UAE08>
    Published -> <8/1/2025>
    Program Start -> <8/1/2025>
    Program End -> <9/30/2025>

- Also handle the single-row header + single-row values table:
    "Program ID Published Program Start Program End [Dealer Review Ends]"
    "V25UTG06 6/3/2025 6/3/2025 9/2/2025 [10/31/2025]"

- Table-y sections:
    * Header line sets MY + model, e.g. "MY24 ID.4" or "MY25 ID. Buzz"
    * We also see year-only headers like "MY24" and year+noise headers like
      "MY25 Bonus" or "MY25 Bonus 8/1-8/21". Those should set ONLY the year
      context; the following model rows ("Tiguan $750", "Atlas $3,500") inherit
      that year.
    * Then rows: "<TRIM> $X [$X]" → one KV per $.
    * Noisy lone lines like "Bonus", "Bonus 8/1-8/21" are ignored.

- “Bonus” is never a model. If the rebate applies to *all* vehicles, set model="all".
  Phrases like "New, unused Volkswagen models" / "New, unused VW models" flip model="all".

- Output: one KV per (amount, model/trim) with context (rebate_type, program_id,
  published/start/end, MY), plus exclusions if present.

- Suppress token-only money hits; keep only programs that actually have ≥1 $.

- NEW: Return stable groupings in provenance:
  provenance["kv_groups"] = { program_id: [indices into kvs] }
  provenance["kv_group_order"] = [program_id,...] in sorted display order

- NEW (2025-08-31): Vehicle/trim exclusions captured from inline *and adjacent lines*,
  with header-level exclusion context propagated to rows.
"""

from __future__ import annotations

import re
from typing import List, Tuple, Optional, Dict, Any, DefaultDict
from collections import defaultdict

from app.models.schemas import Span, KV, DocResult
from extraction.patterns import LABEL_LEXICON, MONEY_REGEX, MODEL_KEYS, MODEL_NORMALIZER
from app.util.layout import same_line  # imported for parity; not required in this implementation
from app.util.logger import get_logger

# ---------- regexes / helpers ----------

money_pat = re.compile(MONEY_REGEX)
DATE_PAT = re.compile(r"\b([01]?\d)[/\-]([0-3]?\d)[/\-](\d{4})\b")  # M/D/YYYY or M-D-YYYY
PROGRAM_ID_PAT = re.compile(r"\bV\d{2}[A-Z]{3}\d{2}\b")  # e.g., V25UAE08

# Rebate section headings that we recognize (used to set rebate_type)
REBATE_HEADING_PAT = re.compile(
    r"\b(Dealer Bonus(?:\s-\sEV)?|Retail Customer Bonus(?:\s-\sEV)?|APR Customer Bonus(?:\s-\sEV| - Labor Day)?|"
    r"Lease Dealer Bonus(?:\s-\sEV)?|Lease Customer Bonus(?:\s - Labor Day)?|Loyalty Bonus|"
    r"Tiguan Loyalty Code Bonus|Volkswagen Private Incentive Code Bonus|Sales Elite Program|VFI Program|Final Pay|"
    r"Target Achievement Bonus)\b",
    re.IGNORECASE,
)

# “$X - $Y” ranges
RANGE_PAT = re.compile(r"\$(\d[\d,]*)\s*[-–]\s*\$(\d[\d,]*)")

# Model header like "MY23 ID.4" or "MY25 ID. Buzz"
# IMPORTANT: explicitly forbid “Bonus” as the model token.
MODEL_HEADER_PAT = re.compile(
    r"^\s*MY\s*(\d{2}|\d{4})\s+((?!Bonus\b)[A-Za-z][A-Za-z0-9\.\s&\-]+?)\s*$",
    flags=re.IGNORECASE,
)

# Standalone year header like "MY24" (no model on the same line)
MY_STANDALONE_PAT = re.compile(r"^\s*MY\s*(\d{2}|\d{4})\s*$", flags=re.IGNORECASE)

# Year + "Bonus" header lines, optionally with a date range on the same line.
MY_WITH_BONUS_PAT = re.compile(
    r"^\s*MY\s*(\d{2}|\d{4})\s+Bonus(?:\s+\d{1,2}\s*/\s*\d{1,2}\s*[-–]\s*\d{1,2}\s*/\s*\d{1,2})?\s*$",
    flags=re.IGNORECASE,
)

# Lines that are structural noise in tables
BONUS_SOLO_PAT = re.compile(r"^\s*Bonus\s*$", flags=re.IGNORECASE)
BONUS_WITH_DATES_PAT = re.compile(
    r"^\s*Bonus\s*(\d{1,2}\s*/\s*\d{1,2})?\s*[-–]\s*(\d{1,2}\s*/\s*\d{1,2})?\s*$",
    flags=re.IGNORECASE,
)
DATE_RANGE_LABEL_PAT = re.compile(
    r"^\s*\d{1,2}\s*/\s*\d{1,2}\s*[-–]\s*\d{1,2}\s*/\s*\d{1,2}\s*$"
)

# Header row pattern: the 4 fields in one line
INLINE_HEADER_PAT = re.compile(
    r"^\s*Program ID\s+Published\s+Program Start\s+Program End\b",
    flags=re.IGNORECASE,
)

# “all vehicles” phrases → model="all"
ALL_VEHICLES_PAT = re.compile(
    r"\bNew,\s*unused\s+VW(?:olkswagen)?\s+models\b|\bNew,\s*unused\s+Volkswagen\s+models\b",
    flags=re.IGNORECASE,
)

# PDF weirdness: treat non-breaking spaces and Unicode dashes as equivalents
_WS = r"[ \t\u00A0\u2007\u202F]"
_DASH = r"[\-\u2010\u2011\u2012\u2013\u2014]"


def _normalize_pdf_text(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"\r?\n+", " ", s)
    s = re.sub(_WS, " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    # un-hyphenate line-break style artifacts
    s = re.sub(rf"{_DASH}\s+", "-", s)
    # normalize “ID . Buzz” -> “ID. Buzz”
    s = re.sub(r"\s*([.])\s*", r"\1 ", s)
    # tighten spaces near punctuation
    s = re.sub(r"\s+([,;:.)\]])", r"\1", s)
    s = re.sub(r"([(])\s+", r"\1", s)
    return s.strip()


def normalize_amount(text: str) -> Optional[int]:
    """'$1,500' -> 1500; returns None if it doesn't parse cleanly."""
    s = text.strip().replace("$", "").replace(",", "")
    return int(s) if s.isdigit() else None


def iso_date_or_none(t: str) -> Optional[str]:
    m = DATE_PAT.search(t)
    if not m:
        return None
    mm, dd, yyyy = m.groups()
    try:
        mm_i = int(mm); dd_i = int(dd); y_i = int(yyyy)
        if not (1 <= mm_i <= 12): return None
        if not (1 <= dd_i <= 31): return None
        if y_i < 1900 or y_i > 2100: return None
        import calendar
        if dd_i > calendar.monthrange(y_i, mm_i)[1]: return None
        return f"{y_i:04d}-{mm_i:02d}-{dd_i:02d}"
    except Exception:
        return None


def lines_from_spans(spans: List[Span]) -> Dict[Tuple[int, int], str]:
    """Group spans by (page, line_id) and join left→right."""
    buckets: DefaultDict[Tuple[int, int], List[Span]] = defaultdict(list)
    for s in spans:
        buckets[(s.page, s.line_id)].append(s)
    out: Dict[Tuple[int, int], str] = {}
    for key, items in buckets.items():
        items.sort(key=lambda sp: sp.bbox[0])
        out[key] = " ".join(sp.text for sp in items).strip()
    return out


def build_toc_index(lines: Dict[Tuple[int, int], str]) -> List[Dict[str, Any]]:
    """Find TOC rows → entries: {program_id, program_name, updated_iso, pages[]}."""
    entries: List[Dict[str, Any]] = []
    in_toc = False
    for (_, _), txt in lines.items():
        t = txt.strip()
        if re.search(r"\bProgram ID\s+Program Name\s+Updated\s+Page\(s\)", t, re.IGNORECASE):
            in_toc = True
            continue
        if in_toc:
            if re.search(r"\bVolkswagen New Vehicle Program Bulletins\b", t, re.IGNORECASE):
                continue
            m = re.search(r"\b(V\d{2}[A-Z]{3}\d{2})\b\s+(.*?)\s+(\d{1,2}[/\-]\d{1,2}[/\-]\d{4})\s+([\d\-]+)", t)
            if m:
                pid, pname, updated, pages = m.groups()
                page_list: List[int] = []
                for chunk in pages.split(","):
                    chunk = chunk.strip()
                    if "-" in chunk:
                        a, b = chunk.split("-", 1)
                        try:
                            a_i = int(a); b_i = int(b)
                            page_list.extend(range(a_i, b_i + 1))
                        except Exception:
                            pass
                    else:
                        try:
                            page_list.append(int(chunk))
                        except Exception:
                            pass
                entries.append({
                    "program_id": pid,
                    "program_name": pname.strip(),
                    "updated_iso": iso_date_or_none(updated),
                    "pages": page_list
                })
    return entries


def normalize_rebate_name(name: Optional[str]) -> Optional[str]:
    """Make TOC program names consistent with our section labels."""
    if not name:
        return None
    n = name.lower()
    n = n.replace("–", "-").replace("—", "-")
    mapping = {
        "dealer bonus - ev": "Dealer Bonus - EV",
        "dealer bonus": "Dealer Bonus",
        "retail customer bonus - ev": "Retail Customer Bonus - EV",
        "retail customer bonus": "Retail Customer Bonus",
        "apr customer bonus – ev": "APR Customer Bonus - EV",
        "apr customer bonus - ev": "APR Customer Bonus - EV",
        "apr customer bonus - labor day": "APR Customer Bonus - Labor Day",
        "lease customer bonus - labor day": "Lease Customer Bonus - Labor Day",
        "lease dealer bonus - ev": "Lease Dealer Bonus",
        "vfi program": "VFI Program",
        "final pay": "Final Pay",
        "sales elite program": "Sales Elite Program",
        "tiguan loyalty code bonus": "Tiguan Loyalty Code Bonus",
        "volkswagen private incentive code bonus": "Volkswagen Private Incentive Code Bonus",
        "target achievement bonus": "Target Achievement Bonus",
    }
    return mapping.get(n, name)


def choose_toc_for_page(toc: List[Dict[str, Any]], page: int, rebate_hint: Optional[str]) -> Optional[Dict[str, Any]]:
    """Pick TOC entry that covers 'page'; prefer one whose name matches rebate heading if provided."""
    candidates = [e for e in toc if page in e.get("pages", [])]
    if not candidates:
        return None
    if rebate_hint:
        rh = rebate_hint.lower()
        tagged = [e for e in candidates if e["program_name"].lower() in rh or rh in e["program_name"].lower()]
        if tagged:
            return tagged[0]
    return candidates[0]


def split_models(raw: str) -> List[str]:
    """Split 'Atlas & Atlas Cross Sport' / 'ID.4 / ID. Buzz' → ['Atlas', 'Atlas Cross Sport'] etc."""
    t = re.sub(r"\s+&\s+|\s*/\s*|,\s*", "|", raw)
    parts = [p.strip() for p in t.split("|") if p.strip()]
    return parts or [raw.strip()]


# --------- VEHICLE/TRIM EXCLUSIONS (robust, inline + adjacent) ---------

_MODEL_EXCLUSION_PATTERNS = [
    rf"\(\s*excludes?\s+(?P<list>[^)]+)\)",                   # (excludes Golf R & ID. Buzz)
    rf"\(\s*except\s+(?P<list>[^)]+)\)",                      # (except Golf R and ID. Buzz)
    rf"\(\s*not{_WS}+eligible[:\s]+(?P<list>[^)]+)\)",        # (not eligible: Golf R, ID. Buzz)
    rf"{_DASH}\s*excludes?\s+(?P<list>.+?)(?:[.;]|\)|$)",     # — excludes Golf R, ID. Buzz
    rf"\bexcludes?\b\s+(?P<list>.+?)(?:[.;]|\)|$)",           # excludes Golf R, ID. Buzz
    rf"\bexcept\b\s+(?P<list>.+?)(?:[.;]|\)|$)",              # except Golf R and ID. Buzz
    rf"\bnot{_WS}+eligible[:\s]+\s*(?P<list>.+?)(?:[.;]|\)|$)",  # not eligible: Golf R / ID. Buzz
]
_EXCL_SPLIT = re.compile(rf"\s*(?:,|{_WS}+and{_WS}+|{_WS}*&{_WS}*|/)\s*", re.IGNORECASE)

def _clean_excl_item(x: str) -> str:
    x = x.strip(" ,;.")
    x = re.sub(r"\s+", " ", x)
    x = re.sub(r"\s*([.])\s*", r"\1 ", x)  # "ID . Buzz" -> "ID. Buzz"
    x = re.sub(r"^(the|all)\s+", "", x, flags=re.IGNORECASE)
    return x.strip()

def _extract_exclusions_any(text: str) -> Optional[str]:
    """Core extractor used for inline and adjacent lines."""
    if not text:
        return None
    t = _normalize_pdf_text(text)
    found: List[str] = []
    for pat in _MODEL_EXCLUSION_PATTERNS:
        for m in re.finditer(pat, t, flags=re.IGNORECASE):
            raw_list = m.group("list")
            raw_list = re.split(r"(?:\)|;|\.)(?:\s|$)", raw_list)[0]
            parts = [p for p in _EXCL_SPLIT.split(raw_list) if p.strip()]
            for p in parts:
                item = _clean_excl_item(p)
                if item:
                    found.append(item)
    if not found:
        return None
    # de-dup case-insensitively, preserve order
    seen = set()
    out: List[str] = []
    for it in found:
        k = it.lower()
        if k not in seen:
            seen.add(k)
            out.append(it)
    return ", ".join(out) if out else None

def parse_exclusions_from_text(text: str) -> Optional[str]:
    """Backward-compatible wrapper (used where we already call it)."""
    return _extract_exclusions_any(text)

def exclusions_from_adjacent_lines(page: int, line_id: int, lines: Dict[Tuple[int,int], str], window: int = 2) -> Optional[str]:
    """
    Look up to `window` lines above and below the current line on the same page
    to find a model/trim exclusion parenthetical like '(excludes …)'.
    Prefer nearer lines; prefer previous line over next if equidistant.
    """
    # Gather all line_ids on this page in order
    neighbor_ids = sorted([lid for (p, lid) in lines.keys() if p == page])
    if line_id not in neighbor_ids:
        return None
    idx = neighbor_ids.index(line_id)

    # search offsets in priority order: -1, -2, +1, +2 ...
    for d in range(1, window + 1):
        # previous
        j = idx - d
        if j >= 0:
            lid = neighbor_ids[j]
            txt = lines.get((page, lid), "")
            hit = _extract_exclusions_any(txt)
            if hit:
                return hit
        # next
        j = idx + d
        if j < len(neighbor_ids):
            lid = neighbor_ids[j]
            txt = lines.get((page, lid), "")
            hit = _extract_exclusions_any(txt)
            if hit:
                return hit
    return None


# --------- label + model detection helpers ---------

def is_label_text(t: str) -> Optional[str]:
    """Return a canonical label key if the line contains a known label/synonym."""
    low = t.lower().strip()

    # Collect keys + synonyms; match longer strings first
    all_matches = []
    for k in LABEL_LEXICON.keys():
        all_matches.append((k, k))
    for k, v in LABEL_LEXICON.items():
        for syn in v.get("syn", []):
            all_matches.append((k, syn))
    all_matches.sort(key=lambda x: len(x[1]), reverse=True)

    for canonical_key, match_string in all_matches:
        if match_string in low:
            return canonical_key
    return None


def detect_model_year_model_trim(t: str) -> Tuple[Optional[int], Optional[str], Optional[str]]:
    """
    Pull MY + model + optional trim from a single line (non-table fallback).
    Never return 'Bonus' as a model.
    """
    year: Optional[int] = None
    my = re.search(r"\bMY\s?(\d{2})\b|\b(20(2[3-9]|3[0-9]))\b", t, flags=re.IGNORECASE)
    if my:
        year = 2000 + int(my.group(1)) if my.group(1) else int(my.group(2))

    low = t.lower()
    model: Optional[str] = None
    for key in sorted(MODEL_KEYS, key=len, reverse=True):
        if key in low:
            cand = MODEL_NORMALIZER.get(key, key)
            if cand.lower() != "bonus":
                model = cand
                break

    trim: Optional[str] = None
    if model:
        idx = low.find(model.lower())
        right = t[idx + len(model):]
        right = right.split("$")[0].split("(")[0].strip(" -–—\t")
        trim = right.strip() or None
        if trim and trim.upper().startswith("MY"):
            trim = None

    return year, model, trim


def parse_trim_and_amounts_from_line(t: str) -> Tuple[Optional[str], List[int]]:
    """
    Table rows (header has already set MY+MODEL):
    - TRIM is text left of the first '$'.
    - AMOUNTS are all dollar values on the line (often two identical columns).
    """
    if "$" not in t:
        return None, []
    left = t.split("$", 1)[0]
    # Throw away 'Bonus', date ranges, and trailing dashes.
    if BONUS_SOLO_PAT.match(left) or BONUS_WITH_DATES_PAT.match(left) or DATE_RANGE_LABEL_PAT.match(left):
        left = ""
    left = re.sub(r"[-–—]\s*$", "", left).strip("•-–— \t")
    trim = left.strip() or None

    amts = [normalize_amount(m.group(0)) for m in re.finditer(r"\$\s?\d[\d,]*", t)]
    amts = [a for a in amts if a is not None]
    return trim, amts


# ---------- main entry ----------

def extract(doc_id: str, spans: List[Span], parser_name: str = "pdfplumber") -> DocResult:
    logger = get_logger()
    logger.info(f"Starting extraction for document: {doc_id} with {len(spans)} spans")
    # light classification (handy for debug)
    for s in spans:
        txt = s.text.strip()
        if money_pat.fullmatch(txt):
            s.kind = "money"
        elif is_label_text(txt):
            s.kind = "label"
        else:
            s.kind = None

    # rebuild lines
    lines = lines_from_spans(spans)

    # TOC index once per doc
    toc = build_toc_index(lines)

    kvs: List[KV] = []

    # per-page + per-section context
    current_rebate_type: Optional[str] = None
    program_id: Optional[str] = None
    published_date: Optional[str] = None
    program_start_date: Optional[str] = None
    program_end_date: Optional[str] = None

    # table header context
    current_model_year_ctx: Optional[int] = None
    current_model_ctx: Optional[str] = None
    # header-level exclusion context to propagate to rows
    current_model_exclusions_ctx: Optional[str] = None

    # walk lines in page/line_id order
    for (page, line_id) in sorted(lines.keys()):
        txt_raw = lines[(page, line_id)]
        txt = txt_raw.strip()

        # On page start: preload TOC program + published AND set section title from TOC
        if line_id == min(l for (p, l) in lines.keys() if p == page):
            toc_hit = choose_toc_for_page(toc, page, current_rebate_type)
            if toc_hit:
                program_id = toc_hit.get("program_id") or program_id
                published_date = toc_hit.get("updated_iso") or published_date
                current_rebate_type = normalize_rebate_name(toc_hit.get("program_name")) or current_rebate_type
            # reset model context at each new page
            current_model_year_ctx = None
            current_model_ctx = None
            current_model_exclusions_ctx = None  # reset exclusions at page start

        # Hard stop: ignore pure “Bonus” lines and date-range “Bonus …” lines
        if BONUS_SOLO_PAT.match(txt) or BONUS_WITH_DATES_PAT.match(txt) or DATE_RANGE_LABEL_PAT.match(txt):
            continue

        # Heading inside the page?
        rh = REBATE_HEADING_PAT.search(txt)
        if rh:
            current_rebate_type = normalize_rebate_name(rh.group(0).strip())
            # refresh TOC context using the hint
            toc_hit = choose_toc_for_page(toc, page, current_rebate_type)
            if toc_hit:
                program_id = toc_hit.get("program_id") or program_id
                published_date = toc_hit.get("updated_iso") or published_date
            # reset model header context at a new section
            current_model_year_ctx = None
            current_model_ctx = None
            current_model_exclusions_ctx = None
            continue

        # Inline header row → next line has PID + dates
        if INLINE_HEADER_PAT.match(txt):
            nxt = lines.get((page, line_id + 1), "").strip()
            pid_m = PROGRAM_ID_PAT.search(nxt)
            dates = DATE_PAT.findall(nxt)
            if pid_m and dates:
                program_id = pid_m.group(0)
                iso_dates = []
                for (mm, dd, yyyy) in dates:
                    try:
                        iso_dates.append(f"{int(yyyy):04d}-{int(mm):02d}-{int(dd):02d}")
                    except Exception:
                        pass
                if len(iso_dates) >= 1:
                    published_date = iso_dates[0]
                if len(iso_dates) >= 2:
                    program_start_date = iso_dates[1]
                if len(iso_dates) >= 3:
                    program_end_date = iso_dates[2]
            continue

        # Program headers (value lives on next line)
        if re.fullmatch(r"Program ID", txt, flags=re.IGNORECASE):
            nxt = lines.get((page, line_id + 1), "").strip()
            pid = PROGRAM_ID_PAT.search(nxt)
            if pid:
                program_id = pid.group(0)
            continue
        if re.fullmatch(r"Published", txt, flags=re.IGNORECASE):
            nxt = lines.get((page, line_id + 1), "").strip()
            val = iso_date_or_none(nxt)
            if val:
                published_date = val
            continue
        if re.fullmatch(r"Program Start", txt, flags=re.IGNORECASE):
            nxt = lines.get((page, line_id + 1), "").strip()
            val = iso_date_or_none(nxt)
            if val:
                program_start_date = val
            continue
        if re.fullmatch(r"Program End", txt, flags=re.IGNORECASE):
            nxt = lines.get((page, line_id + 1), "").strip()
            val = iso_date_or_none(nxt)
            if val:
                program_end_date = val
            continue

        # model header context (e.g., "MY24 ID.4", "MY25 ID. Buzz")
        mh = MODEL_HEADER_PAT.match(txt)
        if mh:
            y = mh.group(1)
            model_raw = mh.group(2).strip()
            # paranoia: never let "Bonus" through as model
            if model_raw.lower() == "bonus":
                # treat this like a year header
                year = int(y) if len(y) == 4 else 2000 + int(y)
                current_model_year_ctx = year
                current_model_ctx = None
                current_model_exclusions_ctx = None
                continue
            year = int(y) if len(y) == 4 else 2000 + int(y)
            low = model_raw.lower()
            model_norm = None
            for key in sorted(MODEL_KEYS, key=len, reverse=True):
                if key in low:
                    model_norm = MODEL_NORMALIZER.get(key, key)
                    break
            current_model_year_ctx = year
            current_model_ctx = model_norm or model_raw

            # capture header-level model exclusions, e.g. "(excludes Golf R & ID. Buzz)"
            header_excl = parse_exclusions_from_text(txt_raw)
            current_model_exclusions_ctx = header_excl  # may be None if absent
            continue

        # Standalone MY header like "MY24" (no model on same line)
        ms = MY_STANDALONE_PAT.match(txt)
        if ms:
            y = ms.group(1)
            year = int(y) if len(y) == 4 else 2000 + int(y)
            current_model_year_ctx = year
            # rows will supply the model while inheriting this year
            current_model_exclusions_ctx = None  # reset to avoid stale carryover
            continue

        # "MY25 Bonus" (optionally with a date range) → set year, clear model ctx
        myb = MY_WITH_BONUS_PAT.match(txt)
        if myb:
            y = myb.group(1)
            year = int(y) if len(y) == 4 else 2000 + int(y)
            current_model_year_ctx = year
            current_model_ctx = None
            current_model_exclusions_ctx = None
            continue

        # money ranges: emit both endpoints, inherit context
        r = RANGE_PAT.search(txt)
        if r and "$" in txt:
            model_override_all = bool(ALL_VEHICLES_PAT.search(txt))
            lo = normalize_amount(r.group(1))
            hi = normalize_amount(r.group(2))

            # Prefer inline exclusions; fall back to adjacent-line; then header-level
            excl_inline = parse_exclusions_from_text(txt_raw)
            excl_adj = exclusions_from_adjacent_lines(page, line_id, lines, window=2) if not excl_inline else None
            excl_final = excl_inline or excl_adj or current_model_exclusions_ctx

            def emit_amt(amt: Optional[int]):
                if not amt:
                    return
                kvs.append(KV(
                    rebate_type=current_rebate_type,
                    program_id=program_id,
                    published_date=published_date,
                    program_start_date=program_start_date,
                    program_end_date=program_end_date,
                    model_year=current_model_year_ctx,
                    model=("all" if model_override_all else (current_model_ctx or "all")),
                    trim=None,
                    exclusions=excl_final,
                    amount_dollars=amt,
                    currency="USD",
                    page=page,
                    confidence=0.7
                ))
            emit_amt(lo)
            if hi and hi != lo:
                emit_amt(hi)
            continue

        # Standard or table row with dollar amounts
        if "$" in txt:
            # “all vehicles” phrase forces model="all"
            model_override_all = bool(ALL_VEHICLES_PAT.search(txt))

            # Table-style first if we have a header context
            if current_model_ctx and not model_override_all:
                trim, amounts = parse_trim_and_amounts_from_line(txt)
                if amounts:
                    # Prefer inline row exclusions; otherwise use adjacent; then header-level context
                    excl_inline = parse_exclusions_from_text(txt_raw)
                    excl_adj = exclusions_from_adjacent_lines(page, line_id, lines, window=2) if not excl_inline else None
                    excl_final = excl_inline or excl_adj or current_model_exclusions_ctx

                    trim_val = "All Trims" if (trim and trim.lower().startswith("all trims")) else trim
                    for a in amounts:
                        kvs.append(KV(
                            rebate_type=current_rebate_type,
                            program_id=program_id,
                            published_date=published_date,
                            program_start_date=program_start_date,
                            program_end_date=program_end_date,
                            model_year=current_model_year_ctx,
                            model=current_model_ctx,
                            trim=trim_val,
                            exclusions=excl_final,
                            amount_dollars=a,
                            currency="USD",
                            page=page,
                            confidence=0.9
                        ))
                    continue  # handled

            # Fallback: inline model/year/trim on the same line or generic “all”
            amounts = [normalize_amount(m.group(0)) for m in re.finditer(r"\$\s?\d[\d,]*", txt)]
            amounts = [a for a in amounts if a is not None]
            if not amounts:
                continue

            my, model, trim = detect_model_year_model_trim(txt)
            if model and model.lower() == "bonus":
                model = None  # nuke it

            # Prefer inline exclusions; otherwise adjacent; otherwise header-level
            excl_inline = parse_exclusions_from_text(txt_raw)
            excl_adj = exclusions_from_adjacent_lines(page, line_id, lines, window=2) if not excl_inline else None
            excl_final = excl_inline or excl_adj or current_model_exclusions_ctx

            if model_override_all:
                targets: List[Optional[str]] = ["all"]
            else:
                targets = split_models(model) if model else [current_model_ctx] if current_model_ctx else ["all"]

            for a in amounts:
                for mdel in targets:
                    kvs.append(KV(
                        rebate_type=current_rebate_type,
                        program_id=program_id,
                        published_date=published_date,
                        program_start_date=program_start_date,
                        program_end_date=program_end_date,
                        model_year=my or current_model_year_ctx,
                        model=mdel,
                        trim=trim,
                        exclusions=excl_final,
                        amount_dollars=a,
                        currency="USD",
                        page=page,
                        confidence=0.9 if (mdel and a) else 0.6
                    ))

    # Keep only programs that actually have amounts
    programs_with_amounts: set[str] = {
        kv.program_id for kv in kvs if kv.program_id and kv.amount_dollars is not None
    }

    filtered: List[KV] = []
    for kv in kvs:
        if kv.program_id:
            if kv.program_id in programs_with_amounts:
                filtered.append(kv)
        else:
            if kv.amount_dollars is not None:
                filtered.append(kv)

    logger.info(f"Extracted {len(filtered)} KV pairs from {len(kvs)} total candidates for document: {doc_id}")
    logger.debug(f"Found {len(toc)} TOC entries and {len(programs_with_amounts)} programs with amounts")

    # Final sweep: never let model be 'Bonus'
    for kv in filtered:
        if kv.model and kv.model.lower() == "bonus":
            kv.model = "all"

    # ---------- Sort by page appearance order ----------
    def sort_key(kv: KV):
        page_num = kv.page if kv.page is not None else 999999
        pid = kv.program_id or ""
        return (
            page_num,
            pid,
            (kv.model_year if kv.model_year is not None else 0),
            (kv.model or ""),
            (kv.trim or ""),
            (kv.amount_dollars if kv.amount_dollars is not None else 0),
        )

    all_pages = [kv.page for kv in filtered if kv.page is not None]
    logger.info(f"Page distribution before sorting: {sorted(set(all_pages))}")
    logger.debug(f"Before sorting (first 15): pages = {[kv.page for kv in filtered[:15]]}")

    filtered.sort(key=sort_key)

    logger.debug(f"After sorting (first 15): pages = {[kv.page for kv in filtered[:15]]}")
    logger.info(f"Sorting complete: {len(filtered)} KV pairs now ordered by page")

    # Build groups: program_id -> list of indices (into filtered)
    kv_groups: Dict[str, List[int]] = {}
    for idx, kv in enumerate(filtered):
        if not kv.program_id:
            kv_groups.setdefault("_NO_PROGRAM_ID_", []).append(idx)
            continue
        kv_groups.setdefault(kv.program_id, []).append(idx)

    # Stable group order (first appearance)
    group_order = list(dict.fromkeys([kv.program_id or "_NO_PROGRAM_ID_" for kv in filtered]))

    return DocResult(
        doc_id=doc_id,
        kvs=filtered,
        provenance={
            "parser": parser_name,
            "rules_version": "2025-08-31-adjacent-exclusions",
            "kv_groups": kv_groups,        # { program_id: [indices into kvs] }
            "kv_group_order": group_order, # processing/display order
        }
    )