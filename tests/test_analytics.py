"""Tests for the dashboard's derived views (tracker/analytics.py) and the dashboard itself on demo data."""

import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tracker import analytics as A  # noqa: E402
from tracker import extract  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def fact(**kw):
    base = {"ticker": "EXCL", "metric": "power_contracted_mw", "value": 100.0, "value_low": None, "value_high": None,
            "unit": "MW", "period": None, "counterparty": None, "site": None, "status": "verified",
            "filing_date": "2026-01-01", "doc_id": "d1", "quote": "q", "url": "u", "change": "not_stated",
            "fact_id": None}
    base.update(kw)
    base["fact_id"] = base["fact_id"] or f"{base['doc_id']}-{base['metric']}-{base['value']}"
    return base


@pytest.mark.parametrize("period,label", [
    ("2026", "2026"), ("FY2026", "2026"), ("fiscal 2026", "2026"), ("full-year 2024", "2024"),
    ("calendar year 2025", "2025"), ("FY27 Q1", "Q1 2027"), ("Q2'26", "Q2 2026"), ("H2 2026", "H2 2026"),
    ("over the next several years", None), ("by mid-2025", None), ("Horizon 1 project", None), (None, None),
])
def test_period_label(period, label):
    assert A.period_label(period) == label


def test_quarter_from_period():
    assert A.quarter_from_period("Q2 2026") == "2026Q2"
    assert A.quarter_from_period("In Q3'25") == "2025Q3"
    assert A.quarter_from_period("second quarter of 2026") == "2026Q2"
    assert A.quarter_from_period("six months ended June 30, 2026") is None


def test_disclosures_merge_repeats_and_keep_first_date():
    facts = pd.DataFrame([
        fact(doc_id="a", filing_date="2026-02-01", status="partial"),
        fact(doc_id="b", filing_date="2026-03-01"),
        fact(doc_id="c", filing_date="2026-04-01"),
        fact(doc_id="d", value=200.0, filing_date="2026-04-01"),
    ])
    d = A.disclosures(facts).set_index("value")
    assert len(d) == 2
    row = d.loc[100.0]
    assert row["mentions"] == 3 and row["status"] == "verified"
    assert row["first_stated"] == pd.Timestamp("2026-02-01") and row["last_stated"] == pd.Timestamp("2026-04-01")


def test_deals_restated_in_other_words_are_merged_within_window():
    facts = pd.DataFrame([
        fact(doc_id="a", metric="customer_contract", unit="USD_billion", value=27.0, counterparty="Meta",
             filing_date="2026-03-16"),
        fact(doc_id="b", metric="customer_contract", unit="USD_billion", value=27.0, counterparty=None,
             period="five-year", filing_date="2026-05-13"),
        fact(doc_id="c", metric="customer_contract", unit="USD_billion", value=27.0, filing_date="2027-06-01"),
    ])
    d = A.disclosures(facts)
    assert len(d) == 2  # the 2027 statement is outside the window, so it counts as a new deal
    first = d.sort_values("first_stated").iloc[0]
    assert first["mentions"] == 2 and first["last_stated"] == pd.Timestamp("2026-05-13")


def test_guidance_revisions_are_computed_from_numbers():
    rows = [
        fact(doc_id="a", metric="capex_guidance", unit="USD_billion", value=125.0, period="2026", filing_date="2026-01-28"),
        fact(doc_id="b", metric="capex_guidance", unit="USD_billion", value=135.0, value_low=125.0, value_high=145.0,
             period="FY2026", filing_date="2026-04-29"),
        fact(doc_id="b2", metric="capex_guidance", unit="USD_billion", value=135.0, period="2026",
             filing_date="2026-04-29"),  # same day, deck repeats the release
        fact(doc_id="c", metric="capex_guidance", unit="USD_billion", value=135.0, value_low=130.0, value_high=140.0,
             period="2026", filing_date="2026-07-29"),  # midpoint flat, range narrowed: low up, high down
        fact(doc_id="d", metric="capex_guidance", unit="USD_billion", value=120.0, period="2026", filing_date="2026-10-29"),
        fact(doc_id="e", metric="capex_guidance", unit="USD_billion", value=50.0, period="over the next several years",
             filing_date="2026-10-29"),
    ]
    h = A.guidance_history(pd.DataFrame(rows))
    assert list(h["revision"]) == ["initial", "raised", "raised", "lowered"]
    assert h.iloc[1]["change_b"] == pytest.approx(10.0)
    assert set(h["period_label"]) == {"2026"}  # open-ended guidance is left out


def test_capex_fills_companies_without_xbrl_from_press_releases():
    xq = pd.DataFrame([{"ticker": "BIG", "metric": "capex", "quarter": "2026Q1", "start": "2026-01-01",
                        "end": "2026-03-31", "value_usd_billion": 10.0, "method": "reported", "concept": "x"}])
    facts = pd.DataFrame([
        fact(ticker="NEB", metric="capex_reported", unit="USD_billion", value=2.5, period="Q1 2026", filing_date="2026-05-13"),
        fact(ticker="NEB", metric="capex_reported", unit="USD_billion", value=2.47, period="Q1 2026", filing_date="2026-05-20"),
        fact(ticker="NEB", metric="capex_reported", unit="USD_billion", value=8.1, period="six months ended June 30, 2026"),
        fact(ticker="BIG", metric="capex_reported", unit="USD_billion", value=99.0, period="Q1 2026"),  # XBRL wins
    ])
    cq = A.capex_quarterly(xq, facts)
    assert cq[cq["ticker"] == "BIG"]["value_usd_billion"].tolist() == [10.0]
    neb = cq[cq["ticker"] == "NEB"]
    assert neb["value_usd_billion"].tolist() == [2.47] and neb["source"].iloc[0] == "Press release"


def test_trailing_totals_need_consecutive_quarters():
    def q(label, v):
        y, n = int(label[:4]), int(label[-1])
        return {"ticker": "T", "quarter": label, "end": pd.Timestamp(year=y, month=3 * n, day=28), "value_usd_billion": v,
                "source": "SEC XBRL"}
    full = pd.DataFrame([q("2025Q1", 1), q("2025Q2", 2), q("2025Q3", 3), q("2025Q4", 4)])
    gap = pd.DataFrame([q("2024Q4", 1), q("2025Q2", 2), q("2025Q3", 3), q("2025Q4", 4)])
    assert A.ttm_total(full, ["T"]) == 10
    assert A.ttm_total(gap, ["T"]) == 0
    assert A.capex_summary(gap)["ttm_b"].isna().all()


def test_latest_values_prefer_company_level_and_largest():
    disc = A.disclosures(pd.DataFrame([
        fact(doc_id="a", value=500.0, filing_date="2026-06-01"),
        fact(doc_id="a", value=900.0, filing_date="2026-06-01"),
        fact(doc_id="a", value=5000.0, site="Big Site", filing_date="2026-06-01"),
        fact(doc_id="z", value=300.0, filing_date="2025-01-01"),
    ]))
    lv = A.latest_values(disc, ["power_contracted_mw"])
    assert lv["value"].tolist() == [900.0]


def test_corrections_exclude_listed_facts():
    facts = pd.DataFrame([fact(fact_id="keep"), fact(fact_id="drop", value=2.0)])
    cor = pd.DataFrame({"fact_id": ["drop "], "action": ["Exclude"], "reason": ["x"], "date": ["2026-11-08"]})
    assert A.apply_corrections(facts, cor)["fact_id"].tolist() == ["keep"]


@pytest.mark.parametrize("quote,unit,value,ok", [
    ("Executed 800 megawatts of long-term agreements with CoreWeave.", "USD_billion", 800, False),  # MW is not dollars
    ("more than $14 billion of contracted revenue", "MW", 14, False),  # dollars are not MW
    ("cash outflow of $(188.0) million for the nine months", "USD_billion", 187.985, False),  # wrong scale
    ("cash outflow of $(188.0) million for the nine months", "USD_billion", 0.188, True),
    ("$0.9 – 1.0b n $ 164 m Capex", "USD_billion", 0.95, False),
    ("and with $40 bil-lion in customer commitments", "USD_billion", 40, True),  # PDF hyphenation
    ("Total revenue estimated at over $3.5Bn³", "USD_billion", 3.5, True),  # footnote mark
    ("Purchases of property and equipment | (1,054.5) | (8,130.3)", "USD_billion", 8.1303, True),  # table in millions
    ("Purchases of property and equipment | (1,054.5) | (8,130.3)", "USD_billion", 8130.3, False),
])
def test_value_check_respects_units(quote, unit, value, ok):
    assert extract.value_match({"quote": quote, "unit": unit, "value": value}) is ok


def test_dashboard_renders_every_tab_on_demo_data(tmp_path, monkeypatch):
    streamlit_testing = pytest.importorskip("streamlit.testing.v1")
    from tracker.demo import build_demo

    db = build_demo(tmp_path / "demo.duckdb")
    monkeypatch.setenv("TRACKER_DB", str(db))
    monkeypatch.chdir(ROOT)
    at = streamlit_testing.AppTest.from_file(str(ROOT / "streamlit_app.py"), default_timeout=120).run()
    assert not at.exception, [e.value for e in at.exception]
    assert [t.label for t in at.tabs] == ["Overview", "Signals", "Capex", "Guidance", "Power & capacity", "Contracts & financing",
                                          "Company profile", "All disclosures", "Methodology"]
    for name in at.selectbox(key="profile").options:
        at.selectbox(key="profile").set_value(name).run()
        assert not at.exception, (name, [e.value for e in at.exception])
    at.radio(key="status").set_value("Verified and partial").run()
    at.toggle(key="ex_raw").set_value(True).run()
    at.text_input(key="ex_query").input("billion").run()
    assert not at.exception, [e.value for e in at.exception]


def test_periodic_reports_use_primary_document_and_narrow_filter():
    from tracker.edgar import select_documents

    docs = [{"document": "msft-10q.htm", "type": "10-Q"}, {"document": "ex31.htm", "type": "EX-31.1"},
            {"document": "ex99.htm", "type": "EX-99.1"}]
    assert [d["document"] for d in select_documents(docs, "10-Q", "msft-10q.htm")] == ["msft-10q.htm"]
    text = ("Risk factors\n\nOur data center financing and credit facility may be affected.\n\nUnrelated text.\n\n"
            "Other text.\n\nAs of June 30, 2026, we had additional leases, primarily for datacenters, that had not "
            "yet commenced of $329.1 billion.")
    periodic = " ".join(extract.relevant_chunks(text, form="10-Q"))
    assert "not yet commenced" in periodic and "credit facility" not in periodic
    assert "credit facility" in " ".join(extract.relevant_chunks(text, form="8-K"))


def test_deals_come_from_announcements_only():
    facts = pd.DataFrame([fact(metric="financing", form="8-K"), fact(metric="financing", form="10-Q", value=2.0),
                          fact(metric="lease_not_commenced", form="10-Q", value=3.0)])
    assert A.drop_periodic_deals(facts)["value"].tolist() == [100.0, 3.0]


def test_lease_series_keeps_dated_totals():
    disc = A.disclosures(pd.DataFrame([
        fact(metric="lease_not_commenced", unit="USD_billion", value=85.1, period="as of December 31, 2023", doc_id="a"),
        fact(metric="lease_not_commenced", unit="USD_billion", value=7.5, period="as of December 31, 2023", doc_id="a"),
        fact(metric="lease_not_commenced", unit="USD_billion", value=9.9, period="between 2027 and 2047", doc_id="b"),
        fact(metric="lease_not_commenced", unit="USD_billion", value=68.0, period="July 2026 additional leases", doc_id="b"),
        fact(metric="lease_not_commenced", unit="USD_billion", value=92.7, period="as of June 30, 2025", doc_id="c"),
    ]))
    s = A.lease_series(disc)
    assert s["value"].tolist() == [85.1, 92.7]
    assert s["as_of"].tolist() == [pd.Timestamp("2023-12-31"), pd.Timestamp("2025-06-30")]


def test_lease_series_adds_operating_and_finance_parts():
    q = "we had additional operating and finance leases that had not yet commenced of $7.5 billion and $85.1 billion, respectively."
    disc = A.disclosures(pd.DataFrame([
        fact(metric="lease_not_commenced", unit="USD_billion", value=7.5, period="as of December 31, 2023", quote=q),
        fact(metric="lease_not_commenced", unit="USD_billion", value=85.1, period="as of December 31, 2023", quote=q),
    ]))
    assert A.lease_series(disc)["value"].tolist() == [pytest.approx(92.6)]


def test_lease_flows_and_projection():
    pools = [("2026-03-31", 100.0), ("2026-06-30", 130.0), ("2026-09-30", 150.0)]
    disc = A.disclosures(pd.DataFrame([
        fact(ticker="T", metric="lease_not_commenced", unit="USD_billion", value=v, doc_id=d,
             period=f"as of {pd.Timestamp(d):%B %d, %Y}") for d, v in pools]))
    xq = pd.DataFrame(
        [{"ticker": "T", "metric": "lease_starts_operating", "end": d, "value_usd_billion": 8.0} for d, _ in pools]
        + [{"ticker": "T", "metric": m, "end": "2026-03-31", "value_usd_billion": v}
           for m, v in (("lease_liability_operating", 10.0), ("lease_payments_operating", 12.5))])
    fl = A.lease_flows(xq, disc).set_index("end")
    row = fl.loc[pd.Timestamp("2026-06-30")]
    assert row["factor"] == pytest.approx(1.25) and row["starts"] == pytest.approx(10.0)
    assert row["signed"] == pytest.approx(130 - 100 + 10)
    proj, params = A.lease_projection(fl.reset_index(), lag=1)
    p = params.iloc[0]
    # starts(t) = rate * pool(t-1): rate = (10 + 10) / (100 + 130)
    assert p["rate"] == pytest.approx(20 / 230) and p["signed_avg"] == pytest.approx((40 + 30) / 2)
    runoff = proj[proj["case"] == "No new signings"]["starts"].tolist()
    assert runoff[0] == pytest.approx(150 * p["rate"]) and runoff[1] < runoff[0]
    cont = proj[proj["case"] == "Signings continue"]["starts"].tolist()
    assert cont[0] == pytest.approx(runoff[0]) and cont[1] > runoff[1]
