# How the tracker works

This guide explains what each file does and how data moves from SEC filings to the dashboard. For setup and day-to-day commands, see [README.md](README.md).

## Overview

```
SEC EDGAR ──► edgar.py ──► filings + document text ─┐
    │                                                ├─► store.py (DuckDB: data/tracker.duckdb)
    └──► xbrl.py ──► quarterly capex & cash flow ────┤        │
                                                     │        ├─► streamlit_app.py (dashboard)
         extract.py ──► Claude ──► checked facts ────┘        └─► publish ──► app_data/ (hosted app)
                         ▲                                     │
                 pipeline.py runs each step         analytics.py turns tables into dashboard views
                                                    (dedup, guidance revisions, capex series),
                                                    after app_data/corrections.csv is applied
```

There are two kinds of data:

- **Quarterly capex and operating cash flow** come from the SEC's structured XBRL data. These numbers are exact, and no model is involved.
- **Everything else** (guidance, power, GPUs, backlog, contracts, financings) is extracted from 8-K and 6-K press releases by Claude, then checked against the source text.

## Files

```
gpu-capex-tracker/
├── streamlit_app.py       Dashboard
├── tracker/
│   ├── config.py          Settings, company list, metric definitions
│   ├── edgar.py           SEC EDGAR client: download, cache, parse
│   ├── xbrl.py            Quarterly capex from XBRL data
│   ├── extract.py         Claude extraction and the checks against the source
│   ├── store.py           DuckDB tables, publish and export
│   ├── analytics.py       Derived views for the dashboard (pure pandas)
│   ├── pipeline.py        Command line entry point
│   ├── evaluate.py        Human review and precision/recall
│   └── demo.py            Fictional demo database
├── tests/
│   ├── test_pipeline.py   Pipeline tests (fake SEC and Claude responses)
│   └── test_analytics.py  Derived views, unit checks, and a render of every dashboard tab
├── .streamlit/config.toml Dashboard theme
├── data/                  Full database and download cache (gitignored)
└── app_data/              Slim database and corrections.csv for the hosted dashboard (committed)
```

### `tracker/config.py`: settings

Every other module reads from this file.

- **Paths.** `data/tracker.duckdb` is the full pipeline database. `app_data/tracker.duckdb` is a slim copy committed to git for the hosted dashboard.
- **`.env` loader.** A small built-in loader reads `ANTHROPIC_API_KEY`, `SEC_USER_AGENT` and the optional overrides below, so no extra package is needed.
- **`COMPANIES`.** The 14 tracked tickers, each tagged `Hyperscaler`, `Neocloud` or `AI infrastructure`. Edit this list to change coverage.
- **`METRICS`.** The nine fact types the model looks for, each with a fixed unit (`USD_billion`, `MW` or `GPU_count`) so values compare across companies.
- **Environment overrides:**

| Variable | Default | Purpose |
|---|---|---|
| `ANTHROPIC_MODEL` | `claude-sonnet-5-5` | Extraction model |
| `ANTHROPIC_EFFORT` | `low` | Thinking effort: `low` to `max`. Higher costs more. |
| `TRACKER_START_DATE` | `2024-01-01` | Earliest filing date |
| `TRACKER_DATA_DIR` | `data/` | Where the database and cache live |

### `tracker/edgar.py`: SEC EDGAR client

Follows the SEC's rules for automated access.

- **`EdgarClient.get()`** is the single place requests are made. It:
  - refuses to run unless `SEC_USER_AGENT` contains an email address, because the SEC blocks requests without one;
  - stays at 8 requests per second, under the SEC's limit of 10;
  - caches every response on disk in `data/cache/`. Filing documents never change, so they are cached permanently. Filing lists and XBRL data refresh after 12 hours;
  - retries 429 and 5xx responses with increasing waits.
- **`filings()`** lists 8-K and 6-K filings since the start date. It drops 8-Ks whose items are all irrelevant and keeps items 1.01 (material agreements), 2.02 (results), 2.03 (debt), 7.01 and 8.01.
- **`parse_index()` and `select_documents()`** read each filing's index page and keep the press-release exhibits (EX-99.x) and the primary document.
- **`html_to_text()`** turns SEC HTML into plain text. Table rows become `cell | cell | cell` lines so numbers keep their labels.

### `tracker/xbrl.py`: quarterly capex and cash flow

Uses the SEC's XBRL "company facts" API, which carries the tagged financial statements.

- **The problem.** Cash flow statements in 10-Qs report year-to-date totals (three, six and nine months), not single quarters.
- **`quarterly_series()`** gets single quarters back. A value that covers about one quarter (80 to 100 days) is used as reported. Otherwise it subtracts consecutive year-to-date values that share a start date: Q2 = first half − Q1, and Q4 = full year − nine months. Each row is marked `method = reported` or `method = derived`.
- **`CONCEPTS`** lists the tag names companies use for capex. The tag with the most recent data is used. **`FALLBACK_CONCEPTS`** is tried only when none of those exist (Hut 8 tags capex as `PaymentsToAcquireMachineryAndEquipment`).
- Nebius files an annual 20-F, so it has no quarterly XBRL. The dashboard fills its quarters from press releases (see `analytics.py`).
- When a period has been restated, the latest filing's value wins.

### `tracker/extract.py`: Claude extraction

Each document goes through three stages.

1. **`relevant_chunks()`** filters the text. A keyword pattern (capex, MW, GPU, backlog, term loan and so on) selects matching paragraphs plus one paragraph on each side, grouped into chunks of up to 18,000 characters. Boilerplate never reaches the API, which keeps cost down.
2. **`call_model()`** sends each chunk to Claude. `SYSTEM_PROMPT` sets the rules: canonical units, ranges as low/high/midpoint, a verbatim quote of at most 300 characters, and never guessing. Claude replies by calling the `record_facts` tool, whose strict JSON schema fixes the output format. The system prompt is cached, so repeated calls cost less.
3. **`ground()`** checks each fact against the source and gives it a status:
   - `quote_match()` checks the quote appears in the filing. It returns `exact` for a word-for-word match, `fuzzy` if at least 70% of the quote's three-word sequences appear, and `missing` otherwise.
   - `value_match()` checks the number appears in the quote **with a compatible unit**. It handles scaling ("$1.2 billion" = 1.2, "1.2 GW" = 1,200 MW) and accepts a range midpoint when both ends appear. A number carrying a unit of another kind never matches ("800 MW" cannot support an $800B contract). A number with no unit word takes its scale from the rest of the quote ("$18 to $22 billion"), or is read as millions in a table row, never as billions unless the quote says "billion". PDF artefacts such as "bil-lion" and "$3.5Bn³" are tolerated.

| Status | Meaning |
|---|---|
| `verified` | The quote matches the filing exactly and the number is in the quote |
| `partial` | One of the two checks passed |
| `unverified` | Neither check passed |

`clean_fact()` sets the correct unit and fills in missing midpoints. `fact_id()` hashes each fact so duplicates across chunks are dropped.

Current models (including `claude-sonnet-5-5`) reject a forced `tool_choice`. The call uses `tool_choice: auto`, a strict tool schema and a prompt instruction to call the tool.

### `tracker/store.py`: DuckDB storage

| Table | Contents |
|---|---|
| `companies` | Ticker, name, group, SEC CIK |
| `filings` | One row per 8-K or 6-K |
| `documents` | Each exhibit's full text, plus when and with which model it was extracted |
| `xbrl_quarterly` | Quarterly capex and operating cash flow, in USD billions |
| `facts` | Extracted facts with quote, check status and confidence |
| `reviews`, `misses`, `review_docs` | Human review results |

- **`upsert()`** inserts or replaces rows, so rerunning any step is safe.
- **`publish()`** copies every table except document text into `app_data/tracker.duckdb`, small enough to commit.
- **`export_csv()`** writes one CSV per table to `data/export/`.

### `tracker/pipeline.py`: command line

Each `python -m tracker.pipeline <command>` maps to one function.

| Command | What it does |
|---|---|
| `companies` | Looks up each ticker's SEC CIK |
| `xbrl` | Loads quarterly capex and operating cash flow |
| `filings` | Downloads filings and exhibits and stores their text |
| `extract` | Runs Claude on documents not yet extracted |
| `run` | All of the above, in order |
| `regrade` | Re-runs the source checks on every stored fact against its saved text. No API calls |
| `status` | Row counts and extraction coverage by ticker |
| `review-sheet`, `import-reviews`, `eval` | Human review workflow |
| `publish` | Writes the slim database to `app_data/` |
| `export-csv` | Writes one CSV per table |
| `demo` | Builds the fictional demo database |

Options: `--tickers MSFT,CRWV`, `--limit N`, `--model`, `--redo`, `--dry-run`, `--since`, `--db`.

`extract` only processes documents with no `extracted_at` time, so an interrupted run resumes where it stopped. A document that fails is logged and retried on the next run.

### `tracker/evaluate.py`: measuring accuracy

The automatic checks catch made-up quotes and numbers, but not facts the model missed. A human review loop measures both:

1. `review-sheet` samples about 25 whole documents (at least one per metric) into a CSV.
2. A reviewer marks each fact `correct`, `incorrect` or `unsure`, and adds rows marked `missed` for facts the model skipped.
3. `scores()` computes precision (the share of extracted facts that are right) and recall (the share of real facts that were found) for each metric. The dashboard's Data quality tab shows them.

### `tracker/analytics.py`: dashboard views

Pure pandas functions, tested in `tests/test_analytics.py`.

- **`disclosures()`** merges repeated facts. Companies restate the same number in the release, the deck and later releases, so each distinct (company, metric, value, period, counterparty, site) is kept once, with `first_stated`, `last_stated` and `mentions`. Contracts and financings with the same company and amount stated within 180 days are treated as one deal, because deals are restated in different words.
- **`period_label()`** normalizes periods ("FY2026", "fiscal 2026", "2026" → "2026"; "FY27 Q1" → "Q1 2027"). Open-ended periods ("over the next several years") return `None`.
- **`guidance_history()`** orders each company's guidance for a period and labels every statement `initial`, `raised`, `lowered` or `maintained` from the numbers. A narrowed range counts as raised if its midpoint or an end moved up. The company's own wording is kept separately.
- **`capex_quarterly()`** returns XBRL quarters plus press-release quarters for companies with no quarterly XBRL, with a `source` column.
- **`capex_summary()` / `ttm_total()`** compute latest quarter, quarter-on-quarter, year-over-year and last four quarters. Four-quarter totals require four consecutive quarters.
- **`latest_values()` / `metric_series()`** pick company-level figures (no site named; the largest value when a filing gives a total and its parts).
- **`apply_corrections()`** drops facts listed in `app_data/corrections.csv`.

### `app_data/corrections.csv`: manual corrections

The checks prove a number was stated, not that it was classified correctly. A fact judged misclassified is listed here with `fact_id`, `action` (`exclude`), `reason` and `date`. The dashboard drops it everywhere and shows the list on the Methodology tab.

### `streamlit_app.py`: dashboard

- **Data source (`pick_source`).** Uses `TRACKER_DB` if set, then `data/tracker.duckdb`, then `app_data/tracker.duckdb`, then the fictional demo database, which shows a warning banner.
- **Sidebar filters:** company type, companies (empty means all), which check statuses to include (verified only by default) and a start date. Filters apply to every tab.
- **Tabs:**
  1. **Overview:** headline numbers with explanations on hover, quarterly capex by company, the latest guidance per company, and *What's new*, every disclosure first stated in the last 7, 30 or 90 days.
  2. **Capex:** quarterly capex for hyperscalers and smaller companies on separate scales, a scorecard (latest quarter, change on prior quarter and year, last four quarters, source) and capex ÷ operating cash flow.
  3. **Guidance:** guidance statements over time with ranges and revision markers, and a revision log.
  4. **Power & capacity:** latest active, contracted and (optionally) pipeline MW for neoclouds and AI infrastructure, one company's history, GPUs deployed and revenue backlog.
  5. **Contracts & financing:** counts and the largest deal over 12 months (values are not summed, because deals are often "up to" amounts), bubble charts and a deal log.
  6. **Company profile:** one company's capex, guidance, capacity, latest disclosure per metric and recent filings.
  7. **All disclosures:** searchable list of every disclosure, or every mention, with CSV export including `fact_id`.
  8. **Methodology:** how the data is built and checked, metric definitions, limitations, data quality, coverage by company and the manual corrections.
- Every chart and table states its source and method beneath it, and every table links to the filing.

### `tracker/demo.py` and the tests

- **`demo.py`** generates fictional companies, numbers and quotes so the dashboard can be tried before the pipeline runs.
- **`test_pipeline.py`** uses fake SEC and Claude responses, so it needs no network or API key. It covers HTML parsing, year-to-date differencing, the checks against the source (the fake Claude returns a made-up quote and a wrong number on purpose) and an end-to-end pipeline run.
- **`test_analytics.py`** covers period parsing, deduplication, deal merging, guidance revisions, the Nebius capex fill-in, consecutive-quarter totals, corrections, the unit-aware number check (with real cases that were wrongly verified before), and renders every dashboard tab and company profile on demo data.
- Run everything with `.venv/bin/python -m pytest -q`.

## Viewing the database

DuckDB lets only one process hold the file open for writing. **Close other connections before running the pipeline, and open the database read-only when you're only looking.** While `extract` is running, `data/tracker.duckdb` is locked; open `app_data/tracker.duckdb` instead.

**Browser UI.** Install the DuckDB command-line tool, then open the file:

```bash
brew install duckdb
duckdb -ui data/tracker.duckdb
```

**Python, with no extra install:**

```bash
.venv/bin/python -i -c "import duckdb; con = duckdb.connect('data/tracker.duckdb', read_only=True)"
>>> con.sql("SHOW TABLES").show()
```

**CSV:** `python -m tracker.pipeline export-csv` writes every table to `data/export/`.

Useful queries:

```sql
-- Facts by company
SELECT ticker, COUNT(*) facts, SUM(status = 'verified') verified
FROM facts GROUP BY ticker ORDER BY facts DESC;

-- Latest capex guidance
SELECT ticker, period, value_low, value_high, change, filing_date
FROM facts WHERE metric = 'capex_guidance' ORDER BY filing_date DESC;

-- Facts that failed the checks
SELECT ticker, metric, value, quote FROM facts WHERE status = 'unverified';

-- Quarterly capex
SELECT ticker, quarter, ROUND(value_usd_billion, 2) capex_b
FROM xbrl_quarterly WHERE metric = 'capex' ORDER BY ticker, quarter;
```

## Known limitations

- **Earnings calls are not covered.** Much capex guidance (Microsoft especially) is given only on calls, which are not SEC filings.
- **10-Q and 10-K text is not read**, only their XBRL numbers. Some capacity, lease and commitment detail lives there.
- **Classification is not checked automatically.** A real number put under the wrong metric or period passes the checks. Human review (`evaluate.py`) measures this, and `corrections.csv` removes known cases.
- **Definitions differ by company.** "Contracted MW" may be gross or critical IT load, and hyperscaler backlog is company-wide RPO.
