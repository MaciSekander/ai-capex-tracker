"""Command line pipeline.

    python -m tracker.pipeline companies          resolve tickers to SEC CIKs
    python -m tracker.pipeline xbrl               quarterly capex and operating cash flow
    python -m tracker.pipeline filings            list 8-K/6-K filings and download exhibits
    python -m tracker.pipeline extract            run LLM extraction on new documents
    python -m tracker.pipeline run                all of the above, in order
    python -m tracker.pipeline regrade            re-run the source checks on stored facts (no API calls)
    python -m tracker.pipeline status             row counts and extraction coverage
    python -m tracker.pipeline review-sheet       sample documents for human review
    python -m tracker.pipeline import-reviews F   load a filled review sheet
    python -m tracker.pipeline eval               precision / recall by metric
    python -m tracker.pipeline publish            slim database copy in app_data/ for the hosted dashboard
    python -m tracker.pipeline export-csv         CSVs of every table
    python -m tracker.pipeline demo               build the fictional demo database

Common options: --tickers MSFT,CRWV  --limit 20  --model claude-sonnet-5-5  --db path
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from . import config, evaluate, extract, xbrl
from .edgar import EdgarClient, EdgarError, select_documents
from .store import Store


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def selected_companies(args) -> list[config.Company]:
    if not args.tickers:
        return config.COMPANIES
    wanted = {t.strip().upper() for t in args.tickers.split(",")}
    return [c for c in config.COMPANIES if c.ticker in wanted]


def cmd_companies(store: Store, client: EdgarClient, args) -> None:
    tickers = client.ticker_map()
    rows = []
    for c in selected_companies(args):
        if c.ticker not in tickers:
            log(f"  {c.ticker}: not in SEC ticker list, skipped")
            continue
        cik, title = tickers[c.ticker]
        rows.append({"ticker": c.ticker, "name": c.name, "grp": c.group, "cik": cik})
        log(f"  {c.ticker}: CIK {cik} ({title})")
    store.upsert("companies", pd.DataFrame(rows))


def companies_df(store: Store, args) -> pd.DataFrame:
    df = store.table("companies")
    if args.tickers:
        df = df[df["ticker"].isin([t.strip().upper() for t in args.tickers.split(",")])]
    if df.empty:
        raise SystemExit("No companies resolved yet. Run: python -m tracker.pipeline companies")
    return df


def cmd_xbrl(store: Store, client: EdgarClient, args) -> None:
    for _, c in companies_df(store, args).iterrows():
        try:
            q = xbrl.company_quarterly(client.company_facts(int(c["cik"])), c["ticker"])
        except EdgarError as exc:
            log(f"  {c['ticker']}: {exc}")
            continue
        n = store.upsert("xbrl_quarterly", q)
        capex = q[q["metric"] == "capex"]
        latest = capex.iloc[-1] if len(capex) else None
        log(f"  {c['ticker']}: {n} quarterly values" + (
            f", latest capex {latest['quarter']} ${latest['value_usd_billion']:.2f}B" if latest is not None else ""))


def cmd_filings(store: Store, client: EdgarClient, args) -> None:
    have = set(store.df("SELECT doc_id FROM documents")["doc_id"])
    for _, c in companies_df(store, args).iterrows():
        cik = int(c["cik"])
        filings = client.filings(cik, config.FILING_FORMS, args.since or config.START_DATE)
        store.upsert("filings", pd.DataFrame([{**f, "ticker": c["ticker"]} for f in filings]))
        new_docs = 0
        for f in filings:
            try:
                docs = select_documents(client.filing_documents(cik, f["accession"]), f["form"], f["primary_document"])
            except EdgarError as exc:
                log(f"  {c['ticker']} {f['accession']}: {exc}")
                continue
            rows = []
            for d in docs:
                doc_id = f"{f['accession']}/{d['document']}"
                if doc_id in have:
                    continue
                text = client.document_text(d["url"])
                relevant = sum(len(ch) for ch in extract.relevant_chunks(text))
                rows.append({
                    "doc_id": doc_id, "accession": f["accession"], "ticker": c["ticker"], "document": d["document"],
                    "doc_type": d["type"], "description": d["description"], "url": d["url"], "text": text,
                    "chars": len(text), "relevant_chars": relevant, "extracted_at": None, "model": None,
                })
            new_docs += store.upsert("documents", pd.DataFrame(rows))
        log(f"  {c['ticker']}: {len(filings)} filings, {new_docs} new documents")


def cmd_extract(store: Store, args) -> None:
    where = "d.relevant_chars > 0" + ("" if args.redo else " AND d.extracted_at IS NULL")
    if args.tickers:
        tick = ",".join(f"'{t.strip().upper()}'" for t in args.tickers.split(","))
        where += f" AND d.ticker IN ({tick})"
    todo = store.df(
        f"""SELECT d.doc_id, d.accession, d.ticker, d.doc_type, d.description, d.url, d.text, d.relevant_chars,
                   f.form, f.filing_date, c.name AS company
            FROM documents d JOIN filings f USING (accession) JOIN companies c ON c.ticker = d.ticker
            WHERE {where} ORDER BY f.filing_date DESC"""
    )
    if args.limit:
        todo = todo.head(args.limit)
    tokens = int(todo["relevant_chars"].sum() / 4)
    log(f"{len(todo)} documents to extract, about {tokens:,} input tokens of filing text")
    if args.dry_run or todo.empty:
        return
    client = extract.make_client()
    model = args.model or config.ANTHROPIC_MODEL
    total = 0
    for i, d in enumerate(todo.itertuples(), 1):
        meta = {"company": d.company, "ticker": d.ticker, "form": d.form, "filing_date": str(d.filing_date)[:10],
                "doc_type": d.doc_type, "description": d.description, "doc_id": d.doc_id}
        try:
            facts = extract.extract_document(client, meta, d.text, model)
        except Exception as exc:
            log(f"  [{i}/{len(todo)}] {d.ticker} {d.doc_id}: failed ({exc}); will retry next run")
            continue
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        rows = [{**f, "doc_id": d.doc_id, "accession": d.accession, "ticker": d.ticker, "filing_date": d.filing_date,
                 "form": d.form, "url": d.url, "model": model, "extracted_at": now} for f in facts]
        store.con.execute("DELETE FROM facts WHERE doc_id = ?", [d.doc_id])
        store.upsert("facts", pd.DataFrame(rows))
        store.con.execute("UPDATE documents SET extracted_at = ?, model = ? WHERE doc_id = ?", [now, model, d.doc_id])
        total += len(facts)
        verified = sum(f["status"] == "verified" for f in facts)
        log(f"  [{i}/{len(todo)}] {d.ticker} {d.doc_id}: {len(facts)} facts, {verified} verified")
    log(f"Done: {total} facts")


def cmd_regrade(store: Store, args) -> None:
    """Re-run the source checks on every stored fact against its document text. No API calls."""
    facts = store.table("facts")
    if facts.empty:
        log("No facts to regrade.")
        return
    texts = store.df("SELECT doc_id, text FROM documents WHERE doc_id IN (SELECT DISTINCT doc_id FROM facts)")
    text_by_doc = dict(zip(texts["doc_id"], texts["text"]))
    before = facts["status"].value_counts().to_dict()
    rows = []
    for f in facts.to_dict("records"):
        source = text_by_doc.get(f["doc_id"])
        if source is None:
            rows.append(f)
            continue
        g = extract.ground({k: (None if v != v else v) if isinstance(v, float) else v for k, v in f.items()}, source)
        rows.append({**f, "quote_match": g["quote_match"], "value_in_quote": g["value_in_quote"], "status": g["status"]})
    out = pd.DataFrame(rows)
    store.upsert("facts", out)
    after = out["status"].value_counts().to_dict()
    changed = int((out["status"].values != facts["status"].values).sum())
    log(f"Regraded {len(out):,} facts; {changed:,} changed status. Before {before}, after {after}")


def cmd_status(store: Store, args) -> None:
    for t in ("companies", "filings", "documents", "xbrl_quarterly", "facts", "reviews", "misses"):
        log(f"{t:15s} {store.df(f'SELECT COUNT(*) n FROM {t}')['n'][0]:>7,}")
    cov = store.df(
        """SELECT ticker, COUNT(*) docs, COUNT(extracted_at) extracted, SUM(relevant_chars > 0)::INT relevant
           FROM documents GROUP BY ticker ORDER BY ticker"""
    )
    if not cov.empty:
        print(cov.to_string(index=False))
    st = store.df("SELECT status, COUNT(*) n FROM facts GROUP BY status ORDER BY n DESC")
    if not st.empty:
        print(st.to_string(index=False))


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="python -m tracker.pipeline", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("command", choices=["companies", "xbrl", "filings", "extract", "run", "regrade", "status", "review-sheet",
                                       "import-reviews", "eval", "publish", "export-csv", "demo"])
    p.add_argument("path", nargs="?", help="review sheet path for import-reviews")
    p.add_argument("--tickers", help="comma-separated subset, e.g. MSFT,CRWV")
    p.add_argument("--since", help=f"earliest filing date (default {config.START_DATE})")
    p.add_argument("--limit", type=int, help="max documents to extract this run")
    p.add_argument("--model", help=f"Anthropic model (default {config.ANTHROPIC_MODEL})")
    p.add_argument("--redo", action="store_true", help="re-extract documents already done")
    p.add_argument("--dry-run", action="store_true", help="count documents and tokens without calling the API")
    p.add_argument("--docs", type=int, default=25, help="documents in the review sheet")
    p.add_argument("--db", help="DuckDB path (default data/tracker.duckdb)")
    args = p.parse_args(argv)

    if args.command == "demo":
        from .demo import build_demo

        path = build_demo(Path(args.db) if args.db else config.DATA_DIR / "demo.duckdb")
        log(f"Demo database written to {path}")
        return

    store = Store(args.db)
    try:
        if args.command in ("companies", "xbrl", "filings", "run"):
            client = EdgarClient()
            if args.command in ("companies", "run"):
                log("Resolving companies"); cmd_companies(store, client, args)
            if args.command in ("xbrl", "run"):
                log("Quarterly capex and cash flow (XBRL)"); cmd_xbrl(store, client, args)
            if args.command in ("filings", "run"):
                log("Filings and exhibits"); cmd_filings(store, client, args)
            if args.command == "run":
                log("LLM extraction"); cmd_extract(store, args)
        elif args.command == "extract":
            cmd_extract(store, args)
        elif args.command == "regrade":
            cmd_regrade(store, args)
        elif args.command == "status":
            cmd_status(store, args)
        elif args.command == "review-sheet":
            out = evaluate.make_review_sheet(store, args.docs, config.DATA_DIR / "review" / "review_sheet.csv")
            log(f"Review sheet: {out}")
        elif args.command == "import-reviews":
            if not args.path:
                raise SystemExit("Give the path to the filled review sheet.")
            log(str(evaluate.import_reviews(store, Path(args.path))))
        elif args.command == "eval":
            s = evaluate.scores(store.table("facts"), store.table("reviews"), store.table("misses"))
            with pd.option_context("display.float_format", "{:.2f}".format, "display.width", 140):
                print(s.to_string(index=False))
        elif args.command == "publish":
            out = store.publish(config.PUBLISHED_DB_PATH)
            log(f"Published {out} ({out.stat().st_size / 1e6:.1f} MB). Commit and push it to update the live app.")
        elif args.command == "export-csv":
            for path in store.export_csv(config.DATA_DIR / "export"):
                log(f"  {path}")
    except EdgarError as exc:
        raise SystemExit(f"SEC EDGAR error: {exc}")
    finally:
        store.close()


if __name__ == "__main__":
    main(sys.argv[1:])
