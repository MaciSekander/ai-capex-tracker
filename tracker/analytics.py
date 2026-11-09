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


# Companies whose fiscal year does not end in December (month it ends). Their periods are labelled
# "FY2026" / "Q1 FY2027" so they are never compared with calendar-year statements.
FISCAL_YEAR_END_MONTH = {"MSFT": 6, "ORCL": 5, "APLD": 5, "IREN": 6}
_CALENDAR = re.compile(r"\bcalendar\b|\bCY\s*['’]?\d", re.IGNORECASE)


def period_label(period, ticker: str | None = None) -> str | None:
    """A comparable label for a guidance period: '2026', 'H2 2026', 'Q1 2027'; None if open-ended.

    For companies with a non-December fiscal year (``FISCAL_YEAR_END_MONTH``) fiscal periods are labelled
    'FY2026', 'H1 FY2026', 'Q1 FY2027' unless the text says calendar; for everyone else fiscal and
    calendar years are the same thing.
    """
    if not isinstance(period, str) or not period.strip():
        return None
    p = period.strip()
    fy = "FY" if ticker in FISCAL_YEAR_END_MONTH and not _CALENDAR.search(p) else ""
    m = _Q.search(p)
    if m:
        return f"Q{m.group(1)} {fy}{_yr(m.group(2))}"
    m = _Q_REV.search(p)
    if m:
        return f"Q{m.group(2)} {fy}{_yr(m.group(1))}"
    m = _H.search(p)
    if m:
        half = m.group(1) or ("1" if m.group(2).lower() == "first" else "2")
        return f"H{half} {fy}{_yr(m.group(3))}"
    if _VAGUE.search(p):
        return None
    years = {_yr(a or b) for a, b in _YEAR.findall(p)}
    if len(years) == 1:
        return f"{fy}{years.pop()}"
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
    f["_p"] = [period_label(p, t) or _norm_text(p) for p, t in zip(f["period"], f["ticker"])]
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
RESTATING_FORMS = PERIODIC_FORMS + ("Transcript",)  # restate deals announced elsewhere


def drop_periodic_deals(facts: pd.DataFrame) -> pd.DataFrame:
    """Contracts and financings come from 8-K / 6-K announcements only.

    10-Q and 10-K notes restate older deals every quarter (and give year-to-date totals), which would
    otherwise show up as new deals dated to the report.
    """
    if facts.empty or "form" not in facts:
        return facts
    return facts[~(facts["form"].isin(RESTATING_FORMS) & facts["metric"].isin(DEAL_METRICS))]


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
    g["period_label"] = [period_label(p, t) for p, t in zip(g["period"], g["ticker"])]
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
    m = re.match(r"(?:(Q|H)(\d) )?(FY)?(\d{4})", label or "")
    if not m:
        return (9999, 9, 9)
    kind, n, fiscal, year = m.groups()
    # A fiscal year ending mid-year sorts just before the calendar year of the same number.
    return (int(year) - (0.5 if fiscal else 0), 0 if kind is None else (1 if kind == "H" else 2), int(n or 0))


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
    # A backlog total is stated as of a specific date ("June 30, 2026"); "July 2026" is a tranche signed that month.
    full_date = text.str.contains(r"\b\d{1,2},?\s+\d{4}\b|\d{4}-\d{2}-\d{2}", regex=True)
    d["as_of"] = pd.to_datetime(text.where(full_date), format="mixed", errors="coerce")
    d = d[d["as_of"].notna()]
    # "operating and finance leases ... of $8.4 billion and $87.8 billion, respectively": the parts of one total.
    parts = d["quote"].fillna("").str.contains("respectively", case=False)
    if parts.any():
        keys = ["ticker", "as_of", "quote"]
        summed = d[parts].sort_values("value", ascending=False).groupby(keys, as_index=False).agg(
            {**{c: "first" for c in d.columns if c not in keys + ["value"]}, "value": "sum"})
        d = pd.concat([d[~parts], summed], ignore_index=True)
    return d.sort_values("value", ascending=False).drop_duplicates(["ticker", "as_of"]).sort_values("as_of")


# ---------------------------------------------------------------------------
# Lease pipeline: signed, started and projected
# ---------------------------------------------------------------------------
#
# pool(t)    = leases signed but not yet commenced, total future payments (10-Q / 10-K text)
# starts(t)  = leases that commenced in quarter t, present value (XBRL right-of-use assets obtained),
#              converted to total payments with the company's ratio of lease payments due to lease liability
# signed(t)  = pool(t) - pool(t-1) + starts(t)        (implied: companies do not report it)


def _pivot(xq: pd.DataFrame, metrics) -> pd.DataFrame:
    x = xq[xq["metric"].isin(metrics)].copy()
    if x.empty:
        return pd.DataFrame(columns=["ticker", "end", *metrics])
    x["end"] = pd.to_datetime(x["end"]).astype("datetime64[ns]")
    out = x.pivot_table(index=["ticker", "end"], columns="metric", values="value_usd_billion", aggfunc="last")
    return out.reindex(columns=list(metrics)).reset_index()


def estimated_lease_starts(xq: pd.DataFrame) -> pd.DataFrame:
    """Quarterly lease starts (present value) estimated from lease-asset balances, for quarters a company
    does not report (Meta reports lease starts only for the full year from 2025).

    operating starts ~ change in operating right-of-use asset + amortization, where the amortization rate
    (share of the average asset per year) is calibrated on the latest full year with reported starts;
    finance starts ~ change in gross finance right-of-use asset x (reported full-year starts / that year's change).
    By construction the calibration year's quarters add up to its reported total.
    """
    cols = ["ticker", "end", "starts_pv_est"]
    bal = _pivot(xq, ["lease_rou_operating", "lease_rou_finance_gross"])
    fy = _pivot(xq, ["lease_starts_operating_fy", "lease_starts_finance_fy"])
    if bal.empty or fy.empty:
        return pd.DataFrame(columns=cols)
    out = []
    for t, b in bal.groupby("ticker"):
        b = b.sort_values("end").set_index("end")
        f = fy[fy["ticker"] == t].dropna(subset=["lease_starts_operating_fy"]).sort_values("end")
        calib = None
        for _, r in f.iloc[::-1].iterrows():
            e0, e1 = r["end"] - pd.offsets.YearEnd(1), r["end"]
            if e0 in b.index and e1 in b.index and b.loc[[e0, e1], "lease_rou_operating"].notna().all():
                d_op = b.at[e1, "lease_rou_operating"] - b.at[e0, "lease_rou_operating"]
                avg = (b.at[e1, "lease_rou_operating"] + b.at[e0, "lease_rou_operating"]) / 2
                amort = max(r["lease_starts_operating_fy"] - d_op, 0.0) / avg if avg > 0 else 0.0
                d_fin = b.at[e1, "lease_rou_finance_gross"] - b.at[e0, "lease_rou_finance_gross"]
                fin_fy = r.get("lease_starts_finance_fy")
                k_fin = (fin_fy / d_fin) if fin_fy == fin_fy and d_fin == d_fin and d_fin > 0 else 0.0
                calib = (amort, k_fin)
                break
        if calib is None:
            continue
        amort, k_fin = calib
        op, fin = b["lease_rou_operating"], b["lease_rou_finance_gross"]
        gap = pd.Series(b.index, index=b.index).diff().dt.days
        est = (op.diff() + amort / 4 * (op + op.shift()) / 2) + k_fin * fin.diff().fillna(0)
        est = est[gap.between(80, 100)].dropna()
        out.append(pd.DataFrame({"ticker": t, "end": est.index, "starts_pv_est": est.clip(lower=0).values}))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=cols)


def lease_flows(xq: pd.DataFrame, disc: pd.DataFrame) -> pd.DataFrame:
    """One row per company and quarter end with pool, starts and implied signings (USD billions)."""
    cols = ["ticker", "end", "pool", "pool_prev", "starts_pv", "starts_basis", "factor", "starts", "signed"]
    pool = lease_series(disc)
    if pool.empty:
        return pd.DataFrame(columns=cols)
    pool = pool[["ticker", "as_of", "value"]].rename(columns={"as_of": "end", "value": "pool"})
    pool["end"] = pd.to_datetime(pool["end"]).astype("datetime64[ns]")

    st = _pivot(xq, ["lease_starts_operating", "lease_starts_finance"])
    st["starts_pv"] = st["lease_starts_operating"] + st["lease_starts_finance"].fillna(0)
    st["starts_basis"] = "reported"
    est = estimated_lease_starts(xq)
    if not est.empty:  # fill quarters after the company's last reported quarter
        last_rep = st.dropna(subset=["starts_pv"]).groupby("ticker")["end"].max()
        est = est[est["end"] > est["ticker"].map(last_rep).fillna(pd.Timestamp.min)]
        est = est.rename(columns={"starts_pv_est": "starts_pv"}).assign(starts_basis="estimated")
        st = pd.concat([st[["ticker", "end", "starts_pv", "starts_basis"]], est], ignore_index=True)
    bal = _pivot(xq, ["lease_payments_operating", "lease_payments_finance", "lease_liability_operating",
                      "lease_liability_finance"])
    bal["factor"] = ((bal["lease_payments_operating"] + bal["lease_payments_finance"].fillna(0))
                     / (bal["lease_liability_operating"] + bal["lease_liability_finance"].fillna(0)))
    bal = bal[bal["factor"].between(1.0, 3.0)]

    rows = []
    for t, g in pool.sort_values("end").groupby("ticker"):
        g = g.copy()
        g["pool_prev"] = g["pool"].shift()
        gap = (g["end"] - g["end"].shift()).dt.days
        g.loc[~gap.between(80, 100), "pool_prev"] = np.nan
        s = st[st["ticker"] == t][["end", "starts_pv", "starts_basis"]].dropna(subset=["starts_pv"])
        g = pd.merge_asof(g.sort_values("end"), s.sort_values("end"), on="end", direction="nearest",
                          tolerance=pd.Timedelta(days=7))
        b = bal[bal["ticker"] == t][["end", "factor"]].sort_values("end")
        g = pd.merge_asof(g, b, on="end", direction="backward") if not b.empty else g.assign(factor=np.nan)
        if not b.empty:
            g["factor"] = g["factor"].fillna(b["factor"].iloc[0])
        rows.append(g)
    out = pd.concat(rows, ignore_index=True)
    out["starts"] = out["starts_pv"] * out["factor"]
    out["signed"] = out["pool"] - out["pool_prev"] + out["starts"]
    return out[cols]


LEASE_LAG_QUARTERS = 4  # leases signed now mostly start once construction finishes, about a year later


def _pool_at(g: pd.DataFrame, when: pd.Timestamp, tol_days: int = 20) -> float:
    d = (g["end"] - when).abs()
    return float(g.loc[d.idxmin(), "pool"]) if len(g) and d.min() <= pd.Timedelta(days=tol_days) else np.nan


def _interp_pool(pools: dict, when: pd.Timestamp) -> float:
    """Backlog at ``when``: the disclosed figure (within 20 days), else linear between the nearest dates around it."""
    for e, p_ in pools.items():
        if abs((e - when).days) <= 20:
            return p_
    before = [(e, p_) for e, p_ in pools.items() if e < when]
    after = [(e, p_) for e, p_ in pools.items() if e > when]
    if before and after:
        (e0, p0), (e1, p1) = max(before), min(after)
        return p0 + (p1 - p0) * (when - e0).days / (e1 - e0).days
    return (max(before) if before else min(after))[1]



_WINDOW = re.compile(r"commence\b(.*)", re.IGNORECASE | re.DOTALL)
_WINDOW_YEAR = re.compile(r"(fiscal\s+)?((?:19|20)\d{2})", re.IGNORECASE)


def stated_window(quote: str, ticker: str):
    """(start, end) dates of the period in which the company says its backlog will commence, if stated."""
    m = _WINDOW.search(quote or "")
    if not m:
        return None
    years = _WINDOW_YEAR.findall(m.group(1))
    if not years:
        return None
    fy_end = FISCAL_YEAR_END_MONTH.get(ticker, 12)

    def bound(fiscal, year, start):
        y = int(year)
        if fiscal and fy_end != 12:
            return (pd.Timestamp(year=y - 1, month=fy_end, day=1) + pd.offsets.MonthBegin(1) if start
                    else pd.Timestamp(year=y, month=fy_end, day=1) + pd.offsets.MonthEnd(0))
        return pd.Timestamp(year=y, month=1, day=1) if start else pd.Timestamp(year=y, month=12, day=31)

    (f0, y0), (f1, y1) = years[0], years[-1]
    return bound(f0, y0, True), bound(f1, y1, False)


def stated_windows(disc: pd.DataFrame) -> pd.DataFrame:
    """Latest commencement window stated with each company's backlog total (within the last three quarters)."""
    ls = lease_series(disc)
    rows = []
    for t, g in ls.sort_values("as_of").groupby("ticker"):
        g = g[g["as_of"] >= g["as_of"].max() - pd.Timedelta(days=280)]
        for _, r in g.iloc[::-1].iterrows():
            w = stated_window(r["quote"], t)
            if w:
                rows.append({"ticker": t, "window_start": w[0], "window_end": w[1], "window_quote": r["quote"],
                             "window_as_of": r["as_of"]})
                break
    return pd.DataFrame(rows, columns=["ticker", "window_start", "window_end", "window_quote", "window_as_of"])


def annual_lease_starts(xq: pd.DataFrame, flows: pd.DataFrame) -> pd.DataFrame:
    """Full-year lease starts in total payments: (ticker, end, starts)."""
    fy = _pivot(xq, ["lease_starts_operating_fy", "lease_starts_finance_fy"])
    if fy.empty:
        return pd.DataFrame(columns=["ticker", "end", "starts"])
    fy["starts_pv"] = fy["lease_starts_operating_fy"] + fy["lease_starts_finance_fy"].fillna(0)
    out = []
    for t, g in fy.dropna(subset=["starts_pv"]).groupby("ticker"):
        f = flows[flows["ticker"] == t].dropna(subset=["factor"]).sort_values("end")
        if f.empty:
            continue
        g = pd.merge_asof(g.sort_values("end"), f[["end", "factor"]], on="end", direction="nearest")
        out.append(pd.DataFrame({"ticker": t, "end": g["end"], "starts": g["starts_pv"] * g["factor"]}))
    return pd.concat(out, ignore_index=True) if out else pd.DataFrame(columns=["ticker", "end", "starts"])


def lease_projection(flows: pd.DataFrame, quarters: int = 8, lag: int = LEASE_LAG_QUARTERS, lookback: int = 4,
                     annual: pd.DataFrame | None = None, windows: pd.DataFrame | None = None,
                     tranches: pd.DataFrame | None = None):
    """Project lease starts from the backlog of signed leases, all in total future payments ($B).

    Leases start when the building is finished, so starts in a quarter are modeled as a share ``rate``
    of the backlog ``lag`` quarters earlier:  starts(t) = rate x pool(t - lag).  ``rate`` is fitted on
    the company's last ``lookback`` quarters with lease starts. The next ``lag`` quarters therefore
    follow from backlog already disclosed; later quarters need the backlog itself projected:
    pool(t) = pool(t-1) + signed - starts(t), with ``signed`` either the trailing average of implied
    signings ("Signings continue") or zero ("No new signings").

    Returns (projection rows, per-company assumptions).
    """
    proj, params = [], []
    for t, g in flows.sort_values("end").groupby("ticker"):
        g = g.reset_index(drop=True)
        hist = g.dropna(subset=["starts"])
        hist = hist[hist["end"] >= g["end"].max() - pd.Timedelta(days=550)].tail(lookback)
        lagged = [_pool_at(g, e - pd.offsets.MonthEnd(3 * lag)) for e in hist["end"]]
        pairs = [(s_, p_) for s_, p_ in zip(hist["starts"], lagged) if p_ == p_]
        basis = "quarterly"
        if len(pairs) < 2 and annual is not None and t in set(annual["ticker"]):
            # A full year of starts against the backlog at the four quarter ends a year earlier.
            pairs = []
            for _, a in annual[annual["ticker"] == t].sort_values("end").tail(2).iterrows():
                q_ends = [a["end"] - pd.offsets.MonthEnd(3 * (lag + i)) for i in range(4)]
                lag_pools = [_pool_at(g, e) for e in q_ends]
                if all(p_ == p_ for p_ in lag_pools):
                    pairs.append((a["starts"], sum(lag_pools)))
            basis = "annual"
            if not pairs:
                continue
        elif len(pairs) < 2:
            continue
        if sum(p_ for _, p_ in pairs) <= 0:
            continue
        rate = sum(s_ for s_, _ in pairs) / sum(p_ for _, p_ in pairs)
        signed_recent = g.dropna(subset=["signed"]).tail(lookback)["signed"]
        signed = max(float(signed_recent.mean()), 0.0) if len(signed_recent) else 0.0
        factor = float(g["factor"].dropna().iloc[-1]) if g["factor"].notna().any() else np.nan
        last = g.iloc[-1]
        q_starts = g.dropna(subset=["starts"])
        recent_q = q_starts[q_starts["end"] > last["end"] - pd.Timedelta(days=370)]
        if len(recent_q) >= 4:
            starts_4q = float(recent_q.tail(4)["starts"].sum())
        elif annual is not None and t in set(annual["ticker"]):
            starts_4q = float(annual[annual["ticker"] == t].sort_values("end")["starts"].iloc[-1])
        else:
            starts_4q = float(recent_q["starts"].sum()) if len(recent_q) else np.nan
        w = windows[windows["ticker"] == t] if windows is not None and not windows.empty else pd.DataFrame()
        params.append({"ticker": t, "rate": rate, "signed_avg": signed, "factor": factor, "pool": float(last["pool"]),
                       "as_of": last["end"], "quarters_used": len(pairs), "basis": basis, "starts_4q": starts_4q,
                       "years_of_backlog": float(last["pool"]) / starts_4q if starts_4q and starts_4q > 0 else np.nan,
                       "window_start": w["window_start"].iloc[0] if len(w) else pd.NaT,
                       "window_end": w["window_end"].iloc[0] if len(w) else pd.NaT})
        for case, add in (("Signings continue", signed), ("No new signings", 0.0)):
            pools = {e: p_ for e, p_ in zip(g["end"], g["pool"])}
            pool, end = float(last["pool"]), last["end"]
            for k in range(1, quarters + 1):
                end = end + pd.offsets.MonthEnd(3)
                src = end - pd.offsets.MonthEnd(3 * lag)
                base = _interp_pool(pools, src)
                starts = rate * base
                pool = max(pool - starts + add, 0.0)
                pools[end] = pool
                proj.append({"ticker": t, "end": end, "case": case, "starts": starts, "pool": pool,
                             "from_disclosed_backlog": k <= lag})
        if len(w):
            # Company's stated timing: the backlog already signed, spread evenly over the stated window;
            # tranches with their own stated window (Meta's $68B "in 2027 and 2028") are spread over theirs.
            parts = []
            tr = tranches[tranches["ticker"] == t] if tranches is not None and not tranches.empty else pd.DataFrame()
            rest = float(last["pool"]) - (float(tr["value"].sum()) if len(tr) else 0.0)
            if len(tr) and rest < 0:
                tr, rest = tr.iloc[0:0], float(last["pool"])
            parts.append((rest, w["window_start"].iloc[0], w["window_end"].iloc[0]))
            parts += [(float(r["value"]), r["start"], r["end"]) for _, r in tr.iterrows()]
            qends = [last["end"] + pd.offsets.MonthEnd(3 * k) for k in range(1, quarters + 1)]
            amounts = np.zeros(quarters)
            for value, start, stop in parts:
                start = max(start, last["end"] + pd.Timedelta(days=1))
                live = [i for i, e in enumerate(qends) if start - pd.Timedelta(days=1) < e and e - pd.offsets.MonthEnd(3) < stop]
                n = max(int(round((stop - start).days / 91.3)), 1)
                for i in live:
                    amounts[i] += value / n
            for k, (end, amt) in enumerate(zip(qends, amounts), 1):
                proj.append({"ticker": t, "end": end, "case": "Company's stated timing", "starts": float(amt),
                             "pool": np.nan, "from_disclosed_backlog": k <= lag})
    return pd.DataFrame(proj), pd.DataFrame(params)


def lease_backtest(flows: pd.DataFrame, annual: pd.DataFrame | None = None, holdout: int = 4) -> pd.DataFrame:
    """Track record of the lease projection: refit on data up to ``holdout`` quarters before each company's
    latest quarter, project the next ``holdout`` quarters, and compare with the starts that followed."""
    rows = []
    for t, g in flows.sort_values("end").groupby("ticker"):
        g = g.reset_index(drop=True)
        actual = g.dropna(subset=["starts"]).tail(holdout)
        if len(actual) < holdout or len(g) <= holdout:
            continue
        cutoff = actual["end"].iloc[0] - pd.Timedelta(days=20)
        past = g[g["end"] <= cutoff]
        an = annual[annual["end"] <= cutoff] if annual is not None and not annual.empty else None
        proj, params = lease_projection(past, quarters=holdout, annual=an)
        if params.empty:
            continue
        pred = proj[proj["case"] == "Signings continue"].head(holdout)["starts"].sum()
        act = float(actual["starts"].sum())
        rows.append({"ticker": t, "cutoff": past["end"].max(), "predicted": float(pred), "actual": act,
                     "error": float(pred) / act - 1 if act > 0 else np.nan,
                     "estimated_quarters": int((actual["starts_basis"] == "estimated").sum())})
    return pd.DataFrame(rows, columns=["ticker", "cutoff", "predicted", "actual", "error", "estimated_quarters"])


# ---------------------------------------------------------------------------
# Expected dates: capacity coming online and lease tranches
# ---------------------------------------------------------------------------

_QWORD = {"first": 1, "1st": 1, "second": 2, "2nd": 2, "third": 3, "3rd": 3, "fourth": 4, "4th": 4}


def _qend(y: int, q: int) -> pd.Timestamp:
    return pd.Timestamp(year=y, month=3 * q, day=1) + pd.offsets.MonthEnd(0)


def parse_expected(text) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    """(start, end) of a stated timing such as "Q2 2027", "by end of 2026", "H1 2027", "2027 and 2028".

    Calendar periods only; fiscal-year wording is read as the calendar year it names.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    t = text.lower().replace("’", "'")
    halves = re.split(r"\s*(?:-|–|—|\bto\b|\bthrough\b)\s*", t, maxsplit=1)
    if len(halves) == 2 and halves[0] and halves[1] and not re.fullmatch(r"\d{4}", halves[0]):
        a, b = _parse_one(halves[0]), _parse_one(halves[1])
        if a and b:
            return a[0], b[1]
    return _parse_one(t)


def _parse_one(t: str) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    m = re.search(r"\bq([1-4])\s*(?:of\s*)?(?:fy|cy)?\s*'?(\d{2,4})\b", t) or re.search(r"\b(\d{4})\s*q([1-4])\b", t)
    if m:
        a, b = m.groups()
        q, y = (int(a), int(b)) if len(a) == 1 else (int(b), int(a))
        y = y + 2000 if y < 100 else y
        return _qend(y, q) - pd.offsets.QuarterBegin(1, startingMonth=1) + pd.Timedelta(days=0), _qend(y, q)
    m = re.search(r"\b(first|second|third|fourth|1st|2nd|3rd|4th) quarter (?:of )?(?:fiscal |calendar )?(?:year )?(\d{4})", t)
    if m:
        y, q = int(m.group(2)), _QWORD[m.group(1)]
        return _qend(y, q) - pd.offsets.MonthBegin(3), _qend(y, q)
    m = re.search(r"\b(h1|h2|1h|2h|first half|second half)\s*(?:of\s*)?(?:fiscal |calendar )?'?(\d{2,4})", t)
    if m:
        y = int(m.group(2)); y = y + 2000 if y < 100 else y
        first = m.group(1) in ("h1", "1h", "first half")
        return (pd.Timestamp(y, 1, 1), pd.Timestamp(y, 6, 30)) if first else (pd.Timestamp(y, 7, 1), pd.Timestamp(y, 12, 31))
    years = [int(y) for y in re.findall(r"\b(20\d{2})\b", t)]
    if not years:
        return None
    y0, y1 = years[0], years[-1]
    if re.search(r"\b(early|beginning of|start of)\b", t) and y0 == y1:
        return pd.Timestamp(y0, 1, 1), pd.Timestamp(y0, 3, 31)
    if re.search(r"\bmid[- ]", t) and y0 == y1:
        return pd.Timestamp(y0, 4, 1), pd.Timestamp(y0, 9, 30)
    if re.search(r"\b(late|end of|year[- ]end)\b", t) and y0 == y1:
        return pd.Timestamp(y0, 10, 1), pd.Timestamp(y0, 12, 31)
    return pd.Timestamp(min(y0, y1), 1, 1), pd.Timestamp(max(y0, y1), 12, 31)


CAPACITY_METRICS = ("power_contracted_mw", "power_pipeline_mw", "power_active_mw")


def _capacity_statements(facts: pd.DataFrame) -> pd.DataFrame:
    d = facts[facts["metric"].isin(CAPACITY_METRICS) & facts["expected_date"].notna()].copy() \
        if not facts.empty and "expected_date" in facts else pd.DataFrame()
    if d.empty:
        return d
    win = d["expected_date"].map(parse_expected)
    d = d[win.notna()].copy()
    d["expected_start"] = [w[0] for w in win[win.notna()]]
    d["expected_end"] = [w[1] for w in win[win.notna()]]
    d["filing_date"] = pd.to_datetime(d["filing_date"])
    d["site_key"] = d["site"].fillna("").str.lower().str.replace(r"[^a-z0-9]+", " ", regex=True).str.strip()
    return d


def capacity_schedule(facts: pd.DataFrame, as_of: pd.Timestamp) -> pd.DataFrame:
    """Capacity companies expect to bring online after ``as_of``, from each company's latest statement per site.

    Works on raw facts (every statement), so a later statement that moves a date replaces the earlier one.
    Company-level figures (no site) are used only for companies that name no dated sites.
    """
    cols = ["ticker", "site", "mw", "expected_start", "expected_end", "expected_text", "metric", "stated", "quote",
            "url", "first_expected_end", "slip_days"]
    d = _capacity_statements(facts)
    if d.empty:
        return pd.DataFrame(columns=cols)
    d = d.sort_values("filing_date")
    first = d.groupby(["ticker", "site_key", "value"])["expected_end"].first().rename("first_expected_end")
    latest_doc = d.groupby(["ticker", "site_key"])["filing_date"].transform("max")
    d = d[d["filing_date"] == latest_doc].drop_duplicates(["ticker", "site_key", "value", "expected_end"])
    named = d[d["site_key"] != ""]
    d = pd.concat([named, d[(d["site_key"] == "") & ~d["ticker"].isin(named["ticker"])]])
    d = d.join(first, on=["ticker", "site_key", "value"])
    d["slip_days"] = (d["expected_end"] - d["first_expected_end"]).dt.days
    d = d[d["expected_end"] > as_of]
    out = d.rename(columns={"value": "mw", "expected_date": "expected_text", "filing_date": "stated"})
    return out[cols].sort_values(["expected_end", "ticker"]).reset_index(drop=True)


def stated_tranches(disc: pd.DataFrame, pool_as_of: dict) -> pd.DataFrame:
    """Lease tranches with their own stated commencement (e.g. Meta's $68B "in 2027 and 2028"),
    signed by the date of each company's latest backlog total and therefore inside it."""
    cols = ["ticker", "value", "start", "end"]
    d = disc[(disc["metric"] == "lease_not_commenced")].copy() if not disc.empty else disc
    if d.empty:
        return pd.DataFrame(columns=cols)
    dated = lease_series(d)
    totals = set(zip(dated["ticker"], dated["value"]))
    rows = []
    for _, r in d.iterrows():
        if (r["ticker"], r["value"]) in totals or r["ticker"] not in pool_as_of:
            continue
        text = r.get("expected_date") if isinstance(r.get("expected_date"), str) else None
        m = re.search(r"commence[^.]*", r["quote"] or "", re.IGNORECASE)
        w = parse_expected(text) or (parse_expected(m.group(0)) if m else None)
        stated = pd.Timestamp(r["first_stated"])
        if w and pool_as_of[r["ticker"]] - pd.Timedelta(days=200) <= stated <= pool_as_of[r["ticker"]] + pd.Timedelta(days=45):
            rows.append({"ticker": r["ticker"], "value": float(r["value"]), "start": w[0], "end": w[1]})
    return pd.DataFrame(rows, columns=cols).drop_duplicates()


# ---------------------------------------------------------------------------
# Cloud revenue: what the spending is earning
# ---------------------------------------------------------------------------

# One consistent business line per company, in order of preference: (label, pattern on segment or quote).
# (label, include pattern, exclude pattern) per company, tried in order; the one covering the most
# quarters wins. Patterns run on the fact's business line, or on its quote when no line was recorded.
_ORCL_TOTAL = r"plus saas|saas|total cloud|^total$"
CLOUD_SEGMENTS = {
    "MSFT": [("Microsoft Cloud", r"microsoft cloud", None), ("Intelligent Cloud", r"intelligent cloud", None)],
    "GOOGL": [("Google Cloud", r"google cloud|\bcloud\b", None)],
    "AMZN": [("AWS", r"\baws\b|amazon web services", None)],
    "ORCL": [("OCI (infrastructure)", r"infrastructure|\boci\b|iaas", _ORCL_TOTAL)],
    "CRWV": [("Revenue", r".", None)],
    "NBIS": [("Revenue", r".", None)],
    "IREN": [("AI cloud", r"\bai cloud\b", None)],
}
GROWTH_SEGMENTS = {
    "MSFT": [("Azure", r"azure", None)],
    "GOOGL": [("Google Cloud", r"google cloud|\bcloud\b", None)],
    "AMZN": [("AWS", r"\baws\b|amazon web services", None)],
    "ORCL": [("OCI (infrastructure)", r"infrastructure|\boci\b|iaas", _ORCL_TOTAL)],
}


_MONTH_NUM = {m: i + 1 for i, m in enumerate(["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct",
                                                "nov", "dec"])}
_ENDED = re.compile(r"\b(?:months|quarter|q)\s+ended\s+(Jan\w*|Feb\w*|Mar\w*|Apr\w*|May|Jun\w*|Jul\w*|Aug\w*|"
                    r"Sep\w*|Oct\w*|Nov\w*|Dec\w*)\s+\d{1,2},?\s+(\d{4})", re.IGNORECASE)
_Q_FISCAL = re.compile(r"\bQ([1-4])\s+fiscal\s+(?:year\s+)?(\d{4}|\d{2})\b", re.IGNORECASE)


def quarter_end(period, ticker: str | None = None) -> pd.Timestamp | None:
    """Last day of the quarter a period names, on the company's own fiscal calendar.

    "Q1 FY2026" for Microsoft (fiscal year ends June) is the quarter ending September 30, 2025.
    """
    if not isinstance(period, str):
        return None
    m = _ENDED.search(period)  # an explicit "quarter ended June 30, 2026" wins over any label
    if m:
        return pd.Timestamp(year=int(m.group(2)), month=_MONTH_NUM[m.group(1)[:3].lower()], day=1) \
            + pd.offsets.MonthEnd(0)
    m = _Q.search(period) or _Q_FISCAL.search(period)
    n, y = (int(m.group(1)), _yr(m.group(2))) if m else (None, None)
    if not m:
        m = _Q_REV.search(period)
        if m:
            n, y = int(m.group(2)), _yr(m.group(1))
    if not m:
        m = re.search(r"\b(first|second|third|fourth) quarter(?: of)?\s*(?:fiscal\s*(?:year\s*)?)?(\d{4})", period,
                      re.IGNORECASE)
        if not m:
            return None
        n, y = ["first", "second", "third", "fourth"].index(m.group(1).lower()) + 1, int(m.group(2))
    fy_end = FISCAL_YEAR_END_MONTH.get(ticker)
    if fy_end and not _CALENDAR.search(period):
        month = fy_end - 3 * (4 - n)
        if month <= 0:
            month, y = month + 12, y - 1
    else:
        month = 3 * n
    return pd.Timestamp(year=y, month=month, day=1) + pd.offsets.MonthEnd(0)


def _cloud_series(facts: pd.DataFrame, metric: str, segments: dict) -> pd.DataFrame:
    cols = ["ticker", "segment", "end", "value", "filing_date", "quote", "url"]
    d = facts[facts["metric"] == metric].copy() if not facts.empty else pd.DataFrame()
    if d.empty:
        return pd.DataFrame(columns=cols)
    d["filing_date"] = pd.to_datetime(d["filing_date"])
    d["end"] = [quarter_end(p, t) for p, t in zip(d["period"], d["ticker"])]
    d = d[d["end"].notna()]
    lag = (d["filing_date"] - pd.to_datetime(d["end"])).dt.days
    d = d[lag.between(0, 120)]  # stated in the release for that quarter, not a prior-year comparison
    seg = d["segment"].fillna("").astype(str).str.strip() if "segment" in d else pd.Series("", index=d.index)
    seg_text = seg.where(seg != "", d["quote"].fillna("")).str.lower()
    d["_verified"] = (d["status"] == "verified") if "status" in d else True
    out = []
    for t, g in d.groupby("ticker"):
        best = None
        text = seg_text.loc[g.index]
        for label, pat, exclude in segments.get(t, []):
            keep = text.str.contains(pat, regex=True)
            if exclude:
                keep &= ~text.str.contains(exclude, regex=True)
            hit = g[keep]
            if not hit.empty and (best is None or hit["end"].nunique() > best[1]["end"].nunique()):
                best = (label, hit)
        if best is None:
            continue
        label, hit = best
        # latest filing for each quarter (restatements win); within a filing, verified over partial
        hit = hit.sort_values(["filing_date", "_verified"]).groupby("end").tail(1)
        out.append(pd.DataFrame({"ticker": t, "segment": label, "end": hit["end"], "value": hit["value"],
                                 "filing_date": hit["filing_date"], "quote": hit["quote"], "url": hit["url"]}))
    return pd.concat(out, ignore_index=True).sort_values(["ticker", "end"]) if out else pd.DataFrame(columns=cols)


def cloud_revenue(facts: pd.DataFrame) -> pd.DataFrame:
    """Quarterly cloud revenue per company ($B), one consistent segment each (``CLOUD_SEGMENTS``)."""
    return _cloud_series(facts, "cloud_revenue", CLOUD_SEGMENTS)


def cloud_growth(facts: pd.DataFrame) -> pd.DataFrame:
    """Stated year-over-year cloud growth per company and quarter (%), e.g. Azure."""
    return _cloud_series(facts, "cloud_growth_pct", GROWTH_SEGMENTS)


def _last_n_sum(df: pd.DataFrame, col: str, n: int, offset: int = 0) -> float:
    """Sum of ``n`` consecutive quarters ending ``offset`` quarters before the latest; NaN if any is missing."""
    d = df.sort_values("end")
    ends = list(pd.to_datetime(d["end"]))
    vals = list(d[col])
    if len(ends) < n + offset:
        return np.nan
    window_ends = ends[len(ends) - offset - n:len(ends) - offset]
    gaps = [(b - a).days for a, b in zip(window_ends, window_ends[1:])]
    if any(not 80 <= g <= 100 for g in gaps):
        return np.nan
    return float(sum(vals[len(ends) - offset - n:len(ends) - offset]))


def cloud_summary(rev: pd.DataFrame, growth: pd.DataFrame, capex_q: pd.DataFrame) -> pd.DataFrame:
    """Per company: latest quarter, growth, last-4-quarter cloud revenue, and how it compares with capex.

    capex_to_revenue      = capex, last 4Q / cloud revenue, last 4Q
    new_revenue_per_capex = (cloud revenue, last 4Q - the 4Q before) / capex in the 4Q before
    """
    rows = []
    for t, g in rev.groupby("ticker"):
        g = g.sort_values("end")
        last = g.iloc[-1]
        year_ago = g[(g["end"] - (last["end"] - pd.DateOffset(years=1))).abs() <= pd.Timedelta(days=20)]
        yoy = last["value"] / year_ago["value"].iloc[0] - 1 if len(year_ago) and year_ago["value"].iloc[0] else np.nan
        sg = growth[(growth["ticker"] == t) & ((growth["end"] - last["end"]).abs() <= pd.Timedelta(days=20))] \
            if not growth.empty else growth
        c = capex_q[capex_q["ticker"] == t].rename(columns={"value_usd_billion": "capex"})
        rev4, rev_prev4 = _last_n_sum(g, "value", 4), _last_n_sum(g, "value", 4, offset=4)
        capex4 = _last_n_sum(c, "capex", 4) if not c.empty else np.nan
        capex_prev4 = _last_n_sum(c, "capex", 4, offset=4) if not c.empty else np.nan
        rows.append({
            "ticker": t, "segment": last["segment"], "latest_end": last["end"], "latest_b": float(last["value"]),
            "yoy": yoy, "stated_growth_pct": float(sg["value"].iloc[-1]) if len(sg) else np.nan,
            "stated_growth_segment": sg["segment"].iloc[-1] if len(sg) else None,
            "rev_4q": rev4, "rev_prev_4q": rev_prev4, "capex_4q": capex4,
            "capex_to_revenue": capex4 / rev4 if rev4 == rev4 and rev4 and capex4 == capex4 else np.nan,
            "new_revenue_per_capex": ((rev4 - rev_prev4) / capex_prev4
                                      if rev4 == rev4 and rev_prev4 == rev_prev4 and capex_prev4 == capex_prev4
                                      and capex_prev4 > 0 else np.nan),
            "url": last["url"],
        })
    return pd.DataFrame(rows)
