"""
PDF -> Span list (parser adapter).

- Uses pdfplumber to walk pages and words.
- Emits a flat list of `Span` objects with text + bbox + page + coarse line/block IDs.
- Leaves layout heuristics to the extraction layer (this module just surfaces
  what the PDF actually contains in a structured, consistent way).

Why separate this:
- Easy to swap/compare parsers (pdfplumber, PyMuPDF, etc.) without touching
  the extraction rules.
- Keeps extraction code focused on text patterns, not PDF internals.
"""

from typing import List
import pdfplumber
from app.models.schemas import Span
from app.util.logger import get_logger


def parse_with_pdfplumber(path: str) -> List[Span]:
    """
    Read a PDF and return a flat list of Spans (one per word-ish token).

    Notes/assumptions:
    - Words are sorted left→right within a line.
    - line_id and block_id are best-effort (good enough for "same line" checks).
    - No layout inference here; just surface what the PDF gives us.

    Returns:
        List[Span]: tokens across all pages, 1-based page numbers.
    """
    logger = get_logger()
    logger.info(f"Starting PDF parsing for: {path}")
    
    spans: List[Span] = []
    try:
        with pdfplumber.open(path) as pdf:
            logger.info(f"Opened PDF with {len(pdf.pages)} pages")
            for p_idx, page in enumerate(pdf.pages, start=1):
                logger.debug(f"Processing page {p_idx}")
                # Use text flow to get better left-to-right word order
                words = page.extract_words(
                    use_text_flow=True,
                    keep_blank_chars=False,
                    x_tolerance=2,   # horizontal merge tolerance
                    y_tolerance=3    # vertical grouping tolerance
                ) or []

                logger.debug(f"Page {p_idx}: extracted {len(words)} words")

                # Create a monotonic line_id by detecting 'y' jumps
                line_id = -1
                last_y = None
                for w in words:
                    y0 = w.get("top")
                    if last_y is None or abs(y0 - last_y) > 3:
                        line_id += 1
                        last_y = y0

                    spans.append(Span(
                        text=w["text"],
                        bbox=(w["x0"], w["top"], w["x1"], w["bottom"]),
                        page=p_idx,
                        font_bold=False,      # pdfplumber doesn't expose style easily; keep False
                        line_id=line_id,
                        block_id=None
                    ))
        
        logger.info(f"Successfully parsed PDF: extracted {len(spans)} total spans")
        return spans
        
    except Exception as e:
        logger.error(f"Error parsing PDF {path}: {str(e)}")
        raise
