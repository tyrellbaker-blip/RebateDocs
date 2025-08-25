"""
Rules that turn Spans into structured rebate rows (KVs).

- Build a per-document TOC index from lines like:
    Program ID Program Name Updated Page(s)
    V25URC08 Retail Customer Bonus 8/1/2025 10-11
  Then, at the start of each new page, preload that page’s program context
  (program_id + published date) BEFORE parsing its lines.

- Section context tracking (overrides TOC if present on the page):
    * Rebate heading (rebate_type)
    * Program header fields whose values are on the NEXT line:
        Program ID
        <V25UAE08>
        Published
        <8/1/2025>
        Program Start
        <8/1/2025>
        Program End
        <9/30/2025>

- Line-based rebate extraction:
    * "MY25 Atlas $3,500"
    * "Atlas Cross Sport $2,500"
    * "MY24 ID.4 Pro S AWD $6,400"
    * ranges like "$500 - $1,500" for generic entries
    * exclusions like "(excludes SEL)" or "excludes Golf R & ID. Buzz" → captured into `exclusions`

- Output: KV rows with structured fields (rebate_type, program_id, published/start/end, MY, model/trim, amount, exclusions).
- Token-only “money” hits are SUPPRESSED to avoid noisy rows.
"""


import re
from typing import List, Tuple, Optional, Dict, Any

from app.models.schemas import Span, KV, DocResult
from extraction.patterns import LABEL_LEXICON, MONEY_REGEX, MODEL_NORMALIZER, MODEL_KEYS
from app.util.layout import same_line  # kept for potential overlays

# --------- Precompiled regexes ---------
money_pat = re.compile(MONEY_REGEX)

# "MY25 Atlas $3,500" OR "Atlas Cross Sport $2,500" OR "Atlas $3,500 / unit"
LINE_MODEL_AMOUNT = re.compile(
    r"(?i)^(?P<prefix>MY(?P<year>\d{2})\s+)?(?P<model>[A-Za-z0-9\.\s&/+_-]+?)\s+\$?(?P<amount>\d{1,3}(?:,\d{3})*)\s*(?:/ unit)?(?:\s*\((?P<paren>[^)]*?)\))?\s*$"
)

# "MY24 ID.4 Pro S AWD $6,400" (captures model + trim)
LINE_MODEL_TRIM_AMOUNT = re.compile(
    r"(?i)^(?P<prefix>MY(?P<year>\d{2})\s+)?(?P<model>ID\.?\s*\.?\s*4|ID\.?\s*Buzz|Atlas(?:\s*Cross\s*Sport)?|Tiguan|Taos|Golf\s*GTI|Jetta(?:\s*GLI)?)"
    r"[\s\-]*(?P<trim>[A-Za-z0-9\.\s/&+_-]+?)\s+\$?(?P<amount>\d{1,3}(?:,\d{3})*)\s*(?:\((?P<paren>[^)]*?)\))?\s*$"
)

# Ranges like "$500 - $1,500" (applies to generic entries such as "New, unused VW models (excludes X) $500 - $1,500")
RANGE_MONEY = re.compile(r"(?i)\$\s*(?P<min>\d{1,3}(?:,\d{3})*)\s*-\s*\$\s*(?P<max>\d{1,3}(?:,\d{3})*)")

# Section headings (rebate types)
REBATE_HEADING = re.compile(
    r"(?i)^(Retail Customer Bonus(?:\s*[–-]\s*EV)?|Lease Dealer Bonus(?:\s*[–-]\s*EV)?|Dealer Bonus(?:\s*[–-]\s*EV)?|APR Customer Bonus(?:\s*[–-]\s*EV)?|"
    r"Loyalty Bonus|Tiguan Loyalty Code Bonus|Final Payout(?:\s*Bonus)?|Target Achievement Bonus|Sales Elite Program|VFI Program|Volkswagen Fleet Incentive)\b"
)

# --- Header labels whose values appear on the NEXT line (individual labels) ---
PROGRAM_ID_LABEL      = re.compile(r"(?i)^\s*Program\s+ID\s*:?\s*$")
PUBLISHED_LABEL       = re.compile(r"(?i)^\s*Published\s*:?\s*$")
PROGRAM_START_LABEL   = re.compile(r"(?i)^\s*Program\s+Start\s*:?\s*$")
PROGRAM_END_LABEL     = re.compile(r"(?i)^\s*Program\s+End\s*:?\s*$")

# --- Combined header row (all or most labels on one line) ---
COMBINED_HEADER_ROW   = re.compile(
    r"(?i)^\s*Program\s+ID(?:\s+Published)?(?:\s+Program\s+Start)?(?:\s+Program\s+End)?(?:\s+Dealer\s+Review\s+Ends)?\s*$"
)

# The code line itself, e.g., V25UAE08 (allow trailing period)
PROGRAM_ID_CODE       = re.compile(r"(?i)^\s*([A-Z]\d{2}[A-Z]{3}\d{2})\.?\s*$")

# Dates like 8/1/2025, 08-01-2025 → we’ll normalize to YYYY-MM-DD
DATE_VALUE            = re.compile(r"^\s*(\d{1,2})[/-](\d{1,2})[/-](\d{4})\s*$")

# --- TOC header + rows ---
TOC_HEADER_LINE = re.compile(r"(?i)^\s*Program\s+ID\s+Program\s+Name\s+Updated\s+Page\(s\)\s*$")
TOC_ROW = re.compile(
    r"^\s*(?P<pid>[A-Z]\d{2}[A-Z]{3}\d{2})\s+(?P<pname>.+?)\s+(?P<updated>\d{1,2}[/-]\d{1,2}[/-]\d{4})\s+(?P<pages>\d+(?:\s*[–-]\s*\d+)?)\s*$"
)

# Exclusion tokens: plain English “excludes …” phrases
EXCLUDES_INLINE = re.compile(r"(?i)\bexcludes?\b\s+(?P<ex>.+)$")


# --------- helpers ---------
def normalize_amount(text: str) -> Optional[int]:
    """Convert '$1,500' or '1,500' to integer dollars. Returns None on parse error."""
    s = (text or "").strip().replace("$", "").replace(",", "")
    try:
        return int(s)
    except ValueError:
        return None


def is_label_text(t: str) -> Optional[str]:
    """Check if a token likely refers to a known program heading (rebate type)."""
    low = (t or "").lower().strip(": ")
    for k, v in LABEL_LEXICON.items():
        if k in low:
            return k
        if any(syn in low for syn in v["syn"]):
            return k
    return None


def norm_model(raw: Optional[str]) -> Optional[str]:
    """Normalize a detected model string to its canonical form using MODEL_NORMALIZER."""
    if not raw:
        return None
    r = re.sub(r"\s+", " ", raw.strip().lower())
    r = r.replace("id. 4", "id.4").replace("id .4", "id.4")
    return MODEL_NORMALIZER.get(r, raw.strip())


def to_int(s: Optional[str]) -> Optional[int]:
    """Digits-with-commas → int; else None."""
    if not s:
        return None
    ss = s.replace(",", "").strip()
    return int(ss) if ss.isdigit() else None


def iso_date_or_none(raw: Optional[str]) -> Optional[str]:
    """Convert M/D/YYYY or M-D-YYYY to ISO YYYY-MM-DD; return None if not parseable."""
    if not raw:
        return None
    m = DATE_VALUE.match(raw.strip())
    if not m:
        return None
    mm, dd, yyyy = m.groups()
    try:
        mi, di, yi = int(mm), int(dd), int(yyyy)
        if not (1 <= mi <= 12 and 1 <= di <= 31 and 2000 <= yi <= 2100):
            return None
        return f"{yi:04d}-{mi:02d}-{di:02d}"
    except ValueError:
        return None


def lines_from_spans(spans: List[Span]) -> Dict[tuple, str]:
    """
    Group word spans by (page, line_id) and join text left-to-right.
    Returns dict: {(page, line_id): "joined text"}
    """
    from collections import defaultdict
    buckets = defaultdict(list)
    for s in spans:
        buckets[(s.page, s.line_id)].append(s)
    lines = {}
    for key, items in buckets.items():
        items.sort(key=lambda t: t.bbox[0])
        text = " ".join((t.text or "").strip() for t in items if (t.text or "").strip())
        lines[key] = text
    return lines


def split_models(raw: str) -> List[str]:
    """
    Split combined model strings into individual models.
    Examples:
      - "Atlas & Atlas Cross Sport" → ["Atlas", "Atlas Cross Sport"]
      - "Jetta & Jetta GLI" → ["Jetta", "Jetta GLI"]
      - "ID.4 / ID. Buzz" → ["ID.4", "ID. Buzz"]
    """
    if not raw:
        return []
    parts = re.split(r"\s*(?:&|/|,)\s*", raw.strip())
    out: List[str] = []
    for p in parts:
        p = p.strip(" -")
        if not p:
            continue
        nm = norm_model(p)
        out.append(nm or p)
    return out


def clean_trim(raw: Optional[str]) -> Optional[str]:
    """Remove placeholders like '-' and excess spaces."""
    if not raw:
        return None
    t = raw.strip()
    if t in {"-", "–"}:
        return None
    return t


def contains_page(pages_spec: List[int], page: int) -> bool:
    """True if 'page' is listed in the pages_spec list."""
    return page in pages_spec


def parse_pages_field(pages: str) -> List[int]:
    """Convert '7', '3-4', '19–20' into a list of ints [start..end]."""
    pages = pages.strip()
    if not pages:
        return []
    if re.fullmatch(r"\d+", pages):
        return [int(pages)]
    m = re.fullmatch(r"(\d+)\s*[–-]\s*(\d+)", pages)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        lo, hi = (a, b) if a <= b else (b, a)
        return list(range(lo, hi + 1))
    out: List[int] = []
    for tok in re.split(r"[,\s]+", pages):
        if tok.isdigit():
            out.append(int(tok))
    return out


def build_toc_index(lines: Dict[tuple, str]) -> List[Dict[str, Any]]:
    """
    Scan for a TOC block and return a list of entries:
      { program_id, program_name, updated_iso, pages: [ints] }
    """
    entries: List[Dict[str, Any]] = []
    toc_active = False

    for (page, line_id), text in sorted(lines.items()):
        t = text.strip()
        if TOC_HEADER_LINE.match(t):
            toc_active = True
            continue

        if toc_active:
            if not t:
                toc_active = False
                continue

            m = TOC_ROW.match(t)
            if m:
                pid = m.group("pid").upper()
                pname = m.group("pname").strip()
                updated = iso_date_or_none(m.group("updated"))
                pages = parse_pages_field(m.group("pages"))
                entries.append({
                    "program_id": pid,
                    "program_name": pname,
                    "updated_iso": updated,
                    "pages": pages
                })
            else:
                # Non-TOC-looking line usually ends the TOC block.
                toc_active = False

    return entries


def choose_toc_for_page(toc: List[Dict[str, Any]], page: int, rebate_type: Optional[str]) -> Optional[Dict[str, Any]]:
    """
    Pick the TOC entry whose pages include 'page'.
    If multiple match, prefer the one whose program_name contains the rebate_type string (case-insensitive).
    """
    candidates = [e for e in toc if contains_page(e.get("pages", []), page)]
    if not candidates:
        return None
    if rebate_type:
        rlow = rebate_type.lower()
        for e in candidates:
            if rlow in e.get("program_name", "").lower():
                return e
    return candidates[0]


def parse_exclusions_from_text(t: str, paren_hint: Optional[str]) -> Optional[str]:
    """
    Pull exclusion info from either an inline '(...)' group or trailing 'excludes ...' phrases.
    Returns a concise, normalized string or None.
    """
    # Preferred: parenthetical hint captured by the line regex
    if paren_hint:
        ph = paren_hint.strip()
        if ph:
            return ph

    # Fallback: trailing "excludes ..." phrase on the line
    m = EXCLUDES_INLINE.search(t)
    if m:
        ex = m.group("ex").strip().rstrip(".")
        return ex if ex else None

    return None


def link_amount_to_label(amount: Span, candidates: List[Span]) -> Tuple[Optional[Span], float, Optional[str]]:
    """
    Heuristic linker (kept for potential overlay/debug).
    Prefer labels on the same line and to the left; score by proximity and left bias.
    """
    same = [c for c in candidates if same_line(amount, c) and c.bbox[0] <= amount.bbox[0]]
    ordered = sorted(same, key=lambda c: abs(c.bbox[0] - amount.bbox[0]))
    pool = ordered or candidates

    best, best_score, best_key = None, 0.0, None
    for c in pool:
        key = is_label_text(c.text)
        if not key:
            continue

        dx = abs(amount.bbox[0] - c.bbox[0]) + 1
        dy = abs(amount.bbox[1] - c.bbox[1]) + 1
        score = 1.0 / (dx + dy)
        if c.bbox[0] < amount.bbox[0]:
            score += 0.2

        if score > best_score:
            best, best_score, best_key = c, score, key

    conf = min(0.99, best_score) if best else 0.0
    return best, conf, best_key


# --------- main ---------
def extract(doc_id: str, spans: List[Span], parser_name: str = "pdfplumber") -> DocResult:
    """
    Main extraction entry point.
    - Classifies tokens as 'money' vs 'label' (for future overlays; no token-only rows emitted).
    - Builds a TOC index to recover program_id/published by page.
    - Parses joined lines to derive structured rebates (rebate_type, header fields, MY, model/trim, amount, exclusions).
    - Supports any 'MYXX' → 2000+XX mapping.
    """
    # -------- Pass 1: classify tokens --------
    for s in spans:
        text = (s.text or "").strip()
        if money_pat.fullmatch(text) or money_pat.search(text):
            s.kind = "money"
        else:
            s.kind = "label" if is_label_text(text) else None

    # -------- Pass 2: lines + TOC --------
    lines = lines_from_spans(spans)

    # Build TOC index first
    toc_entries = build_toc_index(lines)

    kvs: List[KV] = []

    current_rebate_type: Optional[str] = None
    current_program_id: Optional[str] = None
    current_published: Optional[str] = None
    current_start: Optional[str] = None
    current_end: Optional[str] = None

    # Flags for "value is on the next line" (individual labels)
    expect_program_id_next = False
    expect_published_next = False
    expect_start_next = False
    expect_end_next = False

    # Flag for combined header row → next line has 3–4 values
    expect_combined_values_next = False

    last_seen_page: Optional[int] = None

    for (page, line_id), text in sorted(lines.items()):
        t = text.strip()

        # --- New page? preload TOC context before anything else on that page ---
        if last_seen_page != page:
            last_seen_page = page
            # Reset only the fields that are sourced from TOC (don't clear start/end here)
            toc = choose_toc_for_page(toc_entries, page, current_rebate_type)
            if toc:
                # Only set if not already set by explicit headers
                if not current_program_id:
                    current_program_id = toc.get("program_id")
                if not current_published:
                    current_published = toc.get("updated_iso")

        # (A) Rebate section heading updates context
        head = REBATE_HEADING.search(t)
        if head:
            current_rebate_type = head.group(1)

        # (B) Combined header row (e.g., "Program ID Published Program Start Program End")
        if COMBINED_HEADER_ROW.match(t):
            expect_combined_values_next = True
            expect_program_id_next = expect_published_next = expect_start_next = expect_end_next = False
            continue

        # (C) If combined header detected, parse the next line as values row
        if expect_combined_values_next:
            tokens = t.split()
            code = None
            mcode = PROGRAM_ID_CODE.match(t)
            if mcode:
                code = mcode.group(1).upper()
                rest = t[mcode.end():].strip()
                parts = rest.split()
            else:
                parts = tokens
                if parts:
                    m2 = PROGRAM_ID_CODE.match(parts[0])
                    if m2:
                        code = m2.group(1).upper()
                        parts = parts[1:]

            dates_found: List[str] = []
            for tok in parts:
                iso = iso_date_or_none(tok)
                if iso:
                    dates_found.append(iso)

            if code:
                current_program_id = code
            if len(dates_found) >= 1:
                current_published = dates_found[0]
            if len(dates_found) >= 2:
                current_start = dates_found[1]
            if len(dates_found) >= 3:
                current_end = dates_found[2]

            expect_combined_values_next = False
            continue

        # (D) Individual header labels → set flags to read next line
        if PROGRAM_ID_LABEL.match(t):
            expect_program_id_next = True
            continue
        if PUBLISHED_LABEL.match(t):
            expect_published_next = True
            continue
        if PROGRAM_START_LABEL.match(t):
            expect_start_next = True
            continue
        if PROGRAM_END_LABEL.match(t):
            expect_end_next = True
            continue

        # (E) If any individual-label flag is set, consume this line as the value
        if expect_program_id_next:
            mcode = PROGRAM_ID_CODE.match(t)
            if mcode:
                current_program_id = mcode.group(1).upper()
                expect_program_id_next = False
                continue

        if expect_published_next:
            iso = iso_date_or_none(t)
            if iso:
                current_published = iso
                expect_published_next = False
                continue

        if expect_start_next:
            iso = iso_date_or_none(t)
            if iso:
                current_start = iso
                expect_start_next = False
                continue

        if expect_end_next:
            iso = iso_date_or_none(t)
            if iso:
                current_end = iso
                expect_end_next = False
                continue

        # (F) "MY25 Atlas $3,500" OR "Atlas $3,500" (optional parenthetical exclusions)
        m1 = LINE_MODEL_AMOUNT.match(t)
        if m1:
            amt = to_int(m1.group("amount"))
            model_raw = m1.group("model")
            year2 = m1.group("year")
            year4 = (2000 + int(year2)) if year2 else None
            paren = m1.group("paren")
            exclusions = parse_exclusions_from_text(t, paren)

            models = split_models(model_raw)
            if not models:
                continue

            for model in models:
                if not any(model.lower().startswith(k) for k in MODEL_KEYS):
                    continue

                kvs.append(KV(
                    amount_dollars=amt,
                    currency="USD",
                    rebate_type=current_rebate_type,
                    program_id=current_program_id,
                    published_date=current_published,
                    program_start_date=current_start,
                    program_end_date=current_end,
                    model_year=year4,
                    model=model,
                    trim=None,
                    exclusions=exclusions,
                    page=page,
                    confidence=0.94 if current_rebate_type else 0.84
                ))
            continue

        # (G) "MY24 ID.4 Pro S AWD $6,400" (captures trim + optional parenthetical exclusions)
        m2 = LINE_MODEL_TRIM_AMOUNT.match(t)
        if m2:
            amt = to_int(m2.group("amount"))
            model = norm_model(m2.group("model"))
            trim = clean_trim((m2.group("trim") or ""))
            year2 = m2.group("year")
            year4 = (2000 + int(year2)) if year2 else None
            paren = m2.group("paren")
            exclusions = parse_exclusions_from_text(t, paren)

            if model:
                kvs.append(KV(
                    amount_dollars=amt,
                    currency="USD",
                    rebate_type=current_rebate_type,
                    program_id=current_program_id,
                    published_date=current_published,
                    program_start_date=current_start,
                    program_end_date=current_end,
                    model_year=year4,
                    model=model,
                    trim=trim if (trim and trim.lower() != model.lower()) else None,
                    exclusions=exclusions,
                    page=page,
                    confidence=0.95 if current_rebate_type else 0.85
                ))
            continue

        # (H) Ranges like "$500 - $1,500" (emit both endpoints for a generic entry)
        r = RANGE_MONEY.search(t)
        if r:
            lo, hi = to_int(r.group("min")), to_int(r.group("max"))
            paren = None
            # Capture any parenthetical exclusion from generic lines like:
            # "New, unused VW models (excludes Golf R & ID. Buzz) $500 - $1,500"
            pm = re.search(r"\(([^)]*?)\)", t)
            if pm:
                paren = pm.group(1)
            exclusions = parse_exclusions_from_text(t, paren)

            if lo is not None and hi is not None:
                for amt in (lo, hi):
                    kvs.append(KV(
                        amount_dollars=amt,
                        currency="USD",
                        rebate_type=current_rebate_type,
                        program_id=current_program_id,
                        published_date=current_published,
                        program_start_date=current_start,
                        program_end_date=current_end,
                        model_year=None,
                        model="New, unused VW models",
                        trim=None,
                        exclusions=exclusions,
                        page=page,
                        confidence=0.85
                    ))

    return DocResult(
        doc_id=doc_id,
        kvs=kvs,
        provenance={"parser": parser_name, "rules_version": "2025-08-25"}
    )
