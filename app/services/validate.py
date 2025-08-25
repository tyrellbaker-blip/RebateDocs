"""
Post-processing for extracted rows.

- De-dupe: collapse repeats across the same program context + model/year/trim + amount.
- Normalize: light text cleanup where it prevents duplicate keys (e.g., spacing).
- Keep conservative: prefer keeping a legit duplicate over dropping good data.

This runs after `extract()` and before we show/export results so the tables are
stable and ready for data entry.
"""


from typing import List, Dict, Tuple, Optional
from app.models.schemas import KV


def _norm(s: Optional[str]) -> Optional[str]:
    """Lowercase + trim for stable comparisons."""
    return s.strip().lower() if s else None


def _dedupe_key(kv: KV) -> Tuple:
    """
    Define 'uniqueness' for rebates.
    Adjust if your downstream system expects different semantics.

    We consider a rebate unique by:
    - rebate_type (program)
    - program_id
    - published_date
    - program_start_date
    - program_end_date
    - model_year
    - model
    - trim
    - amount_dollars
    - currency
    """
    return (
        _norm(kv.rebate_type),
        _norm(kv.program_id),
        _norm(kv.published_date),
        _norm(kv.program_start_date),
        _norm(kv.program_end_date),
        kv.model_year,
        _norm(kv.model),
        _norm(kv.trim),
        kv.amount_dollars,
        kv.currency,
    )


def tighten(kvs: List[KV]) -> List[KV]:
    """
    - Drop entries with no amount.
    - Keep the highest-confidence row for each unique composite key.
    - Sort for a stable, human-friendly table (program, published desc, year desc, model, amount desc).
    """
    kvs = [k for k in kvs if k.amount_dollars is not None]

    best: Dict[Tuple, KV] = {}
    for kv in kvs:
        key = _dedupe_key(kv)
        if key not in best or (kv.confidence or 0) > (best[key].confidence or 0):
            best[key] = kv

    out = list(best.values())
    out.sort(key=lambda k: (
        _norm(k.rebate_type) or "",
        _norm(k.published_date) or "",
        -(k.model_year or 0),
        _norm(k.model) or "",
        -(k.amount_dollars or 0),
    ))
    return out
