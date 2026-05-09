# Gousto vs HelloFresh Dashboard

Three tabs: Gousto recipes (auto-scraped), HelloFresh recipes (manual CSV drop), Pricing comparison.

## Layout
- `scraper.py` + `.github/workflows/scrape.yml` — Gousto recipe scrape, runs every Wed 07:00 UTC.
- `app.py` — Streamlit dashboard (deployed via Streamlit Community Cloud).
- `data/gousto_menu_*.csv` — written by the scraper.
- `data/hellofresh/*.csv` — drop weekly HelloFresh exports here. Required columns: `week, slot_number, brand, recipe_title, person_size, calories, grams`. The `calories` column is treated as **kJ per serving** (matches HelloFresh's labelling); the dashboard converts to kcal via ÷ 4.184.
- `data/prices/prices_<YYYY-Www>.csv` — one file per week. Header row: `Gousto,HelloFresh`. Single data row: `<gousto_box_price>,<hf_box_price>`. Box price = full subscription price for **4 people × 5 meals = 20 servings**.

## Tuesday workflow (you, manual)
1. Run your Databricks query → export the HelloFresh CSV.
2. On https://github.com/rohanvaswani-code/gousto-menu-dashboard:
   - Open `data/hellofresh/` → **Add file** → **Upload files** → drag the CSV → **Commit changes**.
   - Open `data/prices/` → **Add file** → upload `prices_2026-Www.csv` for the new week.
3. Streamlit Cloud auto-redeploys within ~1 minute.

## Wednesday (automatic)
- 07:00 UTC: Gousto scraper runs, writes/overwrites `data/gousto_menu_*.csv`, commits.
- Streamlit Cloud auto-redeploys.
- Dashboard now shows the new week.

## Pricing math
- `price_per_serving = box_price / 20`
- `price_per_100_cal = price_per_serving / (avg_kcal_per_serving / 100)`
- `price_per_100g = price_per_serving / (avg_grams_per_serving / 100)`
- `avg_*` are means across all menu items in the week.
- `Δ% = (HelloFresh - Gousto) / Gousto` (red = HF more expensive, green = HF cheaper).

## Local development
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-scraper.txt
streamlit run app.py
```
