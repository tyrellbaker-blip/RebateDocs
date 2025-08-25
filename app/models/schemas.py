"""
Data shapes for the extractor.

- Span: single token from the PDF + layout (text, bbox, page, line/block IDs).
- KV: one extracted rebate row (program context, model/year/trim, amount, exclusions).
- DocResult: per-PDF wrapper with a doc_id, list of KVs, and provenance.

If I need a new output column, I add it to `KV` here first and then populate it
in `extract.py`.
"""



from typing import List, Tuple, Optional, Literal, Dict
from pydantic import BaseModel, Field

BBox = Tuple[float, float, float, float]


class Span(BaseModel):
    """
    Word/token with layout metadata.

    Fields I care about:
    - text: the string as it appeared
    - bbox: (x0, y0, x1, y1) in page coords (left/top/right/bottom)
    - page: 1-based page number
    - line_id/block_id: grouping info to rebuild lines or blocks
    - kind: set later to "money" or "label" to help debugging/linkage
    """

    text: str
    bbox: BBox
    page: int
    font_bold: bool = False
    line_id: Optional[int] = None
    block_id: Optional[int] = None
    kind: Optional[Literal["money", "label"]] = None


class KV(BaseModel):
    """
    One extracted rebate entry.

    What ends up in CSV/JSON:
    - rebate_type: e.g. "Retail Customer Bonus", "Dealer Bonus - EV"
    - program_id, published/program start/end (ISO dates)
    - model_year, model, trim, exclusions (if text said "excludes …")
    - amount_dollars, currency (always USD here)
    - page, confidence: where we saw it and how sure we are
    """

    label_key: Optional[str] = None
    label_text: Optional[str] = None

    amount_dollars: Optional[int] = None
    currency: str = "USD"

    # Structured VW fields
    rebate_type: Optional[str] = None
    program_id: Optional[str] = None           # e.g., V25UAE08
    published_date: Optional[str] = None       # ISO YYYY-MM-DD
    program_start_date: Optional[str] = None   # ISO YYYY-MM-DD
    program_end_date: Optional[str] = None     # ISO YYYY-MM-DD
    model_year: Optional[int] = None
    model: Optional[str] = None
    trim: Optional[str] = None
    exclusions: Optional[str] = None           # Free-text exclusions parsed from the line/section
    notes: Optional[str] = None

    # layout/provenance
    page: int
    label_bbox: Optional[BBox] = None
    amount_bbox: Optional[BBox] = None
    confidence: float = 0.0


class DocResult(BaseModel):
    """
    Final output for one PDF.

    - doc_id: filename or filename-hash
    - kvs: list of extracted rows
    - provenance: misc info (parser name, rules version, debug counters)
    """

    doc_id: str
    kvs: List[KV] = Field(default_factory=list)
    provenance: Dict = Field(default_factory=dict)
