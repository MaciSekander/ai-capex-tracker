"""AI Infrastructure Capex Tracker: Streamlit dashboard.

Reads the pipeline's tables from DuckDB: TRACKER_DB if set, else the local pipeline database,
else the published snapshot in app_data/ (what Streamlit Community Cloud serves), else the
fictional demo database. All derived views (deduplicated disclosures, guidance revisions,
quarterly capex) come from tracker/analytics.py.
"""

import os
from datetime import date
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from tracker import analytics as A
from tracker import config
from tracker.evaluate import scores

st.set_page_config(page_title="AI Infrastructure Capex Tracker", page_icon="📈", layout="wide")

TABLES = ("companies", "filings", "documents", "xbrl_quarterly", "facts", "reviews", "misses")
GROUP_ORDER = ["Hyperscaler", "Neocloud", "AI infrastructure"]
GROUP_COLORS = alt.Scale(domain=GROUP_ORDER, range=["#1f5f8b", "#6a4fc4", "#c2571a"])
STATUS_COLORS = alt.Scale(domain=["verified", "partial", "unverified"], range=["#2f7d4f", "#c08a1e", "#b83b2c"])
REVISION_SHAPES = alt.Scale(domain=["initial", "raised", "maintained", "lowered"],
                            range=["circle", "triangle-up", "diamond", "triangle-down"])
PALETTE = ["#1f5f8b", "#e07b39", "#3a9a5b", "#c23b4a", "#7b5ea7", "#8c6d4f", "#d36fae", "#5f6b73", "#a8a23a",
           "#2aa3b8", "#4f7cc9", "#e8a33d", "#58a68e", "#b5546b"]
METRIC_LABELS = {k: v["label"] for k, v in config.METRICS.items()}
REVISION_LABEL = {"raised": "▲ Raised", "lowered": "▼ Lowered", "maintained": "● Maintained", "initial": "○ First stated"}

st.markdown(
    """
    <style>
      .block-container {padding-top: 2.2rem; padding-bottom: 3rem; max-width: 1400px;}
      [data-testid="stMetric"] {background: var(--secondary-background-color); border-radius: 10px;
                                padding: 14px 16px 10px 16px;}
      [data-testid="stMetricLabel"] p {font-size: 0.86rem;}
      .subtle {color: #6b7480; font-size: 0.9rem;}
      h3 {margin-top: 0.6rem;}
    </style>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Data
# ---------------------------------------------------------------------------


@st.cache_data(ttl=600, show_spinner="Loading data...")
def load(path: str, mtime: float) -> dict:  # mtime busts the cache when the file changes
    import duckdb

    out = {}
    con = duckdb.connect(path, read_only=True)
    for t in TABLES:
        cols = "* EXCLUDE (text)" if t == "documents" else "*"
        out[t] = con.execute(f"SELECT {cols} FROM {t}").df()
    con.close()
    return out


def has_data(path: Path) -> bool:
    if not path.exists():
        return False
    import duckdb

    try:
        con = duckdb.connect(str(path), read_only=True)
        n = con.execute("SELECT COUNT(*) FROM xbrl_quarterly").fetchone()[0]
        con.close()
        return n > 0
    except Exception:
        return False


def pick_source():
    """TRACKER_DB if set, else the local pipeline database, else the published snapshot, else demo data."""
    candidates = [Path(os.environ["TRACKER_DB"])] if os.environ.get("TRACKER_DB") else []
    candidates += [config.DB_PATH, config.PUBLISHED_DB_PATH]
    for p in candidates:
        if has_data(p):
            return str(p), False
    from tracker.demo import build_demo

    demo = config.DATA_DIR / "demo.duckdb"
    if not has_data(demo):  # missing, or written by a different DuckDB version
        build_demo(demo)
    return str(demo), True


path, is_demo = pick_source()
data = load(path, Path(path).stat().st_mtime)
companies = data["companies"].rename(columns={"grp": "group"})
companies["group"] = pd.Categorical(companies["group"], GROUP_ORDER, ordered=True)
companies = companies.sort_values(["group", "name"]).reset_index(drop=True)
companies["group"] = companies["group"].astype(str)
NAME = dict(zip(companies["ticker"], companies["name"]))
GROUP = dict(zip(companies["ticker"], companies["group"]))
COLOR_OF = dict(zip(companies["name"], (PALETTE * 2)[: len(companies)]))


def company_colors(df: pd.DataFrame) -> alt.Scale:
    """Stable color per company, with the legend limited to companies in ``df``."""
    present = [n for n in companies["name"] if n in set(df["name"])]
    return alt.Scale(domain=present, range=[COLOR_OF[n] for n in present])

def load_corrections() -> pd.DataFrame:
    try:
        return pd.read_csv(config.CORRECTIONS_PATH, dtype=str).fillna("")
    except (FileNotFoundError, pd.errors.EmptyDataError):
        return pd.DataFrame(columns=["fact_id", "action", "reason", "date"])


corrections = load_corrections() if not is_demo else pd.DataFrame(columns=["fact_id", "action", "reason", "date"])
facts_all = A.drop_periodic_deals(A.apply_corrections(data["facts"], corrections)).copy()
facts_all["filing_date"] = pd.to_datetime(facts_all["filing_date"])
filings = data["filings"].copy()
filings["filing_date"] = pd.to_datetime(filings["filing_date"])
data_through = pd.Timestamp(filings["filing_date"].max() if not filings.empty else date.today())


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------


def money(v, decimals=1) -> str:
    if v is None or v != v:
        return "—"
    sign, v = ("−" if v < 0 else ""), abs(v)
    if v >= 1:
        return sign + (f"${v:,.0f}B" if v >= 100 else f"${v:,.{decimals}f}B")
    return f"{sign}${v * 1000:,.0f}M"


def fmt_value(v, unit) -> str:
    if v is None or v != v:
        return "—"
    if unit == "USD_billion":
        return money(v, 2 if abs(v) < 10 else 1)
    if unit == "MW":
        return f"{v / 1000:,.2f} GW" if v >= 1000 else f"{v:,.0f} MW"
    if unit == "GPU_count":
        return f"{v:,.0f} GPUs"
    return f"{v:,.2f}"


def fmt_range(low, high, mid) -> str:
    if low == low and high == high and low is not None and high is not None and low != high:
        return f"${low:,.0f}–{high:,.0f}B"
    return money(mid)


def pct(v, signed=True) -> str:
    if v is None or v != v:
        return "—"
    return f"{v * 100:+.0f}%" if signed else f"{v * 100:.0f}%"


def chart(c, height=320):
    st.altair_chart(c.properties(height=height), width="stretch")


def table(df, height=None, **column_config):
    kwargs = {"width": "stretch", "hide_index": True, "column_config": column_config}
    if height:
        kwargs["height"] = height
    st.dataframe(df, **kwargs)


def download(df: pd.DataFrame, name: str, label="Download CSV"):
    st.download_button(label, df.to_csv(index=False).encode(), file_name=f"{name}.csv", mime="text/csv",
                       key=f"dl_{name}")


def source_note(text: str):
    st.caption(f"**Source and method:** {text}")


LINK = st.column_config.LinkColumn("Source", display_text="Open filing", width="small")


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

sb = st.sidebar
sb.markdown("### Filters")
groups = [g for g in GROUP_ORDER if g in set(companies["group"])]
sb.markdown("**Company type**")
GROUP_PLURAL = {"Hyperscaler": "Hyperscalers", "Neocloud": "Neoclouds", "AI infrastructure": "AI infrastructure"}
sel_groups = [g for g in groups if sb.checkbox(GROUP_PLURAL[g], value=True, key=f"grp_{g}")]
pool = companies[companies["group"].isin(sel_groups)]
picked = sb.multiselect("Companies", pool["name"].tolist(), default=[], key="names", placeholder="All companies",
                        help="Leave empty to include every company of the selected types.")
sel_names = picked or pool["name"].tolist()
sel_tickers = companies.loc[companies["name"].isin(sel_names), "ticker"].tolist()

status_choice = sb.radio(
    "Extracted facts to include",
    ["Verified only", "Verified and partial"],
    index=0, key="status",
    help="Verified: the quote was found word for word in the filing and the number appears in the quote. "
         "Partial: one of those two checks passed. See the Methodology tab.",
)
allowed_status = ["verified"] if status_choice == "Verified only" else ["verified", "partial"]
since = sb.date_input("Filings since", value=date(2024, 1, 1), key="since")

sb.divider()
sb.markdown(
    "**How to use this dashboard**\n\n"
    "- **Overview** for the headline numbers and what changed recently.\n"
    "- **Company profile** for everything on one company.\n"
    "- Every extracted number links to the SEC filing it came from.\n"
    "- **Methodology** explains how each number is produced and checked."
)

# Filtered views
facts = facts_all[facts_all["ticker"].isin(sel_tickers) & facts_all["status"].isin(allowed_status)
                  & (facts_all["filing_date"] >= pd.Timestamp(since))].copy()
facts["name"] = facts["ticker"].map(NAME)
facts["group"] = facts["ticker"].map(GROUP)
disc = A.disclosures(facts)
if not disc.empty:
    disc["name"] = disc["ticker"].map(NAME)
    disc["group"] = disc["ticker"].map(GROUP)
    disc["metric_label"] = disc["metric"].map(METRIC_LABELS)
    disc["display_value"] = [fmt_value(v, u) for v, u in zip(disc["value"], disc["unit"])]

cq_all = A.capex_quarterly(data["xbrl_quarterly"], facts_all[facts_all["status"] == "verified"])
cq_all["name"] = cq_all["ticker"].map(NAME)
cq_all["group"] = cq_all["ticker"].map(GROUP)
cq = cq_all[cq_all["ticker"].isin(sel_tickers)]
ocf = data["xbrl_quarterly"][data["xbrl_quarterly"]["metric"] == "operating_cash_flow"].copy()
ocf["end"] = pd.to_datetime(ocf["end"])

gh = A.guidance_history(facts)
if not gh.empty:
    gh["name"] = gh["ticker"].map(NAME)

hyp = [t for t in sel_tickers if GROUP.get(t) == "Hyperscaler"]
others = [t for t in sel_tickers if t not in hyp]

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("AI Infrastructure Capex Tracker")
st.markdown(
    "<div class='subtle'>What hyperscalers, neoclouds and AI infrastructure companies have disclosed to the SEC "
    "about capital spending, capex guidance, data-center leases, power capacity, GPUs, backlog, customer contracts "
    "and financings.</div>",
    unsafe_allow_html=True,
)
last_run = pd.to_datetime(data["documents"]["extracted_at"]).max() if not data["documents"].empty else None
st.caption(
    f"Filings through **{data_through:%b %d, %Y}** · {len(companies)} companies · "
    f"{len(A.disclosures(facts_all[facts_all['status'] == 'verified'])):,} verified disclosures from "
    f"{facts_all['doc_id'].nunique():,} documents"
    + (f" · last updated {last_run:%b %d, %Y}" if last_run is not None and last_run == last_run else "")
)
if is_demo:
    st.warning("Demo data: every company, number and quote here is fictional. Run the pipeline to load real filings.")
if not sel_tickers:
    st.info("Select at least one company in the sidebar.")
    st.stop()

tabs = st.tabs(["Overview", "Signals", "Capex", "Guidance", "Power & capacity", "Contracts & financing", "Company profile",
                "All disclosures", "Methodology"])

# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------

with tabs[0]:
    capex_ttm, capex_prev = A.ttm_total(cq_all, hyp), A.ttm_total(cq_all, hyp, 4)
    ocf_q = ocf.rename(columns={"value_usd_billion": "v"})
    ocf_ttm = sum(ocf_q[ocf_q["ticker"] == t].sort_values("end")["v"].iloc[-4:].sum() for t in hyp
                  if (ocf_q["ticker"] == t).sum() >= 4)
    other_ttm, other_prev = A.ttm_total(cq_all, others), A.ttm_total(cq_all, others, 4)
    recent_g = gh[gh["filing_date"] >= data_through - pd.Timedelta(days=120)] if not gh.empty else gh
    n_raised = int((recent_g["revision"] == "raised").sum()) if len(recent_g) else 0
    n_lowered = int((recent_g["revision"] == "lowered").sum()) if len(recent_g) else 0

    k = st.columns(4)
    k[0].metric("Hyperscaler capex · last 4Q", money(capex_ttm),
                f"{pct(capex_ttm / capex_prev - 1)} vs prior 4 quarters" if capex_prev else None,
                help="Sum of each selected hyperscaler's four most recent reported quarters of capital "
                     "expenditures (SEC XBRL cash flow statements).")
    k[1].metric("Hyperscaler capex ÷ op. cash flow", pct(capex_ttm / ocf_ttm, False) if ocf_ttm else "—",
                help="Last four quarters of capex divided by last four quarters of operating cash flow. "
                     "Above 100% means capex is funded partly from cash reserves, debt or equity.")
    k[2].metric("Neocloud & infra capex · last 4Q", money(other_ttm),
                f"{pct(other_ttm / other_prev - 1)} vs prior 4 quarters" if other_prev else None,
                help="Same calculation for the selected neoclouds and AI infrastructure companies. Companies "
                     "without four consecutive quarters of data are left out.")
    k[3].metric("Guidance revisions · 120 days", f"▲{n_raised}  ▼{n_lowered}",
                f"{n_raised} raised, {n_lowered} lowered", delta_color="off",
                help="Revisions are computed by comparing each guidance statement with the company's previous "
                     "statement for the same period. 'Raised' means the midpoint (or an end of the range) went up.")

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.subheader("Quarterly capex by company")
        view = cq[cq["end"] >= pd.Timestamp("2024-01-01")]
        if view.empty:
            st.info("No capex data for the current selection.")
        else:
            c = alt.Chart(view).mark_bar().encode(
                x=alt.X("yearquarter(end):O", title=None, axis=alt.Axis(labelAngle=0, format="Q%q %y")),
                y=alt.Y("sum(value_usd_billion):Q", title="Capex ($B)"),
                color=alt.Color("name:N", scale=company_colors(view), title=None,
                                legend=alt.Legend(orient="bottom", columns=5)),
                order=alt.Order("group:N"),
                tooltip=[alt.Tooltip("name:N", title="Company"), alt.Tooltip("quarter:N", title="Quarter"),
                         alt.Tooltip("value_usd_billion:Q", title="Capex $B", format=",.2f"),
                         alt.Tooltip("source:N", title="Source")],
            )
            chart(c, 360)
            source_note("SEC XBRL cash flow statements (calendar quarter of the period end). Nebius from its "
                        "quarterly press releases, since it files annual XBRL only.")
    with right:
        st.subheader("Latest capex guidance")
        if gh.empty:
            st.info("No capex guidance for the current selection.")
        else:
            lg = A.latest_guidance(gh)
            annual = lg[lg["period_label"].str.fullmatch(r"\d{4}")]
            cur = annual[annual["period_label"].astype(int) >= data_through.year]
            lg = (cur if not cur.empty else annual).sort_values("value", ascending=False)
            show = pd.DataFrame({
                "Company": lg["name"], "Period": lg["period_label"],
                "Guidance": [fmt_range(l, h, v) for l, h, v in zip(lg["value_low"], lg["value_high"], lg["value"])],
                "Last change": lg["revision"].map(REVISION_LABEL),
                "Stated": lg["filing_date"].dt.strftime("%b %Y"), "url": lg["url"],
            })
            table(show, url=LINK)
            source_note("Company press releases (8-K / 6-K). Revisions compare each statement with the previous one "
                        "for the same period.")

    st.subheader("What's new")
    window = st.radio("Window", ["Last 7 days", "Last 30 days", "Last 90 days"], index=1, horizontal=True,
                      key="new_window", label_visibility="collapsed")
    days = int(window.split()[1])
    if disc.empty:
        st.info("No disclosures for the current selection.")
    else:
        new = disc[disc["first_stated"] >= data_through - pd.Timedelta(days=days)].sort_values(
            ["first_stated", "name"], ascending=[False, True])
        st.caption(f"{len(new):,} disclosures first stated in the {days} days to {data_through:%b %d, %Y}.")
        if not new.empty:
            out = pd.DataFrame({
                "Date": new["first_stated"].dt.date, "Company": new["name"], "Metric": new["metric_label"],
                "Value": new["display_value"], "Period / detail": new["period"].fillna("").str.slice(0, 60),
                "Counterparty": new["counterparty"].fillna(""), "Quote": new["quote"], "url": new["url"],
            })
            table(out, height=min(420, 38 + 35 * len(out)), url=LINK,
                  Quote=st.column_config.TextColumn("Quote", width="large"))
            download(out.rename(columns={"url": "Source"}), "whats_new")

# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

with tabs[1]:
    st.subheader("Where spending is heading")
    st.markdown(
        "<div class='subtle'>Calculated from the figures on the other tabs. Each section states its formula. "
        "Projections extend each company's recent rates mechanically; they are not company guidance.</div>",
        unsafe_allow_html=True)

    flows = A.lease_flows(data["xbrl_quarterly"], disc) if not disc.empty else pd.DataFrame()
    flows = flows[flows["ticker"].isin(sel_tickers)] if not flows.empty else flows
    proj, params = A.lease_projection(flows) if not flows.empty else (pd.DataFrame(), pd.DataFrame())
    if not params.empty:  # only meaningful, current pipelines
        params = params[(params["pool"] >= 1) & (params["as_of"] >= data_through - pd.Timedelta(days=270))]
        proj = proj[proj["ticker"].isin(params["ticker"])]
    fresh = flows[flows["end"] >= data_through - pd.Timedelta(days=200)] if not flows.empty else flows
    latest_pool = fresh.sort_values("end").groupby("ticker").tail(1) if not fresh.empty else fresh

    def pool_year_ago(t, end):
        g = flows[(flows["ticker"] == t) & (flows["end"] <= end - pd.Timedelta(days=330))]
        g = g[g["end"] >= end - pd.Timedelta(days=400)]
        return float(g.sort_values("end")["pool"].iloc[-1]) if len(g) else np.nan

    hp = latest_pool[latest_pool["ticker"].isin(hyp)] if not latest_pool.empty else latest_pool
    pool_now = float(hp["pool"].sum()) if len(hp) else 0.0
    ago = [pool_year_ago(t, e) for t, e in zip(hp["ticker"], hp["end"])] if len(hp) else []
    pool_then = sum(a for a in ago if a == a) if ago and all(a == a for a in ago) else np.nan
    last4 = flows.dropna(subset=["signed", "starts"]) if not flows.empty else flows
    last4 = (last4[last4["ticker"].isin(params["ticker"]) & (last4["end"] > data_through - pd.Timedelta(days=380))]
             if not params.empty else last4.iloc[0:0])
    signed_4q, starts_4q = (float(last4["signed"].sum()), float(last4["starts"].sum())) if len(last4) else (0.0, 0.0)
    next4 = (proj[proj["from_disclosed_backlog"]].groupby("case")["starts"].sum()
             if not proj.empty else pd.Series(dtype=float))

    capex_now, capex_prev = A.ttm_total(cq, hyp), A.ttm_total(cq, hyp, offset=4)

    # Headlines, generated from the numbers
    lines = []
    if pool_now:
        lines.append(f"Hyperscalers have **{money(pool_now)}** of signed leases, mostly data centers, that have not "
                     f"started yet" + (f", **{pct(pool_now / pool_then - 1)}** from a year earlier." if pool_then == pool_then
                                       and pool_then else "."))
    if signed_4q:
        lines.append(f"In the last four quarters they signed an implied **{money(signed_4q)}** of new leases and "
                     f"**{money(starts_4q)}** started, so the backlog of signed leases is "
                     f"{'growing' if signed_4q > starts_4q else 'shrinking'}.")
    if capex_now and capex_prev:
        lines.append(f"Hyperscaler capex over the last four quarters is **{money(capex_now)}**, "
                     f"**{pct(capex_now / capex_prev - 1)}** from the four quarters before.")
    if not latest_pool.empty:
        g = [(t, p / pool_year_ago(t, e) - 1) for t, p, e in zip(latest_pool["ticker"], latest_pool["pool"], latest_pool["end"])
             if pool_year_ago(t, e) == pool_year_ago(t, e) and pool_year_ago(t, e) > 1]
        if g:
            t, gr = max(g, key=lambda x: x[1])
            lines.append(f"Fastest-growing lease backlog: **{NAME[t]}**, {pct(gr)} over the year.")
    if lines:
        st.markdown("\n".join(f"- {l}" for l in lines).replace("$", "\\$"))

    k = st.columns(4)
    k[0].metric("Signed leases not yet started", money(pool_now) if pool_now else "—",
                f"{pct(pool_now / pool_then - 1)} vs year ago" if pool_then == pool_then and pool_then else None,
                help="Sum of each selected hyperscaler's latest figure (total future lease payments), from 10-Q and "
                     "10-K lease notes.")
    k[1].metric("New leases signed · last 4Q", money(signed_4q) if signed_4q else "—",
                help="Implied: change in leases not yet started, plus leases that started. Companies do not report "
                     "this directly. Total future payments.")
    k[2].metric("Leases started · last 4Q", money(starts_4q) if starts_4q else "—",
                help="Leases that commenced (SEC XBRL right-of-use assets obtained), converted to total future "
                     "payments so they compare with the figures beside them.")
    if len(next4):
        nxt = float(next4.iloc[0])
        k[3].metric("Projected lease starts · next 4Q", money(nxt),
                    f"{pct(nxt / starts_4q - 1)} vs last 4Q" if starts_4q else None,
                    help="Projected from leases already signed a year earlier, so it does not depend on future "
                         "signings. It assumes recent leases start as quickly as earlier ones did; very large campus "
                         "leases may take longer to build. See the method below.")
    else:
        k[3].metric("Projected lease starts · next 4Q", "—")

    st.subheader("Lease pipeline: signed, started and projected")
    if flows.empty or flows["signed"].notna().sum() == 0:
        st.info("No lease pipeline data for the current selection.")
    else:
        opts = (flows.groupby("ticker")["pool"].last().sort_values(ascending=False).index.map(NAME).tolist())
        who = st.selectbox("Company", opts, key="sig_company")
        t = companies.loc[companies["name"] == who, "ticker"].iloc[0]
        f = flows[flows["ticker"] == t].sort_values("end")
        left, right = st.columns(2, gap="large")
        with left:
            st.markdown("**Signed and started each quarter** · total future payments, $B")
            bars = f.dropna(subset=["signed"]).melt(id_vars=["end"], value_vars=["signed", "starts"], var_name="flow",
                                                    value_name="value")
            bars["flow"] = bars["flow"].map({"signed": "Signed (implied)", "starts": "Started"})
            if bars.empty:
                st.info("Needs two consecutive quarters of lease data.")
            else:
                chart(alt.Chart(bars).mark_bar().encode(
                    x=alt.X("yearquarter(end):O", title=None, axis=alt.Axis(labelAngle=0, format="Q%q %y")),
                    xOffset="flow:N",
                    y=alt.Y("value:Q", title="$B"),
                    color=alt.Color("flow:N", title=None, scale=alt.Scale(domain=["Signed (implied)", "Started"],
                                                                          range=["#1f5f8b", "#e07b39"]),
                                    legend=alt.Legend(orient="top")),
                    tooltip=[alt.Tooltip("end:T", title="Quarter ending"), alt.Tooltip("flow:N", title="Flow"),
                             alt.Tooltip("value:Q", title="$B", format=",.1f")]), 320)
        with right:
            st.markdown("**Lease starts, actual and projected** · total future payments, $B")
            hist = f.dropna(subset=["starts"])[["end", "starts"]].assign(case="Actual")
            pj = proj[proj["ticker"] == t][["end", "starts", "case"]] if not proj.empty else pd.DataFrame()
            if pj.empty:
                st.info(f"Not enough recent quarters of lease starts to project {who}.")
            else:
                if not hist.empty:  # join the projection lines to the last actual point
                    pj = pd.concat([pd.DataFrame({"end": [hist["end"].iloc[-1]] * 2,
                                                  "starts": [hist["starts"].iloc[-1]] * 2,
                                                  "case": ["Signings continue", "No new signings"]}), pj])
                pj["end"] = pd.to_datetime(pj["end"]).astype("datetime64[ns]")
                hist["end"] = pd.to_datetime(hist["end"]).astype("datetime64[ns]")
                band = pj.pivot_table(index="end", columns="case", values="starts").reset_index()
                dom, rng = ["Actual", "Signings continue", "No new signings"], ["#1f2933", "#1f5f8b", "#7a8794"]
                lines_ = alt.Chart(pd.concat([hist, pj])).mark_line(point=True, strokeWidth=2).encode(
                    x=alt.X("end:T", title=None), y=alt.Y("starts:Q", title="$B"),
                    color=alt.Color("case:N", title=None, scale=alt.Scale(domain=dom, range=rng),
                                    legend=alt.Legend(orient="top")),
                    strokeDash=alt.condition(alt.datum.case == "Actual", alt.value([1, 0]), alt.value([5, 4])),
                    order=alt.Order("end:T"),
                    tooltip=[alt.Tooltip("end:T", title="Quarter ending"), alt.Tooltip("case:N", title="Series"),
                             alt.Tooltip("starts:Q", title="$B", format=",.1f")])
                area = alt.Chart(band).mark_area(opacity=0.12, color="#1f5f8b").encode(
                    x="end:T", y="No new signings:Q", y2="Signings continue:Q")
                chart(area + lines_, 320)

        if not params.empty:
            pa = params.assign(name=params["ticker"].map(NAME)).sort_values("pool", ascending=False)
            yr1 = proj[proj["from_disclosed_backlog"] & (proj["case"] == "Signings continue")].groupby("ticker")["starts"].sum()
            yr2 = proj[~proj["from_disclosed_backlog"]].groupby(["ticker", "case"])["starts"].sum().unstack()
            pa = pa.assign(yr1=pa["ticker"].map(yr1)).join(yr2, on="ticker")
            table(pd.DataFrame({
                "Company": pa["name"], "Signed, not started": pa["pool"].map(money),
                "As of": pa["as_of"].dt.strftime("%b %d, %Y"),
                "Started · last 4Q": pa["starts_4q"].map(money),
                "Years of backlog at that pace": pa["years_of_backlog"].map(lambda y: "—" if y != y else f"{y:.1f}"),
                "Signed per quarter (avg)": pa["signed_avg"].map(money),
                "Projected starts · next 4Q": pa["yr1"].map(money),
                "Projected starts · 4Q after": [f"{money(a)}–{money(b)}" for a, b in
                                                zip(pa["No new signings"], pa["Signings continue"])],
            }))
        source_note(
            "All figures are total future lease payments. Signed, not started: 10-Q / 10-K lease notes (operating "
            "and finance parts added where given separately). Started: SEC XBRL right-of-use assets obtained for "
            "new leases, a present value, converted to total payments with each company's ratio of lease payments "
            "due to lease liability (1.2× to 1.8×). Signed (implied) = change in signed-not-started + started. "
            "Projection: leases start when construction finishes, so each quarter's starts are modeled as a fixed "
            "share of the backlog four quarters earlier, with the share fitted on the company's last four quarters. "
            "The next four quarters therefore follow from backlog already disclosed. The four after that need the "
            "backlog itself projected: the range runs from no new signings to signings continuing at their recent "
            "average. Started leases include all leases, not only data centers. Meta stopped tagging quarterly lease "
            "starts after 2024, so it has no projection. Read the projection as a scale, not a schedule: it "
            "assumes recently signed leases start as quickly as earlier ones did, while multi-gigawatt campuses may "
            "take longer (Oracle, for example, says its backlog starts over roughly the next three years).")

    st.subheader("Capex momentum")
    if cq.empty:
        st.info("No capex data for the current selection.")
    else:
        rows = []
        for t, g in cq.sort_values("end").groupby("ticker"):
            v = dict(zip(g["quarter"], g["value_usd_billion"]))
            qs = sorted(v, key=A._qnum)
            if len(qs) < 6:
                continue
            def yoy(q):
                prev = [p for p in qs if A._qnum(p) == A._qnum(q) - 4]
                return v[q] / v[prev[0]] - 1 if prev and v[prev[0]] > 0 else np.nan
            y0, y1 = yoy(qs[-1]), yoy(qs[-2])
            rows.append({"ticker": t, "latest": qs[-1], "yoy": y0, "prev_yoy": y1,
                         "trend": "—" if y0 != y0 or y1 != y1 else ("▲ Accelerating" if y0 > y1 else "▼ Slowing")})
        mom = pd.DataFrame(rows)
        if mom.empty:
            st.info("Not enough quarters of capex for the current selection.")
        else:
            summ = A.capex_summary(cq).set_index("ticker")
            mom = mom.join(summ[["ttm_b", "ttm_yoy"]], on="ticker").sort_values("ttm_b", ascending=False)
            table(pd.DataFrame({
                "Company": mom["ticker"].map(NAME), "Type": mom["ticker"].map(GROUP),
                "Capex · last 4Q": mom["ttm_b"].map(money), "vs prior 4Q": mom["ttm_yoy"].map(pct),
                "Latest quarter": mom["latest"].str.replace("Q", " Q"), "Latest vs year ago": mom["yoy"].map(pct),
                "Prior quarter vs year ago": mom["prev_yoy"].map(pct), "Trend": mom["trend"]}))
            source_note("SEC XBRL quarterly capex. Trend compares the latest quarter's year-over-year growth with the "
                        "previous quarter's.")

    if others:
        st.subheader("Neocloud and AI infrastructure funding")
        summ = A.capex_summary(cq[cq["ticker"].isin(others)]).set_index("ticker") if not cq.empty else pd.DataFrame()
        bl = A.latest_values(disc[disc["ticker"].isin(others)], ["backlog_usd"]).set_index("ticker") \
            if not disc.empty else pd.DataFrame()
        o4 = ocf[ocf["ticker"].isin(others)].sort_values("end").groupby("ticker").tail(4).groupby("ticker").agg(
            ocf_4q=("value_usd_billion", "sum"), n=("value_usd_billion", "size"))
        fund = pd.DataFrame({"ticker": [t for t in others if (t in summ.index) or (t in bl.index)]})
        if fund.empty:
            st.info("No data for the current selection.")
        else:
            fund["capex"] = fund["ticker"].map(summ["ttm_b"]) if not summ.empty else np.nan
            fund["backlog"] = fund["ticker"].map(bl["value"]) if not bl.empty else np.nan
            fund["ocf"] = fund["ticker"].map(o4[o4["n"] == 4]["ocf_4q"])
            fund = fund.sort_values("backlog", ascending=False)
            table(pd.DataFrame({
                "Company": fund["ticker"].map(NAME), "Revenue backlog": fund["backlog"].map(money),
                "Capex · last 4Q": fund["capex"].map(money),
                "Backlog ÷ capex": [f"{b / c:.1f}×" if b == b and c == c and c > 0 else "—"
                                    for b, c in zip(fund["backlog"], fund["capex"])],
                "Operating cash flow · last 4Q": fund["ocf"].map(money),
                "Capex funded by operations": ["None (cash flow negative)" if o == o and o <= 0 else
                                               pct(o / c, False) if o == o and c == c and c > 0 else "—"
                                               for o, c in zip(fund["ocf"], fund["capex"])]}))
            source_note("Backlog: latest figure from filings. Capex and operating cash flow: SEC XBRL, last four "
                        "quarters. Backlog ÷ capex shows how much contracted revenue stands behind each dollar of "
                        "recent spending; capex funded by operations is operating cash flow ÷ capex (the rest comes "
                        "from debt, equity or cash on hand).")

# ---------------------------------------------------------------------------
# Capex
# ---------------------------------------------------------------------------

with tabs[2]:
    st.subheader("Quarterly capital expenditures")
    if cq.empty:
        st.info("No capex data for the current selection.")
    else:
        c1, c2 = st.columns([1, 1])
        view = c1.radio("Group by", ["Company", "Company type"], horizontal=True, key="capex_view")
        start = c2.selectbox("From", [2023, 2024, 2025], index=1, key="capex_from")
        v = cq[cq["end"] >= pd.Timestamp(f"{start}-01-01")]
        if view == "Company":
            panels = [("Hyperscalers", v[v["group"] == "Hyperscaler"]),
                      ("Neoclouds & AI infrastructure", v[v["group"] != "Hyperscaler"])]
            panels = [(t_, d_) for t_, d_ in panels if not d_.empty]
            for col, (title_, d_) in zip(st.columns(len(panels), gap="large"), panels):
                with col:
                    st.markdown(f"**{title_}**")
                    chart(alt.Chart(d_).mark_line(point=True, strokeWidth=2).encode(
                        x=alt.X("end:T", title=None),
                        y=alt.Y("value_usd_billion:Q", title="Capex ($B)"),
                        color=alt.Color("name:N", scale=company_colors(d_), title=None,
                                        legend=alt.Legend(orient="bottom", columns=3)),
                        tooltip=[alt.Tooltip("name:N", title="Company"), alt.Tooltip("quarter:N", title="Quarter"),
                                 alt.Tooltip("value_usd_billion:Q", title="Capex $B", format=",.2f"),
                                 alt.Tooltip("source:N", title="Source")]), 360)
        else:
            agg = v.groupby(["end", "group"], as_index=False)["value_usd_billion"].sum()
            c = alt.Chart(agg).mark_bar().encode(
                x=alt.X("yearquarter(end):O", title=None, axis=alt.Axis(labelAngle=0, format="Q%q %y")),
                y=alt.Y("value_usd_billion:Q", title="Capex ($B)", stack="zero"),
                color=alt.Color("group:N", scale=GROUP_COLORS, title=None, legend=alt.Legend(orient="bottom")),
                tooltip=[alt.Tooltip("group:N", title="Type"), alt.Tooltip("end:T", title="Quarter ending"),
                         alt.Tooltip("value_usd_billion:Q", title="Capex $B", format=",.1f")],
            )
            chart(c, 380)

        st.subheader("Capex scorecard")
        summ = A.capex_summary(cq)
        summ = summ.assign(name=summ["ticker"].map(NAME), group=summ["ticker"].map(GROUP)).sort_values("ttm_b", ascending=False)
        sc = pd.DataFrame({
            "Company": summ["name"], "Type": summ["group"], "Latest quarter": summ["latest_quarter"],
            "Capex": summ["latest_b"].map(lambda x: money(x, 2)), "vs prior quarter": summ["qoq"].map(pct),
            "vs year ago": summ["yoy"].map(pct), "Last 4 quarters": summ["ttm_b"].map(money),
            "Last 4Q vs prior 4Q": summ["ttm_yoy"].map(pct), "Source": summ["source"],
        })
        table(sc)
        download(sc, "capex_scorecard")
        source_note("SEC XBRL. 10-Q cash flow statements report year-to-date totals, so most quarters are derived "
                    "by subtraction (e.g. Q2 = six months minus Q1), shown as 'from year-to-date'. Quarters are "
                    "calendar quarters of the period end, so fiscal years that end mid-year (Microsoft, Oracle, "
                    "Applied Digital) are comparable by date.")

        st.subheader("How much of operating cash flow goes to capex")
        both = (cq_all[["ticker", "end", "quarter", "value_usd_billion"]].rename(columns={"value_usd_billion": "capex"})
                .merge(ocf[["ticker", "end", "value_usd_billion"]].rename(columns={"value_usd_billion": "ocf"}),
                       on=["ticker", "end"]))
        both = both[both["ticker"].isin(sel_tickers)].sort_values("end")
        if both.empty:
            st.info("No operating cash flow data for the current selection.")
        else:
            both["capex_4q"] = both.groupby("ticker")["capex"].transform(lambda s: s.rolling(4).sum())
            both["ocf_4q"] = both.groupby("ticker")["ocf"].transform(lambda s: s.rolling(4).sum())
            both = both[(both["ocf_4q"] > 0) & both["capex_4q"].notna() & (both["end"] >= pd.Timestamp("2024-01-01"))]
            both["ratio"] = both["capex_4q"] / both["ocf_4q"]
            both["name"] = both["ticker"].map(NAME)
            include_all = st.toggle("Include neoclouds and AI infrastructure (log scale)", key="ocf_all",
                                    help="Their operating cash flow is small relative to capex, so ratios run far above 100%.")
            if not include_all:
                both = both[both["ticker"].isin(hyp)]
            r = alt.Chart(both).mark_line(point=True, strokeWidth=2).encode(
                x=alt.X("end:T", title="Quarter ending"),
                y=alt.Y("ratio:Q", title="Capex ÷ op. cash flow (last 4Q)", axis=alt.Axis(format="%"),
                        scale=alt.Scale(type="log") if include_all else alt.Scale(zero=True)),
                color=alt.Color("name:N", scale=company_colors(both), title=None, legend=alt.Legend(orient="bottom", columns=5)),
                tooltip=[alt.Tooltip("name:N", title="Company"), alt.Tooltip("quarter:N", title="Quarter"),
                         alt.Tooltip("ratio:Q", title="Ratio", format=".0%"),
                         alt.Tooltip("capex_4q:Q", title="Capex 4Q $B", format=",.1f"),
                         alt.Tooltip("ocf_4q:Q", title="Operating cash flow 4Q $B", format=",.1f")],
            )
            rule = alt.Chart(pd.DataFrame({"y": [1.0]})).mark_rule(strokeDash=[4, 4], color="#7a8794").encode(y="y:Q")
            chart(r + rule, 320)
            source_note("SEC XBRL. The dashed line is 100%: above it, capex exceeds the cash the business generates. "
                        "Quarters with negative trailing operating cash flow are omitted.")

    st.subheader("Signed leases not yet commenced")
    st.markdown(
        "<div class='subtle'>Leases a company has signed as tenant, mostly data centers still under construction, "
        "whose terms have not started yet. The amount is the total future lease payments. It shows capacity already "
        "committed that is not yet in capex or on the balance sheet.</div>", unsafe_allow_html=True)
    lser = A.lease_series(disc) if not disc.empty else pd.DataFrame()
    if lser.empty:
        st.info("No lease commitments for the current selection.")
    else:
        lc1, lc2 = st.columns([3, 2], gap="large")
        with lc1:
            chart(alt.Chart(lser).mark_line(point=True, strokeWidth=2).encode(
                x=alt.X("as_of:T", title="As of"),
                y=alt.Y("value:Q", title="$B"),
                color=alt.Color("name:N", scale=company_colors(lser), title=None, legend=alt.Legend(orient="bottom", columns=5)),
                tooltip=[alt.Tooltip("name:N", title="Company"), alt.Tooltip("display_value:N", title="Value"),
                         alt.Tooltip("period:N", title="As of"), alt.Tooltip("first_stated:T", title="Stated"),
                         alt.Tooltip("quote:N", title="Quote")]), 320)
        with lc2:
            ll = lser.sort_values("as_of").groupby("ticker").tail(1).sort_values("value", ascending=False)
            lt = pd.DataFrame({"Company": ll["name"], "Latest": ll["display_value"],
                               "As of": ll["as_of"].dt.strftime("%b %d, %Y"), "Source": ll["url"]})
            table(lt, Source=LINK)
            download(lt, "leases_not_commenced")
        source_note("10-Q and 10-K lease notes, extracted by Claude and checked against the filing text. Only totals "
                    "stated as of a date are plotted (not individual leases signed after it). Where a filing gives "
                    "operating and finance leases separately (Microsoft before 2025, Meta 2023), the larger is shown. "
                    "Company wording differs, so check the quote.")

# ---------------------------------------------------------------------------
# Guidance
# ---------------------------------------------------------------------------

with tabs[3]:
    st.subheader("Capex guidance and how it has moved")
    if gh.empty:
        st.info("No capex guidance for the current selection.")
    else:
        c1, c2 = st.columns([3, 2])
        periods = sorted(gh["period_label"].unique(), key=A.period_sort_key, reverse=True)
        default = [p for p in periods if p.isdigit() and int(p) >= data_through.year - 1] or periods[:2]
        sel_p = c1.multiselect("Guidance period", periods, default=default, key="g_periods",
                               help="Fiscal and calendar years share a label; each company guides on one basis. "
                                    "Quarterly (Q) and half-year (H) guidance is listed separately.")
        tiers = [x for x in ("Hyperscalers", "Neoclouds & AI infrastructure")
                 if (gh["ticker"].map(GROUP).eq("Hyperscaler") if x == "Hyperscalers"
                     else gh["ticker"].map(GROUP).ne("Hyperscaler")).any()]
        tier = c2.radio("Companies", tiers, horizontal=True, key="g_tier",
                        help="Shown separately because their guidance differs in scale by 100x.")
        g = gh[gh["period_label"].isin(sel_p)].copy()
        g = g[g["ticker"].map(GROUP).eq("Hyperscaler")] if tier == "Hyperscalers" else g[g["ticker"].map(GROUP).ne("Hyperscaler")]
        g["low"] = g["value_low"].fillna(g["value"])
        g["high"] = g["value_high"].fillna(g["value"])
        g["series"] = g["name"] + " · " + g["period_label"]
        g["revision_label"] = g["revision"].map(REVISION_LABEL)
        base = alt.Chart(g).encode(x=alt.X("filing_date:T", title="Date stated"))
        line = base.mark_line(strokeWidth=1.5, opacity=0.6).encode(
            y="value:Q", color=alt.Color("name:N", scale=company_colors(g), title=None), detail="series:N")
        band = base.mark_rule(strokeWidth=3, opacity=0.5).encode(
            y=alt.Y("low:Q", title="Guided capex ($B)"), y2="high:Q", color=alt.Color("name:N", scale=company_colors(g), title=None))
        pts = base.mark_point(filled=True, size=110, opacity=1).encode(
            y="value:Q",
            color=alt.Color("name:N", scale=company_colors(g), title=None, legend=alt.Legend(orient="bottom", columns=6)),
            shape=alt.Shape("revision:N", scale=REVISION_SHAPES, title="Revision", legend=alt.Legend(orient="right")),
            tooltip=[alt.Tooltip("name:N", title="Company"), alt.Tooltip("period_label:N", title="Period"),
                     alt.Tooltip("value:Q", title="Midpoint $B", format=",.1f"),
                     alt.Tooltip("low:Q", title="Low $B", format=",.1f"), alt.Tooltip("high:Q", title="High $B", format=",.1f"),
                     alt.Tooltip("revision_label:N", title="Revision"),
                     alt.Tooltip("change_b:Q", title="Change $B", format="+,.1f"),
                     alt.Tooltip("filing_date:T", title="Stated"), alt.Tooltip("quote:N", title="Quote")])
        if g.empty:
            st.info("No guidance for these periods and companies.")
        else:
            chart(line + band + pts, 400)
        st.caption("Each point is a statement; the vertical bar is the guided range; lines join statements about the "
                   "same period. Hover for the exact quote.")

        st.subheader("Revision log")
        log_df = gh[gh["period_label"].isin(sel_p)].sort_values("filing_date", ascending=False)
        out = pd.DataFrame({
            "Stated": log_df["filing_date"].dt.date, "Company": log_df["name"], "Period": log_df["period_label"],
            "Guidance": [fmt_range(l, h, v) for l, h, v in zip(log_df["value_low"], log_df["value_high"], log_df["value"])],
            "Revision": log_df["revision"].map(REVISION_LABEL),
            "Change": [("" if b != b else "No change" if abs(b) < 1e-9 else f"{'+' if b >= 0 else '−'}{money(abs(b))} ({pct(p_)})")
                       for b, p_ in zip(log_df["change_b"], log_df["change_pct"])],
            "Company's wording": log_df["company_wording"].fillna(""), "Quote": log_df["quote"], "url": log_df["url"],
        })
        table(out, url=LINK, Quote=st.column_config.TextColumn(width="large"))
        download(out.rename(columns={"url": "Source"}), "capex_guidance")
        source_note("Capex guidance stated in 8-K and 6-K press releases, extracted by Claude and checked against the "
                    "filing text. Revision is computed from the numbers, not taken from the company's wording. "
                    "Open-ended statements ('over the next several years', project-level budgets) are listed under "
                    "All disclosures instead. Guidance given only on earnings calls is not captured.")

# ---------------------------------------------------------------------------
# Power & capacity
# ---------------------------------------------------------------------------

POWER = {"power_active_mw": "Active", "power_contracted_mw": "Contracted", "power_pipeline_mw": "Pipeline"}

with tabs[4]:
    st.subheader("Power capacity")
    st.markdown(
        "<div class='subtle'><b>Active</b>: energized and operating. <b>Contracted</b>: secured under signed leases, "
        "PPAs or customer contracts. <b>Pipeline</b>: in development, not yet contracted or online. "
        "Shown for neoclouds and AI infrastructure companies; hyperscalers disclose power <i>added</i>, not "
        "totals, so their statements are listed under All disclosures.</div>", unsafe_allow_html=True)
    pdisc = disc[disc["group"] != "Hyperscaler"] if not disc.empty else disc
    lv = A.latest_values(pdisc, list(POWER)) if not pdisc.empty else pd.DataFrame()
    if lv.empty:
        st.info("No power capacity disclosures for the current selection.")
    else:
        lv["measure"] = lv["metric"].map(POWER)
        show_pipe = st.toggle("Include pipeline", value=False, key="mw_pipe",
                              help="Pipelines are early-stage and can be 10x contracted capacity, which compresses the chart.")
        measures = list(POWER.values()) if show_pipe else ["Active", "Contracted"]
        lv_chart = lv[lv["measure"].isin(measures)]
        order = (lv[lv["measure"] == "Contracted"].sort_values("value", ascending=False)["name"].tolist()
                 + [n for n in lv["name"].unique() if n not in set(lv.loc[lv["measure"] == "Contracted", "name"])])
        bars = alt.Chart(lv_chart).mark_bar().encode(
            y=alt.Y("name:N", title=None, sort=order, axis=alt.Axis(labelLimit=160, labelOverlap=False)),
            yOffset=alt.YOffset("measure:N", sort=measures),
            x=alt.X("value:Q", title="Latest stated (MW)"),
            color=alt.Color("measure:N", sort=list(POWER.values()), title=None,
                            scale=alt.Scale(domain=list(POWER.values()), range=["#2f7d4f", "#1f5f8b", "#a9b9c9"]),
                            legend=alt.Legend(orient="top")),
            tooltip=[alt.Tooltip("name:N", title="Company"), alt.Tooltip("measure:N", title="Measure"),
                     alt.Tooltip("display_value:N", title="Value"), alt.Tooltip("period:N", title="As of"),
                     alt.Tooltip("first_stated:T", title="Stated"), alt.Tooltip("quote:N", title="Quote")])
        chart(bars, max(260, (22 * len(measures) + 10) * lv["name"].nunique()))

        snap = lv.pivot_table(index="name", columns="measure", values="value", aggfunc="first").reindex(
            columns=list(POWER.values()), index=order)
        dates = lv.pivot_table(index="name", columns="measure", values="first_stated", aggfunc="max").max(axis=1)
        snap_out = pd.DataFrame({"Company": snap.index,
                                 **{m: snap[m].map(lambda v: fmt_value(v, "MW")) for m in POWER.values()},
                                 "Latest statement": dates.reindex(snap.index).dt.strftime("%b %d, %Y").values})
        with st.expander("Table and download"):
            table(snap_out)
            download(snap_out, "power_snapshot")
        source_note("Press releases, investor updates (8-K / 6-K) and 10-Q / 10-K reports, extracted by Claude and checked against the "
                    "filing text. Site-level figures are excluded; where a filing gives a total and its parts, the "
                    "largest company-level figure is used. Definitions vary by company (gross vs critical IT MW).")

        st.subheader("One company over time")
        pick = st.selectbox("Company", order, key="mw_company")
        tser = pd.concat([A.metric_series(pdisc[pdisc["name"] == pick], m).assign(measure=POWER[m]) for m in POWER])
        if tser.empty:
            st.info("No history for this company.")
        else:
            chart(alt.Chart(tser).mark_line(point=True, strokeWidth=2).encode(
                x=alt.X("first_stated:T", title="Date stated"), y=alt.Y("value:Q", title="MW"),
                color=alt.Color("measure:N", sort=list(POWER.values()), title=None,
                                scale=alt.Scale(domain=list(POWER.values()), range=["#2f7d4f", "#1f5f8b", "#a9b9c9"]),
                                legend=alt.Legend(orient="top")),
                tooltip=[alt.Tooltip("measure:N", title="Measure"), alt.Tooltip("display_value:N", title="Value"),
                         alt.Tooltip("period:N", title="As of"), alt.Tooltip("first_stated:T", title="Stated"),
                         alt.Tooltip("quote:N", title="Quote")]), 300)
            st.caption("Each point is a figure as first stated in a filing. Companies sometimes redefine a measure "
                       "between releases, so check the quote when a line jumps.")

    st.subheader("GPUs and revenue backlog")
    cols = st.columns(2, gap="large")
    specs = ((cols[0], "gpus_deployed", "GPUs deployed (latest)", "GPUs", pdisc,
              "Neoclouds and AI infrastructure. Hyperscaler chip counts (e.g. Amazon's AI chips landed) are not "
              "comparable and are listed under All disclosures."),
             (cols[1], "backlog_usd", "Revenue backlog (latest, $B)", "Backlog ($B)", disc,
              "Hyperscaler figures are company-wide remaining performance obligations, not only AI."))
    for col, metric, title, xtitle, src, note in specs:
        with col:
            st.markdown(f"**{title}**")
            lm = A.latest_values(src, [metric]) if not src.empty else pd.DataFrame()
            if lm.empty:
                st.info("None for the current selection.")
                continue
            chart(alt.Chart(lm).mark_bar().encode(
                y=alt.Y("name:N", title=None, sort="-x", axis=alt.Axis(labelLimit=160, labelOverlap=False)),
                x=alt.X("value:Q", title=xtitle),
                color=alt.Color("group:N", scale=GROUP_COLORS, title=None, legend=alt.Legend(orient="top")),
                tooltip=[alt.Tooltip("name:N", title="Company"), alt.Tooltip("display_value:N", title="Value"),
                         alt.Tooltip("period:N", title="As of"), alt.Tooltip("first_stated:T", title="Stated"),
                         alt.Tooltip("quote:N", title="Quote")]), alt.Step(30))
            st.caption(note)

# ---------------------------------------------------------------------------
# Contracts & financing
# ---------------------------------------------------------------------------

with tabs[5]:
    deals = disc[disc["metric"].isin(["customer_contract", "financing"])].copy() if not disc.empty else pd.DataFrame()
    if deals.empty:
        st.info("No contracts or financings for the current selection.")
    else:
        deals["type"] = deals["metric"].map({"customer_contract": "Customer contract", "financing": "Financing"})
        last12 = deals[deals["first_stated"] >= data_through - pd.Timedelta(days=365)]
        k = st.columns(4)
        cc12 = last12[last12["metric"] == "customer_contract"]
        fi12 = last12[last12["metric"] == "financing"]

        def largest(d):
            if d.empty:
                return "—", None
            r = d.loc[d["value"].idxmax()]
            return money(r["value"]), f"{r['name']}, {r['first_stated']:%b %Y}"

        k[0].metric("Contracts announced · 12 months", f"{len(cc12):,}")
        v, dlt = largest(cc12)
        k[1].metric("Largest contract · 12 months", v, dlt, delta_color="off",
                    help="Contract values are often 'up to' or total-term amounts, and the same deal can be restated "
                         "in different words, so values are not summed.")
        k[2].metric("Financings announced · 12 months", f"{len(fi12):,}")
        v, dlt = largest(fi12)
        k[3].metric("Largest financing · 12 months", v, dlt, delta_color="off",
                    help="Debt and equity financings, including facilities not yet drawn.")

        a, b = st.columns(2, gap="large")
        for col, metric, title, color_field in ((a, "customer_contract", "Customer and offtake contracts", "group"),
                                                (b, "financing", "Financings", "group")):
            with col:
                st.subheader(title)
                d = deals[deals["metric"] == metric]
                if d.empty:
                    st.info("None for the current selection.")
                    continue
                c = alt.Chart(d).mark_circle(opacity=0.75, stroke="white", strokeWidth=0.5).encode(
                    x=alt.X("first_stated:T", title="Announced"),
                    y=alt.Y("name:N", title=None, sort=companies["name"].tolist(), axis=alt.Axis(labelLimit=160)),
                    size=alt.Size("value:Q", title="$B", scale=alt.Scale(range=[30, 900]), legend=None),
                    color=alt.Color("group:N", scale=GROUP_COLORS, legend=None),
                    tooltip=[alt.Tooltip("name:N", title="Company"), alt.Tooltip("display_value:N", title="Value"),
                             alt.Tooltip("counterparty:N", title="Counterparty"), alt.Tooltip("instrument:N", title="Instrument"),
                             alt.Tooltip("term_years:Q", title="Term (years)"), alt.Tooltip("first_stated:T", title="Announced"),
                             alt.Tooltip("quote:N", title="Quote")])
                chart(c, max(220, 26 * d["name"].nunique()))
        st.caption("Bubble size is the stated amount. Hover for counterparty, instrument, term and the exact quote.")

        st.subheader("Deal log")
        kind = st.radio("Type", ["All", "Customer contract", "Financing"], horizontal=True, key="deal_kind")
        dl = deals if kind == "All" else deals[deals["type"] == kind]
        dl = dl.sort_values("first_stated", ascending=False)
        out = pd.DataFrame({
            "Date": dl["first_stated"].dt.date, "Company": dl["name"], "Type": dl["type"], "Amount": dl["display_value"],
            "Counterparty": dl["counterparty"].fillna(""), "Instrument": dl["instrument"].fillna(""),
            "Term (years)": dl["term_years"].map(lambda x: "" if x != x else f"{x:g}"), "Site": dl["site"].fillna(""), "Quote": dl["quote"], "url": dl["url"],
        })
        table(out, height=460, url=LINK, Quote=st.column_config.TextColumn(width="large"))
        download(out.rename(columns={"url": "Source"}), "deals")
        source_note("Deal announcements and press releases (8-K / 6-K), extracted by Claude "
                    "and checked against the filing text. Repeated mentions of the same deal are shown once, dated "
                    "to the first filing that stated it.")

# ---------------------------------------------------------------------------
# Company profile
# ---------------------------------------------------------------------------

with tabs[6]:
    names = [n for n in companies["name"] if n in sel_names]
    who = st.selectbox("Company", names, key="profile")
    t = companies.loc[companies["name"] == who, "ticker"].iloc[0]
    st.markdown(f"### {who} <span class='subtle'>· {t} · {GROUP[t]}</span>", unsafe_allow_html=True)

    cqt = cq_all[cq_all["ticker"] == t]
    summ = A.capex_summary(cqt)
    dt = disc[disc["ticker"] == t] if not disc.empty else pd.DataFrame()
    ght = gh[gh["ticker"] == t] if not gh.empty else gh
    lvt = A.latest_values(dt, list(POWER) + ["gpus_deployed", "backlog_usd", "lease_not_commenced"]) if not dt.empty else pd.DataFrame()

    def latest_of(metric):
        r = lvt[lvt["metric"] == metric] if not lvt.empty else lvt
        return (fmt_value(r["value"].iloc[0], r["unit"].iloc[0]), f"as of {r['first_stated'].iloc[0]:%b %Y}") if len(r) else ("—", None)

    k = st.columns(4)
    if len(summ):
        s0 = summ.iloc[0]
        k[0].metric(f"Capex, {s0['latest_quarter'][:4]} Q{s0['latest_quarter'][-1]}", money(s0["latest_b"], 2),
                    f"{pct(s0['yoy'])} vs year ago" if s0["yoy"] == s0["yoy"] else None)
        k[1].metric("Capex, last 4 quarters", money(s0["ttm_b"]),
                    f"{pct(s0['ttm_yoy'])} vs prior 4 quarters" if s0["ttm_yoy"] == s0["ttm_yoy"] else None)
    else:
        k[0].metric("Latest quarterly capex", "—")
        k[1].metric("Capex, last 4 quarters", "—")
    if len(ght):
        lg = A.latest_guidance(ght).sort_values("period_label", key=lambda s: s.map(A.period_sort_key)).iloc[-1]
        k[2].metric(f"Capex guidance, {lg['period_label']}", fmt_range(lg["value_low"], lg["value_high"], lg["value"]),
                    REVISION_LABEL[lg["revision"]], delta_color="off")
    else:
        k[2].metric("Capex guidance", "—", "none in press releases", delta_color="off")
    v, d = latest_of("backlog_usd")
    k[3].metric("Revenue backlog", v, d, delta_color="off")
    k = st.columns(5)
    for col, metric, label in ((k[0], "power_active_mw", "Active power"), (k[1], "power_contracted_mw", "Contracted power"),
                               (k[2], "power_pipeline_mw", "Power pipeline"), (k[3], "gpus_deployed", "GPUs deployed"),
                               (k[4], "lease_not_commenced", "Leases not yet commenced")):
        v, d = latest_of(metric)
        col.metric(label, v, d, delta_color="off")

    left, right = st.columns([3, 2], gap="large")
    with left:
        st.markdown("**Quarterly capex and operating cash flow**")
        o = ocf[ocf["ticker"] == t][["end", "value_usd_billion"]].assign(series="Operating cash flow")
        bars = alt.Chart(cqt[cqt["end"] >= pd.Timestamp("2023-01-01")]).mark_bar(color=PALETTE[0], opacity=0.85).encode(
            x=alt.X("end:T", title=None), y=alt.Y("value_usd_billion:Q", title="$B"),
            tooltip=[alt.Tooltip("quarter:N", title="Quarter"), alt.Tooltip("value_usd_billion:Q", title="Capex $B", format=",.2f"),
                     alt.Tooltip("source:N", title="Source")])
        layers = bars
        if not o.empty:
            layers = bars + alt.Chart(o[o["end"] >= pd.Timestamp("2023-01-01")]).mark_line(
                color=PALETTE[1], point=True, strokeWidth=2).encode(
                x="end:T", y="value_usd_billion:Q",
                tooltip=[alt.Tooltip("end:T", title="Quarter ending"),
                         alt.Tooltip("value_usd_billion:Q", title="Operating cash flow $B", format=",.2f")])
        if cqt.empty:
            st.info("No quarterly capex for this company.")
        else:
            chart(layers, 300)
            st.caption("Bars: capex. Line: operating cash flow (where reported in XBRL).")
    with right:
        st.markdown("**Recent filings**")
        ft = filings[filings["ticker"] == t].sort_values("filing_date", ascending=False).head(12)
        table(pd.DataFrame({"Filed": ft["filing_date"].dt.date, "Form": ft["form"],
                            "url": ft["url"]}), height=300, url=st.column_config.LinkColumn("Filing", display_text="Open"))

    st.markdown("**Latest disclosure for each metric**")
    if dt.empty:
        st.info("No extracted disclosures for this company under the current filters.")
    else:
        dt_show = dt[dt["metric"] != "capex_reported"] if not cqt.empty else dt
        latest = dt_show.sort_values(["first_stated", "value"], ascending=[False, False]).groupby("metric").head(1)
        latest = latest.sort_values("metric", key=lambda s: s.map(list(config.METRICS).index))
        table(pd.DataFrame({"Metric": latest["metric_label"], "Value": latest["display_value"],
                            "Period / detail": latest["period"].fillna(""), "Stated": latest["first_stated"].dt.date,
                            "Mentions": latest["mentions"], "Check": latest["status"], "Quote": latest["quote"],
                            "url": latest["url"]}),
              url=LINK, Quote=st.column_config.TextColumn(width="large"),
              Mentions=st.column_config.NumberColumn(help="Number of documents that state this number"))
        with st.expander(f"All {len(dt):,} disclosures for {who}"):
            all_t = dt.sort_values("first_stated", ascending=False)
            out = pd.DataFrame({"Stated": all_t["first_stated"].dt.date, "Metric": all_t["metric_label"],
                                "Value": all_t["display_value"], "Period / detail": all_t["period"].fillna(""),
                                "Counterparty": all_t["counterparty"].fillna(""), "Check": all_t["status"],
                                "Quote": all_t["quote"], "url": all_t["url"]})
            table(out, height=420, url=LINK, Quote=st.column_config.TextColumn(width="large"))
            download(out.rename(columns={"url": "Source"}), f"disclosures_{t}")

# ---------------------------------------------------------------------------
# All disclosures
# ---------------------------------------------------------------------------

with tabs[7]:
    st.subheader("Every extracted disclosure, with its source")
    if disc.empty:
        st.info("No disclosures for the current selection.")
    else:
        c1, c2, c3 = st.columns([2, 3, 1])
        sel_metrics = c1.multiselect("Metrics", list(METRIC_LABELS), format_func=METRIC_LABELS.get, key="ex_metrics")
        query = c2.text_input("Search quotes, counterparties and periods", key="ex_query",
                              placeholder="e.g. Microsoft, Abilene, term loan, 2026")
        raw = c3.toggle("Show every mention", key="ex_raw",
                        help="Off: each distinct number once, dated to its first filing. On: one row per document.")
        ex = facts.assign(first_stated=facts["filing_date"], mentions=1,
                          metric_label=facts["metric"].map(METRIC_LABELS),
                          display_value=[fmt_value(v, u) for v, u in zip(facts["value"], facts["unit"])]) if raw else disc
        if sel_metrics:
            ex = ex[ex["metric"].isin(sel_metrics)]
        if query:
            hay = (ex["quote"].fillna("") + " " + ex["counterparty"].fillna("") + " " + ex["period"].fillna("")
                   + " " + ex["name"].fillna("") + " " + ex["site"].fillna("")).str.lower()
            ex = ex[hay.str.contains(query.lower(), regex=False)]
        ex = ex.sort_values("first_stated", ascending=False)
        st.caption(f"{len(ex):,} {'mentions' if raw else 'disclosures'}")
        out = pd.DataFrame({
            "Stated": ex["first_stated"].dt.date, "Company": ex["name"], "Metric": ex["metric_label"],
            "Value": ex["display_value"], "Period / detail": ex["period"].fillna(""),
            "Counterparty": ex["counterparty"].fillna(""), "Mentions": ex["mentions"], "Check": ex["status"],
            "Quote": ex["quote"], "url": ex["url"],
        })
        table(out, height=560, url=LINK, Quote=st.column_config.TextColumn(width="large"))
        download(out.rename(columns={"url": "Source"}).assign(fact_id=ex["fact_id"].values), "disclosures")

# ---------------------------------------------------------------------------
# Methodology
# ---------------------------------------------------------------------------

with tabs[8]:
    st.subheader("How this dashboard is built")
    a, b = st.columns(2, gap="large")
    with a:
        st.markdown(
            """
**1. Collect.** Every relevant filing since January 2024 by the tracked companies is downloaded from
SEC EDGAR: earnings releases, deal announcements and financings (8-K and 6-K), and quarterly and annual
reports (10-Q, 10-K and 20-F) for lease commitments, capacity and backlog. Contracts and financings are taken
from announcements only, because quarterly reports restate older deals.

**2. Quarterly capex (no AI involved).** Capital expenditures and operating cash flow come from the
SEC's XBRL financial data, the tagged numbers in each 10-Q and 10-K. Year-to-date figures are differenced
into single quarters. Nebius files annual XBRL only, so its quarterly capex comes from its press releases.

**3. Extraction.** Paragraphs that mention capex, power, GPUs, backlog, leases, contracts or financing are sent to
Claude (Anthropic), which records each fact in a fixed format and must copy the sentence it came from,
word for word.

**4. Checking.** Every fact is checked automatically against the filing text:
            """
        )
        st.markdown(
            "| Check | Meaning |\n|---|---|\n"
            "| **Verified** | The quote appears word for word in the filing, **and** the number appears in the quote |\n"
            "| **Partial** | Only one of the two checks passed. Hidden by default |\n"
            "| **Unverified** | Neither check passed. Never shown |"
        )
        st.markdown(
            """
**5. Deduplication.** Companies repeat the same number in the press release, the investor deck and later
releases. Each distinct number is shown once, dated to the first filing that stated it; *Mentions* counts the
documents that repeat it.

**6. Guidance revisions.** Each capex guidance statement is compared with the same company's previous
statement for the same period: raised, lowered or maintained. This is computed from the numbers, not taken
from the company's wording.
            """
        )
    with b:
        st.markdown("**What each metric means**")
        table(pd.DataFrame({"Metric": [v["label"] for v in config.METRICS.values()],
                            "Unit": [v["unit"].replace("_", " ") for v in config.METRICS.values()],
                            "Definition": [v["desc"] for v in config.METRICS.values()]}),
              Definition=st.column_config.TextColumn(width="large"))
        st.markdown("**Known limitations**")
        st.markdown(
            """
- **Earnings calls are not covered.** Much capex guidance (Microsoft especially) is given only on calls,
  which are not SEC filings.
- **Definitions differ by company.** "Contracted MW" may be gross or critical IT load; backlog for
  hyperscalers is company-wide RPO, not AI-only.
- **Filing lag.** Companies report quarterly; the latest quarter appears 3 to 6 weeks after it ends.
            """
        )

    st.subheader("Data quality")
    s = scores(facts_all, data["reviews"], data["misses"])
    s["metric"] = s["metric"].map(lambda m: METRIC_LABELS.get(m, "All metrics"))
    allrow = s[s["metric"] == "All metrics"].iloc[0]
    reviewed = allrow["precision"] == allrow["precision"]
    q = st.columns(4 if reviewed else 2)
    q[0].metric("Facts extracted (all mentions)", f"{int(allrow['facts']):,}")
    q[1].metric("Passed both checks", pct(allrow["verified"], False))
    if reviewed:
        q[2].metric("Precision (human-reviewed)", pct(allrow["precision"], False),
                    help="Share of reviewed facts a person marked correct.")
        q[3].metric("Recall (human-reviewed)", pct(allrow["recall"], False) if allrow["recall"] == allrow["recall"] else "—",
                    help="Share of the facts in reviewed documents that the model found.")
    stat = facts_all.groupby(["metric", "status"], as_index=False).size()
    stat["metric"] = stat["metric"].map(lambda m: METRIC_LABELS.get(m, m))
    c = alt.Chart(stat).mark_bar().encode(
        y=alt.Y("metric:N", title=None, axis=alt.Axis(labelOverlap=False, labelLimit=200)),
        x=alt.X("size:Q", title="Share of facts", stack="normalize", axis=alt.Axis(format="%", tickCount=5)),
        color=alt.Color("status:N", scale=STATUS_COLORS, title=None, legend=alt.Legend(orient="bottom")),
        tooltip=[alt.Tooltip("metric:N", title="Metric"), alt.Tooltip("status:N", title="Check"),
                 alt.Tooltip("size:Q", title="Facts")])
    chart(c, alt.Step(28))

    st.subheader("Coverage by company")
    docs = data["documents"]
    xq_cap = cq_all.groupby("ticker").agg(quarters=("quarter", "count"), latest=("quarter", "max"),
                                          source=("source", lambda s: "Press releases" if (s == "Press release").all() else "SEC XBRL"))
    cov = companies[["ticker", "name", "group"]].merge(
        docs.groupby("ticker").agg(documents=("doc_id", "count"), extracted=("extracted_at", "count")).reset_index(),
        on="ticker", how="left").merge(xq_cap.reset_index(), on="ticker", how="left").merge(
        A.disclosures(facts_all[facts_all["status"] == "verified"]).groupby("ticker").size().rename("disclosures").reset_index(),
        on="ticker", how="left")
    table(pd.DataFrame({"Company": cov["name"], "Type": cov["group"], "Filings read": cov["documents"],
                        "Verified disclosures": cov["disclosures"], "Capex quarters": cov["quarters"],
                        "Latest capex quarter": cov["latest"], "Capex source": cov["source"]}))
    if not corrections.empty:
        st.subheader("Excluded figures")
        st.markdown("<div class='subtle'>Figures removed on review because they were filed under the wrong metric. "
                    "They are left out everywhere in the dashboard.</div>", unsafe_allow_html=True)
        fx = data["facts"].set_index("fact_id")
        cor = corrections.assign(
            company=corrections["fact_id"].map(fx["ticker"]).map(NAME),
            metric=corrections["fact_id"].map(fx["metric"]).map(METRIC_LABELS),
            value=[fmt_value(fx.at[i, "value"], fx.at[i, "unit"]) if i in fx.index else "—" for i in corrections["fact_id"]],
            url=corrections["fact_id"].map(fx["url"]))
        table(pd.DataFrame({"Company": cor["company"], "Metric": cor["metric"], "Value": cor["value"],
                            "Reason": cor["reason"], "Date": cor["date"], "url": cor["url"]}),
              url=LINK, Reason=st.column_config.TextColumn(width="large"))

