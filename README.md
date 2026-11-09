# AI Infrastructure Capex Tracker

A dashboard of what hyperscalers, neoclouds and AI infrastructure companies disclose about the AI buildout: capital expenditures, capex guidance, power capacity, GPU counts, revenue backlog, customer contracts and financings.

- **Quarterly capex and operating cash flow** come from SEC XBRL data, with year-to-date figures differenced into quarters.
- **Everything else** is extracted by Claude, into a fixed schema, from 8-K and 6-K filings (press releases and deal announcements) and from the text of 10-Q, 10-K and 20-F reports (lease commitments, capacity, backlog).
- **Every extracted fact is checked.** It must carry a verbatim quote, and the pipeline confirms the quote appears in the filing and the number, with a matching unit, appears in the quote. Facts are marked verified, partial or unverified, and the dashboard shows only verified facts by default.
- **Every number links to its source filing.** Repeated statements of the same number are shown once, dated to the first filing that stated it.
- **Accuracy is measured.** A review workflow samples whole documents for a person to mark up, which gives precision and recall by metric. The Methodology tab shows both once a review has been run.

For what each file does, how the checks work and how to browse the database, see [ARCHITECTURE.md](ARCHITECTURE.md).

## Using the dashboard

| Tab | Use it to |
|---|---|
| **Overview** | See headline capex, guidance revisions and every disclosure first stated in the last 7, 30 or 90 days |
| **Capex** | Compare quarterly capex, the capex scorecard (quarter, year-over-year, last four quarters), capex as a share of operating cash flow and signed data-center leases not yet commenced |
| **Guidance** | Follow each company's capex guidance and whether it was raised, lowered or maintained |
| **Power & capacity** | Compare active, contracted and pipeline MW, GPUs deployed and revenue backlog |
| **Contracts & financing** | Review customer contracts and debt or equity financings, with counterparties and terms |
| **Company profile** | See everything on one company: capex, guidance, capacity, latest disclosures and recent filings |
| **All disclosures** | Search every extracted number by keyword, counterparty or period, and download it |
| **Methodology** | Read how each number is produced and checked, its limitations, coverage and data quality |

- **Filters** in the sidebar apply to every tab: company type, companies, which check statuses to include and the earliest filing date.
- **Hover** over any chart point for the exact quote. The **Source** column in every table opens the SEC filing.
- **Download CSV** buttons export what each table shows.
- **Verified only** (the default) shows facts whose quote was found word for word in the filing and whose number, with its unit, appears in the quote. **Verified and partial** adds facts that passed only one of the two checks.

## Companies tracked

Hyperscalers: Microsoft, Alphabet, Amazon, Meta, Oracle. Neoclouds: CoreWeave, Nebius, IREN. AI infrastructure: Applied Digital, Cipher Mining, TeraWulf, Hut 8, Core Scientific, Galaxy Digital. Edit `COMPANIES` in `tracker/config.py` to change the list.

## Set up (Mac)

```bash
cd gpu-capex-tracker
python3.13 -m venv .venv && source .venv/bin/activate   # Python 3.10+; macOS's built-in python3 is 3.9 (brew install python@3.13)
pip install -r requirements.txt
cp .env.example .env
open -e .env        # add SEC_USER_AGENT (your name and email) and ANTHROPIC_API_KEY
```

Try the dashboard first on fictional demo data:

```bash
streamlit run streamlit_app.py
```

## Load real data

```bash
python -m tracker.pipeline companies              # look up SEC IDs for each ticker
python -m tracker.pipeline xbrl                   # quarterly capex and operating cash flow
python -m tracker.pipeline filings                # 8-K/6-K filings and exhibits, 10-Q/10-K/20-F reports, since 2024
python -m tracker.pipeline extract --dry-run      # how many documents and tokens, no API calls
python -m tracker.pipeline extract --limit 10     # small first run; check the results
python -m tracker.pipeline extract                # everything else
python -m tracker.pipeline status
streamlit run streamlit_app.py                    # now shows real data
```

- **First run time.** Downloads respect SEC's limit of 10 requests a second, so the first `filings` run takes a while. Everything is cached, and later runs only fetch what's new.
- **Cost.** Run `--dry-run` to see the token count before spending anything. Extraction sends only the paragraphs that mention capex, power, GPUs, backlog, contracts or financing.
- **Picking up new filings.** Rerun `filings` then `extract`; only new documents are processed. `--tickers MSFT,CRWV` limits any step to a few companies, and `--redo` re-extracts.
- **Effort and cost.** Extraction runs `claude-sonnet-5-5` at low effort by default (`ANTHROPIC_EFFORT` in `.env`). A full run of about 1,000 press-release documents costs roughly $10; the 140 periodic reports since 2024 cost about $5 more.

## Weekly update

```bash
python -m tracker.pipeline xbrl                   # new quarters
python -m tracker.pipeline filings                # new 8-K, 6-K, 10-Q, 10-K and 20-F filings
python -m tracker.pipeline extract                # only new documents
python -m tracker.pipeline publish                # refresh app_data/tracker.duckdb
git add app_data && git commit -m "Update data" && git push
```

Close the local dashboard before running the pipeline: DuckDB lets only one process write to the database.

## Correcting a fact

The automatic checks prove a number was stated in a filing, not that it was put under the right metric. When a fact is misclassified, add a row to [app_data/corrections.csv](app_data/corrections.csv) with its `fact_id` (shown in a CSV export or the database), `exclude` and a reason. The dashboard drops it everywhere and lists every correction on the Methodology tab.

If the checking rules change, `python -m tracker.pipeline regrade` re-runs them on every stored fact using the saved filing text, with no API calls.

## Measure accuracy

```bash
python -m tracker.pipeline review-sheet --docs 25    # writes data/review/review_sheet.csv
```

Open each document's link and fill in the sheet:

1. Set `verdict` to `correct`, `incorrect` or `unsure` on each row. You can also put the right number in `corrected_value`.
2. For facts the model missed, add a row with `doc_id`, `metric`, `value`, `period` and `quote`, leave `fact_id` empty, and set `verdict` to `missed`.

Then:

```bash
python -m tracker.pipeline import-reviews data/review/review_sheet.csv
python -m tracker.pipeline eval
```

## Publish the dashboard (Streamlit Community Cloud)

The pipeline database in `data/` is large and gitignored. The hosted app reads a slim copy without the full
document text, `app_data/tracker.duckdb`, which is committed to the repo.

1. After a pipeline run, write the snapshot and push it:
   ```bash
   python -m tracker.pipeline publish
   git add app_data/tracker.duckdb && git commit -m "Update data" && git push
   ```
2. First time only: at [share.streamlit.io](https://share.streamlit.io), choose **Create app**, pick this repo and
   branch, set the main file to `streamlit_app.py`, and deploy. No secrets are needed, because the dashboard only
   reads the snapshot.

Each push redeploys the app with the new data.

## How it works

```
SEC EDGAR ──► companies ──► XBRL company facts ──► xbrl_quarterly (capex, operating cash flow)
          └─► 8-K / 6-K filings ──► EX-99 exhibits ──► documents (text)
                                                        │
                       keyword filter (capex, MW, GPUs, backlog, contracts, financing)
                                                        │
                       Claude, forced tool call ──► facts with verbatim quotes
                                                        │
                       grounding: quote in filing? number in quote? ──► verified / partial / unverified
                                                        │
                       review sheet ──► reviews, misses ──► precision and recall
```

| File | What it does |
|---|---|
| `tracker/edgar.py` | SEC client: user agent, rate limit, retries, disk cache, filing index parsing, HTML to text |
| `tracker/xbrl.py` | Quarterly series from mixed 3-month and year-to-date XBRL facts |
| `tracker/extract.py` | Chunking, extraction prompt and tool schema, quote and number grounding |
| `tracker/evaluate.py` | Review sheets, precision and recall by metric |
| `tracker/store.py` | DuckDB tables, and the slim snapshot for the hosted app |
| `tracker/pipeline.py` | Command line |
| `tracker/demo.py` | Fictional demo data |
| `streamlit_app.py` | Dashboard |
| `app_data/tracker.duckdb` | Published snapshot the hosted dashboard reads |
| `tests/` | Tests with mocked SEC responses and a mocked Claude client |

## Known limits

- **Hyperscaler guidance is often oral.** Microsoft, Alphabet and Amazon usually give capex guidance on earnings calls, not in the press release filed with the SEC, so guidance coverage for them will be thin. Earnings call transcripts would be the next source to add.
- **Nebius files as a foreign issuer.** Its results come on 6-K and its XBRL uses IFRS tags; both are handled, but check its numbers first.
- **Capex definitions differ.** Some companies include finance leases or equipment acquired under leases in what they call capex; the XBRL series uses cash paid for property and equipment.
- **Verified isn't the same as correct.** It means the quote and number are really in the filing. The extracted period or metric could still be wrong, which is what human review measures.
