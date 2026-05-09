"""
Gousto Menu Dashboard.
Reads every gousto_menu_*.csv in ./data and presents a filterable, charted view.
"""

from pathlib import Path

import pandas as pd
import streamlit as st

DATA_DIR = Path(__file__).resolve().parent / "data"

st.set_page_config(page_title="Gousto Menu Dashboard", layout="wide")


@st.cache_data
def load_data():
    files = sorted(DATA_DIR.glob("gousto_menu_*.csv"))
    if not files:
        return pd.DataFrame(), []
    frames = []
    for f in files:
        df = pd.read_csv(f)
        df["_source_file"] = f.name
        frames.append(df)
    df = pd.concat(frames, ignore_index=True)

    # Latest snapshot wins per (menu_week_start, id) — guards against duplicate scrapes of the same week.
    if "scraped_at" in df.columns:
        df["scraped_at"] = pd.to_datetime(df["scraped_at"], errors="coerce")
        df = df.sort_values("scraped_at").drop_duplicates(
            subset=["menu_week_start", "id"], keep="last"
        )

    numeric_cols = [c for c in df.columns if c.endswith(("_per_portion", "_per_100g", "_g", "_min"))
                    or c in ("kcal_per_portion", "kcal_per_100g", "portion_weight_g",
                             "rating_avg", "rating_count", "five_a_day")]
    for c in numeric_cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")

    df["menu_week_start"] = df["menu_week_start"].astype(str)
    return df, [f.name for f in files]


df, files = load_data()

st.title("Gousto Menu Dashboard")

if df.empty:
    st.warning(
        f"No CSVs found in `{DATA_DIR}`. Run `python scraper.py` to populate it."
    )
    st.stop()

st.caption(
    f"Loaded **{len(files)}** weekly snapshot(s) — **{df['menu_week_start'].nunique()}** unique menu week(s), "
    f"**{len(df):,}** recipe-rows total."
)

# -------------------- SIDEBAR FILTERS --------------------
st.sidebar.header("Filters")

weeks = sorted(df["menu_week_start"].unique(), reverse=True)
selected_weeks = st.sidebar.multiselect("Menu week(s)", weeks, default=weeks)

all_claims = sorted({c.strip() for s in df["dietary_claims"].dropna()
                     for c in str(s).split(";") if c.strip()})
selected_claims = st.sidebar.multiselect("Dietary claims (any-of)", all_claims)

brands = sorted(df["food_brand"].dropna().astype(str).unique())
brands = [b for b in brands if b]
selected_brands = st.sidebar.multiselect("Food brand", brands)

spice_levels = sorted(df["spice_level"].dropna().astype(str).unique())
spice_levels = [s for s in spice_levels if s]
selected_spice = st.sidebar.multiselect("Spice level", spice_levels)

name_search = st.sidebar.text_input("Search recipe name")

kcal_max = int(df["kcal_per_portion"].max(skipna=True) or 2000)
kcal_range = st.sidebar.slider("kcal per portion", 0, kcal_max, (0, kcal_max), step=10)

prep_max = int(df["prep_time_min"].max(skipna=True) or 90)
prep_range = st.sidebar.slider("Prep time (min)", 0, prep_max, (0, prep_max), step=5)

# -------------------- APPLY FILTERS --------------------
f = df.copy()
f = f[f["menu_week_start"].isin(selected_weeks)]
if selected_claims:
    f = f[f["dietary_claims"].fillna("").apply(
        lambda s: any(c in s for c in selected_claims))]
if selected_brands:
    f = f[f["food_brand"].isin(selected_brands)]
if selected_spice:
    f = f[f["spice_level"].isin(selected_spice)]
if name_search:
    f = f[f["name"].astype(str).str.contains(name_search, case=False, na=False)]
f = f[(f["kcal_per_portion"].fillna(0).between(*kcal_range))]
f = f[(f["prep_time_min"].fillna(0).between(*prep_range))]

# -------------------- METRICS --------------------
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Recipes (filtered)", f"{len(f):,}")
c2.metric("Unique recipes", f"{f['name'].nunique():,}")
c3.metric("Avg kcal / portion", f"{f['kcal_per_portion'].mean():.0f}" if len(f) else "—")
c4.metric("Avg protein (g)", f"{f['protein_g_per_portion'].mean():.1f}" if len(f) else "—")
c5.metric("Avg prep (min)", f"{f['prep_time_min'].mean():.0f}" if len(f) else "—")

# -------------------- TABS --------------------
tab1, tab2, tab3, tab4 = st.tabs(["Recipes", "Nutrition", "Trends across weeks", "Raw data"])

with tab1:
    st.subheader("Recipes")
    show_cols = [
        "menu_week_start", "name", "food_brand", "kcal_per_portion",
        "protein_g_per_portion", "fat_g_per_portion", "carbs_g_per_portion",
        "fibre_g_per_portion", "salt_g_per_portion",
        "prep_time_min", "spice_level", "dietary_claims",
        "rating_avg", "rating_count",
    ]
    show_cols = [c for c in show_cols if c in f.columns]
    st.dataframe(
        f[show_cols].sort_values(["menu_week_start", "name"], ascending=[False, True]),
        use_container_width=True, hide_index=True,
    )

with tab2:
    st.subheader("Nutrition distributions")
    nutri_cols = [
        ("kcal_per_portion", "kcal / portion"),
        ("protein_g_per_portion", "protein (g) / portion"),
        ("fat_g_per_portion", "fat (g) / portion"),
        ("carbs_g_per_portion", "carbs (g) / portion"),
        ("fibre_g_per_portion", "fibre (g) / portion"),
        ("salt_g_per_portion", "salt (g) / portion"),
    ]
    cols = st.columns(2)
    for i, (col, label) in enumerate(nutri_cols):
        if col in f.columns:
            with cols[i % 2]:
                st.caption(label)
                hist = f[col].dropna()
                if len(hist):
                    binned = pd.cut(hist, bins=20).value_counts().sort_index()
                    chart_df = pd.DataFrame({
                        "bin": [f"{iv.left:.0f}–{iv.right:.0f}" for iv in binned.index],
                        "count": binned.values,
                    })
                    st.bar_chart(chart_df, x="bin", y="count", height=200)

    st.subheader("Top recipes")
    if len(f):
        c_a, c_b = st.columns(2)
        with c_a:
            st.caption("Highest protein per portion")
            st.dataframe(
                f.nlargest(10, "protein_g_per_portion")[
                    ["name", "menu_week_start", "protein_g_per_portion", "kcal_per_portion"]
                ],
                hide_index=True, use_container_width=True,
            )
        with c_b:
            st.caption("Lowest kcal per portion")
            st.dataframe(
                f.nsmallest(10, "kcal_per_portion")[
                    ["name", "menu_week_start", "kcal_per_portion", "protein_g_per_portion"]
                ],
                hide_index=True, use_container_width=True,
            )

with tab3:
    st.subheader("Across menu weeks")
    by_week = (
        f.groupby("menu_week_start")
        .agg(
            recipes=("id", "count"),
            avg_kcal=("kcal_per_portion", "mean"),
            avg_protein=("protein_g_per_portion", "mean"),
            avg_prep=("prep_time_min", "mean"),
        )
        .reset_index()
        .sort_values("menu_week_start")
    )
    st.dataframe(by_week, hide_index=True, use_container_width=True)

    if len(by_week) >= 2:
        st.line_chart(by_week.set_index("menu_week_start")[["avg_kcal", "avg_protein"]])
        st.line_chart(by_week.set_index("menu_week_start")[["recipes"]])

    # Recipe churn — what's new this week vs last
    if len(selected_weeks) >= 2 and len(by_week) >= 2:
        st.subheader("Recipe churn (most recent vs previous week)")
        sorted_weeks = sorted(f["menu_week_start"].unique())
        if len(sorted_weeks) >= 2:
            prev, curr = sorted_weeks[-2], sorted_weeks[-1]
            curr_names = set(f[f["menu_week_start"] == curr]["name"])
            prev_names = set(f[f["menu_week_start"] == prev]["name"])
            new = sorted(curr_names - prev_names)
            gone = sorted(prev_names - curr_names)
            ca, cb = st.columns(2)
            ca.metric(f"New in {curr}", len(new))
            cb.metric(f"Dropped from {prev}", len(gone))
            with st.expander(f"New recipes in {curr}"):
                st.write(new or "—")
            with st.expander(f"Dropped from {prev}"):
                st.write(gone or "—")

with tab4:
    st.subheader("Raw filtered data")
    st.dataframe(f, use_container_width=True, hide_index=True)
    st.download_button(
        "Download filtered CSV",
        f.to_csv(index=False).encode("utf-8"),
        file_name="gousto_filtered.csv",
        mime="text/csv",
    )

with st.sidebar:
    st.divider()
    st.caption("**Files loaded**")
    for name in files:
        st.caption(f"- {name}")
    if st.button("Reload data"):
        st.cache_data.clear()
        st.rerun()
