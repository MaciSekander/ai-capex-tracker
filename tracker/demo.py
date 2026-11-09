"""A demo database of FICTIONAL companies, for trying the dashboard before running the pipeline.

Every company, number, quote and counterparty here is made up. The dashboard
shows a banner whenever it is reading this database.
"""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

from .extract import fact_id
from .store import Store

COMPANIES = [
    ("HYP1", "Demo Hyperscaler A", "Hyperscaler"),
    ("HYP2", "Demo Hyperscaler B", "Hyperscaler"),
    ("HYP3", "Demo Hyperscaler C", "Hyperscaler"),
    ("NEO1", "Demo Neocloud A", "Neocloud"),
    ("NEO2", "Demo Neocloud B", "Neocloud"),
    ("INF1", "Demo Power Developer", "AI infrastructure"),
]


def _quarter_ends(start_year: int = 2023, n: int = 15) -> list[date]:
    out, y, q = [], start_year, 1
    for _ in range(n):
        month = 3 * q
        nxt = date(y + (month == 12), (month % 12) + 1, 1)
        out.append(nxt - timedelta(days=1))
        q = q % 4 + 1
        y += q == 1
    return out


def build_demo(path: Path) -> Path:
    if path.exists():
        path.unlink()
    rng = np.random.default_rng(3)
    store = Store(path)
    store.upsert("companies", pd.DataFrame([{"ticker": t, "name": n, "grp": g, "cik": 0} for t, n, g in COMPANIES]))

    ends = _quarter_ends()
    xb = []
    profiles = {"HYP1": (9, 30, 26, 42), "HYP2": (7, 26, 22, 36), "HYP3": (5, 18, 15, 24),
                "NEO1": (0.4, 6.5, 0.1, 1.6), "NEO2": (0.1, 2.2, -0.05, 0.4), "INF1": (0.05, 1.1, -0.02, 0.15)}
    for t, (c0, c1, o0, o1) in profiles.items():
        for i, e in enumerate(ends):
            w = (i / (len(ends) - 1)) ** 1.4
            capex = (c0 + (c1 - c0) * w) * rng.normal(1, 0.05)
            ocf = (o0 + (o1 - o0) * w) * rng.normal(1, 0.08)
            for metric, v in (("capex", capex), ("operating_cash_flow", ocf)):
                xb.append({"ticker": t, "metric": metric, "quarter": f"{e.year}Q{(e.month - 1) // 3 + 1}",
                           "start": e - timedelta(days=90), "end": e, "value_usd_billion": round(v, 3),
                           "method": "reported" if i % 4 == 0 else "derived", "concept": "demo"})
    store.upsert("xbrl_quarterly", pd.DataFrame(xb))

    facts, filings, docs = [], [], []

    def add(t, filed: date, metric, value, unit, period, quote, low=None, high=None, change="not_stated",
            counterparty=None, site=None, instrument=None, term=None, status="verified", conf=0.95, as_of=None):
        digest = int(hashlib.sha1(f"{t}{filed}".encode()).hexdigest(), 16)
        acc = f"0000000000-{filed:%y}-{digest % 999999:06d}"
        doc_id = f"{acc}/ex99-1.htm"
        url = "https://example.com/demo-filing"
        if acc not in {f["accession"] for f in filings}:
            filings.append({"accession": acc, "ticker": t, "cik": 0, "form": "8-K", "filing_date": filed,
                            "report_date": filed, "items": "2.02", "url": url})
            docs.append({"doc_id": doc_id, "accession": acc, "ticker": t, "document": "ex99-1.htm",
                         "doc_type": "EX-99.1", "description": "Press release (demo)", "url": url, "text": "",
                         "chars": 0, "relevant_chars": 1, "extracted_at": datetime(2026, 11, 1), "model": "demo"})
        f = {"doc_id": doc_id, "accession": acc, "ticker": t, "filing_date": filed, "form": "8-K", "url": url,
             "metric": metric, "value": value, "value_low": low, "value_high": high, "unit": unit, "period": period,
             "as_of_date": as_of, "change": change, "counterparty": counterparty, "site": site,
             "instrument": instrument, "term_years": term, "quote": quote, "confidence": conf,
             "quote_match": "exact" if status == "verified" else ("fuzzy" if status == "partial" else "missing"),
             "value_in_quote": status != "unverified", "status": status, "model": "demo",
             "extracted_at": datetime(2026, 11, 1)}
        f["fact_id"] = fact_id(doc_id, f)
        facts.append(f)

    guidance = {
        "HYP1": [(date(2025, 1, 29), 80, "FY2025", "new"), (date(2025, 4, 30), 85, "FY2025", "raised"),
                 (date(2025, 7, 30), 95, "FY2025", "raised"), (date(2025, 10, 29), 105, "FY2026", "new"),
                 (date(2026, 1, 28), 115, "FY2026", "raised"), (date(2026, 4, 29), 120, "FY2026", "raised"),
                 (date(2026, 7, 29), 130, "FY2026", "raised"), (date(2026, 10, 28), 150, "FY2027", "new")],
        "HYP2": [(date(2025, 2, 4), 60, "2025", "new"), (date(2025, 7, 23), 70, "2025", "raised"),
                 (date(2025, 10, 28), 72, "2025", "reaffirmed"), (date(2026, 2, 3), 95, "2026", "new"),
                 (date(2026, 7, 28), 105, "2026", "raised"), (date(2026, 10, 27), 110, "2026", "raised")],
        "HYP3": [(date(2025, 1, 30), 40, "2025", "new"), (date(2025, 10, 30), 46, "2025", "raised"),
                 (date(2026, 1, 29), 60, "2026", "new"), (date(2026, 4, 30), 58, "2026", "lowered"),
                 (date(2026, 10, 29), 64, "2026", "raised")],
    }
    names = dict((t, n) for t, n, _ in COMPANIES)
    for t, rows in guidance.items():
        for filed, mid, period, change in rows:
            low, high = mid - 5, mid + 5
            verb = {"raised": "now expects", "lowered": "now expects", "reaffirmed": "continues to expect",
                    "new": "expects"}[change]
            add(t, filed, "capex_guidance", float(mid), "USD_billion", period,
                f"{names[t]} {verb} capital expenditures of ${low} billion to ${high} billion for {period}.",
                low=float(low), high=float(high), change=change)

    for t in guidance:
        for i in range(6):
            filed = date(2025, 5, 1) + timedelta(days=91 * i)
            as_of = (filed - timedelta(days=31)).isoformat()
            amount = round(40 + 18 * i + 9 * list(guidance).index(t), 1)
            add(t, filed, "lease_not_commenced", amount, "USD_billion", f"as of {as_of}",
                f"As of quarter end, we had additional leases, primarily for data centers, that had not yet "
                f"commenced of ${amount} billion.", as_of=as_of)

    neo = {"NEO1": (420, 2900, 160, 1400, 85_000, 310_000, 9, 62),
           "NEO2": (60, 900, 20, 310, 6_000, 72_000, 0.6, 14),
           "INF1": (120, 1500, 0, 380, 0, 0, 0.2, 9)}
    q_dates = [date(2024, 12, 16) + timedelta(days=91 * i) for i in range(8)]
    for t, (c0, c1, a0, a1, g0, g1, b0, b1) in neo.items():
        for i, filed in enumerate(q_dates):
            w = (i / 7) ** 1.2
            as_of = (filed - timedelta(days=35)).isoformat()
            contracted = round(c0 + (c1 - c0) * w, -1)
            active = round(a0 + (a1 - a0) * w, -1)
            add(t, filed, "power_contracted_mw", contracted, "MW", f"as of {as_of}",
                f"Contracted power reached {contracted:,.0f} MW as of quarter end.", as_of=as_of)
            if active:
                add(t, filed, "power_active_mw", active, "MW", f"as of {as_of}",
                    f"Active power was approximately {active:,.0f} MW across our sites.", as_of=as_of)
            add(t, filed, "power_pipeline_mw", round(contracted * 1.8, -1), "MW", f"as of {as_of}",
                f"Our development pipeline totals {contracted * 1.8:,.0f} MW of additional capacity.", as_of=as_of,
                status="partial" if i == 3 else "verified")
            if g1:
                gpus = round(g0 + (g1 - g0) * w, -3)
                add(t, filed, "gpus_deployed", gpus, "GPU_count", f"as of {as_of}",
                    f"We operate more than {gpus:,.0f} GPUs across our fleet.", as_of=as_of)
            backlog = round(b0 + (b1 - b0) * w, 1)
            add(t, filed, "backlog_usd", backlog, "USD_billion", f"as of {as_of}",
                f"Revenue backlog was ${backlog} billion at quarter end.", as_of=as_of,
                status="unverified" if (t == "NEO2" and i == 5) else "verified", conf=0.9)

    deals = [
        ("NEO1", date(2025, 6, 12), "customer_contract", 11.2, "Demo AI Lab X", None, 5, "Site North"),
        ("NEO1", date(2025, 11, 4), "customer_contract", 14.0, "Demo Hyperscaler B", None, 6, None),
        ("NEO2", date(2026, 2, 18), "customer_contract", 3.1, "Demo AI Lab Y", None, 4, "Site Delta"),
        ("INF1", date(2025, 9, 9), "customer_contract", 7.4, "Demo Neocloud A", None, 15, "Site Prairie"),
        ("INF1", date(2026, 5, 20), "customer_contract", 5.0, "Demo Hyperscaler C", None, 10, "Site Ridge"),
        ("NEO1", date(2025, 7, 22), "financing", 7.5, "Demo Bank Group", "delayed draw term loan", 5, None),
        ("NEO1", date(2026, 3, 10), "financing", 2.0, None, "senior notes", 7, None),
        ("NEO2", date(2025, 12, 2), "financing", 1.0, None, "convertible notes", 6, None),
        ("INF1", date(2026, 1, 14), "financing", 3.2, "Demo Credit Fund", "project financing", 8, "Site Prairie"),
        ("HYP3", date(2026, 8, 5), "financing", 25.0, None, "senior notes", 10, None),
    ]
    for t, filed, metric, v, cp, inst, term, site in deals:
        if metric == "customer_contract":
            q = f"{names[t]} signed a {term}-year agreement with {cp} valued at approximately ${v} billion."
        else:
            q = f"{names[t]} closed ${v} billion of {inst}" + (f" with {cp}" if cp else "") + "."
        add(t, filed, metric, v, "USD_billion", f"{filed.year}", q, counterparty=cp, instrument=inst, term=term,
            site=site, change="new")

    store.upsert("filings", pd.DataFrame(filings))
    store.upsert("documents", pd.DataFrame(docs))
    fdf = pd.DataFrame(facts)
    store.upsert("facts", fdf)

    # Review sample: a handful of documents marked up as a reviewer would.
    sample = fdf.drop_duplicates("doc_id").sample(18, random_state=1)["doc_id"]
    reviewed = fdf[fdf["doc_id"].isin(sample)]
    verdicts = np.where(reviewed["status"] == "unverified", "incorrect",
                        np.where(rng.random(len(reviewed)) < 0.06, "incorrect", "correct"))
    store.upsert("reviews", pd.DataFrame({"fact_id": reviewed["fact_id"], "doc_id": reviewed["doc_id"],
                                          "verdict": verdicts, "corrected_value": None, "note": "",
                                          "reviewed_at": datetime(2026, 11, 2)}))
    store.upsert("review_docs", pd.DataFrame({"doc_id": sample, "reviewed_at": datetime(2026, 11, 2)}))
    miss = reviewed.drop_duplicates("doc_id").head(3)
    store.con.register("_m", pd.DataFrame({"doc_id": miss["doc_id"], "metric": ["power_pipeline_mw", "backlog_usd", "gpus_deployed"],
                                           "value": [500.0, 2.5, 4000.0], "unit": ["MW", "USD_billion", "GPU_count"],
                                           "period": "demo", "quote": "missed in demo", "note": "",
                                           "reviewed_at": datetime(2026, 11, 2)}))
    store.con.execute("INSERT INTO misses SELECT * FROM _m")
    store.close()
    return path
