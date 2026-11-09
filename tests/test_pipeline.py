"""Tests with a mocked SEC EDGAR and a mocked Claude client. "Example Cloud Inc." is fictional."""

import json
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tracker import config, edgar, evaluate, extract, pipeline, xbrl  # noqa: E402
from tracker.store import Store  # noqa: E402

CIK = 9999999
ACC = "0009999999-26-000123"

PRESS_RELEASE = """
<html><body>
<p>EXAMPLE CLOUD INC. REPORTS THIRD QUARTER 2026 RESULTS</p>
<p>Revenue grew 80% year over year. We remain focused on customers.</p>
<p>Example Cloud now expects capital expenditures of $18 billion to $22 billion for fiscal 2026, raised from prior guidance.</p>
<p>As of September 30, 2026, active power was approximately 1.2 GW, and contracted power was 2,900 MW.</p>
<p>We operate more than 250,000 GPUs across 30 data centers.</p>
<table><tr><td>Revenue backlog</td><td>$</td><td>55.6</td><td>billion</td></tr></table>
<p>The company signed a five-year agreement with Example AI Lab valued at approximately $6.3 billion.</p>
<p>Forward-looking statements are subject to risks.</p>
</body></html>
"""

INDEX_HTML = f"""
<html><body><table class="tableFile" summary="Document Format Files">
<tr><th>Seq</th><th>Description</th><th>Document</th><th>Type</th><th>Size</th></tr>
<tr><td>1</td><td>8-K</td><td><a href="/Archives/edgar/data/{CIK}/x/form8k.htm">form8k.htm</a></td><td>8-K</td><td>10</td></tr>
<tr><td>2</td><td>PRESS RELEASE</td><td><a href="/Archives/edgar/data/{CIK}/x/ex99-1.htm">ex99-1.htm</a></td><td>EX-99.1</td><td>50</td></tr>
<tr><td>3</td><td>GRAPHIC</td><td><a href="/Archives/edgar/data/{CIK}/x/logo.jpg">logo.jpg</a></td><td>GRAPHIC</td><td>5</td></tr>
</table></body></html>
"""


def companyfacts():
    def q(start, end, val, filed):
        return {"start": start, "end": end, "val": val, "filed": filed, "form": "10-Q"}

    capex = [
        q("2025-01-01", "2025-03-31", 2.0e9, "2025-05-01"),  # Q1, 3 months
        q("2025-01-01", "2025-06-30", 4.5e9, "2025-08-01"),  # 6 months YTD -> Q2 = 2.5B
        q("2025-01-01", "2025-09-30", 7.5e9, "2025-11-01"),  # 9 months YTD -> Q3 = 3.0B
        q("2025-01-01", "2025-12-31", 11.0e9, "2026-02-20"),  # FY -> Q4 = 3.5B
        q("2025-07-01", "2025-09-30", 3.0e9, "2025-11-01"),  # also reported directly
    ]
    ocf = [q("2025-01-01", "2025-03-31", 1.0e9, "2025-05-01"), q("2025-01-01", "2025-06-30", 2.2e9, "2025-08-01")]
    return {"facts": {"us-gaap": {
        "PaymentsToAcquirePropertyPlantAndEquipment": {"units": {"USD": capex}},
        "NetCashProvidedByUsedInOperatingActivities": {"units": {"USD": ocf}},
    }}}


def fake_sec(url: str, headers: dict):
    assert "@" in headers["User-Agent"]
    if url.endswith("company_tickers.json"):
        return 200, json.dumps({"0": {"cik_str": CIK, "ticker": "EXCL", "title": "Example Cloud Inc."}}).encode()
    if "submissions/CIK" in url:
        recent = {"accessionNumber": [ACC, "0009999999-26-000050", "0009999999-23-000001"],
                  "filingDate": ["2026-11-03", "2026-05-01", "2023-03-01"], "reportDate": ["2026-11-03", "", ""],
                  "form": ["8-K", "8-K", "8-K"], "items": ["2.02,9.01", "5.07", "2.02"],
                  "primaryDocument": ["form8k.htm", "vote.htm", "old.htm"]}
        return 200, json.dumps({"filings": {"recent": recent, "files": []}}).encode()
    if url.endswith("-index.htm"):
        return 200, INDEX_HTML.encode()
    if url.endswith("ex99-1.htm"):
        return 200, PRESS_RELEASE.encode()
    if url.endswith("form8k.htm"):
        return 200, b"<html><body><p>Item 2.02 Results of Operations. See Exhibit 99.1.</p></body></html>"
    if "companyfacts" in url:
        return 200, json.dumps(companyfacts()).encode()
    return 404, b""


class FakeClaude:
    """Returns canned facts, including one hallucinated quote and one wrong number."""

    def __init__(self):
        self.calls = 0
        self.messages = self

    def create(self, **kw):
        self.calls += 1
        assert kw["tool_choice"]["name"] == "record_facts"
        facts = [
            {"metric": "capex_guidance", "value": 20, "value_low": 18, "value_high": 22, "unit": "USD_billion",
             "period": "fiscal 2026", "change": "raised", "confidence": 0.95,
             "quote": "Example Cloud now expects capital expenditures of $18 billion to $22 billion for fiscal 2026, raised from prior guidance."},
            {"metric": "power_active_mw", "value": 1200, "unit": "MW", "period": "as of September 30, 2026",
             "as_of_date": "2026-09-30", "confidence": 0.9,
             "quote": "As of September 30, 2026, active power was approximately 1.2 GW"},
            {"metric": "power_contracted_mw", "value": 2900, "unit": "MW", "period": "as of September 30, 2026",
             "confidence": 0.9, "quote": "contracted power was 2,900 MW"},
            {"metric": "gpus_deployed", "value": 250000, "unit": "GPU_count", "period": "current", "confidence": 0.9,
             "quote": "We operate more than 250,000 GPUs across 30 data centers."},
            {"metric": "backlog_usd", "value": 55.6, "unit": "USD_billion", "period": "Q3 2026", "confidence": 0.9,
             "quote": "Revenue backlog | 55.6 | billion"},
            {"metric": "customer_contract", "value": 6.3, "unit": "USD_billion", "period": "2026", "confidence": 0.9,
             "counterparty": "Example AI Lab", "term_years": 5,
             "quote": "The company signed a five-year agreement with Example AI Lab valued at approximately $6.3 billion."},
            {"metric": "financing", "value": 4.0, "unit": "USD_billion", "period": "2026", "confidence": 0.6,
             "quote": "The company closed a $4 billion delayed draw term loan."},  # not in the text
            {"metric": "power_pipeline_mw", "value": 5000, "unit": "MW", "period": "current", "confidence": 0.5,
             "quote": "contracted power was 2,900 MW"},  # right quote, wrong number
            {"metric": "not_a_metric", "value": 1, "unit": "MW", "period": "x", "quote": "x", "confidence": 1},
        ]
        block = SimpleNamespace(type="tool_use", name="record_facts", input={"facts": facts})
        return SimpleNamespace(content=[block])


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "COMPANIES", [config.Company("EXCL", "Example Cloud", "Neocloud")])
    client = edgar.EdgarClient(user_agent="Test Runner test@example.com", cache_dir=tmp_path / "cache",
                               fetch=fake_sec, max_rps=1000)
    store = Store(tmp_path / "t.duckdb")
    yield client, store
    store.close()


# ---------------------------------------------------------------------------


def test_user_agent_required(tmp_path):
    with pytest.raises(edgar.EdgarError):
        edgar.EdgarClient(user_agent="no contact", cache_dir=tmp_path)


def test_html_to_text_keeps_sentences_and_table_rows():
    text = edgar.html_to_text(PRESS_RELEASE)
    assert "capital expenditures of $18 billion to $22 billion" in text
    assert "Revenue backlog | 55.6 | billion" in text


def test_index_parsing_and_document_selection():
    docs = edgar.parse_index(INDEX_HTML, CIK, ACC)
    assert [d["type"] for d in docs] == ["8-K", "EX-99.1", "GRAPHIC"]
    chosen = edgar.select_documents(docs, "8-K", "form8k.htm")
    assert [d["document"] for d in chosen] == ["form8k.htm", "ex99-1.htm"]
    assert chosen[1]["url"].endswith(f"/{CIK}/{ACC.replace('-', '')}/ex99-1.htm")


def test_filings_filter_by_date_and_8k_item(env):
    client, _ = env
    f = client.filings(CIK, ["8-K"], "2024-01-01")
    assert [x["accession"] for x in f] == [ACC]  # 5.07 vote and the 2023 filing are dropped


def test_xbrl_quarters_are_derived_from_year_to_date():
    q = xbrl.company_quarterly(companyfacts(), "EXCL")
    capex = q[q["metric"] == "capex"].set_index("quarter")["value_usd_billion"]
    assert capex.to_dict() == pytest.approx({"2025Q1": 2.0, "2025Q2": 2.5, "2025Q3": 3.0, "2025Q4": 3.5})
    methods = q[q["metric"] == "capex"].set_index("quarter")["method"]
    assert methods["2025Q3"] == "reported" and methods["2025Q4"] == "derived"
    ocf = q[q["metric"] == "operating_cash_flow"].set_index("quarter")["value_usd_billion"]
    assert ocf["2025Q2"] == pytest.approx(1.2)


def test_relevant_chunks_skip_boilerplate():
    text = edgar.html_to_text(PRESS_RELEASE)
    chunks = extract.relevant_chunks(text)
    joined = "\n".join(chunks)
    assert "capital expenditures" in joined and "GPUs" in joined
    assert len(chunks) == 1


@pytest.mark.parametrize(
    "quote,unit,value,ok",
    [
        ("capex of $18 billion to $22 billion", "USD_billion", 18, True),
        ("approximately 1.2 GW", "MW", 1200, True),
        ("2,900 MW", "MW", 2900, True),
        ("$850 million", "USD_billion", 0.85, True),
        ("Revenue backlog | 55,600 |", "USD_billion", 55.6, True),  # table in millions
        ("more than 250,000 GPUs", "GPU_count", 250000, True),
        ("2,900 MW", "MW", 5000, False),
    ],
)
def test_value_grounding(quote, unit, value, ok):
    assert extract.value_match({"quote": quote, "unit": unit, "value": value}) is ok


def test_range_midpoint_is_accepted_when_bounds_are_in_quote():
    f = {"quote": "$18 billion to $22 billion", "unit": "USD_billion", "value": 20, "value_low": 18, "value_high": 22}
    assert extract.value_match(f)


def test_quote_matching():
    src = "Example Cloud now expects capital expenditures of $18 billion to $22 billion for fiscal 2026."
    assert extract.quote_match("expects capital expenditures of $18 billion", src) == "exact"
    assert extract.quote_match("Example Cloud now expects capital expenditures of $18 billion to $22 billion in fiscal 2026", src) == "fuzzy"
    assert extract.quote_match("The company closed a $4 billion delayed draw term loan.", src) == "missing"


def test_extract_document_flags_hallucinations():
    text = edgar.html_to_text(PRESS_RELEASE)
    meta = {"company": "Example Cloud", "ticker": "EXCL", "form": "8-K", "filing_date": "2026-11-03", "doc_id": "d1"}
    facts = {f["metric"]: f for f in extract.extract_document(FakeClaude(), meta, text)}
    assert "not_a_metric" not in facts
    for m in ("capex_guidance", "power_active_mw", "power_contracted_mw", "gpus_deployed", "backlog_usd",
              "customer_contract"):
        assert facts[m]["status"] == "verified", (m, facts[m])
    assert facts["financing"]["status"] == "unverified"  # quote not in the filing
    assert facts["power_pipeline_mw"]["status"] == "partial"  # quote real, number not in it


def test_end_to_end_pipeline_and_evaluation(env, tmp_path, monkeypatch):
    client, store = env
    args = SimpleNamespace(tickers=None, since="2024-01-01", limit=None, model="test-model", redo=False, dry_run=False)
    pipeline.cmd_companies(store, client, args)
    pipeline.cmd_xbrl(store, client, args)
    pipeline.cmd_filings(store, client, args)
    assert len(store.table("documents")) == 2
    fake = FakeClaude()
    monkeypatch.setattr(extract, "make_client", lambda: fake)
    pipeline.cmd_extract(store, args)
    facts = store.table("facts")
    assert len(facts) == 8 and fake.calls == 1  # the short 8-K cover page has no relevant text
    pipeline.cmd_extract(store, args)  # nothing left to do
    assert fake.calls == 1

    sheet = evaluate.make_review_sheet(store, 5, tmp_path / "review.csv")
    df = pd.read_csv(sheet, dtype=str)
    df["verdict"] = ["incorrect" if m in ("financing", "power_pipeline_mw") else "correct" for m in df["metric"]]
    miss = {c: "" for c in df.columns}
    miss.update({"doc_id": df["doc_id"].iloc[0], "metric": "capex_reported", "value": "3.0", "verdict": "missed"})
    df = pd.concat([df, pd.DataFrame([miss])], ignore_index=True)
    df.loc[df.index[-1], "fact_id"] = None
    df.to_csv(sheet, index=False)
    counts = evaluate.import_reviews(store, sheet)
    assert counts == {"verdicts": 8, "missed": 1, "documents": 1}
    s = evaluate.scores(store.table("facts"), store.table("reviews"), store.table("misses")).set_index("metric")
    assert s.loc["all", "precision"] == pytest.approx(6 / 8)
    assert s.loc["all", "recall"] == pytest.approx(6 / 7)
    assert s.loc["capex_guidance", "precision"] == 1.0


def test_requests_are_cached(env):
    client, _ = env
    calls = []

    def counting(url, headers):
        calls.append(url)
        return fake_sec(url, headers)

    client._fetch = counting
    client.document_text("https://www.sec.gov/Archives/edgar/data/1/2/ex99-1.htm")
    client.document_text("https://www.sec.gov/Archives/edgar/data/1/2/ex99-1.htm")
    assert len(calls) == 1
