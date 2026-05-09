"""
Gousto UK Menu Scraper - local Windows port.
Fetches this week's menu plus the next two weeks (4-portion box).
Writes one CSV per week into ./data. Re-running a week overwrites that week's file
(latest snapshot wins per menu_week_start).
"""

import csv
import json
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from seleniumwire import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


# -------------------- CONFIG --------------------
NUM_PORTIONS = 4
WEEKS_TO_FETCH = [0, 1, 2]
SCRIPT_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = SCRIPT_DIR / "data"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
SLUG_CACHE = OUTPUT_DIR / "gousto_slug_cache.json"
DETAIL_DELAY = 0.3
LISTING_DELAY = 0.3
LISTING_LIMIT = 50

MENU_URL = "https://www.gousto.co.uk/menu"
COOKBOOK_LISTING_API = "https://production-api.gousto.co.uk/cmsreadbroker/v1/recipes"
RECIPE_DETAIL_API = "https://production-api.gousto.co.uk/cmsreadbroker/v1/recipe/{slug}"

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


# -------------------- BOOTSTRAP --------------------
def make_driver():
    opts = Options()
    for arg in ["--headless=new", "--no-sandbox", "--disable-dev-shm-usage",
                "--disable-gpu", "--window-size=1400,1000",
                "--disable-blink-features=AutomationControlled"]:
        opts.add_argument(arg)
    opts.add_argument(f"user-agent={USER_AGENT}")
    return webdriver.Chrome(
        service=Service(ChromeDriverManager().install()),
        options=opts,
        seleniumwire_options={"disable_encoding": True},
    )


def bootstrap():
    print("-> bootstrapping with Selenium (~30s)...")
    driver = make_driver()
    try:
        driver.get(MENU_URL)
        time.sleep(8)
        for _ in range(3):
            driver.execute_script("window.scrollBy(0, 1500);")
            time.sleep(1.5)
        for req in driver.requests:
            if req.response and "/menu/v3/menus" in req.url:
                device_id = req.headers.get("x-gousto-device-id")
                if device_id:
                    print("   captured device_id and menu URL template")
                    return device_id, req.url
        return None, None
    finally:
        driver.quit()


def make_session(device_id):
    s = requests.Session()
    s.headers.update({
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Origin": "https://www.gousto.co.uk",
        "Referer": "https://www.gousto.co.uk/",
        "x-gousto-device-id": device_id,
    })
    return s


# -------------------- MENU --------------------
def fetch_menu(session, url_template, delivery_date, num_portions):
    url = re.sub(r"num_portions=\d+", f"num_portions={num_portions}", url_template)
    url = re.sub(r"delivery_date=\d{4}-\d{2}-\d{2}", f"delivery_date={delivery_date}", url)
    r = session.get(url, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("recipes", {}), data.get("period", {})


# -------------------- COOKBOOK MAP --------------------
def build_uuid_to_slug_map(session, use_cache=True):
    if use_cache and SLUG_CACHE.exists():
        try:
            mapping = json.loads(SLUG_CACHE.read_text())
            print(f"-> loaded {len(mapping)} cached UUID->slug mappings")
            return mapping
        except Exception:
            pass

    print("-> paging cookbook (~1 minute, one-off per session)...")
    mapping = {}
    offset = 0
    fails = 0
    total = None
    while True:
        try:
            r = session.get(
                COOKBOOK_LISTING_API,
                params={"category": "recipes", "limit": LISTING_LIMIT, "offset": offset},
                timeout=20,
            )
            if r.status_code != 200:
                fails += 1
                if fails >= 3:
                    print(f"   stopping after 3 consecutive failures (last: HTTP {r.status_code})")
                    break
                time.sleep(2 ** fails)
                continue
            data = r.json()
            fails = 0
        except Exception as e:
            fails += 1
            if fails >= 3:
                print(f"   stopping: {e}")
                break
            time.sleep(2 ** fails)
            continue

        body = data.get("data", {})
        if total is None:
            total = body.get("count")
        entries = body.get("entries", [])
        if not entries:
            break
        for e in entries:
            uid = e.get("gousto_uid")
            url_path = e.get("url", "")
            if uid and url_path:
                mapping[uid] = url_path.rstrip("/").split("/")[-1]
        offset += len(entries)
        if offset % 1000 == 0:
            print(f"   {offset}/{total or '?'}")
        if len(entries) < LISTING_LIMIT or (total and offset >= total):
            break
        time.sleep(LISTING_DELAY)

    print(f"   total: {len(mapping)} mappings")
    if mapping:
        try:
            SLUG_CACHE.write_text(json.dumps(mapping))
        except Exception:
            pass
    return mapping


def fetch_recipe_detail(session, slug):
    try:
        r = session.get(RECIPE_DETAIL_API.format(slug=slug), timeout=15)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


# -------------------- ROW BUILDING --------------------
def to_g(mg, dp=1):
    if mg in (None, ""):
        return ""
    try:
        return round(float(mg) / 1000, dp)
    except (TypeError, ValueError):
        return ""


def make_row(rid, menu_recipe, detail, week_start, scraped_at):
    name = menu_recipe.get("name", "")
    menu_nutri = (menu_recipe.get("nutritional_information") or {}).get("per_portion") or {}
    detail_nutri = {}
    if detail:
        entry = (detail.get("data") or {}).get("entry") or {}
        detail_nutri = entry.get("nutritional_information") or {}

    pp = detail_nutri.get("per_portion") or menu_nutri
    p100 = detail_nutri.get("per_hundred_grams") or {}

    portion_weight_g = round(pp["net_weight_mg"] / 1000) if pp.get("net_weight_mg") else ""
    kcal_per_100g = p100.get("energy_kcal", "")
    if kcal_per_100g == "" and pp.get("net_weight_mg") and pp.get("energy_kcal"):
        kcal_per_100g = round(pp["energy_kcal"] / (pp["net_weight_mg"] / 1000) * 100)

    return {
        "menu_week_start": week_start,
        "scraped_at": scraped_at,
        "id": rid,
        "core_recipe_id": menu_recipe.get("core_recipe_id", ""),
        "name": name,
        "portion_weight_g": portion_weight_g,
        "kcal_per_portion": pp.get("energy_kcal", ""),
        "protein_g_per_portion": to_g(pp.get("protein_mg")),
        "fat_g_per_portion": to_g(pp.get("fat_mg")),
        "fat_saturates_g_per_portion": to_g(pp.get("fat_saturates_mg")),
        "carbs_g_per_portion": to_g(pp.get("carbs_mg")),
        "sugars_g_per_portion": to_g(pp.get("carbs_sugars_mg")),
        "fibre_g_per_portion": to_g(pp.get("fibre_mg")),
        "salt_g_per_portion": to_g(pp.get("salt_mg"), 2),
        "kcal_per_100g": kcal_per_100g,
        "protein_g_per_100g": to_g(p100.get("protein_mg")),
        "fat_g_per_100g": to_g(p100.get("fat_mg")),
        "fat_saturates_g_per_100g": to_g(p100.get("fat_saturates_mg")),
        "carbs_g_per_100g": to_g(p100.get("carbs_mg")),
        "sugars_g_per_100g": to_g(p100.get("carbs_sugars_mg")),
        "fibre_g_per_100g": to_g(p100.get("fibre_mg")),
        "salt_g_per_100g": to_g(p100.get("salt_mg"), 2),
        "five_a_day": menu_recipe.get("five_a_day", ""),
        "prep_time_min": menu_recipe.get("prep_time", ""),
        "preparation_type": menu_recipe.get("preparation_type", ""),
        "spice_level": (menu_recipe.get("spice_level") or {}).get("name", ""),
        "food_brand": (menu_recipe.get("food_brand") or {}).get("name", ""),
        "dietary_claims": "; ".join(d.get("name", "") for d in (menu_recipe.get("dietary_claims") or [])),
        "rating_avg": (menu_recipe.get("rating") or {}).get("average", ""),
        "rating_count": (menu_recipe.get("rating") or {}).get("count", ""),
        "is_available": menu_recipe.get("is_available", ""),
    }


def write_csv(rows, path):
    if not rows:
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"   wrote {len(rows)} rows -> {path.name}")


# -------------------- TOP-LEVEL --------------------
def scrape_week(session, url_template, weeks_ahead, uuid_to_slug, num_portions, scraped_at):
    target = date.today() + timedelta(days=max(weeks_ahead * 7, 2))
    delivery_date = target.isoformat()
    print(f"\n--- Week +{weeks_ahead}: requesting delivery_date={delivery_date} ---")

    menu_recipes, period = fetch_menu(session, url_template, delivery_date, num_portions)
    week_start = (period.get("when_start", "") or delivery_date)[:10]
    print(f"   menu period: {week_start} -> {(period.get('when_cutoff','') or '')[:10]}")
    print(f"   {len(menu_recipes)} recipes on this menu")

    if not menu_recipes:
        return None

    rows = []
    matched = 0
    total = len(menu_recipes)
    for i, (rid, menu_r) in enumerate(menu_recipes.items(), 1):
        slug = uuid_to_slug.get(rid)
        detail = fetch_recipe_detail(session, slug) if slug else None
        if detail and detail.get("data", {}).get("entry", {}).get("nutritional_information"):
            matched += 1
        rows.append(make_row(rid, menu_r, detail, week_start, scraped_at))
        if i % 50 == 0 or i == total:
            print(f"   {i}/{total} ({matched} with full detail)")
        time.sleep(DETAIL_DELAY)

    rows.sort(key=lambda x: x["name"].lower())
    out_path = OUTPUT_DIR / f"gousto_menu_{week_start}_{scraped_at}.csv"
    write_csv(rows, out_path)
    print(f"   per-100g coverage: {matched}/{total} ({matched/total*100:.0f}%)")
    return out_path


def run():
    scraped_at = date.today().isoformat()
    device_id, url_template = bootstrap()
    if not device_id:
        raise RuntimeError("Bootstrap failed - couldn't capture device_id")
    session = make_session(device_id)
    uuid_to_slug = build_uuid_to_slug_map(session, use_cache=True)

    csv_paths = []
    for wk in WEEKS_TO_FETCH:
        try:
            path = scrape_week(session, url_template, wk, uuid_to_slug, NUM_PORTIONS, scraped_at)
            if path:
                csv_paths.append(path)
        except Exception as e:
            print(f"   FAILED week +{wk}: {e}")

    print(f"\n{'='*60}\nDONE: {len(csv_paths)} CSV(s) created\n{'='*60}")
    for p in csv_paths:
        print(f"  {p}")
    return csv_paths


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
