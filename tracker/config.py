"""Settings, tracked companies and the fact schema."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = Path(os.environ.get("TRACKER_DATA_DIR", ROOT / "data"))
CACHE_DIR = DATA_DIR / "cache"
DB_PATH = DATA_DIR / "tracker.duckdb"
# Slim copy of the database (no document text) committed to git for the hosted dashboard.
PUBLISHED_DB_PATH = ROOT / "app_data" / "tracker.duckdb"
# Manual corrections applied by the dashboard: one row per fact to exclude, with the reason. Committed.
CORRECTIONS_PATH = ROOT / "app_data" / "corrections.csv"


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal .env loader so the project needs no extra dependency."""
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()


@dataclass(frozen=True)
class Company:
    ticker: str
    name: str
    group: str  # "Hyperscaler", "Neocloud", "AI infrastructure"


COMPANIES: list[Company] = [
    Company("MSFT", "Microsoft", "Hyperscaler"),
    Company("GOOGL", "Alphabet", "Hyperscaler"),
    Company("AMZN", "Amazon", "Hyperscaler"),
    Company("META", "Meta", "Hyperscaler"),
    Company("ORCL", "Oracle", "Hyperscaler"),
    Company("CRWV", "CoreWeave", "Neocloud"),
    Company("NBIS", "Nebius", "Neocloud"),
    Company("IREN", "IREN", "Neocloud"),
    Company("APLD", "Applied Digital", "AI infrastructure"),
    Company("CIFR", "Cipher Mining", "AI infrastructure"),
    Company("WULF", "TeraWulf", "AI infrastructure"),
    Company("HUT", "Hut 8", "AI infrastructure"),
    Company("CORZ", "Core Scientific", "AI infrastructure"),
    Company("GLXY", "Galaxy Digital", "AI infrastructure"),
]

# Filings to scan. 8-K and 6-K carry earnings releases and deal announcements; 10-Q, 10-K and 20-F
# (periodic reports) carry lease commitments, capacity and backlog in their notes.
FILING_FORMS = ("8-K", "6-K", "10-Q", "10-K", "20-F")
PERIODIC_FORMS = ("10-Q", "10-K", "20-F")
START_DATE = os.environ.get("TRACKER_START_DATE", "2024-01-01")

# SEC requires a descriptive User-Agent with contact details for automated access.
SEC_USER_AGENT = os.environ.get("SEC_USER_AGENT", "")
SEC_MAX_RPS = 8.0  # SEC fair-access limit is 10 requests per second

# Earnings call transcripts from the Equibles API (free tier: 100 requests a day). Optional.
TRANSCRIPT_API_KEY = os.environ.get("TRANSCRIPT_API_KEY") or os.environ.get("EQUIBLES_API_KEY", "")
TRANSCRIPT_SINCE = os.environ.get("TRANSCRIPT_SINCE", "2024-10-01")
# Requests per run; the free tier allows 100 a day. Raise it (e.g. 1000) on a paid plan.
TRANSCRIPT_MAX_REQUESTS = int(os.environ.get("TRANSCRIPT_MAX_REQUESTS", "95"))

ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5")
# Thinking effort for extraction: low | medium | high | xhigh | max. Low keeps a full run within a ~$20 budget.
ANTHROPIC_EFFORT = os.environ.get("ANTHROPIC_EFFORT", "low")

# Metrics the extractor looks for. Units are canonical so values compare across filings.
METRICS: dict[str, dict] = {
    "capex_guidance": {
        "label": "Capex guidance",
        "unit": "USD_billion",
        "desc": "Forward-looking capital expenditure outlook for a stated period (e.g. FY2026), including ranges.",
    },
    "capex_reported": {
        "label": "Capex reported",
        "unit": "USD_billion",
        "desc": "Capital expenditures actually spent in a stated past period.",
    },
    "power_contracted_mw": {
        "label": "Contracted power",
        "unit": "MW",
        "desc": "Power capacity secured or contracted (signed leases, PPAs, contracted critical IT load).",
    },
    "power_active_mw": {
        "label": "Active power",
        "unit": "MW",
        "desc": "Power capacity energized, online or operational as of a date.",
    },
    "power_pipeline_mw": {
        "label": "Power pipeline",
        "unit": "MW",
        "desc": "Power capacity in development or pipeline, not yet contracted or online.",
    },
    "gpus_deployed": {
        "label": "GPUs deployed",
        "unit": "GPU_count",
        "desc": "Number of GPUs or accelerators deployed, installed or under management as of a date.",
    },
    "backlog_usd": {
        "label": "Revenue backlog",
        "unit": "USD_billion",
        "desc": "Contracted revenue backlog or remaining performance obligations as of a date.",
    },
    "customer_contract": {
        "label": "Customer contract",
        "unit": "USD_billion",
        "desc": "A specific new or expanded customer, lease or offtake contract: total value, counterparty, term.",
    },
    "lease_not_commenced": {
        "label": "Leases not yet commenced",
        "unit": "USD_billion",
        "desc": "Leases the company has signed as tenant (mostly data centers still under construction) whose terms "
                "have not yet commenced: total future lease payments as of a date. Not a customer contract.",
    },
    "cloud_revenue": {
        "label": "Cloud revenue",
        "unit": "USD_billion",
        "desc": "Revenue of the company's cloud / AI-infrastructure business for a single quarter: AWS, Google Cloud, "
                "Microsoft Intelligent Cloud or Microsoft Cloud, Oracle cloud infrastructure (OCI) or total cloud; for "
                "neoclouds, total revenue. The segment is named in 'segment'.",
    },
    "cloud_growth_pct": {
        "label": "Cloud revenue growth",
        "unit": "percent",
        "desc": "Stated year-over-year growth of a cloud business for a single quarter, e.g. 'Azure and other cloud "
                "services revenue grew 40%'. The segment is named in 'segment'.",
    },
    "financing": {
        "label": "Financing",
        "unit": "USD_billion",
        "desc": "A specific debt or equity financing: amount, instrument (notes, term loan, DDTL, convertible, equity).",
    },
}

UNITS = ("USD_billion", "MW", "GPU_count", "percent")
