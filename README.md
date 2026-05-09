# Gousto Menu Dashboard

Automated weekly scrape of the Gousto UK menu, surfaced as a Streamlit dashboard.

- **Scraper** runs every Wednesday 07:00 UTC via GitHub Actions, commits 3 weekly CSVs into `data/`.
- **Dashboard** is a Streamlit app deployed via Streamlit Community Cloud, redeploys automatically when `data/` updates.

## Layout
- `scraper.py` — fetches current week + next 2 weeks. Latest snapshot wins per `menu_week_start`.
- `app.py` — Streamlit dashboard reading every `data/gousto_menu_*.csv`.
- `data/` — accumulated weekly CSVs.
- `.github/workflows/scrape.yml` — weekly cron + on-demand trigger.
- `requirements.txt` — dashboard deps (used by Streamlit Cloud).
- `requirements-scraper.txt` — scraper deps (used by GitHub Actions and local runs).

## Local development
```powershell
cd C:\Users\RohanVaswani\Desktop\Gousto
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt -r requirements-scraper.txt

# Run dashboard
streamlit run app.py

# Run scraper
python scraper.py
```

## Manual trigger of the cloud scrape
GitHub → Actions tab → "Weekly Gousto scrape" → Run workflow.
