"""Gousto vs HelloFresh menu + pricing dashboard."""

import base64
import json
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


def _fingerprint(root: Path, pattern: str) -> tuple:
    """A tiny hashable summary of every matching file so cache_data invalidates
    the moment any CSV is added / removed / modified (e.g. via a fresh GitHub
    Action commit landing on Streamlit Cloud). Without this the @st.cache_data
    holds stale dataframes until the process restarts."""
    if not root.exists():
        return ()
    return tuple(sorted((f.name, f.stat().st_mtime, f.stat().st_size)
                        for f in root.glob(pattern)))


@st.cache_data(ttl=600)
def load_gousto(fingerprint=None):
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
        # For each menu_week_start keep ONLY rows from the latest scrape — drop
        # earlier snapshots entirely. Row-level dedup on (week, id) would let
        # rows that exist only in older snapshots (e.g. pre-filter sub-category
        # items) survive and inflate the count.
        latest = df.groupby("menu_week_start")["scraped_at"].transform("max")
        df = df[df["scraped_at"] == latest]
    df["menu_week_start"] = df["menu_week_start"].astype(str)
    df["week"] = df["menu_week_start"].apply(to_week)
    for c in [
        "kcal_per_portion", "portion_weight_g", "protein_g_per_portion",
        "fat_g_per_portion", "carbs_g_per_portion", "fibre_g_per_portion",
        "salt_g_per_portion", "prep_time_min", "rating_avg", "rating_count",
        "surcharge_per_portion_gbp",
    ]:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c], errors="coerce")

    # Derive a presentation-friendly tier label. CSVs scraped before May 2026
    # don't carry surcharge data; mark those as "Unknown" rather than misleading
    # the user into thinking everything is "Core".
    if "is_surcharge" in df.columns:
        df["tier"] = df["is_surcharge"].map({True: "Premium", False: "Core"})
        df["tier"] = df["tier"].fillna("Unknown")
    else:
        df["tier"] = "Unknown"
        df["is_surcharge"] = pd.NA
        df["surcharge_per_portion_gbp"] = pd.NA
    return df


@st.cache_data(ttl=600)
def load_hellofresh(fingerprint=None):
    if not HF_DIR.exists():
        return pd.DataFrame()
    # Sort by file mtime ascending so the LATEST upload's rows appear last in
    # the concat — then drop_duplicates(keep="last") keeps them. Dedupe key is
    # (week, slot_number) only: if the same slot appears in two uploads with
    # different recipe_titles (menu was edited between exports), the newer
    # upload wins per slot. Prevents inflated per-week counts when files overlap.
    files = sorted(HF_DIR.glob("*.csv"), key=lambda p: p.stat().st_mtime)
    if not files:
        return pd.DataFrame()
    frames = [pd.read_csv(f) for f in files]
    df = pd.concat(frames, ignore_index=True)
    df = df.drop_duplicates(subset=["week", "slot_number"], keep="last")
    # CSV's `calories` column is kJ per serving (per HF labelling); convert to kcal.
    df["kj_per_serving"] = pd.to_numeric(df["calories"], errors="coerce")
    df["kcal_per_serving"] = df["kj_per_serving"] / KJ_PER_KCAL
    df["grams_per_serving"] = pd.to_numeric(df["grams"], errors="coerce")
    df["week"] = df["week"].astype(str)
    # Tier mapping: HelloFresh slots 1-71 are core recipes; 72-80 are premium
    # (surcharge) recipes. This is fixed at the slot-numbering level.
    slot_num = pd.to_numeric(df["slot_number"], errors="coerce")
    df["tier"] = slot_num.apply(
        lambda s: "Premium" if pd.notna(s) and s >= 72 else
                  ("Core" if pd.notna(s) else "Unknown")
    )
    return df


@st.cache_data(ttl=600)
def load_prices(fingerprint=None):
    """Return long-form: rows of (week, brand, box_config, box_price).
    Supports both formats:
      - Legacy: single row `Gousto,HelloFresh` (treated as 4x5)
      - Multi:  `box_config,Gousto,HelloFresh` with one row per config
    """
    if not PRICES_DIR.exists():
        return pd.DataFrame(columns=["week", "brand", "box_config", "box_price"])
    rows = []
    for f in sorted(PRICES_DIR.glob("*.csv")):
        m = re.search(r"(\d{4}-W\d{2})", f.stem)
        if not m:
            continue
        week = m.group(1)
        df = pd.read_csv(f)
        if df.empty:
            continue
        has_config = "box_config" in df.columns
        for _, row in df.iterrows():
            box_config = str(row["box_config"]).strip() if has_config else "4x5"
            for brand in ("Gousto", "HelloFresh"):
                if brand in df.columns:
                    try:
                        rows.append({
                            "week": week,
                            "brand": brand,
                            "box_config": box_config,
                            "box_price": float(row[brand]),
                        })
                    except (TypeError, ValueError):
                        pass
    return pd.DataFrame(rows)


BOX_CONFIGS = {
    "4x5": {"label": "4 people × 5 meals",
            "portions": 4, "meals": 5, "servings": 20},
    "2x3": {"label": "2 people × 3 meals",
            "portions": 2, "meals": 3, "servings": 6},
}


def compute_weekly_metrics(gousto_df, hf_df, prices_df, tier="Total", box_config="4x5"):
    """For each week with both a Gousto and a HelloFresh menu plus a price,
    compute price-per-serving, price-per-100-cal, price-per-100g.
    Returns long-form DataFrame with one row per (week, brand).
    `tier` controls which recipes are averaged when computing avg_kcal / avg_grams:
      - "Total"   : every recipe on the menu (including Unknown for older scrapes)
      - "Core"    : non-surcharge recipes only
      - "Premium" : surcharge recipes only"""
    rows = []
    if prices_df.empty:
        return pd.DataFrame(rows)

    # Filter prices down to the requested box config; legacy CSVs default
    # to '4x5' during load, so this is always safe.
    if "box_config" in prices_df.columns:
        prices_df = prices_df[prices_df["box_config"] == box_config]
    if prices_df.empty:
        return pd.DataFrame(rows)

    servings = BOX_CONFIGS.get(box_config, {}).get("servings", 20)

    def _gousto_has_tier(week):
        """True if Gousto's scrape for this week carries Core/Premium info."""
        if gousto_df.empty or "tier" not in gousto_df.columns:
            return False
        g_wk = gousto_df[gousto_df["week"] == week]
        if g_wk.empty:
            return False
        return g_wk["tier"].isin(["Core", "Premium"]).any()

    for week in sorted(prices_df["week"].unique()):
        # Per-week methodology, driven by Gousto data availability. If Gousto's
        # scrape for this week has surcharge tracking, BOTH brands filter to
        # the requested tier (apples-to-apples). If it doesn't, BOTH brands
        # average across all recipes — HF must drop its tier filter so it
        # isn't unfairly compared against an all-recipes Gousto average.
        if tier == "Total":
            apply_tier = False
            methodology = "All recipes"
            applied_tier_label = "All recipes"
        elif _gousto_has_tier(week):
            apply_tier = True
            methodology = f"{tier} only"
            applied_tier_label = tier
        else:
            apply_tier = False
            methodology = "All recipes (estimated — premium tracking added W22)"
            applied_tier_label = "All recipes"

        for brand in ("Gousto", "HelloFresh"):
            pr = prices_df[(prices_df["week"] == week) & (prices_df["brand"] == brand)]
            if pr.empty:
                continue
            box_price = pr["box_price"].iloc[0]
            if brand == "Gousto":
                m = gousto_df[gousto_df["week"] == week] if not gousto_df.empty else gousto_df
                if apply_tier and "tier" in m.columns:
                    m = m[m["tier"] == tier]
                if m.empty:
                    continue
                avg_kcal = m["kcal_per_portion"].mean()
                avg_grams = m["portion_weight_g"].mean()
            else:
                m = hf_df[hf_df["week"] == week] if not hf_df.empty else hf_df
                if apply_tier and "tier" in m.columns:
                    m = m[m["tier"] == tier]
                if m.empty:
                    continue
                avg_kcal = m["kcal_per_serving"].mean()
                avg_grams = m["grams_per_serving"].mean()
            pps = box_price / servings
            rows.append({
                "week": week,
                "brand": brand,
                "tier": applied_tier_label,
                "box_config": box_config,
                "core_methodology": methodology,
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
# Pass file fingerprints so @st.cache_data automatically invalidates whenever
# a CSV is added/modified (e.g. the Wednesday auto-scrape commit). Also a
# 10-minute TTL as a belt-and-braces fallback.
gousto_all = load_gousto(_fingerprint(DATA_DIR, "gousto_menu_*.csv"))
hf_all = load_hellofresh(_fingerprint(HF_DIR, "*.csv"))
prices_all = load_prices(_fingerprint(PRICES_DIR, "*.csv"))

# Box-config selector rendered FIRST so all pricing precomputes below react to
# it. Sits at the top of the sidebar because sidebar blocks are emitted in file
# order and multiple `with st.sidebar:` blocks append to the same panel.
with st.sidebar:
    st.markdown("### Box configuration")
    selected_box_config = st.radio(
        "Box size for pricing views",
        options=list(BOX_CONFIGS.keys()),
        format_func=lambda k: BOX_CONFIGS[k]["label"],
        key="box_config",
        label_visibility="collapsed",
    )
    st.caption(
        f"Applies to the **Pricing comparison** and **Historic trends** tabs "
        f"only. Currently viewing: **{BOX_CONFIGS[selected_box_config]['label']}** "
        f"({BOX_CONFIGS[selected_box_config]['servings']} servings)."
    )
    st.divider()

# Filter prices to the selected box config so weeks lacking a price row for
# this config drop out of the pricing views entirely.
if "box_config" in prices_all.columns:
    prices_for_config = prices_all[
        prices_all["box_config"] == selected_box_config
    ].copy()
else:
    prices_for_config = prices_all.copy()

# The 3-week rolling window is now ONLY applied to the pricing-tab bar charts +
# Δ% summary. Every other surface (underlying numbers tables, historic trends,
# HelloFresh recipes, Gousto recipes) shows the full history so older weeks
# are preserved as new ones land.
all_weeks = sorted(
    set(gousto_all.get("week", pd.Series(dtype=str)).dropna()) |
    set(hf_all.get("week", pd.Series(dtype=str)).dropna()) |
    set(prices_for_config.get("week", pd.Series(dtype=str)).dropna())
)
gousto_weeks = set(gousto_all.get("week", pd.Series(dtype=str)).dropna())
hf_weeks = set(hf_all.get("week", pd.Series(dtype=str)).dropna())
price_weeks = set(prices_for_config.get("week", pd.Series(dtype=str)).dropna())
comparable_weeks = sorted(gousto_weeks & hf_weeks & price_weeks)
visible_weeks = comparable_weeks[-3:] if len(comparable_weeks) >= 3 else comparable_weeks
if not visible_weeks:
    visible_weeks = all_weeks[-3:]

gousto_window = gousto_all[gousto_all["week"].isin(visible_weeks)] if not gousto_all.empty else gousto_all
hf_window = hf_all[hf_all["week"].isin(visible_weeks)] if not hf_all.empty else hf_all
prices_window = prices_for_config[prices_for_config["week"].isin(visible_weeks)] if not prices_for_config.empty else prices_for_config

TIERS = ("Total", "Core", "Premium")
summary_window_by_tier = {
    t: compute_weekly_metrics(gousto_window, hf_window, prices_window,
                              tier=t, box_config=selected_box_config)
    for t in TIERS
}
summary_full_by_tier = {
    t: compute_weekly_metrics(gousto_all, hf_all, prices_for_config,
                              tier=t, box_config=selected_box_config)
    for t in TIERS
}
summary_window = summary_window_by_tier["Total"]  # back-compat alias
summary_full = summary_full_by_tier["Total"]      # back-compat alias

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

if gousto_all.empty and hf_all.empty:
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

tab_pricing, tab_trends, tab_hf, tab_gousto, tab_ingredients = st.tabs([
    "Pricing comparison", "Historic trends", "HelloFresh recipes",
    "Gousto recipes", "Gousto ingredients",
])

# -------------------- TAB: PRICING (3-week window) --------------------
with tab_pricing:
    _cfg = BOX_CONFIGS[selected_box_config]
    st.header(f"Weekly pricing comparison — {_cfg['label']} box")

    if prices_all.empty:
        st.warning(
            "No pricing data yet. Drop a CSV into `data/prices/` named e.g. "
            "`prices_2026-W19.csv` with header `Gousto,HelloFresh` and one row of "
            "box prices (e.g. `67.98,77.98`)."
        )
        st.stop()

    summary_window = summary_window_by_tier["Core"]
    if summary_window.empty:
        st.info(
            "Pricing CSVs found, but no matching menu data for those weeks. "
            "Make sure the Gousto + HelloFresh CSVs cover the same weeks."
        )
        st.stop()

    # If any week in the rolling 3-week window was averaged off Unknown rows,
    # flag it (only happens if the rolling window dips back into pre-W22 data).
    window_est = summary_window["core_methodology"].str.startswith(
        "All recipes", na=False
    )
    if window_est.any():
        est_weeks_here = sorted(summary_window.loc[window_est, "week"].unique())
        st.warning(
            f"⚠ Mixed methodology in this 3-week window. {', '.join(est_weeks_here)} "
            f"averaged BOTH brands across all menu recipes (premium tracking "
            f"added W22); other weeks use Core only.",
            icon="⚠",
        )

    st.caption(
        "Averaged across **Core recipes only** — the standard menu items the "
        "subscription box price buys, with no surcharge upgrades."
    )

    delta_df = compute_deltas(summary_window, METRICS)

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
        weeks_sorted = sorted(summary_window["week"].unique())
        max_val = float(summary_window[metric_name].max())
        y_max = max_val * 1.20 if max_val > 0 else 1.0

        fig = go.Figure()
        for brand, color in [("Gousto", GOUSTO_COLOR), ("HelloFresh", HF_COLOR)]:
            df_b = (summary_window[summary_window["brand"] == brand]
                    .set_index("week").reindex(weeks_sorted).reset_index())
            fig.add_trace(go.Bar(
                x=df_b["week"], y=df_b[metric_name],
                name=brand, marker_color=color, showlegend=False,
                hovertemplate=f"%{{x}}<br>{brand}: £%{{y:.2f}}<extra></extra>",
            ))

        for _, r in summary_window[summary_window["brand"] == "HelloFresh"].iterrows():
            if delta_df.empty:
                continue
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

    with st.expander("Underlying numbers (full history — Core recipes only)"):
        st.caption(
            "The `core_methodology` column flags how each row's averages were "
            "computed. **Core only** = exact (surcharge tracking enabled). "
            "**All recipes (estimated…)** = pre-W22 data, averaged across all "
            "menu recipes because Core/Premium wasn't yet distinguishable."
        )
        disp = summary_full_by_tier["Core"].copy()
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


# -------------------- TAB: HISTORIC TRENDS (full data, growing) --------------------
with tab_trends:
    st.header(f"Historic price trends — {BOX_CONFIGS[selected_box_config]['label']}")

    full_summary = summary_full_by_tier["Core"]
    if full_summary.empty:
        st.info(
            "Not enough data yet. Need at least one week with all three sources "
            "(Gousto + HelloFresh + price)."
        )
    else:
        n_weeks = full_summary["week"].nunique()
        # Detect any week whose Gousto average was computed off Unknown rows
        # (i.e. CSVs scraped before surcharge tracking added on 2026-W22).
        est_mask = full_summary["core_methodology"].str.startswith(
            "All recipes", na=False
        )
        estimated_weeks = sorted(full_summary.loc[est_mask, "week"].unique())

        st.caption(
            f"Tracking **{n_weeks} week(s)** — averaged across **Core recipes "
            f"only**. Each Tuesday/Wednesday update extends these charts by "
            f"one week."
        )
        if estimated_weeks:
            st.warning(
                f"⚠ **Methodology shift at W22**. Surcharge tracking for Gousto "
                f"recipes was added on **2026-05-31** (W22). For weeks **before "
                f"W22** ({', '.join(estimated_weeks)}), **both brands** average "
                f"across all menu recipes (Gousto ~189, HelloFresh 80) so the "
                f"comparison stays apples-to-apples. From **W22 onwards**, "
                f"both brands average Core recipes only (Gousto ~150, "
                f"HelloFresh 71). **Hollow markers** in the charts below mark "
                f"the all-recipes weeks; **filled markers** are Core-only.",
                icon="⚠",
            )

        for metric_name, axis_title in (
            ("Per serving", "Price per serving (£) — incl. shipping"),
            ("Per 100 cal", "Price per 100 kcal (£)"),
            ("Per 100g", "Price per 100g (£)"),
        ):
            fig = go.Figure()
            for brand, color in [("Gousto", GOUSTO_COLOR), ("HelloFresh", HF_COLOR)]:
                df_b = (full_summary[full_summary["brand"] == brand]
                        .sort_values("week")
                        .reset_index(drop=True))
                # Per-point styling: hollow circle if this row's average was
                # taken off Unknown rows (estimated); filled circle if exact.
                is_estimated = df_b["core_methodology"].str.startswith(
                    "All recipes", na=False
                )
                symbols = ["circle-open" if e else "circle" for e in is_estimated]
                sizes = [11 if e else 10 for e in is_estimated]
                line_widths = [2.5 if e else 1.5 for e in is_estimated]
                hover = [
                    f"{brand}: £{y:.2f}<br>"
                    + ("<i>estimated — pre-W22 all-recipe avg</i>"
                       if e else "exact — Core only")
                    for y, e in zip(df_b[metric_name], is_estimated)
                ]
                fig.add_trace(go.Scatter(
                    x=df_b["week"], y=df_b[metric_name],
                    name=brand, mode="lines+markers",
                    line=dict(color=color, width=3),
                    marker=dict(size=sizes, symbol=symbols,
                                color=color,
                                line=dict(color=color, width=line_widths)),
                    text=hover, hovertemplate="%{x}<br>%{text}<extra></extra>",
                ))

            # Vertical "tracking begins" annotation if there's a transition
            if estimated_weeks and "2026-W22" in full_summary["week"].values:
                fig.add_vline(
                    x="2026-W22", line=dict(color="#888", dash="dash", width=1),
                )
                fig.add_annotation(
                    x="2026-W22", y=1.02, xref="x", yref="paper",
                    text="Tier tracking begins →",
                    showarrow=False, xanchor="left",
                    font=dict(size=11, color="#666"),
                )

            # Vertical marker at each known price change. HelloFresh's headline
            # price rose at W33 for both box configs (£77.98 → £84.79 for 4x5;
            # £37.98 → £37.93 for 2x3, i.e. a small drop for 2x3). Both moves
            # show up as a step on the HF line.
            PRICE_CHANGES = {"2026-W33": "HF price change →"}
            for change_week, label in PRICE_CHANGES.items():
                if change_week in full_summary["week"].values:
                    fig.add_vline(
                        x=change_week,
                        line=dict(color=HF_COLOR, dash="dot", width=1.5),
                    )
                    fig.add_annotation(
                        x=change_week, y=1.02, xref="x", yref="paper",
                        text=label, showarrow=False, xanchor="left",
                        font=dict(size=11, color=HF_COLOR),
                    )

            fig.update_layout(
                title=dict(text=axis_title, x=0.5, xanchor="center",
                           font=dict(size=16, color="#333")),
                xaxis=dict(title="HelloFresh week", showgrid=False),
                yaxis=dict(title=axis_title, tickformat="£.2f",
                           gridcolor="rgba(0,0,0,0.08)"),
                height=400,
                margin=dict(l=70, r=30, t=70, b=50),
                plot_bgcolor="white",
                legend=dict(orientation="h", x=0, y=1.10,
                            yanchor="bottom", xanchor="left",
                            bgcolor="rgba(0,0,0,0)"),
                hovermode="x unified",
            )
            st.plotly_chart(fig, use_container_width=True)

        # -------- Δ% summary chart (HF vs Gousto for all 3 metrics) --------
        delta_rows = []
        for week in sorted(full_summary["week"].unique()):
            wdf = full_summary[full_summary["week"] == week].set_index("brand")
            if "Gousto" not in wdf.index or "HelloFresh" not in wdf.index:
                continue
            for metric in ("Per serving", "Per 100 cal", "Per 100g"):
                gv = wdf.loc["Gousto", metric]
                hv = wdf.loc["HelloFresh", metric]
                if pd.notna(gv) and pd.notna(hv) and gv:
                    delta_rows.append({"week": week, "metric": metric,
                                       "delta_pct": (hv - gv) / gv * 100})
        delta_over_time = pd.DataFrame(delta_rows)

        if not delta_over_time.empty:
            METRIC_COLORS = {
                "Per serving": "#5B8DEF",  # blue
                "Per 100 cal": "#F2A93B",  # amber
                "Per 100g": "#9B6BD8",     # purple
            }
            fig_delta = go.Figure()
            fig_delta.add_hline(y=0, line=dict(color="#999", width=1))
            for metric, color in METRIC_COLORS.items():
                dm = delta_over_time[delta_over_time["metric"] == metric].sort_values("week").copy()
                if dm.empty:
                    continue
                # Pre-format label so tooltip always shows exactly 1dp regardless
                # of Plotly's own numeric formatting decisions.
                dm["label"] = dm["delta_pct"].apply(lambda v: f"{v:+.1f}%")
                fig_delta.add_trace(go.Scatter(
                    x=dm["week"], y=dm["delta_pct"],
                    name=metric, mode="lines+markers",
                    line=dict(color=color, width=3),
                    marker=dict(size=9, line=dict(color="white", width=1.5)),
                    text=dm["label"],
                    hovertemplate=f"{metric}: %{{text}}<extra></extra>",
                ))

            # Carry over the same markers (tier tracking / price change) so
            # everyone reading the chart knows when a step-change is
            # methodology-driven vs price-driven.
            if estimated_weeks and "2026-W22" in full_summary["week"].values:
                fig_delta.add_vline(
                    x="2026-W22", line=dict(color="#888", dash="dash", width=1),
                )
            for change_week in PRICE_CHANGES:
                if change_week in full_summary["week"].values:
                    fig_delta.add_vline(
                        x=change_week,
                        line=dict(color=HF_COLOR, dash="dot", width=1.5),
                    )

            fig_delta.update_layout(
                title=dict(
                    text="Δ% HelloFresh vs Gousto — over time",
                    x=0.5, xanchor="center", font=dict(size=16, color="#333"),
                ),
                xaxis=dict(title="HelloFresh week", showgrid=False),
                yaxis=dict(title="Δ% (positive = HF more expensive)",
                           ticksuffix="%", zeroline=False,
                           gridcolor="rgba(0,0,0,0.08)"),
                height=420,
                margin=dict(l=70, r=30, t=60, b=50),
                plot_bgcolor="white",
                legend=dict(orientation="h", x=0, y=1.10,
                            yanchor="bottom", xanchor="left",
                            bgcolor="rgba(0,0,0,0)"),
                hovermode="x unified",
            )
            st.plotly_chart(fig_delta, use_container_width=True)
            st.caption(
                "Positive values mean HelloFresh is more expensive on that "
                "metric; negative means HelloFresh is cheaper. Sign flips "
                "at the horizontal zero line."
            )

        with st.expander("Underlying numbers (full history — Core recipes only)"):
            st.caption(
                "The `core_methodology` column flags how each row's averages "
                "were computed. **Core only** = exact (surcharge tracking "
                "enabled). **All recipes (estimated…)** = pre-W22 data, "
                "averaged across all menu recipes because Core/Premium "
                "wasn't yet distinguishable."
            )
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

# -------------------- TAB: HELLOFRESH (full history) --------------------
with tab_hf:
    st.header("HelloFresh recipes by week")
    if hf_all.empty:
        st.info("No HelloFresh data yet. Drop CSVs into `data/hellofresh/`.")
    else:
        weeks = sorted(hf_all["week"].unique(), reverse=True)
        col_wk, col_tier = st.columns([3, 1])
        with col_wk:
            sel = st.multiselect("HelloFresh week(s)", weeks, default=weeks, key="hf_weeks")
        available_tiers = sorted(hf_all["tier"].dropna().unique().tolist())
        with col_tier:
            tier_sel = st.multiselect(
                "Tier", available_tiers, default=available_tiers, key="hf_tier",
                help="Premium = slots 72-80 (surcharge recipes). "
                     "Core = slots 1-71 — what the £77.98 box buys.",
            )
        f = hf_all[hf_all["week"].isin(sel) & hf_all["tier"].isin(tier_sel)]
        n_core = (f["tier"] == "Core").sum()
        n_prem = (f["tier"] == "Premium").sum()
        st.caption(
            f"{len(f):,} recipe-rows across {f['week'].nunique()} HelloFresh week(s) · "
            f"{f['recipe_title'].nunique():,} unique recipes · "
            f"**{n_core:,} Core**, **{n_prem:,} Premium**"
        )
        show = f[["week", "slot_number", "tier", "recipe_title",
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

# -------------------- TAB: GOUSTO (full history) --------------------
with tab_gousto:
    st.header("Gousto recipes by week")
    if gousto_all.empty:
        st.info("No Gousto data yet.")
    else:
        weeks = sorted(gousto_all["week"].unique(), reverse=True)
        col_wk, col_tier = st.columns([3, 1])
        with col_wk:
            sel = st.multiselect("HelloFresh week(s)", weeks, default=weeks, key="g_weeks")
        # Build tier options dynamically — only show "Unknown" if any rows lack data
        available_tiers = sorted(gousto_all["tier"].dropna().unique().tolist())
        with col_tier:
            tier_sel = st.multiselect(
                "Tier", available_tiers, default=available_tiers, key="g_tier",
                help="Premium = recipes with a surcharge (+£X/portion). "
                     "Core = no surcharge — what the £67.98 box buys.",
            )
        f = gousto_all[gousto_all["week"].isin(sel) & gousto_all["tier"].isin(tier_sel)].copy()
        # Format menu_week_start as DD/MM/YYYY
        f["menu_date"] = pd.to_datetime(f["menu_week_start"], errors="coerce").dt.strftime("%d/%m/%Y")
        # Tier-specific caption
        n_core = (f["tier"] == "Core").sum()
        n_prem = (f["tier"] == "Premium").sum()
        n_unk = (f["tier"] == "Unknown").sum()
        tier_breakdown = f"**{n_core:,} Core**, **{n_prem:,} Premium**"
        if n_unk:
            tier_breakdown += f", {n_unk:,} Unknown (older scrapes pre-surcharge tracking)"
        st.caption(
            f"{len(f):,} recipe-rows across {f['week'].nunique()} HelloFresh week(s) · "
            f"{f['name'].nunique():,} unique recipes · {tier_breakdown}"
        )
        cols = [
            "week", "menu_date", "name", "tier", "surcharge_per_portion_gbp",
            "food_brand", "kcal_per_portion", "portion_weight_g",
            "protein_g_per_portion", "fat_g_per_portion",
            "carbs_g_per_portion", "fibre_g_per_portion", "salt_g_per_portion",
            "prep_time_min", "spice_level", "dietary_claims",
            "rating_avg", "rating_count",
        ]
        cols = [c for c in cols if c in f.columns]
        out = f[cols].sort_values(["week", "tier", "name"],
                                  ascending=[False, True, True])
        out = out.rename(columns={
            "week": "hellofresh_week",
            "surcharge_per_portion_gbp": "surcharge_£_per_portion",
        })
        st.dataframe(out, use_container_width=True, hide_index=True)
        st.download_button(
            "Download filtered CSV",
            out.to_csv(index=False).encode("utf-8"),
            "gousto_filtered.csv", "text/csv",
        )

# -------------------- TAB: GOUSTO INGREDIENTS --------------------
with tab_ingredients:
    st.header("Gousto recipes — ingredient breakdown")
    st.info(
        "📦 Quantities shown are for the **2-portion box** "
        "(Gousto's canonical per-recipe view — matches what's listed on "
        "each recipe page on gousto.co.uk). For a 4-person box, expect "
        "roughly double these quantities; the precise pack counts vary by "
        "recipe.",
        icon="ℹ️",
    )
    if gousto_all.empty:
        st.info("No Gousto data yet.")
    else:
        # Show every recipe — those scraped before ingredient capture (or that
        # Gousto's detail endpoint returned no ingredients for) appear with an
        # empty ingredient list rather than being dropped.
        gi = gousto_all.copy()
        if "ingredients_json" not in gi.columns:
            gi["ingredients_json"] = ""
        gi["ingredients_json"] = gi["ingredients_json"].fillna("")
        if True:
            weeks = sorted(gi["week"].unique(), reverse=True)
            col_wk, col_ing = st.columns([2, 2])
            with col_wk:
                sel_weeks = st.multiselect(
                    "HelloFresh week(s)", weeks, default=weeks,
                    key="ing_weeks",
                )
            with col_ing:
                ingredient_filter = st.text_input(
                    "Filter by ingredient (e.g. chicken, paprika)",
                    key="ing_filter",
                    help="Case-insensitive substring match against any "
                         "ingredient name on the recipe.",
                ).strip().lower()

            f = gi[gi["week"].isin(sel_weeks)].copy()

            # Parse the JSON into a list of label strings per recipe. Gousto's
            # label format is "<name> (<qty><unit>) x<packs>" where 'packs' is
            # the count of standard packs at the requested portion size:
            #   x0  = ingredient not delivered as a separate pack (qty already
            #         baked into another item; common in 2-meals-in-1 bundles).
            #         Drop these — they're sub-recipe accounting, not what the
            #         customer actually receives.
            #   x1  = exactly one pack at the standard size. Strip the suffix
            #         (it's the implicit default).
            #   x2+ = multiple packs. Keep the suffix so quantity is clear.
            _trailing_pack = re.compile(r"\s*x\s*(\d+)\s*$")

            def _clean(label):
                m = _trailing_pack.search(label)
                if not m:
                    return label.strip()
                count = int(m.group(1))
                if count == 0:
                    return None  # signal: drop this ingredient
                if count == 1:
                    return _trailing_pack.sub("", label).strip()
                return label.strip()

            def _parse(js):
                try:
                    arr = json.loads(js) if js else []
                    cleaned = [_clean(i.get("label") or i.get("name") or "")
                               for i in arr]
                    return [c for c in cleaned if c]  # drop None / empty
                except (json.JSONDecodeError, TypeError):
                    return []
            f["_ing_labels"] = f["ingredients_json"].apply(_parse)
            f["ingredient_count"] = f["_ing_labels"].apply(len)
            f["ingredients"] = f["_ing_labels"].apply(lambda lst: ", ".join(lst))

            if ingredient_filter:
                f = f[f["_ing_labels"].apply(
                    lambda lst: any(ingredient_filter in s.lower() for s in lst)
                )]

            f["menu_date"] = pd.to_datetime(
                f["menu_week_start"], errors="coerce"
            ).dt.strftime("%d/%m/%Y")

            n_missing = (f["ingredient_count"] == 0).sum()
            avg_count_pop = (f.loc[f["ingredient_count"] > 0, "ingredient_count"].mean()
                             if (f["ingredient_count"] > 0).any() else 0)
            missing_note = (f" ({n_missing} with no ingredient list)"
                            if n_missing else "")
            st.caption(
                f"{len(f):,} recipe(s) shown across "
                f"{f['week'].nunique()} HelloFresh week(s){missing_note}. "
                f"Average {avg_count_pop:.0f} ingredients per recipe "
                f"(among those with data)."
            )

            cols = ["week", "menu_date", "name", "tier",
                    "ingredient_count", "ingredients"]
            cols = [c for c in cols if c in f.columns]
            out = (f[cols]
                   .sort_values(["week", "name"], ascending=[False, True])
                   .rename(columns={"week": "hellofresh_week"}))

            st.dataframe(
                out,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "ingredients": st.column_config.TextColumn(
                        "ingredients", width="large",
                        help="Comma-separated list of every ingredient with "
                             "its quantity, as published by Gousto.",
                    ),
                },
            )
            st.download_button(
                "Download filtered CSV",
                out.to_csv(index=False).encode("utf-8"),
                "gousto_ingredients.csv", "text/csv",
            )

            # Optional long-format view (one row per recipe × ingredient)
            with st.expander("Long-format view (one row per recipe × ingredient)"):
                rows = []
                for _, r in f.iterrows():
                    for label in r["_ing_labels"]:
                        rows.append({
                            "hellofresh_week": r["week"],
                            "menu_date": r["menu_date"],
                            "recipe_name": r["name"],
                            "tier": r.get("tier", ""),
                            "ingredient": label,
                        })
                long_df = pd.DataFrame(rows)
                st.dataframe(long_df, use_container_width=True, hide_index=True)
                if not long_df.empty:
                    st.download_button(
                        "Download long-format CSV",
                        long_df.to_csv(index=False).encode("utf-8"),
                        "gousto_ingredients_long.csv", "text/csv",
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
        f"`= Box price ÷ {BOX_CONFIGS[selected_box_config]['servings']} servings` "
        f"*(dictated by the selected box configuration above)*"
    )
    st.markdown(
        "**Price per 100 kcal** \n"
        "`= Price per serving ÷ (avg kcal per serving ÷ 100)`  \n"
        "*Avg kcal per serving* is the mean across **Core recipes only** on "
        "that brand's menu for the week — i.e. recipes the standard box "
        "price covers, with no surcharge upgrades."
    )
    st.markdown(
        "**Price per 100g** \n"
        "`= Price per serving ÷ (avg grams per serving ÷ 100)`  \n"
        "*Avg grams per serving* is calculated the same way (Core only)."
    )
    st.info(
        "⚠ **Methodology note**: Gousto surcharge tracking was added on "
        "**2026-05-31** (W22). For weeks **before W22**, **both brands** "
        "average across all menu recipes (Gousto ~189, HelloFresh 80) so "
        "the comparison stays apples-to-apples. **From W22 onwards**, both "
        "brands average Core recipes only (Gousto ~150, HelloFresh 71). "
        "Charts mark all-recipes weeks with **hollow markers**; tables "
        "flag them in the `core_methodology` column.",
        icon="⚠",
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
        "The bar charts and Δ% table on *Pricing comparison* show the rolling "
        "**3-week window** (current + next two). Every other surface — "
        "*Historic trends*, *HelloFresh recipes*, *Gousto recipes*, and the "
        "*Underlying numbers* expander — shows the **full history** and grows "
        "each week as new data is added. Older weekly CSVs are preserved in the "
        "GitHub repo and downloadable any time."
    )
