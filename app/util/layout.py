"""
Layout helpers (pure geometry/position checks).

- same_line(a, b, y_tol): quick test for "are these tokens on the same line?"
- distance(a, b): simple proximity metric to bias left-of/near labels.

Keeps coordinate math out of the extraction code.
"""

from typing import List, Tuple
from app.models.schemas import Span

def bbox_center(b: Tuple[float, float,float, float]):
    x1, y1, x2, y2 = b
    return ( (x1+x2)/2.0, (y1+y2)/2.0 )

def same_line(a: Span, b: Span, y_tol: float = 3.0) -> bool:
    ay = (a.bbox[1] + a.bbox[3]) / 2.0
    by = (b.bbox[1] + b.bbox[3]) / 2.0
    return abs(ay - by) <= y_tol

def distance(a:Span, b:Span) -> float:
    ax, ay = bbox_center(a.bbox)
    bx,by = bbox_center(b.bbox)
    return abs(ax - bx) + abs(ay - by)