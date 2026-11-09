"""Earnings call transcripts: a local folder you fill, plus Microsoft's published transcripts.

Most investor-relations sites block automated downloads, so transcripts are read from

    data/transcripts/<TICKER>/<YYYY-MM-DD>[ any text].pdf | .txt | .html | .htm | .docx

where the date is the day of the call. An optional ``sources.csv`` in the same folder, with columns
``file,url``, gives the public link shown in the dashboard; without it the company's investor site is used.

Microsoft publishes transcripts as Word documents at stable links (aka.ms/transcriptfy26q1), which
``fetch_microsoft`` downloads into the same folder.

Each transcript becomes a ``filings`` row (form ``Transcript``) and a ``documents`` row, so extraction,
checking and the dashboard treat it like any other source.
"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

from . import config
from .edgar import html_to_text

TRANSCRIPT_DIR = config.DATA_DIR / "transcripts"
FORM = "Transcript"
SUFFIXES = (".pdf", ".txt", ".html", ".htm", ".docx")
BROWSER_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"

IR_PAGES = {
    "MSFT": "https://www.microsoft.com/en-us/investor",
    "GOOGL": "https://abc.xyz/investor/",
    "AMZN": "https://ir.aboutamazon.com/quarterly-results/default.aspx",
    "META": "https://investor.atmeta.com/financials/default.aspx",
    "ORCL": "https://investor.oracle.com/",
    "CRWV": "https://investors.coreweave.com/",
    "NBIS": "https://nebius.group/investor-hub",
}

_DATE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})")


# ---------------------------------------------------------------------------
# Reading files
# ---------------------------------------------------------------------------


def docx_text(data: bytes) -> str:
    """Paragraph text from a .docx without a Word library."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        xml = z.read("word/document.xml").decode("utf-8", "replace")
    paras = []
    for p in re.findall(r"<w:p[ >].*?</w:p>", xml, flags=re.DOTALL):
        text = "".join(re.findall(r"<w:t(?: [^>]*)?>([^<]*)</w:t>", p))
        paras.append(text)
    text = "\n\n".join(t.strip() for t in paras if t.strip())
    return (text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"')
            .replace("&#39;", "'").replace("’", "'").replace("“", '"').replace("”", '"'))


def pdf_text(path: Path) -> str:
    from pypdf import PdfReader

    pages = [p.extract_text() or "" for p in PdfReader(str(path)).pages]
    text = "\n\n".join(pages).replace("\xa0", " ").replace("’", "'").replace("“", '"').replace("”", '"')
    # PDF lines break mid-sentence; rejoin lines inside a paragraph.
    text = re.sub(r"(?<![.\n:?!])\n(?!\n)", " ", text)
    return re.sub(r"[ \t]+", " ", text).strip()


def read_file(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return pdf_text(path)
    if suffix == ".docx":
        return docx_text(path.read_bytes())
    raw = path.read_text("utf-8", errors="replace")
    return html_to_text(raw) if suffix in (".html", ".htm") else raw.strip()


def _sources(folder: Path) -> dict[str, str]:
    f = folder / "sources.csv"
    if not f.exists():
        return {}
    with f.open(newline="") as fh:
        return {r["file"].strip(): r["url"].strip() for r in csv.DictReader(fh) if r.get("file") and r.get("url")}


def scan(root: Path = TRANSCRIPT_DIR) -> list[dict]:
    """Every transcript file under ``root``, with ticker, call date and public URL."""
    out = []
    if not root.exists():
        return out
    for folder in sorted(p for p in root.iterdir() if p.is_dir()):
        ticker = folder.name.upper()
        links = _sources(folder)
        for f in sorted(folder.iterdir()):
            if f.suffix.lower() not in SUFFIXES:
                continue
            m = _DATE.search(f.name)
            if not m:
                print(f"  skipped {f}: name must start with the call date, e.g. 2026-10-29.pdf")
                continue
            out.append({"ticker": ticker, "path": f, "call_date": date(*map(int, m.groups())),
                        "url": links.get(f.name) or IR_PAGES.get(ticker, "")})
    return out


def to_rows(items: list[dict], known_docs: set[str], relevant_chars) -> tuple[pd.DataFrame, pd.DataFrame]:
    """``filings`` and ``documents`` rows for transcripts not loaded yet."""
    filings, docs = [], []
    for it in items:
        accession = f"TRANSCRIPT-{it['ticker']}-{it['call_date']:%Y%m%d}"
        doc_id = f"{accession}/{it['path'].name}"
        filings.append({"accession": accession, "ticker": it["ticker"], "cik": None, "form": FORM,
                        "filing_date": it["call_date"], "report_date": None, "items": "", "url": it["url"]})
        if doc_id in known_docs:
            continue
        text = read_file(it["path"])
        docs.append({"doc_id": doc_id, "accession": accession, "ticker": it["ticker"], "document": it["path"].name,
                     "doc_type": "TRANSCRIPT", "description": "Earnings call transcript", "url": it["url"],
                     "text": text, "chars": len(text), "relevant_chars": relevant_chars(text),
                     "extracted_at": None, "model": None})
    return pd.DataFrame(filings), pd.DataFrame(docs)


# ---------------------------------------------------------------------------
# Microsoft
# ---------------------------------------------------------------------------

MSFT_DOCX = "https://cdn-dynmedia-1.microsoft.com/is/content/microsoftcorp/{name}"


def microsoft_quarters(since: date, until: date) -> list[tuple[str, date]]:
    """(label, quarter end) for Microsoft fiscal quarters ending in [since, until]. FY ends June 30."""
    out, d = [], date(since.year - 1, 9, 30)
    while d <= until:
        if d >= since:
            fy = d.year + (1 if d.month > 6 else 0)
            q = {9: 1, 12: 2, 3: 3, 6: 4}[d.month]
            out.append((f"FY{fy % 100:02d}Q{q}", d))
        nxt = {9: (12, 31), 12: (3, 31), 3: (6, 30), 6: (9, 30)}[d.month]
        d = date(d.year + (d.month == 12), *nxt)
    return out


def fetch_microsoft(earnings_dates: list[date], since: date, until: date, folder: Path | None = None) -> list[Path]:
    """Download Microsoft's transcripts into the transcripts folder, named by call date.

    The call date is taken from Microsoft's earnings 8-K (item 2.02) in ``earnings_dates``: the first one
    filed within 60 days after the quarter end.
    """
    import requests

    folder = folder or TRANSCRIPT_DIR / "MSFT"
    folder.mkdir(parents=True, exist_ok=True)
    saved = []
    for label, qend in microsoft_quarters(since - timedelta(days=95), until):
        call = next((d for d in sorted(earnings_dates) if qend < d <= qend + timedelta(days=60)), None)
        if call is None:
            continue
        if list(folder.glob(f"{call:%Y-%m-%d}*")):
            continue
        low = label[:-2] + label[-2:].lower()  # some links use "FY25q4"
        for name in (f"Transcript{label}.docx", f"TranscriptQandA{label}.docx", f"Transcript{label}",
                     f"Transcript{low}.docx", f"TranscriptQandA{low}.docx", f"Transcript{low}"):
            r = requests.get(MSFT_DOCX.format(name=name), headers={"User-Agent": BROWSER_UA}, timeout=60)
            if r.ok and r.content[:2] == b"PK":
                path = folder / f"{call:%Y-%m-%d} Microsoft {label}.docx"
                path.write_bytes(r.content)
                saved.append(path)
                break
    if saved:
        src = folder / "sources.csv"
        rows = _sources(folder)
        for p in saved:
            label = p.stem.split()[-1]
            rows[p.name] = f"https://aka.ms/transcript{label.lower()}"
        with src.open("w", newline="") as fh:
            w = csv.writer(fh)
            w.writerow(["file", "url"])
            w.writerows(sorted(rows.items()))
    return saved


# ---------------------------------------------------------------------------
# Equibles transcript API (https://equibles.com/docs/api/endpoints/earnings)
# ---------------------------------------------------------------------------

EQUIBLES_URL = "https://api.equibles.com/v1"
# Hyperscalers first: their guidance is mostly spoken on calls. Microsoft comes from its own site.
EQUIBLES_ORDER = ["GOOGL", "AMZN", "META", "ORCL", "CRWV", "NBIS", "IREN", "APLD", "CIFR", "WULF", "HUT", "CORZ", "GLXY"]


class RequestBudgetSpent(RuntimeError):
    pass


class Equibles:
    """Minimal client: list a company's earnings calls and download a transcript's speaker turns.

    The free tier allows 100 requests a day, so the client counts requests and stops at ``max_requests``
    (or on HTTP 429); calls already saved are skipped on the next run.
    """

    def __init__(self, api_key: str, max_requests: int = 95):
        import requests

        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {api_key}", "Accept": "application/json"})
        self.max_requests, self.used = max_requests, 0

    def get(self, path: str, **params) -> dict | None:
        if self.used >= self.max_requests:
            raise RequestBudgetSpent(f"stopped after {self.used} requests (daily limit)")
        self.used += 1
        r = self.session.get(f"{EQUIBLES_URL}{path}", params=params, timeout=60)
        if r.status_code == 429:
            raise RequestBudgetSpent("the provider's daily request limit was reached")
        if r.status_code == 404:
            return None
        if r.status_code in (401, 403):
            raise RuntimeError(f"Equibles rejected the API key (HTTP {r.status_code}); check TRANSCRIPT_API_KEY in .env")
        r.raise_for_status()
        return r.json()

    def earnings_calls(self, ticker: str) -> list[dict]:
        out, offset = [], 0
        while True:
            page = self.get(f"/stocks/{ticker}/investor-events", eventType="EarningsCall", limit=100, offset=offset)
            if not page:
                return out
            out += page.get("data", [])
            meta = page.get("meta", {})
            if not meta.get("hasMore"):
                return out
            offset += meta.get("count") or len(page.get("data", []))

    def transcript(self, ticker: str, fiscal_year: int, fiscal_quarter: int) -> dict | None:
        turns, offset, head = [], 0, None
        while True:
            page = self.get(f"/stocks/{ticker}/earnings-calls/{fiscal_year}/{fiscal_quarter}/speakers",
                            limit=200, offset=offset)
            if not page:
                return None
            head = head or page
            turns += page.get("data", [])
            if not page.get("hasMore"):
                break
            offset += page.get("turnCount") or len(page.get("data", []))
        return {**head, "data": turns}


def transcript_text(t: dict) -> str:
    """Speaker-labelled plain text from an Equibles transcript."""
    lines = [t.get("eventTitle") or "", ""]
    for turn in t.get("data", []):
        who = turn.get("speakerName") or turn.get("speakerRole") or "Speaker"
        role = turn.get("speakerRole")
        label = f"{who} ({role})" if turn.get("speakerName") and role else who
        text = (turn.get("text") or "").strip()
        if text:
            lines += [f"{label}:", text, ""]
    return "\n".join(lines).replace("’", "'").replace("“", '"').replace("”", '"').strip()


def fetch_equibles(api_key: str, since: date, tickers: list[str] | None = None, root: Path | None = None,
                   max_requests: int = 95, log=print) -> dict:
    """Download every available earnings call transcript since ``since`` into the transcripts folder.

    Returns counts: saved, skipped (already saved), unavailable (no transcript or a summary only).
    """
    root = root or TRANSCRIPT_DIR
    client = Equibles(api_key, max_requests=max_requests)
    counts = {"saved": 0, "already": 0, "unavailable": 0, "requests": 0, "stopped": None}
    names = {}
    try:
        for t in (tickers or EQUIBLES_ORDER):
            folder = root / t
            folder.mkdir(parents=True, exist_ok=True)
            have = {p.name[:10] for p in folder.iterdir() if p.suffix.lower() in SUFFIXES}
            calls = client.earnings_calls(t)
            got = 0
            for ev in sorted(calls, key=lambda e: e.get("callDate") or ""):
                day = (ev.get("callDate") or "")[:10]
                if not day or day < since.isoformat() or ev.get("callDateIsPeriodEnd"):
                    continue
                if day in have:
                    counts["already"] += 1
                    continue
                if not ev.get("hasTranscript") or ev.get("fiscalYear") is None or ev.get("fiscalQuarter") is None:
                    counts["unavailable"] += 1
                    continue
                tr = client.transcript(t, int(ev["fiscalYear"]), int(ev["fiscalQuarter"]))
                if not tr or tr.get("isFiledRecord") or not tr.get("data"):
                    counts["unavailable"] += 1
                    continue
                name = f"{day} {t} FY{ev['fiscalYear']}Q{ev['fiscalQuarter']}.txt"
                (folder / name).write_text(transcript_text(tr), encoding="utf-8")
                names.setdefault(t, {})[name] = ev.get("webcastUrl") or IR_PAGES.get(t, "")
                counts["saved"] += 1
                got += 1
            log(f"  {t}: {len(calls)} calls listed, {got} transcripts saved")
    except RequestBudgetSpent as exc:
        counts["stopped"] = str(exc)
    finally:
        counts["requests"] = client.used
        for t, rows in names.items():  # record public links for the dashboard
            folder = root / t
            links = _sources(folder)
            links.update(rows)
            with (folder / "sources.csv").open("w", newline="") as fh:
                w = csv.writer(fh)
                w.writerow(["file", "url"])
                w.writerows(sorted(links.items()))
    return counts
