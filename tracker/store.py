"""DuckDB storage."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    ticker VARCHAR PRIMARY KEY, name VARCHAR, grp VARCHAR, cik BIGINT
);
CREATE TABLE IF NOT EXISTS filings (
    accession VARCHAR PRIMARY KEY, ticker VARCHAR, cik BIGINT, form VARCHAR, filing_date DATE,
    report_date DATE, items VARCHAR, url VARCHAR
);
CREATE TABLE IF NOT EXISTS documents (
    doc_id VARCHAR PRIMARY KEY, accession VARCHAR, ticker VARCHAR, document VARCHAR, doc_type VARCHAR,
    description VARCHAR, url VARCHAR, text VARCHAR, chars INTEGER, relevant_chars INTEGER,
    extracted_at TIMESTAMP, model VARCHAR
);
CREATE TABLE IF NOT EXISTS xbrl_quarterly (
    ticker VARCHAR, metric VARCHAR, quarter VARCHAR, start DATE, "end" DATE, value_usd_billion DOUBLE,
    method VARCHAR, concept VARCHAR, PRIMARY KEY (ticker, metric, "end")
);
CREATE TABLE IF NOT EXISTS facts (
    fact_id VARCHAR PRIMARY KEY, doc_id VARCHAR, accession VARCHAR, ticker VARCHAR, filing_date DATE,
    form VARCHAR, url VARCHAR, metric VARCHAR, value DOUBLE, value_low DOUBLE, value_high DOUBLE,
    unit VARCHAR, period VARCHAR, as_of_date VARCHAR, change VARCHAR, counterparty VARCHAR, site VARCHAR,
    instrument VARCHAR, term_years DOUBLE, quote VARCHAR, confidence DOUBLE, quote_match VARCHAR,
    value_in_quote BOOLEAN, status VARCHAR, model VARCHAR, extracted_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS reviews (
    fact_id VARCHAR PRIMARY KEY, doc_id VARCHAR, verdict VARCHAR, corrected_value DOUBLE, note VARCHAR,
    reviewed_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS misses (
    doc_id VARCHAR, metric VARCHAR, value DOUBLE, unit VARCHAR, period VARCHAR, quote VARCHAR, note VARCHAR,
    reviewed_at TIMESTAMP
);
CREATE TABLE IF NOT EXISTS review_docs (
    doc_id VARCHAR PRIMARY KEY, reviewed_at TIMESTAMP
);
"""

TABLES = ("companies", "filings", "documents", "xbrl_quarterly", "facts", "reviews", "misses", "review_docs")


class Store:
    def __init__(self, path: Path | str | None = None, read_only: bool = False):
        import duckdb

        self.path = Path(path or config.DB_PATH)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(self.path), read_only=read_only)
        if not read_only:
            self.con.execute(SCHEMA)

    def close(self):
        self.con.close()

    def upsert(self, table: str, df: pd.DataFrame) -> int:
        if df is None or df.empty:
            return 0
        cols = [c[0] for c in self.con.execute(f"SELECT * FROM {table} LIMIT 0").description]
        df = df.reindex(columns=cols)
        self.con.register("_incoming", df)
        quoted = ", ".join(f'"{c}"' for c in cols)
        self.con.execute(f"INSERT OR REPLACE INTO {table} ({quoted}) SELECT {quoted} FROM _incoming")
        self.con.unregister("_incoming")
        return len(df)

    def df(self, sql: str, params: list | None = None) -> pd.DataFrame:
        return self.con.execute(sql, params or []).df()

    def table(self, name: str) -> pd.DataFrame:
        return self.df(f"SELECT * FROM {name}")

    def publish(self, dest: Path) -> Path:
        """Write a copy of every table to ``dest`` without document text, small enough to commit."""
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_suffix(".tmp.duckdb")
        tmp.unlink(missing_ok=True)
        out = Store(tmp)
        try:
            for t in TABLES:
                sql = "SELECT * EXCLUDE (text) FROM documents" if t == "documents" else f"SELECT * FROM {t}"
                out.upsert(t, self.df(sql))
            out.con.execute("CHECKPOINT")
        finally:
            out.close()
        tmp.replace(dest)
        return dest

    def export_csv(self, out_dir: Path) -> list[Path]:
        out_dir.mkdir(parents=True, exist_ok=True)
        paths = []
        for t in TABLES:
            df = self.table(t)
            if t == "documents":
                df = df.drop(columns=["text"])
            p = out_dir / f"{t}.csv"
            df.to_csv(p, index=False)
            paths.append(p)
        return paths
