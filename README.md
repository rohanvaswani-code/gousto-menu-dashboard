# Gousto vs HelloFresh Dashboard

Three tabs: **Pricing comparison** / **HelloFresh recipes** / **Gousto recipes**.

The dashboard always shows the **latest 3 weeks** of data (current + next 2). Older scrapes are preserved in `data/` for historical analysis but are not displayed.

## Layout
- `scraper.py` + `.github/workflows/scrape.yml` — Gousto recipe scrape, runs every Wed 07:00 UTC. Each scrape writes `gousto_menu_<menu_week_start>_<scraped_at>.csv` so historical snapshots are preserved.
- `app.py` — Streamlit dashboard (deployed via Streamlit Community Cloud).
- `data/gousto_menu_*.csv` — written by the scraper (snapshot per scrape).
- `data/hellofresh/*.csv` — drop weekly HelloFresh exports here. Required columns: `week, slot_number, brand, recipe_title, person_size, calories, grams`. The `calories` column is treated as **kJ per serving** (matches HelloFresh's labelling); the dashboard converts to kcal via ÷ 4.184.
- `data/prices/prices_<YYYY-Www>.csv` — one file per week. Header row: `Gousto,HelloFresh`. Single data row: `<gousto_box_price>,<hf_box_price>`. Box price = full subscription price for **4 people × 5 meals = 20 servings**.

## Tuesday workflow (manual, ~2 min)
1. Run your Databricks query → export the HelloFresh CSV for the new week.
2. On https://github.com/rohanvaswani-code/gousto-menu-dashboard:
   - Open `data/hellofresh/` → **Add file** → **Upload files** → drag the CSV → **Commit changes**.
   - Open `data/prices/` → **Add file** → upload `prices_2026-Www.csv` for the new week.
3. Streamlit Cloud auto-redeploys within ~1 minute. Refresh the app — new week is visible (after Wednesday's scrape).

## Wednesday (automatic)
- 07:00 UTC: Gousto scraper runs, writes new `gousto_menu_*.csv` files into `data/`, commits.
- Streamlit Cloud auto-redeploys.
- Dashboard now shows the **new 3-week window**: oldest week rolls off, newest rolls on.

## Pricing math
- `price_per_serving = box_price / 20`
- `price_per_100_cal = price_per_serving / (avg_kcal_per_serving / 100)`
- `price_per_100g = price_per_serving / (avg_grams_per_serving / 100)`
- `avg_*` are means across all menu items in the week.
- `Δ% = (HelloFresh - Gousto) / Gousto` (red = HF more expensive, green = HF cheaper).

## Historical data
Every Gousto scrape leaves a dated CSV in `data/`. To query history, just read all the CSVs — each file is a snapshot of one menu week as scraped on a specific date. The dashboard's loader dedups to the latest snapshot per week.

## Local development
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-scraper.txt
streamlit run app.py
```
