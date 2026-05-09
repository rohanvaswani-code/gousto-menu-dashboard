"""Gousto vs HelloFresh menu + pricing dashboard."""

import base64
import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

DATA_DIR = Path(__file__).resolve().parent / "data"
HF_DIR = DATA_DIR / "hellofresh"
PRICES_DIR = DATA_DIR / "prices"
ASSETS_DIR = Path(__file__).resolve().parent / "assets"

NUM_PORTIONS = 4
MEALS_PER_BOX = 5
SERVINGS_PER_BOX = NUM_PORTIONS * MEALS_PER_BOX  # 20
KJ_PER_KCAL = 4.184

GOUSTO_COLOR = "#E25822"
HF_COLOR = "#2EA47B"
DELTA_RED = "#C0392B"
DELTA_GREEN = "#1E8449"

st.set_page_config(page_title="Menu Miner 1.0", layout="wide")


def to_week(d):
    """Map a date to a HelloFresh-style 'YYYY-Www' week label.
    HF weeks run Thursday–Wednesday (W19 = Thu 30 Apr → Wed 6 May 2026).
    Shift forward 4 days so a Thursday lands on a Monday, then take ISO week."""
    if pd.isna(d) or not d:
        return ""
    try:
        dt = date.fromisoformat(str(d)[:10]) + timedelta(days=4)
        y, w, _ = dt.isocalendar()
        return f"{y}-W{w:02d}"
    except (ValueError, TypeError):
        return ""


@st.cache_data
def load_gousto():
    files = sorted(DATA_DIR.glob("gousto_menu_*.csv"))
    if not files:
        return pd.DataFrame()
    frames = []
    for f in files:
        df = pd.read_csv(f)
        df["_source_file"] = f.name
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)
    if "scraped_at" in df.columns:
        df["scraped_at"] = pd.to_datetime(df["scraped_at"], errors="coerce")
        df = df.sort_values("scraped_at").drop_duplicates(
            subset=["menu_week_start", "id"], keep="last"
        )
    df["menu_week_start"] = df["menu_week_start"].astype(str)
    df["week"] = df["menu_week_start"].apply(to_week)
    for c in [
        "kcal_per_portion", "portion_weight_g", "protein_g_per_portion",
        "fat_g_per_portion", "carbs_g_per_portion", "fibre_g_per_portion",
        "salt_g_per_portion", "prep_time_min", "rating_avg", "rating_count",
    ]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")
    return df


@st.cache_data
def load_hellofresh():
    if not HF_DIR.exists():
        return pd.DataFrame()
    files = sorted(HF_DIR.glob("*.csv"))
    if not files:
        return pd.DataFrame()
    frames = [pd.read_csv(f) for f in files]
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["week", "slot_number", "recipe_title"], keep="last")
    # CSV's `calories` column is kJ per serving (per HF labelling); convert to kcal.
    df["kj_per_serving"] = pd.to_numeric(df["calories"], errors="coerce")
    df["kcal_per_serving"] = df["kj_per_serving"] / KJ_PER_KCAL
    df["grams_per_serving"] = pd.to_numeric(df["grams"], errors="coerce")
    df["week"] = df["week"].astype(str)
    return df


@st.cache_data
def load_prices():
    """Return long-form: rows of (week, brand, box_price)."""
    if not PRICES_DIR.exists():
        return pd.DataFrame(columns=["week", "brand", "box_price"])
    rows = []
    for f in sorted(PRICES_DIR.glob("*.csv")):
        m = re.search(r"(\d{4}-W\d{2})", f.stem)
        if not m:
            continue
        week = m.group(1)
        df = pd.read_csv(f)
        if df.empty:
            continue
        first = df.iloc[0]
        for brand in ("Gousto", "HelloFresh"):
            if brand in df.columns:
                try:
                    rows.append({
                        "week": week,
                        "brand": brand,
                        "box_price": float(first[brand]),
                    })
                except (TypeError, ValueError):
                    pass
    return pd.DataFrame(rows)


def compute_weekly_metrics(gousto_df, hf_df, prices_df):
    """For each week with both a Gousto and a HelloFresh menu plus a price,
    compute price-per-serving, price-per-100-cal, price-per-100g.
    Returns long-form DataFrame with one row per (week, brand)."""
    rows = []
    if prices_df.empty:
        return pd.DataFrame(rows)
    for week in sorted(prices_df["week"].unique()):
        for brand in ("Gousto", "HelloFresh"):
            pr = prices_df[(prices_df["week"] == week) & (prices_df["brand"] == brand)]
            if pr.empty:
                continue
            box_price = pr["box_price"].iloc[0]
            if brand == "Gousto":
                m = gousto_df[gousto_df["week"] == week] if not gousto_df.empty else gousto_df
                if m.empty:
                    continue
                avg_kcal = m["kcal_per_portion"].mean()
                avg_grams = m["portion_weight_g"].mean()
            else:
                m = hf_df[hf_df["week"] == week] if not hf_df.empty else hf_df
                if m.empty:
                    continue
                avg_kcal = m["kcal_per_serving"].mean()
                avg_grams = m["grams_per_serving"].mean()
            pps = box_price / SERVINGS_PER_BOX
            rows.append({
                "week": week,
                "brand": brand,
                "box_price": box_price,
                "Per serving": pps,
                "Per 100 cal": pps / (avg_kcal / 100) if avg_kcal else None,
                "Per 100g": pps / (avg_grams / 100) if avg_grams else None,
                "avg_kcal_per_serving": avg_kcal,
                "avg_grams_per_serving": avg_grams,
                "recipes_in_menu": len(m),
            })
    return pd.DataFrame(rows)


def compute_deltas(summary_df, metrics):
    """Δ% = (HF − Gousto) / Gousto for each (week, metric)."""
    delta_rows = []
    if summary_df.empty:
        return pd.DataFrame(delta_rows)
    for week in sorted(summary_df["week"].unique()):
        wdf = summary_df[summary_df["week"] == week].set_index("brand")
        if "Gousto" not in wdf.index or "HelloFresh" not in wdf.index:
            continue
        for metric in metrics:
            gv = wdf.loc["Gousto", metric]
            hv = wdf.loc["HelloFresh", metric]
            if pd.notna(gv) and pd.notna(hv) and gv:
                delta_rows.append({"week": week, "metric": metric,
                                   "delta": (hv - gv) / gv})
    return pd.DataFrame(delta_rows)


# -------------------- LOAD --------------------
gousto_all = load_gousto()
hf_all = load_hellofresh()
prices_all = load_prices()

# Determine the latest 3 weeks present anywhere in the data — that's the
# dashboard's rolling window. Older snapshots remain in data/ for analysis.
all_weeks = sorted(
    set(gousto_all.get("week", pd.Series(dtype=str)).dropna()) |
    set(hf_all.get("week", pd.Series(dtype=str)).dropna()) |
    set(prices_all.get("week", pd.Series(dtype=str)).dropna())
)
visible_weeks = all_weeks[-3:] if len(all_weeks) >= 3 else all_weeks

gousto = gousto_all[gousto_all["week"].isin(visible_weeks)] if not gousto_all.empty else gousto_all
hf = hf_all[hf_all["week"].isin(visible_weeks)] if not hf_all.empty else hf_all
prices = prices_all[prices_all["week"].isin(visible_weeks)] if not prices_all.empty else prices_all

logo_candidates = [ASSETS_DIR / "hellofresh_logo.png",
                   ASSETS_DIR / "hellofresh_logo.svg",
                   ASSETS_DIR / "hellofresh_logo.jpg"]
logo_path = next((p for p in logo_candidates if p.exists()), None)

if logo_path is not None:
    col_title, col_logo = st.columns([5, 1])
    with col_title:
        st.title("Menu Miner 1.0 — Gousto vs HelloFresh Menu Dashboard")
    with col_logo:
        mime = "image/png" if logo_path.suffix.lower() == ".png" else f"image/{logo_path.suffix.lstrip('.')}"
        b64 = base64.b64encode(logo_path.read_bytes()).decode()
        st.markdown(
            f'<div style="text-align:right; padding-top:20px;">'
            f'<img src="data:{mime};base64,{b64}" width="160"/></div>',
            unsafe_allow_html=True,
        )
else:
    st.title("Menu Miner 1.0 — Gousto vs HelloFresh Menu Dashboard")

if gousto.empty and hf.empty:
    st.warning(
        "No data yet. Drop Gousto CSVs into `data/`, HelloFresh CSVs into "
        "`data/hellofresh/`, and pricing CSVs into `data/prices/`."
    )
    st.stop()

METRICS = ["Per serving", "Per 100 cal", "Per 100g"]
METRIC_AXIS_TITLE = {
    "Per serving": "Price per serving (£)",
    "Per 100 cal": "Price per 100 kcal (£)",
    "Per 100g": "Price per 100g (£)",
}

tab_pricing, tab_trends, tab_hf, tab_gousto = st.tabs([
    "Pricing comparison", "Historic trends", "HelloFresh recipes", "Gousto recipes",
])

# -------------------- TAB: PRICING (3-week window) --------------------
with tab_pricing:
    st.header(f"Weekly pricing comparison — {NUM_PORTIONS} people × {MEALS_PER_BOX} meals box")

    if prices.empty:
        st.warning(
            "No pricing data yet. Drop a CSV into `data/prices/` named e.g. "
            "`prices_2026-W19.csv` with header `Gousto,HelloFresh` and one row of "
            "box prices (e.g. `67.98,77.98`)."
        )
        st.stop()

    summary = compute_weekly_metrics(gousto, hf, prices)
    if summary.empty:
        st.info(
            "Pricing CSVs found, but no matching menu data for those weeks. "
            "Make sure the Gousto + HelloFresh CSVs cover the same weeks."
        )
        st.stop()

    delta_df = compute_deltas(summary, METRICS)

    # Shared legend + Δ% caption above the three panels
    legend_col, _, hint_col = st.columns([2, 4, 2])
    with legend_col:
        st.markdown(
            f"<div style='padding-top:6px'>"
            f"<span style='display:inline-block;width:14px;height:14px;background:{GOUSTO_COLOR};margin-right:6px;vertical-align:middle'></span>"
            f"<b>Gousto</b>"
            f"&nbsp;&nbsp;&nbsp;<span style='display:inline-block;width:14px;height:14px;background:{HF_COLOR};margin-right:6px;vertical-align:middle'></span>"
            f"<b>HelloFresh</b></div>",
            unsafe_allow_html=True,
        )
    with hint_col:
        st.markdown(
            "<div style='text-align:right;padding-top:8px;color:#777;font-size:13px'>Δ% = HelloFresh vs Gousto</div>",
            unsafe_allow_html=True,
        )

    panel_cols = st.columns(len(METRICS))
    for col, metric_name in zip(panel_cols, METRICS):
        df_m = summary.copy()
        weeks_sorted = sorted(df_m["week"].unique())
        max_val = float(df_m[metric_name].max())
        y_max = max_val * 1.20 if max_val > 0 else 1.0

        fig = go.Figure()
        for brand, color in [("Gousto", GOUSTO_COLOR), ("HelloFresh", HF_COLOR)]:
            df_b = (df_m[df_m["brand"] == brand]
                    .set_index("week").reindex(weeks_sorted).reset_index())
            fig.add_trace(go.Bar(
                x=df_b["week"], y=df_b[metric_name],
                name=brand, marker_color=color, showlegend=False,
                hovertemplate=f"%{{x}}<br>{brand}: £%{{y:.2f}}<extra></extra>",
            ))

        # Δ% annotation above each HF bar
        for _, r in df_m[df_m["brand"] == "HelloFresh"].iterrows():
            d_row = delta_df[(delta_df["week"] == r["week"]) &
                             (delta_df["metric"] == metric_name)]
            if d_row.empty:
                continue
            d = d_row.iloc[0]["delta"]
            fig.add_annotation(
                x=r["week"], y=r[metric_name],
                text=f"<b>{d:+.1%}</b>",
                showarrow=False, yshift=14, xshift=14,
                font=dict(color=DELTA_RED if d > 0 else DELTA_GREEN, size=12),
            )

        fig.update_layout(
            title=dict(text=metric_name, x=0.5, xanchor="center",
                       font=dict(size=16, color="#333")),
            yaxis=dict(title=METRIC_AXIS_TITLE[metric_name],
                       range=[0, y_max], tickformat="£.2f",
                       gridcolor="rgba(0,0,0,0.08)"),
            xaxis=dict(title="HelloFresh week", showgrid=False),
            barmode="group", bargap=0.30, bargroupgap=0.05,
            height=400,
            margin=dict(l=70, r=20, t=50, b=40),
            plot_bgcolor="white",
        )
        with col:
            st.plotly_chart(fig, use_container_width=True)

    # Δ% summary table
    if not delta_df.empty:
        delta_table = (delta_df.pivot(index="metric", columns="week", values="delta")
                       .reindex(METRICS))

        def color_delta(v):
            if pd.isna(v):
                return ""
            return ("background-color: #FADBD8; color: #C0392B"
                    if v > 0
                    else "background-color: #D5F5E3; color: #1E8449")

        st.subheader("Δ% summary")
        st.dataframe(
            delta_table.style.format("{:+.1%}").map(color_delta),
            use_container_width=True,
        )

    with st.expander("Underlying numbers"):
        disp = summary.copy()
        for c in ("box_price", "Per serving", "Per 100 cal", "Per 100g"):
            if c in disp.columns:
                disp[c] = disp[c].apply(
                    lambda x: f"£{x:.2f}" if pd.notna(x) else ""
                )
        for c in ("avg_kcal_per_serving", "avg_grams_per_serving"):
            if c in disp.columns:
                disp[c] = disp[c].round(1)
        st.dataframe(
            disp.rename(columns={"week": "hellofresh_week"}),
            use_container_width=True, hide_index=True,
        )


# -------------------- TAB: HISTORIC TRENDS (full data, growing) --------------------
with tab_trends:
    st.header("Historic price trends")

    full_summary = compute_weekly_metrics(gousto_all, hf_all, prices_all)
    if full_summary.empty:
        st.info(
            "Not enough data yet. Need at least one week with all three sources "
            "(Gousto + HelloFresh + price)."
        )
    else:
        n_weeks = full_summary["week"].nunique()
        st.caption(
            f"Tracking **{n_weeks} week(s)** of data. Each Tuesday/Wednesday "
            f"update extends these charts by one week."
        )

        for metric_name, axis_title in (
            ("Per 100 cal", "Price per 100 kcal (£)"),
            ("Per 100g", "Price per 100g (£)"),
        ):
            fig = go.Figure()
            for brand, color in [("Gousto", GOUSTO_COLOR), ("HelloFresh", HF_COLOR)]:
                df_b = (full_summary[full_summary["brand"] == brand]
                        .sort_values("week"))
                fig.add_trace(go.Scatter(
                    x=df_b["week"], y=df_b[metric_name],
                    name=brand, mode="lines+markers",
                    line=dict(color=color, width=3),
                    marker=dict(size=10, line=dict(color="white", width=1.5)),
                    hovertemplate=f"%{{x}}<br>{brand}: £%{{y:.2f}}<extra></extra>",
                ))
            fig.update_layout(
                title=dict(text=axis_title, x=0.5, xanchor="center",
                           font=dict(size=16, color="#333")),
                xaxis=dict(title="HelloFresh week", showgrid=False),
                yaxis=dict(title=axis_title, tickformat="£.2f",
                           gridcolor="rgba(0,0,0,0.08)"),
                height=380,
                margin=dict(l=70, r=30, t=60, b=50),
                plot_bgcolor="white",
                legend=dict(orientation="h", x=0, y=1.10,
                            yanchor="bottom", xanchor="left",
                            bgcolor="rgba(0,0,0,0)"),
                hovermode="x unified",
            )
            st.plotly_chart(fig, use_container_width=True)

        with st.expander("Underlying numbers (full history)"):
            disp = full_summary.copy()
            for c in ("box_price", "Per serving", "Per 100 cal", "Per 100g"):
                if c in disp.columns:
                    disp[c] = disp[c].apply(
                        lambda x: f"£{x:.2f}" if pd.notna(x) else ""
                    )
            for c in ("avg_kcal_per_serving", "avg_grams_per_serving"):
                if c in disp.columns:
                    disp[c] = disp[c].round(1)
            st.dataframe(
                disp.sort_values(["week", "brand"])
                    .rename(columns={"week": "hellofresh_week"}),
                use_container_width=True, hide_index=True,
            )

# -------------------- TAB: HELLOFRESH --------------------
with tab_hf:
    st.header("HelloFresh recipes by week")
    if hf.empty:
        st.info("No HelloFresh data yet. Drop CSVs into `data/hellofresh/`.")
    else:
        weeks = sorted(hf["week"].unique(), reverse=True)
        sel = st.multiselect("HelloFresh week(s)", weeks, default=weeks, key="hf_weeks")
        f = hf[hf["week"].isin(sel)]
        st.caption(
            f"{len(f):,} recipe-rows across {f['week'].nunique()} HelloFresh week(s) · "
            f"{f['recipe_title'].nunique():,} unique recipes"
        )
        show = f[["week", "slot_number", "recipe_title",
                  "kcal_per_serving", "kj_per_serving", "grams_per_serving"]].copy()
        show["kcal_per_serving"] = show["kcal_per_serving"].round(0)
        show["kj_per_serving"] = show["kj_per_serving"].round(0)
        show["grams_per_serving"] = show["grams_per_serving"].round(1)
        show = show.rename(columns={"week": "hellofresh_week"})
        st.dataframe(
            show.sort_values(["hellofresh_week", "slot_number"], ascending=[False, True]),
            use_container_width=True, hide_index=True,
        )
        st.download_button(
            "Download filtered CSV",
            show.to_csv(index=False).encode("utf-8"),
            "hellofresh_filtered.csv", "text/csv",
        )

# -------------------- TAB: GOUSTO --------------------
with tab_gousto:
    st.header("Gousto recipes by week")
    if gousto.empty:
        st.info("No Gousto data yet.")
    else:
        weeks = sorted(gousto["week"].unique(), reverse=True)
        sel = st.multiselect("HelloFresh week(s)", weeks, default=weeks, key="g_weeks")
        f = gousto[gousto["week"].isin(sel)].copy()
        # Format menu_week_start as DD/MM/YYYY
        f["menu_date"] = pd.to_datetime(f["menu_week_start"], errors="coerce").dt.strftime("%d/%m/%Y")
        st.caption(
            f"{len(f):,} recipe-rows across {f['week'].nunique()} HelloFresh week(s) · "
            f"{f['name'].nunique():,} unique recipes"
        )
        cols = [
            "week", "menu_date", "name", "food_brand",
            "kcal_per_portion", "portion_weight_g",
            "protein_g_per_portion", "fat_g_per_portion",
            "carbs_g_per_portion", "fibre_g_per_portion", "salt_g_per_portion",
            "prep_time_min", "spice_level", "dietary_claims",
            "rating_avg", "rating_count",
        ]
        cols = [c for c in cols if c in f.columns]
        out = f[cols].sort_values(["week", "name"], ascending=[False, True])
        out = out.rename(columns={"week": "hellofresh_week"})
        st.dataframe(out, use_container_width=True, hide_index=True)
        st.download_button(
            "Download filtered CSV",
            out.to_csv(index=False).encode("utf-8"),
            "gousto_filtered.csv", "text/csv",
        )

# -------------------- SIDEBAR --------------------
with st.sidebar:
    st.markdown("### About")
    st.markdown(
        "Weekly comparison of **Gousto** and **HelloFresh** menus, looking at "
        "price, calorie value, and weight value for the standard "
        "**4-person × 5-meal** subscription box (20 servings)."
    )

    st.divider()
    st.markdown("### Methodology")
    st.markdown(
        "**Box price** is the full weekly subscription price for a 4-person "
        "× 5-meal box, taken from each brand's website."
    )
    st.markdown(
        "**Price per serving** \n"
        "`= Box price ÷ 20 servings`"
    )
    st.markdown(
        "**Price per 100 kcal** \n"
        "`= Price per serving ÷ (avg kcal per serving ÷ 100)`  \n"
        "*Avg kcal per serving* is the mean across every recipe on that "
        "week's menu (≈ 270–350 recipes for Gousto, ≈ 70 for HelloFresh)."
    )
    st.markdown(
        "**Price per 100g** \n"
        "`= Price per serving ÷ (avg grams per serving ÷ 100)`  \n"
        "*Avg grams per serving* is calculated the same way."
    )
    st.markdown(
        "**Δ% (delta)** \n"
        "`= (HelloFresh − Gousto) ÷ Gousto`  \n"
        f"<span style='color:{DELTA_RED}'>**Red**</span> = HelloFresh more expensive · "
        f"<span style='color:{DELTA_GREEN}'>**Green**</span> = HelloFresh cheaper",
        unsafe_allow_html=True,
    )

    st.divider()
    st.markdown("### Data sources")
    st.markdown(
        "- **Gousto recipes**: scraped weekly from gousto.co.uk\n"
        "- **HelloFresh recipes**: internal Databricks weekly export\n"
        "- **Box prices**: manual weekly entry from each website"
    )
    st.caption(
        "The dashboard's *Pricing comparison* and *recipes* tabs show the latest "
        "**3 weeks** (current + next two). The *Historic trends* tab grows over "
        "time as new weeks are added."
    )
