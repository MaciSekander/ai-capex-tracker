"""Measuring extraction quality.

Two kinds of measurement:

* Automatic, on every fact: does the quote appear in the filing, and does the
  number appear in the quote? (``status`` = verified / partial / unverified)
* Human review, on a sample of documents: a reviewer marks each extracted fact
  correct or incorrect and adds facts the model missed. Because whole documents
  are reviewed, both precision and recall can be measured.

Workflow:
    python -m tracker.pipeline review-sheet --docs 25      # writes data/review/review_sheet.csv
    (open the CSV, fill in verdict and add missed facts)
    python -m tracker.pipeline import-reviews data/review/review_sheet.csv
    python -m tracker.pipeline eval
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from . import config
from .store import Store

SHEET_COLUMNS = [
    "doc_id", "url", "ticker", "fact_id", "metric", "value", "value_low", "value_high", "unit", "period",
    "counterparty", "quote", "status", "verdict", "corrected_value", "note",
]


def make_review_sheet(store: Store, n_docs: int, out: Path, seed: int = 0) -> Path:
    """Sample documents that produced facts, stratified so every metric is represented."""
    facts = store.table("facts")
    if facts.empty:
        raise RuntimeError("No facts yet. Run extraction first.")
    rng = np.random.default_rng(seed)
    docs: list[str] = []
    for metric in config.METRICS:
        pool = facts.loc[facts["metric"] == metric, "doc_id"].unique().tolist()
        rng.shuffle(pool)
        docs.extend(d for d in pool[: max(1, n_docs // len(config.METRICS))] if d not in docs)
    rest = [d for d in facts["doc_id"].unique() if d not in docs]
    rng.shuffle(rest)
    docs = (docs + rest)[:n_docs]
    sheet = facts[facts["doc_id"].isin(docs)].copy()
    sheet["verdict"] = ""
    sheet["corrected_value"] = ""
    sheet["note"] = ""
    sheet = sheet.reindex(columns=SHEET_COLUMNS).sort_values(["doc_id", "metric"])
    out.parent.mkdir(parents=True, exist_ok=True)
    sheet.to_csv(out, index=False)
    return out


def import_reviews(store: Store, path: Path) -> dict:
    """Rows with a fact_id are verdicts (correct / incorrect / unsure).
    Rows without a fact_id and verdict 'missed' are facts the model failed to extract."""
    sheet = pd.read_csv(path, dtype={"fact_id": str, "doc_id": str})
    sheet["verdict"] = sheet["verdict"].fillna("").str.strip().str.lower()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    reviewed = sheet[sheet["fact_id"].notna() & sheet["verdict"].isin(["correct", "incorrect", "unsure"])]
    reviews = pd.DataFrame(
        {
            "fact_id": reviewed["fact_id"],
            "doc_id": reviewed["doc_id"],
            "verdict": reviewed["verdict"],
            "corrected_value": pd.to_numeric(reviewed["corrected_value"], errors="coerce"),
            "note": reviewed["note"].fillna(""),
            "reviewed_at": now,
        }
    )
    missed = sheet[sheet["fact_id"].isna() & (sheet["verdict"] == "missed")]
    misses = pd.DataFrame(
        {
            "doc_id": missed["doc_id"],
            "metric": missed["metric"],
            "value": pd.to_numeric(missed["value"], errors="coerce"),
            "unit": missed["metric"].map(lambda m: config.METRICS.get(m, {}).get("unit")),
            "period": missed["period"],
            "quote": missed["quote"],
            "note": missed["note"].fillna(""),
            "reviewed_at": now,
        }
    )
    docs = pd.DataFrame({"doc_id": sheet["doc_id"].dropna().unique(), "reviewed_at": now})
    if not misses.empty:
        store.con.execute(
            "DELETE FROM misses WHERE doc_id IN (SELECT UNNEST(?))", [misses["doc_id"].unique().tolist()]
        )
        store.con.register("_m", misses)
        store.con.execute("INSERT INTO misses SELECT * FROM _m")
        store.con.unregister("_m")
    store.upsert("reviews", reviews)
    store.upsert("review_docs", docs)
    return {"verdicts": len(reviews), "missed": len(misses), "documents": len(docs)}


def scores(facts: pd.DataFrame, reviews: pd.DataFrame, misses: pd.DataFrame) -> pd.DataFrame:
    """Per-metric automatic grounding rates and reviewed precision / recall."""
    rows = []
    reviews = reviews if reviews is not None else pd.DataFrame(columns=["fact_id", "verdict"])
    misses = misses if misses is not None else pd.DataFrame(columns=["metric"])
    merged = facts.merge(reviews[["fact_id", "verdict"]], on="fact_id", how="left")
    for metric in list(config.METRICS) + ["all"]:
        f = merged if metric == "all" else merged[merged["metric"] == metric]
        m = misses if metric == "all" else misses[misses["metric"] == metric]
        n = len(f)
        correct = int((f["verdict"] == "correct").sum())
        incorrect = int((f["verdict"] == "incorrect").sum())
        missed = len(m)
        precision = correct / (correct + incorrect) if correct + incorrect else np.nan
        recall = correct / (correct + missed) if correct + missed else np.nan
        f1 = 2 * precision * recall / (precision + recall) if precision == precision and recall == recall and precision + recall else np.nan
        rows.append(
            {
                "metric": metric,
                "facts": n,
                "verified": (f["status"] == "verified").mean() if n else np.nan,
                "quote_found": (f["quote_match"] != "missing").mean() if n else np.nan,
                "reviewed": correct + incorrect,
                "missed": missed,
                "precision": precision,
                "recall": recall,
                "f1": f1,
            }
        )
    return pd.DataFrame(rows)
