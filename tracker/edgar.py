"""SEC EDGAR client: company lookup, filing lists, exhibits and XBRL facts.

Follows SEC fair-access rules: a descriptive User-Agent with contact details and
no more than ``SEC_MAX_RPS`` requests per second. Responses are cached on disk.
Filing documents never change, so they are cached permanently; filing lists and
XBRL facts are refreshed after ``ttl_hours``.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from . import config

TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SUBMISSIONS_PAGE_URL = "https://data.sec.gov/submissions/{name}"
FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
INDEX_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm"
DOC_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{document}"
FILING_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{acc}-index.htm"

# 8-K items that carry results, guidance, contracts and financings.
RELEVANT_8K_ITEMS = {"1.01", "2.02", "2.03", "7.01", "8.01"}


class EdgarError(RuntimeError):
    pass


class EdgarClient:
    def __init__(
        self,
        user_agent: str | None = None,
        cache_dir: Path | None = None,
        max_rps: float = config.SEC_MAX_RPS,
        fetch: Callable[[str, dict], tuple[int, bytes]] | None = None,
        ttl_hours: float = 12.0,
    ):
        self.user_agent = (user_agent if user_agent is not None else config.SEC_USER_AGENT).strip()
        if not self.user_agent or "@" not in self.user_agent:
            raise EdgarError(
                "Set SEC_USER_AGENT to a name and contact email, e.g. 'Jane Doe jane@example.com'. "
                "SEC blocks automated requests without one."
            )
        self.cache_dir = Path(cache_dir or config.CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.min_interval = 1.0 / max_rps
        self.ttl = ttl_hours * 3600
        self._last = 0.0
        self._fetch = fetch or self._http_fetch

    # -- transport ---------------------------------------------------------

    @staticmethod
    def _http_fetch(url: str, headers: dict) -> tuple[int, bytes]:
        import requests

        r = requests.get(url, headers=headers, timeout=30)
        return r.status_code, r.content

    def _cache_path(self, url: str) -> Path:
        return self.cache_dir / hashlib.sha256(url.encode()).hexdigest()[:32]

    def get(self, url: str, immutable: bool = False) -> bytes:
        path = self._cache_path(url)
        if path.exists() and (immutable or time.time() - path.stat().st_mtime < self.ttl):
            return path.read_bytes()
        headers = {"User-Agent": self.user_agent, "Accept-Encoding": "gzip, deflate"}
        for attempt in range(5):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            status, body = self._fetch(url, headers)
            if status == 200:
                path.write_bytes(body)
                return body
            if status in (403,) and attempt == 0:
                raise EdgarError(f"SEC refused the request (403) for {url}. Check SEC_USER_AGENT.")
            if status == 404:
                raise EdgarError(f"Not found (404): {url}")
            time.sleep(min(2**attempt, 20))  # 429 or 5xx: back off
        raise EdgarError(f"Failed after retries ({status}): {url}")

    def get_json(self, url: str, immutable: bool = False):
        return json.loads(self.get(url, immutable))

    # -- companies and filings ----------------------------------------------

    def ticker_map(self) -> dict[str, tuple[int, str]]:
        data = self.get_json(TICKERS_URL)
        return {row["ticker"].upper(): (int(row["cik_str"]), row["title"]) for row in data.values()}

    def filings(self, cik: int, forms: Iterable[str], since: str) -> list[dict]:
        """Filings of the given forms filed on or after ``since`` (YYYY-MM-DD), newest first."""
        forms = set(forms)
        sub = self.get_json(SUBMISSIONS_URL.format(cik=cik))
        tables = [sub["filings"]["recent"]]
        for page in sub["filings"].get("files", []):
            if page.get("filingTo", "9999") >= since:
                tables.append(self.get_json(SUBMISSIONS_PAGE_URL.format(name=page["name"])))
        out = []
        for t in tables:
            for i, form in enumerate(t["form"]):
                if form not in forms or t["filingDate"][i] < since:
                    continue
                items = t.get("items", [""] * len(t["form"]))[i] or ""
                if form == "8-K" and items and not (set(items.split(",")) & RELEVANT_8K_ITEMS):
                    continue
                acc = t["accessionNumber"][i]
                out.append(
                    {
                        "cik": cik,
                        "accession": acc,
                        "form": form,
                        "filing_date": t["filingDate"][i],
                        "report_date": t.get("reportDate", [""] * len(t["form"]))[i] or None,
                        "items": items,
                        "primary_document": t["primaryDocument"][i],
                        "url": FILING_URL.format(cik=cik, acc_nodash=acc.replace("-", ""), acc=acc),
                    }
                )
        out.sort(key=lambda f: f["filing_date"], reverse=True)
        return out

    def filing_documents(self, cik: int, accession: str) -> list[dict]:
        """Documents listed on a filing's index page, with their exhibit type."""
        html = self.get(
            INDEX_URL.format(cik=cik, acc_nodash=accession.replace("-", ""), acc=accession), immutable=True
        ).decode("utf-8", "replace")
        return parse_index(html, cik, accession)

    def document_text(self, url: str) -> str:
        return html_to_text(self.get(url, immutable=True).decode("utf-8", "replace"))

    def company_facts(self, cik: int) -> dict:
        return self.get_json(FACTS_URL.format(cik=cik))


@dataclass
class Document:
    accession: str
    document: str
    type: str
    description: str
    url: str


def parse_index(html: str, cik: int, accession: str) -> list[dict]:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    docs = []
    for table in soup.find_all("table", class_="tableFile"):
        for row in table.find_all("tr")[1:]:
            cells = row.find_all("td")
            if len(cells) < 4:
                continue
            link = cells[2].find("a")
            if not link or not link.get("href"):
                continue
            name = link.get_text(strip=True).split(" ")[0]
            doc_type = cells[3].get_text(strip=True)
            docs.append(
                {
                    "accession": accession,
                    "document": name,
                    "type": doc_type,
                    "description": cells[1].get_text(" ", strip=True),
                    "url": DOC_URL.format(cik=cik, acc_nodash=accession.replace("-", ""), document=name),
                }
            )
    return docs


def select_documents(docs: list[dict], form: str, primary: str) -> list[dict]:
    """Press-release exhibits (EX-99.x) plus the primary document for 6-Ks and short 8-Ks."""
    keep = []
    for d in docs:
        name = d["document"].lower()
        if not name.endswith((".htm", ".html", ".txt")):
            continue
        if d["type"].upper().startswith("EX-99") or d["document"] == primary:
            keep.append(d)
    return keep


_BLOCK_TAGS = ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "h5", "h6", "tr", "table")


def html_to_text(html: str) -> str:
    """Readable text from an EDGAR HTML document. Table rows become ' | '-joined lines."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "head"]):
        tag.decompose()
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c and c not in ("$", "%", ")")]
        tr.replace_with(soup.new_string("\n" + " | ".join(cells) + "\n"))
    for tag in soup.find_all(_BLOCK_TAGS):
        tag.insert_before(soup.new_string("\n"))
    text = soup.get_text()
    text = text.replace("\xa0", " ").replace("’", "'").replace("“", '"').replace("”", '"')
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()
