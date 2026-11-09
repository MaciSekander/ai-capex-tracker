"""AI Capex & Capacity Tracker: Streamlit dashboard.

Reads the pipeline's tables from DuckDB: the local pipeline database when it exists,
otherwise the published snapshot in app_data/ (what Streamlit Community Cloud serves).
Falls back to the fictional demo database when neither has data. Uses Altair syntax
that works on Altair 4 and 5.
"""

import os
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import numpy as np
import pandas as pd
import streamlit as st

from tracker import config
from tracker.evaluate import scores

try:
    st.set_page_config(page_title="AI Capex & Capacity Tracker", layout="wide")
except Exception:
    pass

TABLES = ("companies", "filings", "documents", "xbrl_quarterly", "facts", "reviews", "misses")
GROUP_COLORS = alt.Scale(domain=["Hyperscaler", "Neocloud", "AI infrastructure"], range=["#1f6f8b", "#6a55c9", "#c2410c"])
STATUS_COLORS = alt.Scale(domain=["verified", "partial", "unverified"], range=["#2f7d4f", "#b7791f", "#c2410c"])
CHANGE_SHAPES = alt.Scale(domain=["new", "raised", "reaffirmed", "lowered", "not_stated"],
                          range=["circle", "triangle-up", "diamond", "triangle-down", "square"])


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
xq = data["xbrl_quarterly"].merge(companies[["ticker", "name", "group"]], on="ticker", how="left")
facts = data["facts"].merge(companies[["ticker", "name", "group"]], on="ticker", how="left")
for col in ("filing_date",):
    if col in facts:
        facts[col] = pd.to_datetime(facts[col])
if "end" in xq:
    xq["end"] = pd.to_datetime(xq["end"])


def fmt_b(v):
    return "—" if v is None or v != v else (f"${v:,.0f}B" if abs(v) >= 100 else f"${v:,.1f}B")


def chart(c, height=320):
    st.altair_chart(c.properties(height=height), use_container_width=True)


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------

sb = st.sidebar
sb.header("Filters")
groups = [g for g in ["Hyperscaler", "Neocloud", "AI infrastructure"] if g in set(companies["group"])]
sel_groups = sb.multiselect("Company type", groups, default=groups, key="groups")
pool = companies[companies["group"].isin(sel_groups)]
sel_names = sb.multiselect("Companies", pool["name"].tolist(), default=pool["name"].tolist(), key="names")
sel_tickers = companies.loc[companies["name"].isin(sel_names), "ticker"].tolist()
status_choice = sb.radio("Facts shown", ["Verified only", "Verified and partial", "All"], index=1, key="status",
                         help="Verified: the quote is in the filing and the number is in the quote. "
                              "Partial: one of the two checks passed.")
allowed_status = {"Verified only": ["verified"], "Verified and partial": ["verified", "partial"],
                  "All": ["verified", "partial", "unverified"]}[status_choice]
since = sb.date_input("Filings since", value=date(2024, 1, 1), key="since")
sb.caption(f"Data: {Path(path).parent.name}/{Path(path).name}")

xq_f = xq[xq["ticker"].isin(sel_tickers)]
facts_f = facts[facts["ticker"].isin(sel_tickers) & facts["status"].isin(allowed_status)
                & (facts["filing_date"] >= pd.Timestamp(since))]

# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------

st.title("AI Capex & Capacity Tracker")
st.caption(
    "Capex, guidance, power capacity, GPU counts, contracts and financings for hyperscalers, neoclouds and AI "
    "infrastructure companies. Quarterly capex comes from SEC XBRL data; everything else is extracted from 8-K and "
    "6-K filings by an LLM and checked against the source text."
)
if is_demo:
    st.warning("Demo data: every company, number and quote here is fictional. Run the pipeline to load real filings.")

if xq_f.empty and facts_f.empty:
    st.info("No data for the current filters.")
    st.stop()

# ---------------------------------------------------------------------------
# KPIs
# ---------------------------------------------------------------------------


def ttm(df: pd.DataFrame, metric: str, tickers: list, offset: int = 0) -> float:
    """Sum of the latest four quarters (shifted back ``offset`` quarters) across companies."""
    total = 0.0
    for t in tickers:
        s = df[(df["ticker"] == t) & (df["metric"] == metric)].sort_values("end")["value_usd_billion"]
        if len(s) >= 4 + offset:
            total += s.iloc[len(s) - 4 - offset: len(s) - offset].sum()
    return total


hyp = [t for t in sel_tickers if t in set(companies.loc[companies["group"] == "Hyperscaler", "ticker"])]
others = [t for t in sel_tickers if t not in hyp]
capex_ttm, capex_prev = ttm(xq, "capex", hyp), ttm(xq, "capex", hyp, 4)
ocf_ttm = ttm(xq, "operating_cash_flow", hyp)
other_ttm, other_prev = ttm(xq, "capex", others), ttm(xq, "capex", others, 4)
recent = facts_f[(facts_f["metric"] == "capex_guidance") & (facts_f["filing_date"] >= facts_f["filing_date"].max() - pd.Timedelta(days=120))]
raised = int((recent["change"] == "raised").sum()) if len(recent) else 0

k = st.columns(4)
k[0].metric("Hyperscaler capex, last 4Q", fmt_b(capex_ttm),
            f"{(capex_ttm / capex_prev - 1) * 100:+.0f}% year over year" if capex_prev else None)
k[1].metric("Hyperscaler capex / operating cash flow", f"{capex_ttm / ocf_ttm * 100:.0f}%" if ocf_ttm else "—")
k[2].metric("Neocloud & infra capex, last 4Q", fmt_b(other_ttm),
            f"{(other_ttm / other_prev - 1) * 100:+.0f}% year over year" if other_prev else None)
k[3].metric("Capex guidance raised, last 120 days", f"{raised} of {len(recent)}")

tabs = st.tabs(["Capex", "Guidance", "Capacity", "Contracts & financing", "Fact explorer", "Data quality"])

# ---------------------------------------------------------------------------
# Capex
# ---------------------------------------------------------------------------

with tabs[0]:
    st.subheader("Quarterly capital expenditures")
    cap = xq_f[xq_f["metric"] == "capex"].copy()
    if cap.empty:
        st.info("No XBRL capex data for the selected companies.")
    else:
        view = st.radio("View", ["By company", "By company type"], horizontal=True, key="capex_view")
        if view == "By company":
            c = alt.Chart(cap).mark_line(point=True).encode(
                x=alt.X("end:T", title="Quarter ending"),
                y=alt.Y("value_usd_billion:Q", title="Capex ($B)"),
                color=alt.Color("name:N", title=None, legend=alt.Legend(orient="top")),
                tooltip=["name", "quarter", alt.Tooltip("value_usd_billion:Q", title="$B", format=",.2f"), "method"],
            )
        else:
            agg = cap.groupby(["end", "group"], as_index=False)["value_usd_billion"].sum()
            c = alt.Chart(agg).mark_bar().encode(
                x=alt.X("yearquarter(end):O", title="Quarter ending"),
                y=alt.Y("value_usd_billion:Q", title="Capex ($B)", stack="zero"),
                color=alt.Color("group:N", scale=GROUP_COLORS, title=None, legend=alt.Legend(orient="top")),
                tooltip=["group", alt.Tooltip("end:T", title="Quarter ending"),
                         alt.Tooltip("value_usd_billion:Q", title="$B", format=",.1f")],
            )
        chart(c, 360)
        st.caption("Source: SEC XBRL cash flow statements. Year-to-date figures are differenced into quarters "
                   "(method = derived). Calendar quarter of the period end.")

        st.subheader("How much of operating cash flow goes to capex")
        both = xq_f.pivot_table(index=["ticker", "name", "end"], columns="metric", values="value_usd_billion").reset_index()
        if {"capex", "operating_cash_flow"} <= set(both.columns):
            both = both.sort_values("end")
            both["capex_4q"] = both.groupby("ticker")["capex"].transform(lambda s: s.rolling(4).sum())
            both["ocf_4q"] = both.groupby("ticker")["operating_cash_flow"].transform(lambda s: s.rolling(4).sum())
            both = both[(both["ocf_4q"] > 0) & both["capex_4q"].notna()]
            both["ratio"] = both["capex_4q"] / both["ocf_4q"]
            include_all = st.checkbox("Include neoclouds and infrastructure (log scale)", key="ocf_all",
                                      help="Their operating cash flow is small next to capex, so ratios run far above 100%.")
            if not include_all:
                both = both[both["ticker"].isin(hyp)]
            yscale = alt.Scale(type="log") if include_all else alt.Scale(zero=True)
            r = alt.Chart(both).mark_line(point=True).encode(
                x=alt.X("end:T", title="Quarter ending"),
                y=alt.Y("ratio:Q", title="Capex / operating cash flow, trailing 4 quarters", axis=alt.Axis(format="%"),
                        scale=yscale),
                color=alt.Color("name:N", title=None, legend=alt.Legend(orient="top")),
                tooltip=["name", alt.Tooltip("end:T"), alt.Tooltip("ratio:Q", format=".0%"),
                         alt.Tooltip("capex_4q:Q", title="Capex 4Q $B", format=",.1f"),
                         alt.Tooltip("ocf_4q:Q", title="OCF 4Q $B", format=",.1f")],
            )
            rule = alt.Chart(pd.DataFrame({"y": [1.0]})).mark_rule(strokeDash=[4, 4], color="#7a8794").encode(y="y:Q")
            chart(r + rule, 320)
            st.caption("Above 100%, capex exceeds operating cash flow and must be funded from cash, debt or equity. "
                       "Companies with negative operating cash flow are left out.")

# ---------------------------------------------------------------------------
# Guidance
# ---------------------------------------------------------------------------

with tabs[1]:
    st.subheader("Capex guidance over time")
    g = facts_f[facts_f["metric"] == "capex_guidance"].copy()
    if g.empty:
        st.info("No capex guidance found for the current filters.")
    else:
        g["low"] = g["value_low"].fillna(g["value"])
        g["high"] = g["value_high"].fillna(g["value"])
        base = alt.Chart(g).encode(x=alt.X("filing_date:T", title="Date stated"))
        path = base.mark_line(opacity=0.35, strokeDash=[3, 3]).encode(
            y="value:Q", color=alt.Color("name:N", title=None), detail="period:N")
        bars = base.mark_rule(strokeWidth=2).encode(
            y=alt.Y("low:Q", title="Guided capex ($B)"), y2="high:Q", color=alt.Color("name:N", title=None))
        pts = base.mark_point(filled=True, size=90).encode(
            y="value:Q", color=alt.Color("name:N", title=None, legend=alt.Legend(orient="top")),
            shape=alt.Shape("change:N", scale=CHANGE_SHAPES, title="Change"),
            tooltip=["name", "period", alt.Tooltip("value:Q", title="midpoint $B", format=",.1f"),
                     alt.Tooltip("low:Q", format=",.1f"), alt.Tooltip("high:Q", format=",.1f"), "change",
                     alt.Tooltip("filing_date:T", title="stated"), "quote"])
        chart(path + bars + pts, 380)
        st.caption("Each point is one statement; vertical lines show the guided range, and dashed lines connect statements about the same fiscal period. Hover for the quote.")
        latest = (g.sort_values("filing_date").groupby(["ticker", "period"]).tail(1)
                  .sort_values(["name", "period"]))
        latest = latest.assign(
            range=[f"${l:,.0f}–{h:,.0f}B" if l != h else f"${v:,.0f}B" for l, h, v in zip(latest["low"], latest["high"], latest["value"])],
            stated=latest["filing_date"].dt.date,
        )
        st.markdown("**Latest guidance for each period**")
        st.dataframe(latest[["name", "period", "range", "change", "stated", "quote", "url"]]
                     .rename(columns={"name": "Company", "period": "Period", "range": "Range", "change": "Change",
                                      "stated": "Stated", "quote": "Quote"}),
                     use_container_width=True, hide_index=True,
                     column_config={"url": st.column_config.LinkColumn("Filing", display_text="Open")})

# ---------------------------------------------------------------------------
# Capacity
# ---------------------------------------------------------------------------

with tabs[2]:
    st.subheader("Power capacity")
    p = facts_f[facts_f["metric"].isin(["power_active_mw", "power_contracted_mw", "power_pipeline_mw"])].copy()
    if p.empty:
        st.info("No power capacity facts for the current filters.")
    else:
        p["measure"] = p["metric"].map({"power_active_mw": "Active", "power_contracted_mw": "Contracted",
                                        "power_pipeline_mw": "Pipeline"})
        measure = st.radio("Measure", ["Active", "Contracted", "Pipeline"], horizontal=True, index=1, key="mw_measure")
        pm = p[p["measure"] == measure]
        c = alt.Chart(pm).mark_line(point=True).encode(
            x=alt.X("filing_date:T", title="Date reported"), y=alt.Y("value:Q", title=f"{measure} power (MW)"),
            color=alt.Color("name:N", title=None, legend=alt.Legend(orient="top")),
            tooltip=["name", "period", alt.Tooltip("value:Q", title="MW", format=",.0f"), "quote"])
        chart(c, 340)
        snap = (p.sort_values("filing_date").groupby(["ticker", "measure"]).tail(1)
                .pivot_table(index="name", columns="measure", values="value").rename_axis("Company").reindex(columns=["Active", "Contracted", "Pipeline"]))
        st.markdown("**Latest reported, MW**")
        st.dataframe(snap.style.format("{:,.0f}", na_rep="—"), use_container_width=True)

    st.subheader("GPUs deployed")
    gp = facts_f[facts_f["metric"] == "gpus_deployed"]
    if gp.empty:
        st.info("No GPU counts for the current filters.")
    else:
        c = alt.Chart(gp).mark_line(point=True).encode(
            x=alt.X("filing_date:T", title="Date reported"), y=alt.Y("value:Q", title="GPUs"),
            color=alt.Color("name:N", title=None, legend=alt.Legend(orient="top")),
            tooltip=["name", "period", alt.Tooltip("value:Q", format=",.0f"), "quote"])
        chart(c, 300)

    st.subheader("Revenue backlog")
    bk = facts_f[facts_f["metric"] == "backlog_usd"]
    if bk.empty:
        st.info("No backlog figures for the current filters.")
    else:
        c = alt.Chart(bk).mark_line(point=True).encode(
            x=alt.X("filing_date:T", title="Date reported"), y=alt.Y("value:Q", title="Backlog ($B)"),
            color=alt.Color("name:N", title=None, legend=alt.Legend(orient="top")),
            tooltip=["name", "period", alt.Tooltip("value:Q", title="$B", format=",.1f"), "quote"])
        chart(c, 300)

# ---------------------------------------------------------------------------
# Contracts and financing
# ---------------------------------------------------------------------------

with tabs[3]:
    deals = facts_f[facts_f["metric"].isin(["customer_contract", "financing"])].copy()
    left = right = st.container()
    with left:
        st.subheader("Customer and offtake contracts")
        cc = deals[deals["metric"] == "customer_contract"]
        if cc.empty:
            st.info("No contracts for the current filters.")
        else:
            c = alt.Chart(cc).mark_circle(opacity=0.8).encode(
                x=alt.X("filing_date:T", title="Announced"), y=alt.Y("name:N", title=None, axis=alt.Axis(labelLimit=220)),
                size=alt.Size("value:Q", title="$B", scale=alt.Scale(range=[60, 1200]), legend=None),
                color=alt.Color("group:N", scale=GROUP_COLORS, legend=None),
                tooltip=["name", "counterparty", alt.Tooltip("value:Q", title="$B", format=",.1f"), "term_years", "site", "quote"])
            chart(c, 260)
            st.caption("Bubble size is contract value. Hover for counterparty, term and quote.")
    with right:
        st.subheader("Financings")
        fin = deals[deals["metric"] == "financing"]
        if fin.empty:
            st.info("No financings for the current filters.")
        else:
            c = alt.Chart(fin).mark_circle(opacity=0.8).encode(
                x=alt.X("filing_date:T", title="Announced"), y=alt.Y("name:N", title=None, axis=alt.Axis(labelLimit=220)),
                size=alt.Size("value:Q", title="$B", scale=alt.Scale(range=[60, 1200]), legend=None),
                color=alt.Color("instrument:N", title=None, legend=alt.Legend(orient="top")),
                tooltip=["name", "instrument", "counterparty", alt.Tooltip("value:Q", title="$B", format=",.1f"), "quote"])
            chart(c, 300)
    if not deals.empty:
        table = deals.sort_values("filing_date", ascending=False).assign(
            type=deals["metric"].map({"customer_contract": "Contract", "financing": "Financing"}),
            date=deals["filing_date"].dt.date, amount=deals["value"].map(fmt_b))
        st.dataframe(table[["date", "name", "type", "amount", "counterparty", "instrument", "term_years", "quote", "url"]]
                     .rename(columns={"date": "Date", "name": "Company", "type": "Type", "amount": "Amount",
                                      "counterparty": "Counterparty", "instrument": "Instrument", "quote": "Quote"}),
                     use_container_width=True, hide_index=True,
                     column_config={"url": st.column_config.LinkColumn("Filing", display_text="Open"),
                                    "term_years": st.column_config.NumberColumn("Term (yrs)")})

# ---------------------------------------------------------------------------
# Fact explorer
# ---------------------------------------------------------------------------

with tabs[4]:
    st.subheader("Every extracted fact, with its source")
    c1, c2 = st.columns([1, 2])
    metric_labels = {k: v["label"] for k, v in config.METRICS.items()}
    sel_metrics = c1.multiselect("Metrics", list(metric_labels), format_func=metric_labels.get, key="ex_metrics")
    query = c2.text_input("Search quotes, counterparties and periods", key="ex_query")
    ex = facts_f if not sel_metrics else facts_f[facts_f["metric"].isin(sel_metrics)]
    if query:
        hay = (ex["quote"].fillna("") + " " + ex["counterparty"].fillna("") + " " + ex["period"].fillna("")).str.lower()
        ex = ex[hay.str.contains(query.lower(), regex=False)]
    ex = ex.sort_values("filing_date", ascending=False).assign(
        date=ex["filing_date"].dt.date, metric_label=ex["metric"].map(metric_labels))
    st.caption(f"{len(ex):,} facts")
    st.dataframe(ex[["date", "name", "metric_label", "value", "unit", "period", "status", "confidence", "quote", "url"]]
                 .rename(columns={"date": "Date", "name": "Company", "value": "Value", "unit": "Unit", "period": "Period",
                                  "status": "Status", "quote": "Quote"}),
                 use_container_width=True, hide_index=True, height=520,
                 column_config={"url": st.column_config.LinkColumn("Filing", display_text="Open"),
                                "metric_label": "Metric",
                                "confidence": st.column_config.NumberColumn("Confidence", format="%.2f")})

# ---------------------------------------------------------------------------
# Data quality
# ---------------------------------------------------------------------------

with tabs[5]:
    st.subheader("How far to trust the extracted facts")
    st.markdown(
        "- **Automatic checks** run on every fact: is the quote in the filing, and is the number in the quote?\n"
        "- **Human review** covers a sample of whole documents: each fact is marked correct or incorrect, and facts "
        "the model missed are added, which gives precision and recall."
    )
    s = scores(data["facts"], data["reviews"], data["misses"])
    s["metric"] = s["metric"].map(lambda m: config.METRICS.get(m, {}).get("label", "All metrics"))
    allrow = s[s["metric"] == "All metrics"].iloc[0]
    q = st.columns(4)
    q[0].metric("Facts extracted", f"{int(allrow['facts']):,}")
    q[1].metric("Verified automatically", f"{allrow['verified']:.0%}" if allrow["verified"] == allrow["verified"] else "—")
    q[2].metric("Precision (reviewed)", f"{allrow['precision']:.0%}" if allrow["precision"] == allrow["precision"] else "Not reviewed")
    q[3].metric("Recall (reviewed)", f"{allrow['recall']:.0%}" if allrow["recall"] == allrow["recall"] else "Not reviewed")
    show = s.copy()
    for col in ("verified", "quote_found", "precision", "recall", "f1"):
        show[col] = show[col].map(lambda v: "—" if v != v else f"{v:.0%}")
    st.dataframe(show.rename(columns={"metric": "Metric", "facts": "Facts", "verified": "Verified", "quote_found": "Quote found",
                                      "reviewed": "Reviewed", "missed": "Missed", "precision": "Precision",
                                      "recall": "Recall", "f1": "F1"}),
                 use_container_width=True, hide_index=True)
    stat = data["facts"].groupby(["metric", "status"], as_index=False).size()
    stat["metric"] = stat["metric"].map(lambda m: config.METRICS.get(m, {}).get("label", m))
    c = alt.Chart(stat).mark_bar().encode(
        y=alt.Y("metric:N", title=None, axis=alt.Axis(labelOverlap=False, labelLimit=200)),
        x=alt.X("size:Q", title="Share of facts", stack="normalize", axis=alt.Axis(format="%")),
        color=alt.Color("status:N", scale=STATUS_COLORS, title=None, legend=alt.Legend(orient="top")),
        tooltip=["metric", "status", "size"])
    chart(c, alt.Step(30))
    docs = data["documents"]
    if not docs.empty:
        model = docs["model"].dropna().iloc[-1] if docs["model"].notna().any() else "—"
        last = pd.to_datetime(docs["extracted_at"]).max()
        st.caption(f"{len(docs):,} documents, {int(docs['extracted_at'].notna().sum()):,} extracted. "
                   f"Model: {model}. Last extraction: {last:%Y-%m-%d %H:%M} UTC." if last == last else "")
