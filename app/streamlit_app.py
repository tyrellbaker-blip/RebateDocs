"""
Streamlit front-end for rebate extraction.

- Accepts one or more uploaded PDFs.
- Writes each to `.tmp_uploads/` using a short content hash for stable names.
- Runs `parse_with_pdfplumber()` -> `extract()` -> `validate.tighten()` per file.
- Shows a per-document table and a combined de-duped table.
- Exposes CSV/JSON downloads (per doc and combined).

Goal: stop flipping through PDFs — get a clean, unique list of rebates with
program context (program_id, dates), model/year/trim, amount, and exclusions.
All processing happens locally.
"""

from __future__ import annotations

# --- ensure package imports work when launched directly ---
import sys, os
sys.path.append(os.path.dirname(os.path.dirname(__file__)))

import hashlib
import json
from pathlib import Path
from typing import List, Dict, Any

import pandas as pd
import streamlit as st

# --- Internal modules ---
from app.services.parse_pdf import parse_with_pdfplumber
from app.services.extract import extract
from app.services.validate import tighten
from app.models.schemas import KV, DocResult

# ---------------------------- Page setup ----------------------------

st.set_page_config(page_title="VW Rebate Extractor (Local)", layout="wide")
st.title("VW Rebate Extractor (Local)")
st.caption("Parse PDFs → capture unique rebates (program, dates, model/year, model/trim, amount, exclusions).")

# ---------------------------- Sidebar help ----------------------------

with st.sidebar:
    st.header("How it works")
    st.markdown(
        "- Files are processed locally; a temp copy is saved under `.tmp_uploads/`.\n"
        "- Parser: pdfplumber extracts words + layout; extraction rules scan lines.\n"
        "- We preload Program ID / Updated from the Table of Contents for each page.\n"
        "- Uniqueness: de-dup on (program + dates + model_year + model/trim + amount)."
    )
    st.divider()
    st.markdown("**Tip:** If a row is missing, open the Debug expander to inspect raw tokens.")

# ---------------------------- Uploader & Controls ----------------------------

uploaded = st.file_uploader(
    "Upload one or more incentive PDFs",
    type=["pdf"],
    accept_multiple_files=True,
    help="Drag-and-drop or browse. Multiple files will be combined and de-duplicated below."
)

col_btn1, col_btn2 = st.columns([1, 3])
with col_btn1:
    run_btn = st.button("Run Extraction", type="primary")
with col_btn2:
    st.write("")  # spacer


# ---------------------------- Helpers ----------------------------

def file_hash(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()[:16]


def ensure_tmp_dir() -> Path:
    tmp_dir = Path.cwd() / ".tmp_uploads"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    return tmp_dir


def kv_to_row(kv: KV) -> Dict[str, Any]:
    return {
        "rebate_type": kv.rebate_type,
        "program_id": kv.program_id,
        "published_date": kv.published_date,
        "program_start_date": kv.program_start_date,
        "program_end_date": kv.program_end_date,
        "model_year": kv.model_year,
        "model": kv.model,
        "trim": kv.trim,
        "amount_dollars": kv.amount_dollars,
        "currency": kv.currency,
        "exclusions": kv.exclusions,
        "page": kv.page,
        "confidence": round(kv.confidence or 0.0, 3),
    }


def rows_to_dataframe(rows: List[Dict[str, Any]]) -> pd.DataFrame:
    cols = [
        "rebate_type", "program_id", "published_date", "program_start_date", "program_end_date",
        "model_year", "model", "trim",
        "amount_dollars", "currency", "exclusions",
        "page", "confidence"
    ]
    df = pd.DataFrame(rows)
    for c in cols:
        if c not in df.columns:
            df[c] = None
    return df[cols]


# ---------------------------- Main run ----------------------------

results: List[DocResult] = []

if run_btn and uploaded:
    tmp_dir = ensure_tmp_dir()

    for uf in uploaded:
        data = uf.read()
        doc_id = f"{uf.name}-{file_hash(data)}"
        tmp_path = tmp_dir / f"{doc_id}.pdf"
        with open(tmp_path, "wb") as f:
            f.write(data)

        spans = parse_with_pdfplumber(str(tmp_path))
        doc = extract(doc_id, spans, parser_name="pdfplumber")

        doc.provenance["spans"] = spans
        doc.provenance["parse_stats"] = {"pdfplumber_word_count": len(spans)}

        # Collapse duplicates in this document
        doc.kvs = tighten(doc.kvs)
        results.append(doc)

# ---------------------------- Display results ----------------------------

if not results:
    st.info("Upload PDFs and click **Run Extraction** to see results.")
else:
    tabs = st.tabs([r.doc_id for r in results])
    per_doc_tables: List[pd.DataFrame] = []

    for tab, doc in zip(tabs, results):
        with tab:
            st.subheader("Unique Rebates (This Document)")

            rows = [kv_to_row(kv) for kv in doc.kvs]
            df = rows_to_dataframe(rows)
            st.dataframe(df, use_container_width=True)

            col_dl1, col_dl2 = st.columns(2)
            with col_dl1:
                safe_doc = doc.model_dump()
                safe_doc.get("provenance", {}).pop("spans", None)
                json_blob = json.dumps(safe_doc, indent=2)
                st.download_button(
                    "Download JSON (this doc)",
                    data=json_blob,
                    file_name=f"{doc.doc_id}.json",
                    mime="application/json",
                    use_container_width=True
                )

            with col_dl2:
                csv_blob = df.to_csv(index=False)
                st.download_button(
                    "Download CSV (this doc)",
                    data=csv_blob,
                    file_name=f"{doc.doc_id}.csv",
                    mime="text/csv",
                    use_container_width=True
                )

            with st.expander("Debug: parse stats & first 40 tokens of page 1"):
                stats = doc.provenance.get("parse_stats", {})
                st.write(stats)
                spans_dbg = doc.provenance.get("spans", [])
                toks = [getattr(s, "text", "") for s in spans_dbg if getattr(s, "page", 1) == 1][:40]
                st.write(toks)

            per_doc_tables.append(df)

    st.markdown("## Combined Results (All Documents, De-Duplicated)")
    all_kvs: List[KV] = []
    for doc in results:
        all_kvs.extend(doc.kvs)

    from copy import deepcopy
    from app.services.validate import tighten as tighten_all
    all_unique = tighten_all(deepcopy(all_kvs))

    combined_rows = [kv_to_row(kv) for kv in all_unique]
    combined_df = rows_to_dataframe(combined_rows)
    st.dataframe(combined_df, use_container_width=True)

    col_all1, col_all2 = st.columns(2)
    with col_all1:
        st.download_button(
            "Download CSV (combined unique)",
            data=combined_df.to_csv(index=False),
            file_name="rebates_combined_unique.csv",
            mime="text/csv",
            use_container_width=True
        )
    with col_all2:
        combined_json = json.dumps(combined_rows, indent=2)
        st.download_button(
            "Download JSON (combined unique)",
            data=combined_json,
            file_name="rebates_combined_unique.json",
            mime="application/json",
            use_container_width=True
        )
