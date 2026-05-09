"""Gousto vs HelloFresh menu + pricing dashboard."""

import re
from datetime import date, timedelta
from pathlib import Path

import altair as alt
import pandas as pd
import streamlit as st

DATA_DIR = Path(__file__).resolve().parent / "data"
HF_DIR = DATA_DIR / "hellofresh"
PRICES_DIR = DATA_DIR / "prices"

NUM_PORTIONS = 4
MEALS_PER_BOX = 5
SERVINGS_PER_BOX = NUM_PORTIONS * MEALS_PER_BOX  # 20
KJ_PER_KCAL = 4.184

GOUSTO_COLOR = "#E25822"
HF_COLOR = "#2EA47B"
DELTA_RED = "#C0392B"
DELTA_GREEN = "#1E8449"

st.set_page_config(page_title="Gousto vs HelloFresh", layout="wide")


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


# -------------------- LOAD --------------------
gousto = load_gousto()
hf = load_hellofresh()
prices = load_prices()

st.title("Gousto vs HelloFresh Menu Dashboard")

if gousto.empty and hf.empty:
    st.warning(
        "No data yet. Drop Gousto CSVs into `data/`, HelloFresh CSVs into "
        "`data/hellofresh/`, and pricing CSVs into `data/prices/`."
    )
    st.stop()

tab1, tab2, tab3 = st.tabs(["Gousto recipes", "HelloFresh recipes", "Pricing comparison"])

# -------------------- TAB 1: GOUSTO --------------------
with tab1:
    st.header("Gousto recipes by week")
    if gousto.empty:
        st.info("No Gousto data yet.")
    else:
        weeks = sorted(gousto["week"].unique(), reverse=True)
        sel = st.multiselect("Menu week(s)", weeks, default=weeks, key="g_weeks")
        f = gousto[gousto["week"].isin(sel)]
        st.caption(
            f"{len(f):,} recipe-rows across {f['week'].nunique()} week(s) · "
            f"{f['name'].nunique():,} unique recipes"
        )
        cols = [
            "week", "name", "food_brand",
            "kcal_per_portion", "portion_weight_g",
            "protein_g_per_portion", "fat_g_per_portion",
            "carbs_g_per_portion", "fibre_g_per_portion", "salt_g_per_portion",
            "prep_time_min", "spice_level", "dietary_claims",
            "rating_avg", "rating_count",
        ]
        cols = [c for c in cols if c in f.columns]
        st.dataframe(
            f[cols].sort_values(["week", "name"], ascending=[False, True]),
            use_container_width=True, hide_index=True,
        )
        st.download_button(
            "Download filtered CSV",
            f[cols].to_csv(index=False).encode("utf-8"),
            "gousto_filtered.csv", "text/csv",
        )

# -------------------- TAB 2: HELLOFRESH --------------------
with tab2:
    st.header("HelloFresh recipes by week")
    if hf.empty:
        st.info("No HelloFresh data yet. Drop CSVs into `data/hellofresh/`.")
    else:
        weeks = sorted(hf["week"].unique(), reverse=True)
        sel = st.multiselect("Menu week(s)", weeks, default=weeks, key="hf_weeks")
        f = hf[hf["week"].isin(sel)]
        st.caption(
            f"{len(f):,} recipe-rows across {f['week'].nunique()} week(s) · "
            f"{f['recipe_title'].nunique():,} unique recipes"
        )
        show = f[["week", "slot_number", "recipe_title",
                  "kcal_per_serving", "kj_per_serving", "grams_per_serving"]].copy()
        show["kcal_per_serving"] = show["kcal_per_serving"].round(0)
        show["kj_per_serving"] = show["kj_per_serving"].round(0)
        show["grams_per_serving"] = show["grams_per_serving"].round(1)
        st.dataframe(
            show.sort_values(["week", "slot_number"], ascending=[False, True]),
            use_container_width=True, hide_index=True,
        )
        st.download_button(
            "Download filtered CSV",
            show.to_csv(index=False).encode("utf-8"),
            "hellofresh_filtered.csv", "text/csv",
        )

# -------------------- TAB 3: PRICING --------------------
with tab3:
    st.header(f"Weekly pricing comparison — {NUM_PORTIONS} people × {MEALS_PER_BOX} meals box")

    if prices.empty:
        st.warning(
            "No pricing data yet. Drop a CSV into `data/prices/` named e.g. "
            "`prices_2026-W19.csv` with header `Gousto,HelloFresh` and one row of "
            "box prices (e.g. `67.98,77.98`)."
        )
        st.stop()

    rows = []
    for week in sorted(prices["week"].unique()):
        for brand in ("Gousto", "HelloFresh"):
            pr = prices[(prices["week"] == week) & (prices["brand"] == brand)]
            if pr.empty:
                continue
            box_price = pr["box_price"].iloc[0]
            if brand == "Gousto":
                m = gousto[gousto["week"] == week]
                if m.empty:
                    continue
                avg_kcal = m["kcal_per_portion"].mean()
                avg_grams = m["portion_weight_g"].mean()
                n_recipes = len(m)
            else:
                m = hf[hf["week"] == week]
                if m.empty:
                    continue
                avg_kcal = m["kcal_per_serving"].mean()
                avg_grams = m["grams_per_serving"].mean()
                n_recipes = len(m)
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
                "recipes_in_menu": n_recipes,
            })

    summary = pd.DataFrame(rows)
    if summary.empty:
        st.info(
            "Pricing CSVs found, but no matching menu data for those weeks. "
            "Make sure the Gousto + HelloFresh CSVs cover the same ISO weeks."
        )
        st.stop()

    metrics = ["Per serving", "Per 100 cal", "Per 100g"]
    long = summary.melt(
        id_vars=["week", "brand"],
        value_vars=metrics,
        var_name="metric", value_name="value",
    )

    # Δ% (HelloFresh vs Gousto) per (week, metric)
    delta_rows = []
    for week in sorted(summary["week"].unique()):
        wdf = summary[summary["week"] == week].set_index("brand")
        if "Gousto" not in wdf.index or "HelloFresh" not in wdf.index:
            continue
        for metric in metrics:
            gv = wdf.loc["Gousto", metric]
            hv = wdf.loc["HelloFresh", metric]
            if pd.notna(gv) and pd.notna(hv) and gv:
                delta_rows.append({
                    "week": week, "metric": metric,
                    "delta": (hv - gv) / gv,
                })
    delta_df = pd.DataFrame(delta_rows)

    # Merge delta into long-form so layer + facet share one data source
    combined = long.merge(delta_df, on=["week", "metric"], how="left")
    combined["label"] = combined.apply(
        lambda r: f"{r['delta']:+.1%}"
        if (r["brand"] == "HelloFresh" and pd.notna(r["delta"]))
        else "",
        axis=1,
    )
    combined["label_color"] = combined["delta"].apply(
        lambda x: DELTA_RED if pd.notna(x) and x > 0 else DELTA_GREEN
    )

    color_scale = alt.Scale(
        domain=["Gousto", "HelloFresh"],
        range=[GOUSTO_COLOR, HF_COLOR],
    )

    y_max = max(4.0, float(combined["value"].max()) * 1.15)

    bar = alt.Chart().mark_bar(size=28).encode(
        x=alt.X("week:N", title=None,
                axis=alt.Axis(labelAngle=0, labelFontSize=12, labelPadding=8)),
        xOffset=alt.XOffset("brand:N", sort=["Gousto", "HelloFresh"]),
        y=alt.Y(
            "value:Q", title=None,
            scale=alt.Scale(domain=[0, y_max]),
            axis=alt.Axis(format="£.2f", tickCount=9, gridOpacity=0.4),
        ),
        color=alt.Color(
            "brand:N",
            scale=color_scale,
            sort=["Gousto", "HelloFresh"],
            legend=alt.Legend(title=None, orient="top", direction="horizontal",
                              symbolType="square", symbolSize=180),
        ),
        tooltip=[
            alt.Tooltip("week:N", title="Week"),
            alt.Tooltip("brand:N", title="Brand"),
            alt.Tooltip("metric:N"),
            alt.Tooltip("value:Q", title="£", format=".4f"),
        ],
    )

    text = (
        alt.Chart()
        .mark_text(dy=-10, fontWeight="bold", fontSize=12)
        .transform_filter(alt.datum.brand == "HelloFresh")
        .encode(
            x=alt.X("week:N"),
            xOffset=alt.value(8),
            y=alt.Y("value:Q"),
            text="label:N",
            color=alt.Color("label_color:N", scale=None, legend=None),
        )
    )

    chart = (
        alt.layer(bar, text, data=combined)
        .properties(height=420, title=alt.TitleParams(
            "Δ% = HelloFresh vs Gousto", anchor="end",
            fontSize=12, color="#777", dy=-8,
        ))
        .facet(
            column=alt.Column(
                "metric:N", title=None, sort=metrics,
                header=alt.Header(labelFontSize=15, labelFontWeight="bold",
                                  labelPadding=10, labelColor="#444"),
            ),
            spacing=40,
        )
    )

    st.altair_chart(chart, use_container_width=True)

    # Δ% summary table
    if not delta_df.empty:
        delta_table = (
            delta_df.pivot(index="metric", columns="week", values="delta")
            .reindex(metrics)
        )

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
                    lambda x: f"£{x:.4f}" if pd.notna(x) else ""
                )
        for c in ("avg_kcal_per_serving", "avg_grams_per_serving"):
            if c in disp.columns:
                disp[c] = disp[c].round(1)
        st.dataframe(disp, use_container_width=True, hide_index=True)

# -------------------- SIDEBAR --------------------
with st.sidebar:
    st.subheader("Data sources")
    st.caption(f"Gousto: {len(list(DATA_DIR.glob('gousto_menu_*.csv')))} weekly file(s)")
    st.caption(f"HelloFresh: {len(list(HF_DIR.glob('*.csv'))) if HF_DIR.exists() else 0} file(s)")
    st.caption(f"Prices: {len(list(PRICES_DIR.glob('*.csv'))) if PRICES_DIR.exists() else 0} weekly file(s)")
    if st.button("Reload data"):
        st.cache_data.clear()
        st.rerun()
    st.divider()
    st.caption(
        "**How to update:** drag CSVs into the matching folder on github.com → "
        "the dashboard redeploys ~1 min later. Gousto auto-scrapes every Wed 07:00 UTC."
    )
