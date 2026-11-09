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

# Leases that commenced in the period: right-of-use assets recognized for new lease liabilities (present value).
LEASE_FLOW_CONCEPTS = {
    "lease_starts_operating": [("us-gaap", "RightOfUseAssetObtainedInExchangeForOperatingLeaseLiability")],
    "lease_starts_finance": [("us-gaap", "RightOfUseAssetObtainedInExchangeForFinanceLeaseLiability")],
}
CONCEPTS.update(LEASE_FLOW_CONCEPTS)

# Balance-sheet lease totals at each period end, used to convert present values to total payments.
INSTANT_CONCEPTS = {
    "lease_liability_operating": ("us-gaap", "OperatingLeaseLiability"),
    "lease_liability_finance": ("us-gaap", "FinanceLeaseLiability"),
    "lease_payments_operating": ("us-gaap", "LesseeOperatingLeaseLiabilityPaymentsDue"),
    "lease_payments_finance": ("us-gaap", "FinanceLeaseLiabilityPaymentsDue"),
    # Lease assets, to estimate quarterly lease starts where a company reports them only for the full year.
    "lease_rou_operating": ("us-gaap", "OperatingLeaseRightOfUseAsset"),
    "lease_rou_finance_gross": ("us-gaap", "FinanceLeaseRightOfUseAssetBeforeAccumulatedAmortization"),
}

# Used only when a company reports none of the concepts above (Hut 8 tags capex this way).
FALLBACK_CONCEPTS = {
    "capex": [("us-gaap", "PaymentsToAcquireMachineryAndEquipment")],
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


def _instants_for(facts: dict, taxonomy: str, concept: str) -> pd.DataFrame:
    node = facts.get("facts", {}).get(taxonomy, {}).get(concept)
    if not node:
        return pd.DataFrame()
    rows = [{"end": v["end"], "val": float(v["val"]), "filed": v.get("filed", "")}
            for v in node.get("units", {}).get("USD", []) if "start" not in v]
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    df["end"] = pd.to_datetime(df["end"]).dt.date
    return df.sort_values("filed").drop_duplicates("end", keep="last").reset_index(drop=True)


def company_instants(facts: dict, ticker: str) -> pd.DataFrame:
    """Lease liabilities and total lease payments due at each period end, in USD billions."""
    frames = []
    for metric, (taxonomy, concept) in INSTANT_CONCEPTS.items():
        df = _instants_for(facts, taxonomy, concept)
        if df.empty:
            continue
        frames.append(pd.DataFrame({"ticker": ticker, "metric": metric, "quarter": [quarter_label(e) for e in df["end"]],
                                    "start": df["end"], "end": df["end"], "value_usd_billion": df["val"] / 1e9,
                                    "method": "instant", "concept": f"{taxonomy}:{concept}"}))
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


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
        best = None
        for tier in (candidates, FALLBACK_CONCEPTS.get(metric, [])):
            best_end = None
            for taxonomy, concept in tier:
                df = _facts_for(facts, taxonomy, concept)
                if df.empty:
                    continue
                latest = df["end"].max()
                if best_end is None or latest > best_end:
                    best, best_end = (taxonomy, concept, df), latest
            if best is not None:
                break
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
    # Full-year lease starts, for companies that stop tagging quarters (Meta reports them in the 10-K only).
    for metric, [(taxonomy, concept)] in LEASE_FLOW_CONCEPTS.items():
        df = _facts_for(facts, taxonomy, concept)
        if df.empty:
            continue
        fy = df[[350 <= (e - s_).days <= 380 for s_, e in zip(df["start"], df["end"])]]
        if not fy.empty:
            frames.append(pd.DataFrame({"ticker": ticker, "metric": f"{metric}_fy",
                                        "quarter": [quarter_label(e) for e in fy["end"]], "start": fy["start"],
                                        "end": fy["end"], "value_usd_billion": fy["val"] / 1e9, "method": "annual",
                                        "concept": f"{taxonomy}:{concept}"}))
    inst = company_instants(facts, ticker)
    if not inst.empty:
        frames.append(inst)
    if not frames:
        return pd.DataFrame(columns=["ticker", "metric", "quarter", "start", "end", "value_usd_billion", "method", "concept"])
    out = pd.concat(frames, ignore_index=True)
    return out[["ticker", "metric", "quarter", "start", "end", "value_usd_billion", "method", "concept"]]
