"""Tests for the dashboard's derived views (tracker/analytics.py) and the dashboard itself on demo data."""

import sys
from pathlib import Path

import numpy as np
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
    assert [t.label for t in at.tabs] == ["Overview", "Signals", "Leases", "Cloud revenue", "Capacity", "Capex momentum",
                                          "Neocloud funding", "Capex", "Guidance", "Power & capacity", "Deals",
                                          "Companies", "Explore data", "Methodology"]
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


@pytest.mark.parametrize("ticker,quote,window", [
    ("META", "obligations of $278.99 billion ... which will commence during the remainder of 2026 through 2036",
     ("2026-01-01", "2036-12-31")),
    ("ORCL", "$288 billion of additional lease commitments ... expected to commence between the second quarter of "
             "fiscal 2027 and fiscal 2029", ("2026-06-01", "2029-05-31")),
    ("MSFT", "we had additional leases that had not yet commenced of $364.6 billion.", None),
])
def test_stated_window(ticker, quote, window):
    w = A.stated_window(quote, ticker)
    assert (w is None) if window is None else (w == tuple(pd.Timestamp(x) for x in window))


def test_projection_falls_back_to_full_year_starts_and_uses_stated_timing():
    ends = pd.date_range("2025-03-31", periods=7, freq="QE")
    pools = [10, 20, 30, 40, 60, 80, 100]
    flows = pd.DataFrame({"ticker": "M", "end": ends, "pool": pools, "pool_prev": [np.nan] + pools[:-1],
                          "starts_pv": np.nan, "factor": 1.25, "starts": np.nan, "signed": np.nan})
    annual = pd.DataFrame({"ticker": ["M"], "end": [pd.Timestamp("2026-12-31")], "starts": [20.0]})
    windows = pd.DataFrame({"ticker": ["M"], "window_start": [pd.Timestamp("2026-01-01")],
                            "window_end": [pd.Timestamp("2030-09-30")], "window_quote": ["q"],
                            "window_as_of": [ends[-1]]})
    flows.loc[len(flows)] = ["M", pd.Timestamp("2026-12-31"), 100.0, 100.0, np.nan, 1.25, np.nan, np.nan]
    proj, params = A.lease_projection(flows, annual=annual, windows=windows)
    p = params.iloc[0]
    assert p["basis"] == "annual" and p["rate"] == pytest.approx(20 / (10 + 20 + 30 + 40))
    told = proj[proj["case"] == "Company's stated timing"]["starts"].tolist()
    assert told[0] == pytest.approx(100 / 15) and len(set(round(x, 6) for x in told)) == 1


@pytest.mark.parametrize("text,window", [
    ("Q2 2027", ("2027-04-01", "2027-06-30")), ("by end of 2026", ("2026-10-01", "2026-12-31")),
    ("H1 2027", ("2027-01-01", "2027-06-30")), ("2027 and 2028", ("2027-01-01", "2028-12-31")),
    ("Q3'26", ("2026-07-01", "2026-09-30")), ("over the next several years", None),
])
def test_parse_expected(text, window):
    w = A.parse_expected(text)
    assert (w is None) if window is None else (w == tuple(pd.Timestamp(x) for x in window))


def test_capacity_schedule_uses_latest_statement_and_flags_slips():
    facts = pd.DataFrame([
        fact(ticker="N", site="Site A", value=200.0, expected_date="Q4 2026", filing_date="2026-02-01", doc_id="a"),
        fact(ticker="N", site="Site A", value=200.0, expected_date="Q2 2027", filing_date="2026-08-01", doc_id="b"),
        fact(ticker="N", site=None, value=900.0, expected_date="2027", filing_date="2026-08-01", doc_id="b"),
        fact(ticker="M", site=None, value=50.0, expected_date="H1 2027", filing_date="2026-08-01", doc_id="c"),
    ])
    cap = A.capacity_schedule(facts, pd.Timestamp("2026-09-30")).set_index("ticker")
    assert cap.loc["N", "mw"] == 200 and cap.loc["N", "expected_end"] == pd.Timestamp("2027-06-30")
    assert cap.loc["N", "slip_days"] > 150  # Q4 2026 -> Q2 2027
    assert cap.loc["M", "mw"] == 50  # company-level total kept when no dated sites


def test_lease_starts_estimated_from_lease_assets_add_up_to_reported_year():
    rows = [("lease_rou_operating", d, v) for d, v in (("2024-12-31", 10.0), ("2025-03-31", 11.0), ("2025-06-30", 12.0),
                                                       ("2025-09-30", 14.0), ("2025-12-31", 15.0))]
    rows += [("lease_rou_finance_gross", d, 0.0) for d in ("2024-12-31", "2025-03-31", "2025-06-30", "2025-09-30",
                                                           "2025-12-31")]
    rows += [("lease_starts_operating_fy", "2025-12-31", 6.25)]
    xq = pd.DataFrame([{"ticker": "M", "metric": m, "end": d, "value_usd_billion": v} for m, d, v in rows])
    est = A.estimated_lease_starts(xq)
    assert est["starts_pv_est"].sum() == pytest.approx(6.25, rel=0.02)
    assert est["starts_pv_est"].iloc[2] > est["starts_pv_est"].iloc[1]  # bigger asset change, more starts


def test_stated_timing_spreads_tranches_over_their_own_windows():
    ends = pd.date_range("2025-12-31", periods=4, freq="QE")
    flows = pd.DataFrame({"ticker": "M", "end": ends, "pool": [100.0, 110, 120, 130], "pool_prev": [np.nan, 100, 110, 120],
                          "starts_pv": 4.0, "starts_basis": "reported", "factor": 1.0, "starts": 4.0,
                          "signed": [np.nan, 14, 14, 14]})
    windows = pd.DataFrame({"ticker": ["M"], "window_start": [pd.Timestamp("2026-01-01")],
                            "window_end": [pd.Timestamp("2036-12-31")], "window_quote": ["q"], "window_as_of": [ends[-1]]})
    tranches = pd.DataFrame({"ticker": ["M"], "value": [40.0], "start": [pd.Timestamp("2027-01-01")],
                             "end": [pd.Timestamp("2028-12-31")]})
    proj, _ = A.lease_projection(flows, lag=1, windows=windows, tranches=tranches)
    told = proj[proj["case"] == "Company's stated timing"].set_index("end")["starts"]
    assert told.loc[pd.Timestamp("2026-12-31")] == pytest.approx(90 / 41, rel=0.05)  # rest spread to 2036
    assert told.loc[pd.Timestamp("2027-03-31")] == pytest.approx(90 / 41 + 40 / 8, rel=0.05)  # plus the tranche


def test_transcripts_folder_and_docx(tmp_path):
    import zipfile

    from tracker import transcripts

    folder = tmp_path / "META"
    folder.mkdir()
    (folder / "2026-10-29 Q3 call.txt").write_text("We expect capital expenditures of $125 billion in 2027.")
    (folder / "notes.txt").write_text("no date, skipped")
    (folder / "sources.csv").write_text("file,url\n2026-10-29 Q3 call.txt,https://example.com/t.pdf\n")
    items = transcripts.scan(tmp_path)
    assert [(i["ticker"], str(i["call_date"]), i["url"]) for i in items] == [("META", "2026-10-29", "https://example.com/t.pdf")]
    filings, docs = transcripts.to_rows(items, set(), len)
    assert filings["form"].tolist() == ["Transcript"] and docs["text"].str.contains("125 billion").all()

    buf = tmp_path / "t.docx"
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", "<w:document><w:body><w:p><w:r><w:t>AMY HOOD:</w:t></w:r></w:p>"
                   "<w:p><w:r><w:t xml:space=\"preserve\">Capex will grow </w:t></w:r><w:r><w:t>sequentially.</w:t>"
                   "</w:r></w:p></w:body></w:document>")
    assert transcripts.docx_text(buf.read_bytes()) == "AMY HOOD:\n\nCapex will grow sequentially."


def test_lease_series_ignores_tranches_dated_by_month():
    disc = A.disclosures(pd.DataFrame([
        fact(metric="lease_not_commenced", unit="USD_billion", value=279.0, period="as of June 30, 2026", doc_id="a"),
        fact(metric="lease_not_commenced", unit="USD_billion", value=68.0, period="July 2026", doc_id="a"),
        fact(metric="lease_not_commenced", unit="USD_billion", value=0.02, period="May 31, 2025", doc_id="b"),
    ]))
    assert sorted(A.lease_series(disc)["value"]) == [0.02, 279.0]


def test_equibles_download_pages_resumes_and_respects_budget(tmp_path, monkeypatch):
    from datetime import date

    from tracker import transcripts

    calls = {"data": [
        {"callDate": "2026-10-28T21:00:00+00:00", "fiscalYear": 2026, "fiscalQuarter": 3, "hasTranscript": True,
         "webcastUrl": "https://example.com/q3"},
        {"callDate": "2026-07-22T21:00:00+00:00", "fiscalYear": 2026, "fiscalQuarter": 2, "hasTranscript": False},
        {"callDate": "2023-01-01T21:00:00+00:00", "fiscalYear": 2022, "fiscalQuarter": 4, "hasTranscript": True},
    ], "meta": {"hasMore": False}}
    pages = {0: {"eventTitle": "Q3 2026 Earnings Call", "turnCount": 1, "hasMore": True, "isFiledRecord": False,
                 "data": [{"speakerName": "Jane CFO", "speakerRole": "CFO", "text": "Capex will be $90 billion."}]},
             1: {"turnCount": 1, "hasMore": False, "data": [{"speakerName": None, "speakerRole": "Operator",
                                                             "text": "That concludes the call."}]}}

    def fake_get(self, path, **params):
        self.used += 1
        if self.used > self.max_requests:
            raise transcripts.RequestBudgetSpent("limit")
        return calls if path.endswith("investor-events") else pages[params.get("offset", 0)]

    monkeypatch.setattr(transcripts.Equibles, "get", fake_get)
    out = transcripts.fetch_equibles("k", date(2024, 10, 1), ["GOOGL"], root=tmp_path, log=lambda *_: None)
    assert out["saved"] == 1 and out["unavailable"] == 1 and out["requests"] == 3
    text = (tmp_path / "GOOGL" / "2026-10-28 GOOGL FY2026Q3.txt").read_text()
    assert "Jane CFO (CFO):\nCapex will be $90 billion." in text and "Operator:\nThat concludes" in text
    assert "https://example.com/q3" in (tmp_path / "GOOGL" / "sources.csv").read_text()
    again = transcripts.fetch_equibles("k", date(2024, 10, 1), ["GOOGL"], root=tmp_path, log=lambda *_: None)
    assert again["saved"] == 0 and again["already"] == 1  # resumes without re-downloading
    stopped = transcripts.fetch_equibles("k", date(2024, 10, 1), ["AMZN"], root=tmp_path, max_requests=1,
                                         log=lambda *_: None)
    assert stopped["stopped"] and stopped["saved"] == 0


@pytest.mark.parametrize("period,ticker,label", [
    ("Q1 FY2026", "MSFT", "Q1 FY2026"), ("calendar year 2026", "MSFT", "2026"), ("2026", "MSFT", "FY2026"),
    ("FY2027", "ORCL", "FY2027"), ("FY2026", "META", "2026"), ("Q2 2026", "META", "Q2 2026"),
])
def test_fiscal_periods_are_not_mixed_with_calendar(period, ticker, label):
    assert A.period_label(period, ticker) == label


@pytest.mark.parametrize("period,ticker,end", [
    ("Q1 FY2026", "MSFT", "2025-09-30"), ("Q4 FY2026", "MSFT", "2026-06-30"), ("Q1 FY2027", "ORCL", "2026-08-31"),
    ("third quarter of 2026", "AMZN", "2026-09-30"), ("calendar Q3 2026", "MSFT", "2026-09-30"),
    ("Q3 fiscal 2024", "ORCL", "2024-02-29"),
    ("Q3 2025 (quarter ended September 30, 2025)", "IREN", "2025-09-30"),  # explicit date wins over the label
    ("three months ended December 31, 2024", "IREN", "2024-12-31"),
])
def test_quarter_end_uses_fiscal_calendar(period, ticker, end):
    assert A.quarter_end(period, ticker) == pd.Timestamp(end)


def test_cloud_revenue_keeps_one_segment_and_the_reported_quarter():
    rows = []
    for i, (end, filed) in enumerate([("2025-09-30", "2025-10-29"), ("2025-12-31", "2026-01-28"),
                                      ("2026-03-31", "2026-04-29"), ("2026-06-30", "2026-07-29"),
                                      ("2026-09-30", "2026-10-28")]):
        q = f"Q{pd.Timestamp(end).quarter} {pd.Timestamp(end).year}"
        rows += [fact(ticker="AMZN", metric="cloud_revenue", unit="USD_billion", value=30.0 + i, period=q,
                      segment="AWS", quote="AWS segment sales", filing_date=filed, doc_id=f"d{i}"),
                 fact(ticker="AMZN", metric="cloud_revenue", unit="USD_billion", value=20.0, period="Q3 2024",
                      segment="AWS", quote="AWS segment sales prior year", filing_date=filed, doc_id=f"d{i}")]
    rows.append(fact(ticker="AMZN", metric="cloud_growth_pct", unit="percent", value=10.0, period="Q3 2026",
                     segment="AWS", quote="AWS grew 10%", filing_date="2026-10-28", doc_id="d4"))
    facts = pd.DataFrame(rows)
    rev = A.cloud_revenue(facts)
    assert rev["value"].tolist() == [30.0, 31.0, 32.0, 33.0, 34.0]  # prior-year comparisons left out
    capex = pd.DataFrame({"ticker": "AMZN", "end": pd.date_range("2024-12-31", periods=8, freq="QE"),
                          "value_usd_billion": 10.0})
    s = A.cloud_summary(rev, A.cloud_growth(facts), capex).iloc[0]
    assert s["latest_b"] == 34 and s["stated_growth_pct"] == 10 and s["rev_4q"] == 130
    assert s["capex_to_revenue"] == pytest.approx(40 / 130)


def test_checks_flag_contradictions():
    from tracker import checks as C

    hist = A.guidance_history(pd.DataFrame([
        fact(ticker="X", metric="capex_guidance", unit="USD_billion", value=35.0, period="2026", filing_date="2026-03-01",
             doc_id="a", quote="We expect 2026 capex of $35 billion."),
        fact(ticker="X", metric="capex_guidance", unit="USD_billion", value=15.0, period="2026", filing_date="2026-06-01",
             doc_id="b", quote="Capex will be about $15 billion higher than we forecasted."),
    ]))
    capex = pd.DataFrame({"ticker": "X", "end": pd.to_datetime(["2026-03-31", "2026-06-30"]),
                          "value_usd_billion": [10.0, 12.0], "source": "SEC XBRL"})
    issues = pd.DataFrame(C.check_guidance(pd.DataFrame(columns=["metric"]), hist, capex, pd.Timestamp("2026-07-01")))
    found = " | ".join(issues["finding"])
    assert "says 'higher than'" in found and "below capex already reported" in found
    assert (("X", pd.Timestamp("2026-06-01")) in C.flagged_revisions(issues))
    flows = pd.DataFrame({"ticker": "Y", "end": pd.to_datetime(["2026-06-30"]), "pool": [40.0], "pool_prev": [100.0],
                          "signed": [-55.0]})
    assert {r["finding"] for r in C.check_leases(flows)} == {
        "Lease backlog fell by more than 30% in a quarter", "Implied new lease signings are negative"}


def test_oracle_cloud_series_keeps_infrastructure_apart_from_total_cloud():
    rows = []
    for i, (q, end, filed) in enumerate([("Q1 FY2026", "2025-08-31", "2025-09-09"),
                                         ("Q2 FY2026", "2025-11-30", "2025-12-10")]):
        rows += [fact(ticker="ORCL", metric="cloud_revenue", unit="USD_billion", value=3.3 + i, period=q,
                      segment="Oracle Cloud Infrastructure", quote="Cloud Infrastructure (IaaS) Revenue",
                      filing_date=filed, doc_id=f"o{i}"),
                 fact(ticker="ORCL", metric="cloud_revenue", unit="USD_billion", value=7.2 + i, period=q,
                      segment="total cloud (IaaS plus SaaS)", quote="Cloud Revenue (IaaS plus SaaS)",
                      filing_date=filed, doc_id=f"o{i}")]
    rev = A.cloud_revenue(pd.DataFrame(rows))
    assert rev["value"].tolist() == [3.3, 4.3] and set(rev["segment"]) == {"OCI (infrastructure)"}


def test_cloud_jump_check_accepts_growth_the_release_states():
    from tracker import checks as C

    rev = pd.DataFrame({"ticker": "IREN", "segment": "AI cloud", "value": [0.07, 0.20],
                        "end": pd.to_datetime(["2026-06-30", "2026-09-30"]), "url": "u",
                        "quote": ["AI Cloud revenue 70.5", "AI Cloud quarterly revenue increased 182% sequentially"]})
    assert C.check_cloud(rev, pd.DataFrame(columns=["ticker", "segment", "end", "value"])) == []
    rev.loc[1, "quote"] = "AI Cloud revenue 200"
    assert len(C.check_cloud(rev, pd.DataFrame(columns=["ticker", "segment", "end", "value"]))) == 1
