# Gousto vs HelloFresh Dashboard

Four tabs: **Pricing comparison** / **Historic trends** / **HelloFresh recipes** / **Gousto recipes**.

Rolling-window split:
- **Pricing comparison bar charts + Δ% summary table** → latest **3 weeks** only (current + next 2).
- **Everything else** (Underlying numbers expander, Historic trends, HelloFresh recipes, Gousto recipes) → **full history**, grows every week. Older CSVs are preserved in `data/` and downloadable from GitHub at any time.

## Layout
- `scraper.py` + `.github/workflows/scrape.yml` — Gousto recipe scrape, runs every Wed 07:00 UTC for current + next 2 weeks. Each scrape writes `gousto_menu_<menu_week_start>_<scraped_at>.csv` so historical snapshots are preserved.
- `app.py` — Streamlit dashboard (deployed via Streamlit Community Cloud).
- `data/gousto_menu_*.csv` — written by the scraper (snapshot per scrape).
- `data/hellofresh/*.csv` — drop weekly HelloFresh exports here.
- `data/prices/prices_<YYYY-Www>.csv` — one file per week.

## Tuesday workflow (manual, ~2 min)

Before 07:00 UTC Wednesday, drop two new CSVs into the repo (Tuesday is fine, earlier is fine — just don't miss the cron).

### 1. HelloFresh recipes → `data/hellofresh/`
Run your Databricks query and export the new week's HelloFresh menu as a CSV.

- **Filename**: any `.csv` works (the dashboard reads every file in the folder and dedups using the `week` column inside).
- **Recommended convention**: `hellofresh_<YYYY-Www>.csv`, e.g. `hellofresh_2026-W22.csv` — makes it easy to tell files apart on GitHub.
- **Required columns**: `week, slot_number, brand, recipe_title, person_size, calories, grams`. The `calories` column is treated as **kJ per serving** (matches HelloFresh's labelling); the dashboard converts to kcal via ÷ 4.184.

### 2. Box prices → `data/prices/`
- **Filename**: must contain `YYYY-Www` somewhere — the dashboard parses the week from the filename. Use `prices_<YYYY-Www>.csv`, e.g. `prices_2026-W22.csv`.
- **File contents**: header row `Gousto,HelloFresh`, single data row `<gousto_box_price>,<hf_box_price>`. Box price = full subscription price for **4 people × 5 meals = 20 servings**.

```
Gousto,HelloFresh
67.98,77.98
```

### How to upload via the GitHub web UI
On https://github.com/rohanvaswani-code/gousto-menu-dashboard:
- Open `data/hellofresh/` → **Add file** → **Upload files** → drag the CSV → **Commit changes**.
- Open `data/prices/` → **Add file** → upload `prices_<YYYY-Www>.csv` for the new week.

Streamlit Cloud auto-redeploys within ~1 minute of each commit. The new week becomes visible in pricing comparison after Wednesday's Gousto scrape lands.

## Wednesday (automatic)
- 07:00 UTC: Gousto scraper runs for current + next 2 weeks, writes 3 new `gousto_menu_*.csv` files into `data/`, and commits.
- The Actions log ends with a `PER-WEEK FILTERED COUNTS` block listing each week → recipe count, so you can sanity-check against gousto.co.uk/menu in one glance. If the `all-recipes` slug ever goes missing, the run fails loudly (rather than silently scraping the unfiltered ~271-recipe dump).
- Streamlit Cloud auto-redeploys.
- Pricing-tab bar charts roll forward: oldest of the 3 weeks rolls off, newest rolls on. Every other tab gains a new week without losing any.

## Downloading historic data
- Every Gousto scrape, HelloFresh upload, and prices upload stays in the repo as a CSV.
- To download: navigate to `data/`, `data/hellofresh/`, or `data/prices/` on GitHub and click any file → use the download (raw) button.
- The Gousto and HelloFresh recipe tabs in the dashboard also have a **Download filtered CSV** button that exports whatever weeks you've selected.

## Pricing math
- `price_per_serving = box_price / 20`
- `price_per_100_cal = price_per_serving / (avg_kcal_per_serving / 100)`
- `price_per_100g = price_per_serving / (avg_grams_per_serving / 100)`
- `avg_*` are means across all customer-facing menu items in the week (Gousto: filtered to the `all-recipes` category, ~190 recipes; HelloFresh: full weekly menu, ~70 recipes).
- `Δ% = (HelloFresh - Gousto) / Gousto` (red = HF more expensive, green = HF cheaper).

## Historical data
Every Gousto scrape leaves a dated CSV in `data/`. The dashboard's loader keeps only the latest snapshot per `menu_week_start`, but every snapshot stays in git so you can read all the CSVs to reconstruct history week-by-week.

## Local development
```powershell
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-scraper.txt
streamlit run app.py
```
