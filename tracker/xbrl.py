"""Quarterly capex and operating cash flow from SEC XBRL company facts.

Cash flow statements in 10-Qs report year-to-date figures, so most quarters must
be derived: Q2 = six months - three months, Q4 = full year - nine months. This
module takes every reported duration for a concept, keeps the latest filed value
for each (start, end) pair, and differences cumulative figures that share a
start date. Directly reported three-month figures win over derived ones.
"""

from __future__ import annotations

from datetime import date

import pandas as pd

CONCEPTS = {
    "capex": [
        ("us-gaap", "PaymentsToAcquirePropertyPlantAndEquipment"),
        ("us-gaap", "PaymentsToAcquireProductiveAssets"),
        ("us-gaap", "PaymentsForCapitalImprovements"),
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipmentClassifiedAsInvestingActivities"),
        ("ifrs-full", "PurchaseOfPropertyPlantAndEquipment"),
    ],
    "operating_cash_flow": [
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivities"),
        ("us-gaap", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"),
        ("ifrs-full", "CashFlowsFromUsedInOperatingActivities"),
    ],
}

QUARTER_DAYS = (80, 100)


def _facts_for(facts: dict, taxonomy: str, concept: str) -> pd.DataFrame:
    node = facts.get("facts", {}).get(taxonomy, {}).get(concept)
    if not node:
        return pd.DataFrame()
    rows = []
    for unit, values in node.get("units", {}).items():
        if unit not in ("USD",):
            continue
        for v in values:
            if "start" not in v:
                continue
            rows.append({"start": v["start"], "end": v["end"], "val": float(v["val"]), "filed": v.get("filed", ""),
                         "form": v.get("form", "")})
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["start"] = pd.to_datetime(df["start"]).dt.date
    df["end"] = pd.to_datetime(df["end"]).dt.date
    # Latest filing wins for restated periods.
    return df.sort_values("filed").drop_duplicates(["start", "end"], keep="last").reset_index(drop=True)


def quarterly_series(df: pd.DataFrame) -> pd.DataFrame:
    """Discrete quarterly values from a mix of 3-month and year-to-date facts."""
    if df.empty:
        return pd.DataFrame(columns=["start", "end", "value", "method"])
    df = df.copy()
    df["days"] = [(e - s).days for s, e in zip(df["start"], df["end"])]
    out: dict[date, dict] = {}

    for _, r in df[(df["days"] >= QUARTER_DAYS[0]) & (df["days"] <= QUARTER_DAYS[1])].iterrows():
        out[r["end"]] = {"start": r["start"], "end": r["end"], "value": r["val"], "method": "reported"}

    for _, group in df.groupby("start"):
        g = group.sort_values("end")
        ends, vals = list(g["end"]), list(g["val"])
        for i in range(1, len(g)):
            span = (ends[i] - ends[i - 1]).days
            if QUARTER_DAYS[0] <= span <= QUARTER_DAYS[1] and ends[i] not in out:
                out[ends[i]] = {
                    "start": ends[i - 1],
                    "end": ends[i],
                    "value": vals[i] - vals[i - 1],
                    "method": "derived",
                }
    res = pd.DataFrame(sorted(out.values(), key=lambda r: r["end"]))
    return res


def quarter_label(d: date) -> str:
    """Calendar quarter containing the period end, e.g. 2025-09-30 -> 2025Q3."""
    return f"{d.year}Q{(d.month - 1) // 3 + 1}"


def company_quarterly(facts: dict, ticker: str) -> pd.DataFrame:
    """Quarterly capex and operating cash flow for one company, in USD billions."""
    frames = []
    for metric, candidates in CONCEPTS.items():
        best, best_end = None, None
        for taxonomy, concept in candidates:
            df = _facts_for(facts, taxonomy, concept)
            if df.empty:
                continue
            latest = df["end"].max()
            if best_end is None or latest > best_end:
                best, best_end = (taxonomy, concept, df), latest
        if best is None:
            continue
        taxonomy, concept, df = best
        q = quarterly_series(df)
        if q.empty:
            continue
        q["ticker"] = ticker
        q["metric"] = metric
        q["concept"] = f"{taxonomy}:{concept}"
        q["value_usd_billion"] = q["value"] / 1e9
        q["quarter"] = [quarter_label(e) for e in q["end"]]
        frames.append(q.drop(columns=["value"]))
    if not frames:
        return pd.DataFrame(columns=["ticker", "metric", "quarter", "start", "end", "value_usd_billion", "method", "concept"])
    out = pd.concat(frames, ignore_index=True)
    return out[["ticker", "metric", "quarter", "start", "end", "value_usd_billion", "method", "concept"]]
