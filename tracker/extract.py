"""LLM extraction of capex, capacity, contract and financing facts from filings.

Pipeline for one document:
1. ``relevant_chunks``  keep paragraphs that mention capex, power, GPUs, backlog,
                        contracts or financing, with one paragraph of context.
2. ``call_model``       Claude fills a strict ``record_facts`` tool schema. Every
                        fact must include a verbatim quote from the filing.
3. ``ground``           checks the quote appears in the source text and that the
                        extracted number appears in the quote (with unit scaling).
                        Facts get status verified / partial / unverified.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any

from . import config

KEYWORDS = re.compile(
    r"capital expenditure|\bcapex\b|megawatt|gigawatt|\bMW\b|\bGW\b|\bGPUs?\b|accelerator|data ?cent(?:er|re)"
    r"|backlog|remaining performance obligation|contracted|critical IT|energi[sz]ed|power capacity"
    r"|notes due|senior notes|term loan|delayed draw|convertible|credit facility|financing"
    r"|lease agreement|colocation|\bHPC\b|offtake",
    re.IGNORECASE,
)

MAX_CHUNK_CHARS = 18_000

SYSTEM_PROMPT = """You extract structured facts about AI infrastructure investment from SEC filings and press releases.

Record a fact only when the text states it explicitly about the filing company (or its named subsidiaries). Do not record industry statistics, analyst estimates, or numbers about other companies except as the counterparty to the filer's own contract or financing.

Metrics:
{metrics}

Rules:
- One fact per distinct number. A range like "$90 to $100 billion" is one fact with value_low, value_high, and value = midpoint.
- Units are canonical: money in USD billions ($850 million = 0.85), power in MW (1.2 GW = 1200), GPUs as a count.
- period: the period the number refers to, in the company's words, e.g. "FY2026", "Q3 2025", "calendar 2026", "as of September 30, 2025".
- as_of_date: YYYY-MM-DD when the text gives a date for a point-in-time number, else null.
- change: for guidance, "raised", "lowered", "reaffirmed" or "new" only if the text says so; otherwise "not_stated".
- quote: copy the exact sentence or table row that states the number, verbatim, at most 300 characters. Do not paraphrase.
- confidence: 0 to 1, your confidence the fact is correctly read.
- If nothing qualifies, return an empty list. Never guess.
"""


def metric_lines() -> str:
    return "\n".join(f"- {k} ({v['unit']}): {v['desc']}" for k, v in config.METRICS.items())


TOOL = {
    "name": "record_facts",
    "description": "Record every qualifying fact found in the text.",
    "input_schema": {
        "type": "object",
        "properties": {
            "facts": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "metric": {"type": "string", "enum": list(config.METRICS)},
                        "value": {"type": ["number", "null"]},
                        "value_low": {"type": ["number", "null"]},
                        "value_high": {"type": ["number", "null"]},
                        "unit": {"type": "string", "enum": list(config.UNITS)},
                        "period": {"type": ["string", "null"]},
                        "as_of_date": {"type": ["string", "null"]},
                        "change": {"type": "string", "enum": ["raised", "lowered", "reaffirmed", "new", "not_stated"]},
                        "counterparty": {"type": ["string", "null"]},
                        "site": {"type": ["string", "null"]},
                        "instrument": {"type": ["string", "null"]},
                        "term_years": {"type": ["number", "null"]},
                        "quote": {"type": "string"},
                        "confidence": {"type": "number"},
                    },
                    "required": ["metric", "value", "unit", "period", "quote", "confidence"],
                },
            }
        },
        "required": ["facts"],
    },
}


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------


def relevant_chunks(text: str, max_chars: int = MAX_CHUNK_CHARS) -> list[str]:
    """Paragraphs that match KEYWORDS, plus one paragraph either side, packed into chunks."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    keep = set()
    for i, p in enumerate(paras):
        if KEYWORDS.search(p):
            keep.update({i - 1, i, i + 1})
    selected = [paras[i] for i in sorted(keep) if 0 <= i < len(paras)]
    chunks, cur = [], ""
    for p in selected:
        if len(p) > max_chars:
            p = p[:max_chars]
        if len(cur) + len(p) + 2 > max_chars and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur}\n\n{p}" if cur else p
    if cur:
        chunks.append(cur)
    return chunks


# ---------------------------------------------------------------------------
# Model call
# ---------------------------------------------------------------------------


def build_prompt(meta: dict, chunk: str) -> str:
    return (
        f"Company: {meta['company']} ({meta['ticker']})\n"
        f"Filing: {meta['form']} filed {meta['filing_date']}\n"
        f"Document: {meta.get('doc_type', '')} {meta.get('description', '')}\n\n"
        f"<text>\n{chunk}\n</text>\n\n"
        "Record every qualifying fact with the record_facts tool."
    )


def call_model(client: Any, prompt: str, model: str | None = None, max_retries: int = 5) -> list[dict]:
    model = model or config.ANTHROPIC_MODEL
    system = [{"type": "text", "text": SYSTEM_PROMPT.format(metrics=metric_lines()), "cache_control": {"type": "ephemeral"}}]
    for attempt in range(max_retries):
        try:
            resp = client.messages.create(
                model=model,
                max_tokens=4096,
                system=system,
                tools=[TOOL],
                tool_choice={"type": "tool", "name": "record_facts"},
                messages=[{"role": "user", "content": prompt}],
            )
            for block in resp.content:
                if getattr(block, "type", None) == "tool_use" and block.name == "record_facts":
                    facts = block.input.get("facts", [])
                    return facts if isinstance(facts, list) else []
            return []
        except Exception as exc:  # rate limits and overload: back off and retry
            status = getattr(exc, "status_code", None)
            if status in (429, 500, 502, 503, 529) and attempt < max_retries - 1:
                time.sleep(min(2 ** (attempt + 1), 60))
                continue
            raise
    return []


def make_client():
    import os

    import anthropic

    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise RuntimeError("Set ANTHROPIC_API_KEY in your environment or in the .env file.")
    return anthropic.Anthropic()


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


def normalize(s: str) -> str:
    s = s.lower().replace("\xa0", " ")
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return re.sub(r"\s+", " ", s).strip()


def _ngrams(words: list[str], n: int = 3) -> set:
    return {tuple(words[i : i + n]) for i in range(max(len(words) - n + 1, 1))}


def quote_match(quote: str, source: str) -> str:
    """'exact' if the quote is a substring of the source, 'fuzzy' if 70%+ of its word trigrams appear, else 'missing'."""
    q, s = normalize(quote), normalize(source)
    if not q:
        return "missing"
    if q in s:
        return "exact"
    qw, sw = re.findall(r"\w+", q), re.findall(r"\w+", s)
    if len(qw) < 3:
        return "missing"
    qn = _ngrams(qw)
    overlap = len(qn & _ngrams(sw)) / len(qn)
    return "fuzzy" if overlap >= 0.7 else "missing"


_NUM = re.compile(
    r"(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(trillion|billion|million|thousand|bn\b|mm\b|gigawatts?|megawatts?|gw\b|mw\b|[bmk]\b)?",
    re.IGNORECASE,
)
_SCALE = {
    "USD_billion": {"trillion": 1e3, "billion": 1.0, "bn": 1.0, "b": 1.0, "million": 1e-3, "mm": 1e-3, "m": 1e-3,
                    "thousand": 1e-6, "k": 1e-6, None: None},
    "MW": {"gigawatt": 1e3, "gigawatts": 1e3, "gw": 1e3, "megawatt": 1.0, "megawatts": 1.0, "mw": 1.0, None: 1.0},
    "GPU_count": {"million": 1e6, "thousand": 1e3, "k": 1e3, "m": 1e6, None: 1.0},
}


def numbers_in(quote: str, unit: str) -> list[float]:
    """Candidate canonical values for every number in the quote."""
    scales = _SCALE.get(unit, {None: 1.0})
    out = []
    for raw, word in _NUM.findall(quote):
        n = float(raw.replace(",", ""))
        key = word.lower() if word else None
        if unit == "USD_billion" and key is None:
            # Bare numbers in financial tables are usually in millions; raw dollars if very large.
            out.extend([n, n / 1e3, n / 1e9])
            continue
        scale = scales.get(key, scales.get(None, 1.0))
        out.append(n * (scale if scale is not None else 1.0))
    return out


def value_match(fact: dict) -> bool:
    candidates = numbers_in(fact.get("quote", ""), fact.get("unit", ""))
    targets = [v for v in (fact.get("value"), fact.get("value_low"), fact.get("value_high")) if v is not None]
    if not targets:
        return False
    for t in targets:
        if not any(abs(c - t) <= max(abs(t) * 0.005, 1e-9) for c in candidates):
            # A range midpoint will not appear in the text; accept if low and high both do.
            if t == fact.get("value") and fact.get("value_low") is not None and fact.get("value_high") is not None:
                continue
            return False
    return True


def ground(fact: dict, source: str) -> dict:
    q = quote_match(fact.get("quote", ""), source)
    v = value_match(fact)
    status = "verified" if q == "exact" and v else ("partial" if q != "missing" and (v or q == "exact") else "unverified")
    return {**fact, "quote_match": q, "value_in_quote": v, "status": status}


def clean_fact(f: dict) -> dict | None:
    if f.get("metric") not in config.METRICS:
        return None
    unit = config.METRICS[f["metric"]]["unit"]
    out = {k: f.get(k) for k in TOOL["input_schema"]["properties"]["facts"]["items"]["properties"]}
    out["unit"] = unit
    if out.get("value") is None and out.get("value_low") is not None and out.get("value_high") is not None:
        out["value"] = (out["value_low"] + out["value_high"]) / 2
    if out.get("value") is None and f["metric"] not in ("customer_contract",):
        return None
    try:
        out["confidence"] = float(out.get("confidence") or 0)
    except (TypeError, ValueError):
        out["confidence"] = 0.0
    out["quote"] = (out.get("quote") or "")[:600]
    return out


def fact_id(doc_id: str, f: dict) -> str:
    key = json.dumps([doc_id, f.get("metric"), f.get("value"), f.get("period"), normalize(f.get("quote", ""))[:200]])
    return hashlib.sha1(key.encode()).hexdigest()[:16]


def extract_document(client: Any, meta: dict, text: str, model: str | None = None) -> list[dict]:
    """All grounded facts for one document."""
    facts, seen = [], set()
    for chunk in relevant_chunks(text):
        for raw in call_model(client, build_prompt(meta, chunk), model):
            f = clean_fact(raw)
            if f is None:
                continue
            g = ground(f, text)
            g["fact_id"] = fact_id(meta["doc_id"], g)
            if g["fact_id"] in seen:
                continue
            seen.add(g["fact_id"])
            facts.append(g)
    return facts
