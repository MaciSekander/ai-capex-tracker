"""Automatic consistency checks across the dashboard's outputs.

The extraction checks prove each number is stated in its source. These checks look across numbers for
results that contradict each other or would mislead: a guidance "revision" that is really a change of
basis, guidance below what has already been spent, a lease backlog that halves in a quarter, more active
than contracted power, cloud revenue that disagrees with the stated growth rate.

Each check returns rows of the same shape; ``run_checks`` combines them. Nothing is removed automatically:
flagged guidance revisions are left out of headline counts and marked in the revision log, and every
finding is listed on the Methodology tab for a person to confirm or correct (``app_data/corrections.csv``).
"""

from __future__ import annotations

import re

import numpy as np
import pandas as pd

from . import analytics as A

COLUMNS = ["severity", "area", "ticker", "finding", "detail", "date", "url", "fact_id"]
SEVERITY_ORDER = {"High": 0, "Medium": 1, "Low": 2}

# Wording that usually means the number is not a full-period capex level on the usual basis.
BASIS_WORDS = re.compile(
    r"\bhigher than\b|\blower than\b|\bincrease of\b|\bincrease by\b|\bremainder\b|\brest of the (?:year|fiscal)"
    r"|\bnet cash\b|\bnet of\b|\brepayments?\b|\bper quarter\b|\beach quarter\b|\bquarterly\b|\bexcluding\b"
    r"|\bsequential", re.IGNORECASE)


def _row(severity, area, ticker, finding, detail, date=None, url=None, fact_id=None) -> dict:
    return {"severity": severity, "area": area, "ticker": ticker, "finding": finding, "detail": detail,
            "date": pd.Timestamp(date) if date is not None and date == date else pd.NaT, "url": url,
            "fact_id": fact_id}


# ---------------------------------------------------------------------------
# Guidance
# ---------------------------------------------------------------------------


def check_guidance(facts: pd.DataFrame, history: pd.DataFrame, capex_q: pd.DataFrame,
                   as_of: pd.Timestamp) -> list[dict]:
    out = []
    if history.empty:
        return out
    for r in history.itertuples():
        quote = r.quote or ""
        if r.revision in ("raised", "lowered") and r.change_pct == r.change_pct and abs(r.change_pct) > 0.30:
            out.append(_row("Low", "Guidance", r.ticker, f"{r.period_label} guidance {r.revision} by {abs(r.change_pct):.0%}",
                            f"From {r.prior_value:g} to {r.value:g} ($B). Large moves are usually real, but confirm "
                            f"the two figures are on the same basis. Quote: {quote[:200]}",
                            r.filing_date, r.url))
        m = BASIS_WORDS.search(quote)
        if m and r.revision != "initial" and not (r.period_label or "").startswith(("Q", "H")):
            out.append(_row("Medium", "Guidance", r.ticker, f"{r.period_label} guidance quote says '{m.group(0)}'",
                            f"The number may be an increment, a partial period or a different basis rather than "
                            f"the full-year level. Quote: {quote[:200]}", r.filing_date, r.url))

    # Different numbers for the same period on the same day (before the one-per-day reduction).
    g = facts[(facts["metric"] == "capex_guidance")].copy()
    if not g.empty:
        g["period_label"] = [A.period_label(p, t) for p, t in zip(g["period"], g["ticker"])]
        g = g[g["period_label"].notna()]
        for (t, p, d), grp in g.groupby(["ticker", "period_label", "filing_date"]):
            vals = sorted(set(grp["value"].round(2)))
            if len(vals) > 1 and vals[-1] > vals[0] * 1.10:
                out.append(_row("High", "Guidance", t, f"Conflicting {p} guidance on the same day",
                                f"Values stated: {', '.join(f'{v:g}' for v in vals)} ($B). Only one is used in the "
                                f"revision history; check which is the like-for-like figure.", d, grp["url"].iloc[0]))

    # Guidance against what was actually spent.
    latest = A.latest_guidance(history)
    for r in latest.itertuples():
        label = r.period_label or ""
        if not re.fullmatch(r"(?:FY)?\d{4}", label):
            continue
        year = int(label[-4:])
        fy_end = A.FISCAL_YEAR_END_MONTH.get(r.ticker) if label.startswith("FY") else 12
        end = pd.Timestamp(year=year, month=fy_end or 12, day=1) + pd.offsets.MonthEnd(0)
        start = end - pd.DateOffset(years=1)
        c = capex_q[(capex_q["ticker"] == r.ticker) & (pd.to_datetime(capex_q["end"]) > start)
                    & (pd.to_datetime(capex_q["end"]) <= end)]
        spent = float(c["value_usd_billion"].sum()) if not c.empty else 0.0
        if end <= as_of and len(c) == 4 and r.value:
            gap = spent / r.value - 1
            if abs(gap) > 0.25:
                out.append(_row("Low", "Guidance", r.ticker, f"{label} capex came in {gap:+.0%} against guidance",
                                f"Last guidance {r.value:g}, reported capex {spent:.1f} ($B, SEC XBRL). Usually a "
                                f"definition difference (finance leases, accruals) rather than an error.",
                                r.filing_date, r.url))
        elif end > as_of and r.value and spent > r.value * 1.02:
            out.append(_row("High", "Guidance", r.ticker, f"{label} guidance is below capex already reported",
                            f"Guidance {r.value:g} but {spent:.1f} already spent in {len(c)} quarter(s) ($B). The "
                            f"guidance figure is probably a partial-period or net number.", r.filing_date, r.url))
    return out


# ---------------------------------------------------------------------------
# Leases, capacity, cloud, capex
# ---------------------------------------------------------------------------


def check_leases(flows: pd.DataFrame) -> list[dict]:
    out = []
    if flows.empty:
        return out
    for r in flows.dropna(subset=["pool_prev"]).itertuples():
        if r.pool_prev > 1 and r.pool < r.pool_prev * 0.7:
            out.append(_row("Medium", "Leases", r.ticker, "Lease backlog fell by more than 30% in a quarter",
                            f"{r.pool_prev:.1f} to {r.pool:.1f} ($B). Check that both figures are company totals "
                            f"on the same basis.", r.end))
        if r.signed == r.signed and r.signed < -max(0.1 * r.pool_prev, 0.25):  # ignore rounding-size gaps
            out.append(_row("Medium", "Leases", r.ticker, "Implied new lease signings are negative",
                            f"{r.signed:.1f} ($B): the backlog fell by more than the leases that started, which "
                            f"means a cancellation, a restatement or a misread figure.", r.end))
    return out


def check_capacity(disc: pd.DataFrame) -> list[dict]:
    out = []
    if disc.empty:
        return out
    if "group" in disc:  # hyperscalers state power added, not totals
        disc = disc[disc["group"] != "Hyperscaler"]
    lv = A.latest_values(disc, ["power_active_mw", "power_contracted_mw"])
    for t, g in lv.groupby("ticker"):
        a = g[g["metric"] == "power_active_mw"]
        c = g[g["metric"] == "power_contracted_mw"]
        if len(a) and len(c) and a["value"].iloc[0] > c["value"].iloc[0] * 1.05 \
                and abs((a["first_stated"].iloc[0] - c["first_stated"].iloc[0]).days) <= 120:
            out.append(_row("Medium", "Capacity", t, "Active power exceeds contracted power",
                            f"Active {a['value'].iloc[0]:,.0f} MW vs contracted {c['value'].iloc[0]:,.0f} MW. "
                            f"Companies define the two differently (gross vs critical IT); check both quotes.",
                            a["first_stated"].iloc[0], a["url"].iloc[0]))
    big = disc[(disc["metric"].isin(["power_active_mw", "power_contracted_mw"])) & (disc["value"] > 20_000)
               & (disc["group"] != "Hyperscaler")] if "group" in disc else disc.iloc[0:0]
    for r in big.itertuples():
        out.append(_row("Medium", "Capacity", r.ticker, f"Unusually large capacity figure ({r.value:,.0f} MW)",
                        f"Check units (MW vs GW) and that it is the company's own capacity. Quote: {r.quote[:200]}",
                        r.first_stated, r.url, r.fact_id))
    gpus = disc[(disc["metric"] == "gpus_deployed") & (disc["value"] > 1_000_000)]
    for r in gpus.itertuples():
        out.append(_row("Medium", "Capacity", r.ticker, f"Unusually large GPU count ({r.value:,.0f})",
                        f"Check the number is GPUs the company operates. Quote: {r.quote[:200]}", r.first_stated,
                        r.url, r.fact_id))
    return out


_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")


def _corroborated(ch: float, cur, year_ago: pd.DataFrame, sg: pd.DataFrame) -> bool:
    """A big quarter-on-quarter move is real growth, not a misread, when the release says so: its quote states a
    rate within 5 points of the move, or the company's stated year-over-year rate matches the series."""
    if any(abs(float(x) - ch * 100) <= 5 for x in _PCT.findall(str(cur.get("quote", "")))):
        return True
    if len(year_ago) and len(sg) and year_ago["value"].iloc[0] > 0:
        computed = (cur["value"] / year_ago["value"].iloc[0] - 1) * 100
        return abs(computed - float(sg["value"].iloc[-1])) <= 5
    return False


def check_cloud(rev: pd.DataFrame, growth: pd.DataFrame) -> list[dict]:
    out = []
    if rev.empty:
        return out
    for t, g in rev.groupby("ticker"):
        g = g.sort_values("end").reset_index(drop=True)
        for i in range(1, len(g)):
            prev, cur = g.iloc[i - 1], g.iloc[i]
            gap = (cur["end"] - prev["end"]).days
            year_ago = g[(g["end"] - (cur["end"] - pd.DateOffset(years=1))).abs() <= pd.Timedelta(days=20)]
            sg = growth[(growth["ticker"] == t) & (growth["segment"] == cur["segment"])
                        & ((growth["end"] - cur["end"]).abs() <= pd.Timedelta(days=20))] if not growth.empty else growth
            if 80 <= gap <= 100 and prev["value"] > 0:
                ch = cur["value"] / prev["value"] - 1
                limit = 0.6 if t in ("CRWV", "NBIS", "IREN") else 0.25
                if (ch > limit or ch < -0.15) and not _corroborated(ch, cur, year_ago, sg):
                    out.append(_row("Medium", "Cloud revenue", t,
                                    f"Cloud revenue moved {ch:+.0%} in a quarter ({cur['segment']})",
                                    f"{prev['value']:.2f} to {cur['value']:.2f} ($B). Check the segment is the same "
                                    f"in both quarters (companies re-segment). Quote: {str(cur['quote'])[:200]}",
                                    cur["end"], cur["url"]))
            if len(year_ago) and len(sg) and year_ago["value"].iloc[0] > 0:
                computed = (cur["value"] / year_ago["value"].iloc[0] - 1) * 100
                stated = float(sg["value"].iloc[-1])
                if abs(computed - stated) > 10:
                    out.append(_row("Medium", "Cloud revenue", t,
                                    f"Stated growth {stated:.0f}% vs {computed:.0f}% computed ({cur['segment']})",
                                    "The stated rate may be in constant currency or for a different segment.",
                                    cur["end"], cur["url"]))
    return out


def check_capex(facts: pd.DataFrame, capex_q: pd.DataFrame) -> list[dict]:
    """Press-release capex against SEC XBRL for the same quarter."""
    out = []
    x = capex_q[capex_q["source"].astype(str).str.startswith("SEC XBRL")] if not capex_q.empty else capex_q
    if x.empty:
        return out
    pr = facts[facts["metric"] == "capex_reported"].copy()
    if pr.empty:
        return out
    pr["end"] = [A.quarter_end(p, t) for p, t in zip(pr["period"], pr["ticker"])]
    pr = pr[pr["end"].notna()]
    # Only single-quarter figures on the cash basis: skip year-to-date, trailing and lease-inclusive statements.
    multi = re.compile(r"months|year[- ]to[- ]date|\bytd\b|twelve|\bltm\b|trailing|full[- ]year|fiscal year",
                       re.IGNORECASE)
    pr = pr[~pr["period"].fillna("").str.contains(multi) & ~pr["quote"].fillna("").str.contains(
        r"finance lease|including leases|in-?kind|accrual", case=False, regex=True)]
    x = x.assign(end=pd.to_datetime(x["end"]))
    for r in pr.itertuples():
        m = x[(x["ticker"] == r.ticker) & ((x["end"] - r.end).abs() <= pd.Timedelta(days=20))]
        if len(m) and m["value_usd_billion"].iloc[0] > 0:
            gap = r.value / m["value_usd_billion"].iloc[0] - 1
            if abs(gap) > 0.25:
                out.append(_row("Low", "Capex", r.ticker,
                                f"Press-release capex differs from SEC XBRL by {gap:+.0%} ({r.period})",
                                f"Release {r.value:.2f} vs XBRL {m['value_usd_billion'].iloc[0]:.2f} ($B). Companies often "
                                f"quote capex including finance leases. Quote: {str(r.quote)[:200]}",
                                r.filing_date, r.url, r.fact_id))
    return out


# ---------------------------------------------------------------------------


def run_checks(facts: pd.DataFrame, disc: pd.DataFrame, history: pd.DataFrame, capex_q: pd.DataFrame,
               flows: pd.DataFrame, cloud_rev: pd.DataFrame, cloud_gr: pd.DataFrame,
               as_of: pd.Timestamp) -> pd.DataFrame:
    rows = (check_guidance(facts, history, capex_q, as_of) + check_leases(flows) + check_capacity(disc)
            + check_cloud(cloud_rev, cloud_gr) + check_capex(facts, capex_q))
    out = pd.DataFrame(rows, columns=COLUMNS)
    if out.empty:
        return out
    out = out.drop_duplicates(["severity", "area", "ticker", "finding", "date"])
    return out.sort_values(["severity", "date"], key=lambda s: s.map(SEVERITY_ORDER) if s.name == "severity" else s,
                           ascending=[True, False]).reset_index(drop=True)


def flagged_revisions(issues: pd.DataFrame) -> set[tuple]:
    """(ticker, date) of guidance statements with a finding, to leave out of headline revision counts."""
    if issues.empty:
        return set()
    g = issues[(issues["area"] == "Guidance") & issues["severity"].isin(["High", "Medium"])]
    return {(t, pd.Timestamp(d).normalize()) for t, d in zip(g["ticker"], g["date"]) if d == d}
