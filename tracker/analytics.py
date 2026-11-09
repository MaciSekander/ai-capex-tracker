"""Turns raw tables into the views the dashboard shows. Pure pandas, no I/O.

Raw ``facts`` hold one row per number per document. Companies repeat the same
number across a press release, an investor deck and later releases, so the
dashboard works with *disclosures*: one row per distinct number, with the date
it was first and last stated and how many documents state it.
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

STATUS_RANK = {"verified": 0, "partial": 1, "unverified": 2}

# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------

_Q = re.compile(r"\bQ([1-4])\s*(?:FY)?\s*['’]?\s*(\d{4}|\d{2})\b", re.IGNORECASE)
_Q_REV = re.compile(r"\b(?:FY)?\s*(\d{4}|\d{2})\s*Q([1-4])\b", re.IGNORECASE)
_H = re.compile(r"\b(?:H([12])|(first|second) half(?: of)?)\s*(?:FY|fiscal)?\s*['’]?(\d{4}|\d{2})\b", re.IGNORECASE)
_YEAR = re.compile(r"(?:\bFY\s*['’]?|\bfiscal(?: year)?\s*|\bcalendar(?: year)?\s*|\bfull[- ]year\s*|\b)(20\d{2})\b|\bFY\s*['’]?(\d{2})\b",
                   re.IGNORECASE)
_VAGUE = re.compile(r"next|over|through|by|cumulative|project|horizon|construction|remaining|multi|several|life|"
                    r"expansion|deployment|initial|first \d|phase|near-term|long-term|since", re.IGNORECASE)


def _yr(s: str) -> int:
    y = int(s)
    return y + 2000 if y < 100 else y


def period_label(period) -> str | None:
    """A comparable label for a guidance period: '2026', 'H2 2026', 'Q1 2027'; None if open-ended.

    Fiscal and calendar years share a label because each company guides on one basis consistently.
    """
    if not isinstance(period, str) or not period.strip():
        return None
    p = period.strip()
    m = _Q.search(p)
    if m:
        return f"Q{m.group(1)} {_yr(m.group(2))}"
    m = _Q_REV.search(p)
    if m:
        return f"Q{m.group(2)} {_yr(m.group(1))}"
    m = _H.search(p)
    if m:
        half = m.group(1) or ("1" if m.group(2).lower() == "first" else "2")
        return f"H{half} {_yr(m.group(3))}"
    if _VAGUE.search(p):
        return None
    years = {_yr(a or b) for a, b in _YEAR.findall(p)}
    if len(years) == 1:
        return str(years.pop())
    return None


def quarter_from_period(period) -> str | None:
    """'Q2 2026', "Q2'26", 'second quarter of 2026' -> '2026Q2'. None for anything else."""
    if not isinstance(period, str):
        return None
    m = _Q.search(period) or None
    if m:
        return f"{_yr(m.group(2))}Q{m.group(1)}"
    m = re.search(r"\b(first|second|third|fourth) quarter(?: of)?\s*(?:fiscal\s*)?(\d{4})", period, re.IGNORECASE)
    if m:
        n = ["first", "second", "third", "fourth"].index(m.group(1).lower()) + 1
        return f"{m.group(2)}Q{n}"
    return None


# ---------------------------------------------------------------------------
# Disclosures (deduplicated facts)
# ---------------------------------------------------------------------------


def _norm_text(s) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip() if isinstance(s, str) else ""


DEAL_METRICS = ("customer_contract", "financing")
DEAL_WINDOW_DAYS = 180


def disclosures(facts: pd.DataFrame) -> pd.DataFrame:
    """One row per distinct (company, metric, value, period, counterparty, site).

    Keeps the best-checked row (verified over partial over unverified, then the earliest), and adds
    ``first_stated``, ``last_stated`` and ``mentions`` (number of documents stating it).

    Deals are restated in different words ("a $27 billion agreement with Meta", "up to $27 billion"),
    so contracts and financings with the same company, metric and amount stated within
    ``DEAL_WINDOW_DAYS`` of each other are merged into the first one.
    """
    if facts.empty:
        return facts.assign(first_stated=pd.NaT, last_stated=pd.NaT, mentions=0)
    f = facts.copy()
    f["filing_date"] = pd.to_datetime(f["filing_date"])
    f["_v"] = f["value"].round(4)
    f["_p"] = f["period"].map(lambda p: period_label(p) or _norm_text(p))
    f["_cp"] = f["counterparty"].map(_norm_text)
    f["_site"] = f["site"].map(_norm_text)
    f["_rank"] = f["status"].map(STATUS_RANK).fillna(3)
    key = ["ticker", "metric", "_v", "_p", "_cp", "_site"]
    stats = f.groupby(key, dropna=False).agg(first_stated=("filing_date", "min"), last_stated=("filing_date", "max"),
                                             docs=("doc_id", lambda s: set(s))).reset_index()
    best = f.sort_values(["_rank", "filing_date"]).drop_duplicates(key, keep="first")
    out = best.merge(stats, on=key, how="left")

    deals = out[out["metric"].isin(DEAL_METRICS)].sort_values("first_stated")
    keep, merged_into = [], {}
    for _, grp in deals.groupby(["ticker", "metric", "_v"], dropna=False):
        anchor = None
        for idx, r in grp.iterrows():
            if anchor is not None and (r["first_stated"] - out.at[anchor, "first_stated"]).days <= DEAL_WINDOW_DAYS:
                out.at[anchor, "last_stated"] = max(out.at[anchor, "last_stated"], r["last_stated"])
                out.at[anchor, "docs"] = out.at[anchor, "docs"] | r["docs"]
                merged_into[idx] = anchor
            else:
                anchor = idx
    out = out.drop(index=list(merged_into))
    out["mentions"] = out["docs"].map(len)
    out = out.drop(columns=["_v", "_p", "_cp", "_site", "_rank", "docs"])
    return out.sort_values("first_stated", ascending=False).reset_index(drop=True)


def apply_corrections(facts: pd.DataFrame, corrections: pd.DataFrame) -> pd.DataFrame:
    """Drop facts listed in the corrections file (``action == 'exclude'``)."""
    if corrections is None or corrections.empty:
        return facts
    drop = set(corrections.loc[corrections["action"].str.strip().str.lower() == "exclude", "fact_id"].str.strip())
    return facts[~facts["fact_id"].isin(drop)]


PERIODIC_FORMS = ("10-Q", "10-K", "20-F")


def drop_periodic_deals(facts: pd.DataFrame) -> pd.DataFrame:
    """Contracts and financings come from 8-K / 6-K announcements only.

    10-Q and 10-K notes restate older deals every quarter (and give year-to-date totals), which would
    otherwise show up as new deals dated to the report.
    """
    if facts.empty or "form" not in facts:
        return facts
    return facts[~(facts["form"].isin(PERIODIC_FORMS) & facts["metric"].isin(DEAL_METRICS))]


# ---------------------------------------------------------------------------
# Capex guidance
# ---------------------------------------------------------------------------


def guidance_history(facts: pd.DataFrame, tolerance: float = 0.005) -> pd.DataFrame:
    """Capex guidance statements in order, with the change against the previous statement for the same period.

    ``revision`` is computed from the numbers: 'initial', 'raised', 'lowered' or 'maintained'. A range
    counts as raised if its midpoint rose, or if the midpoint held and either end moved up.
    ``company_wording`` keeps what the filing itself said ('raised', 'reaffirmed', ...), when it said so.
    """
    cols = ["ticker", "period_label", "filing_date", "value", "value_low", "value_high", "revision",
            "change_b", "change_pct", "prior_value", "company_wording", "status", "quote", "url", "period"]
    g = facts[facts["metric"] == "capex_guidance"].copy()
    if g.empty:
        return pd.DataFrame(columns=cols)
    g["period_label"] = g["period"].map(period_label)
    g = g[g["period_label"].notna()].copy()
    g["filing_date"] = pd.to_datetime(g["filing_date"])
    g["_rank"] = g["status"].map(STATUS_RANK).fillna(3)
    g["_has_range"] = g["value_low"].notna() & g["value_high"].notna()
    # Same company, period and day: the press release and the deck say the same thing once.
    g = (g.sort_values(["_rank", "_has_range"], ascending=[True, False])
         .drop_duplicates(["ticker", "period_label", "filing_date"], keep="first")
         .sort_values(["ticker", "period_label", "filing_date"]))
    g["low"] = g["value_low"].fillna(g["value"])
    g["high"] = g["value_high"].fillna(g["value"])
    rows = []
    for _, grp in g.groupby(["ticker", "period_label"], sort=False):
        prev = None
        for r in grp.itertuples():
            if prev is None:
                rev, delta, pct, prior = "initial", np.nan, np.nan, np.nan
            else:
                delta = r.value - prev.value
                pct = delta / prev.value if prev.value else np.nan
                prior = prev.value
                tol = abs(prev.value) * tolerance
                if delta > tol or (abs(delta) <= tol and (r.low > prev.low + tol or r.high > prev.high + tol)):
                    rev = "raised"
                elif delta < -tol or (abs(delta) <= tol and (r.low < prev.low - tol or r.high < prev.high - tol)):
                    rev = "lowered"
                else:
                    rev = "maintained"
            wording = r.change if isinstance(r.change, str) and r.change not in ("not_stated", "new") else None
            rows.append({"ticker": r.ticker, "period_label": r.period_label, "filing_date": r.filing_date,
                         "value": r.value, "value_low": r.value_low, "value_high": r.value_high, "revision": rev,
                         "change_b": delta, "change_pct": pct, "prior_value": prior, "company_wording": wording,
                         "status": r.status, "quote": r.quote, "url": r.url, "period": r.period})
            prev = r
    return pd.DataFrame(rows, columns=cols)


def latest_guidance(history: pd.DataFrame) -> pd.DataFrame:
    """The most recent statement for each company and period."""
    if history.empty:
        return history
    return history.sort_values("filing_date").groupby(["ticker", "period_label"]).tail(1)


def period_sort_key(label: str) -> tuple:
    m = re.match(r"(?:(Q|H)(\d) )?(\d{4})", label or "")
    if not m:
        return (9999, 9, 9)
    kind, n, year = m.groups()
    return (int(year), 0 if kind is None else (1 if kind == "H" else 2), int(n or 0))


# ---------------------------------------------------------------------------
# Quarterly capex
# ---------------------------------------------------------------------------


def _quarter_end(label: str) -> pd.Timestamp:
    year, q = int(label[:4]), int(label[-1])
    return pd.Timestamp(year=year, month=3 * q, day=1) + pd.offsets.MonthEnd(0)


def capex_quarterly(xq: pd.DataFrame, facts: pd.DataFrame) -> pd.DataFrame:
    """XBRL quarterly capex, plus press-release quarters for companies with no quarterly XBRL capex.

    Some filers (Nebius files an annual 20-F) have no quarterly XBRL. Their press releases state
    quarterly capex, so verified ``capex_reported`` facts for a single quarter fill the gap, latest
    filing winning. The ``source`` column says which is which.
    """
    cols = ["ticker", "quarter", "end", "value_usd_billion", "source", "url"]
    x = xq[xq["metric"] == "capex"].copy()
    x["end"] = pd.to_datetime(x["end"])
    x["source"] = np.where(x["method"] == "reported", "SEC XBRL", "SEC XBRL (from year-to-date)")
    x["url"] = None
    x = x[cols]
    have = set(x["ticker"])
    f = facts[(facts["metric"] == "capex_reported") & (facts["status"] == "verified") & ~facts["ticker"].isin(have)].copy()
    if not f.empty:
        f["quarter"] = f["period"].map(quarter_from_period)
        f = f[f["quarter"].notna()].copy()
        f["filing_date"] = pd.to_datetime(f["filing_date"])
        f = f.sort_values("filing_date").drop_duplicates(["ticker", "quarter"], keep="last")
        f["end"] = f["quarter"].map(_quarter_end)
        f["value_usd_billion"] = f["value"]
        f["source"] = "Press release"
        x = pd.concat([x, f[cols]], ignore_index=True)
    return x.sort_values(["ticker", "end"]).reset_index(drop=True)


def _qnum(label: str) -> int:
    return int(label[:4]) * 4 + int(label[-1]) - 1


def _window_sum(quarters: list[str], values: list[float], end_idx: int) -> float:
    """Sum of four consecutive quarters ending at ``end_idx``; NaN if any is missing."""
    if end_idx < 3:
        return np.nan
    q = quarters[end_idx - 3: end_idx + 1]
    if _qnum(q[-1]) - _qnum(q[0]) != 3:
        return np.nan
    return float(sum(values[end_idx - 3: end_idx + 1]))


def capex_summary(cq: pd.DataFrame) -> pd.DataFrame:
    """Per company: latest quarter, change on the prior quarter and year, trailing four quarters."""
    rows = []
    for t, s in cq.sort_values("end").groupby("ticker"):
        s = s.set_index("quarter")
        last = s.iloc[-1]
        q, y = last.name, int(last.name[:4])
        prev_q = s["value_usd_billion"].iloc[-2] if len(s) > 1 else np.nan
        yago = s["value_usd_billion"].get(f"{y - 1}{q[4:]}", np.nan)
        qs, vs = list(s.index), list(s["value_usd_billion"])
        ttm = _window_sum(qs, vs, len(qs) - 1)
        prev_idx = qs.index(f"{y - 1}{q[4:]}") if f"{y - 1}{q[4:]}" in qs else -1
        ttm_prev = _window_sum(qs, vs, prev_idx) if prev_idx >= 0 else np.nan
        rows.append({"ticker": t, "latest_quarter": q, "latest_end": last["end"], "latest_b": last["value_usd_billion"],
                     "qoq": last["value_usd_billion"] / prev_q - 1 if prev_q and prev_q == prev_q else np.nan,
                     "yoy": last["value_usd_billion"] / yago - 1 if yago and yago == yago else np.nan,
                     "ttm_b": ttm, "ttm_yoy": ttm / ttm_prev - 1 if ttm_prev and ttm_prev == ttm_prev else np.nan,
                     "source": last["source"]})
    return pd.DataFrame(rows)


def ttm_total(cq: pd.DataFrame, tickers, offset: int = 0) -> float:
    """Sum across companies of four consecutive quarters ending ``offset`` quarters before each one's latest.

    Companies without four consecutive quarters in that window are left out.
    """
    total = 0.0
    for t in tickers:
        s = cq[cq["ticker"] == t].sort_values("end")
        qs, vs = list(s["quarter"]), list(s["value_usd_billion"])
        v = _window_sum(qs, vs, len(qs) - 1 - offset)
        if v == v:
            total += v
    return total


# ---------------------------------------------------------------------------
# Point-in-time metrics (power, GPUs, backlog)
# ---------------------------------------------------------------------------


def latest_values(disc: pd.DataFrame, metrics) -> pd.DataFrame:
    """Latest company-level value per company and metric.

    Company-level means no site named. Within the latest filing date the largest value is taken,
    because filings often list a total alongside its parts.
    """
    d = disc[disc["metric"].isin(metrics)].copy()
    if d.empty:
        return d
    d["_company_level"] = d["site"].isna() | (d["site"].astype(str).str.strip() == "")
    d = d.sort_values(["_company_level", "first_stated", "value"], ascending=[False, False, False])
    d = d[d["_company_level"]] if d["_company_level"].any() else d
    return d.groupby(["ticker", "metric"]).head(1).drop(columns="_company_level")


def metric_series(disc: pd.DataFrame, metric: str) -> pd.DataFrame:
    """Company-level values over time for one metric: the largest value per company per filing date."""
    d = disc[(disc["metric"] == metric)].copy()
    if d.empty:
        return d
    d = d[d["site"].isna() | (d["site"].astype(str).str.strip() == "")]
    return d.sort_values("value", ascending=False).drop_duplicates(["ticker", "first_stated"]).sort_values("first_stated")


def lease_series(disc: pd.DataFrame) -> pd.DataFrame:
    """Leases not yet commenced, as point-in-time totals by company and as-of date.

    Keeps only figures whose period is a date ("as of June 30, 2026"); amounts for individual leases signed
    after the balance sheet date ("subsequent to ...", "July 2026") are left out. Where several totals are
    given for one date (operating and finance leases separately), the largest is kept.
    """
    d = disc[disc["metric"] == "lease_not_commenced"].copy()
    if d.empty:
        return d.assign(as_of=pd.Series(dtype="datetime64[ns]"))
    text = d["period"].fillna("").str.replace(r"(?i)^\s*as of\s+", "", regex=True)
    d["as_of"] = pd.to_datetime(text, format="mixed", errors="coerce")
    d = d[d["as_of"].notna()]
    return d.sort_values("value", ascending=False).drop_duplicates(["ticker", "as_of"]).sort_values("as_of")
