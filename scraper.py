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
import unicodedata
from datetime import date, timedelta
from pathlib import Path

import requests
from seleniumwire import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from webdriver_manager.chrome import ChromeDriverManager


# -------------------- CONFIG --------------------
NUM_PORTIONS = 4
# Gousto's menu API exposes the current week + 6-7 forward weeks. We fetch
# everything available so the dashboard's Gousto-only tabs (recipes,
# ingredients) reach as far as possible — useful even before HF/prices are
# uploaded for a given week. Weeks the API doesn't have are gracefully
# skipped (the API returns an empty menu).
WEEKS_TO_FETCH = [0, 1, 2, 3, 4, 5, 6, 7]
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
    all_recipes = data.get("recipes", {})

    # Customer-facing menu = the `all-recipes` category. The raw `recipes` dict
    # also includes sub-category-only items (Health Hub / Desserts & Sides /
    # Premium) that don't appear on gousto.co.uk/menu, so we MUST filter.
    # Fail loudly if the category is missing — silent fallback would ship ~271
    # rows when the website only shows ~189.
    categories = data.get("categories", {})
    cats_iter = categories.values() if isinstance(categories, dict) else categories

    visible_ids = None
    for c in cats_iter:
        if isinstance(c, dict) and c.get("slug") == "all-recipes":
            ids = []
            for entry in (c.get("recipes") or []):
                if isinstance(entry, str):
                    ids.append(entry)
                elif isinstance(entry, dict):
                    rid = entry.get("id") or entry.get("core_recipe_id")
                    if rid:
                        ids.append(str(rid))
            visible_ids = set(ids)
            break

    if not visible_ids:
        raise RuntimeError(
            f"'all-recipes' category missing or empty for delivery_date={delivery_date}. "
            f"Refusing to write unfiltered menu ({len(all_recipes)} recipes) — "
            f"the customer-facing menu is much smaller. Check Gousto API response shape."
        )

    recipes = {rid: r for rid, r in all_recipes.items() if rid in visible_ids}
    print(f"   filtered to 'all-recipes' category: {len(recipes)} (was {len(all_recipes)})")
    return recipes, data.get("period", {})


# -------------------- COOKBOOK MAP --------------------
def _current_cookbook_count(session):
    """Quick peek at the cookbook listing to learn its total count."""
    try:
        r = session.get(
            COOKBOOK_LISTING_API,
            params={"category": "recipes", "limit": 1, "offset": 0},
            timeout=10,
        )
        if r.status_code == 200:
            return r.json().get("data", {}).get("count")
    except Exception:
        pass
    return None


def build_uuid_to_slug_map(session, use_cache=True):
    if use_cache and SLUG_CACHE.exists():
        try:
            mapping = json.loads(SLUG_CACHE.read_text())
            cookbook_count = _current_cookbook_count(session)
            if cookbook_count is None:
                print(f"-> loaded {len(mapping)} cached UUID->slug mappings "
                      f"(couldn't check freshness)")
                return mapping
            # If cache is missing >2% of the current cookbook, rebuild — new
            # recipes added since the cache was built won't have slugs
            # otherwise, and their ingredient lists go blank in the dashboard.
            shortfall = (cookbook_count - len(mapping)) / cookbook_count
            if shortfall <= 0.02:
                print(f"-> loaded {len(mapping)} cached UUID->slug mappings "
                      f"(cookbook has {cookbook_count}; cache is fresh)")
                return mapping
            print(f"-> cache stale ({len(mapping)} cached vs {cookbook_count} "
                  f"in cookbook, {shortfall*100:.1f}% missing) — rebuilding")
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


_SLUG_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def derive_slug_from_name(name):
    """Best-effort slug guess for recipes missing from the cookbook listing.
    Gousto's URL slugs are kebab-case ascii of the recipe name, so e.g.
    'Beef Spaghetti Bolognese' -> 'beef-spaghetti-bolognese'. Empirically
    matches Gousto's actual slug for most menu-but-not-in-cookbook recipes."""
    if not name:
        return None
    # Strip diacritics and non-ASCII (catches '�' replacement chars too)
    ascii_name = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    slug = _SLUG_NON_ALNUM.sub("-", ascii_name.lower()).strip("-")
    return slug or None


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

    # Surcharge / "premium recipe" handling. Gousto's surcharge object looks like
    # {"name": "Premium Recipe Surcharge", "price": 999, "price_per_portion": 250}
    # — both values in pence. Core recipes have surcharge = null.
    surcharge_obj = menu_recipe.get("surcharge") or {}
    surcharge_pence = surcharge_obj.get("price_per_portion") if surcharge_obj else 0
    surcharge_per_portion_gbp = round((surcharge_pence or 0) / 100, 2)
    is_surcharge = surcharge_per_portion_gbp > 0

    # Ingredient list — pulled from the recipe detail endpoint, scoped to
    # the 2-PORTION view (Gousto's canonical per-recipe quantities; matches
    # what you'd see if browsing the recipe page directly on gousto.co.uk).
    # The endpoint returns a UNION of SKU variants across all portion sizes;
    # we look up portion_sizes[portions=2].ingredients_skus and rebuild
    # labels with the in_box multiplier from that entry. SKUs not delivered
    # at the 2-portion size are filtered out at scrape time.
    INGREDIENT_PORTION_SIZE = 2
    ingredients_json = ""
    if detail:
        entry = (detail.get("data") or {}).get("entry") or {}
        all_ings = entry.get("ingredients") or []
        portion_sizes = entry.get("portion_sizes") or []
        target = next(
            (p for p in portion_sizes
             if p.get("portions") == INGREDIENT_PORTION_SIZE and p.get("is_offered")),
            None,
        )
        if target:
            sku_count = {}
            for s in target.get("ingredients_skus") or []:
                uid = s.get("id")
                if uid:
                    sku_count[uid] = (s.get("quantities") or {}).get("in_box", 0) or 0

            _trailing = re.compile(r"\s*x\s*\d+\s*$")
            slim = []
            for ing in all_ings:
                if not isinstance(ing, dict):
                    continue
                uid = ing.get("gousto_uuid")
                cnt = sku_count.get(uid, 0)
                if cnt < 1:
                    continue
                base = _trailing.sub("", ing.get("label", "")).strip()
                label = base if cnt == 1 else f"{base} x{cnt}"
                slim.append({"name": ing.get("name", ""),
                             "label": label, "in_box": cnt})
            if slim:
                ingredients_json = json.dumps(slim, ensure_ascii=False)

    return {
        "menu_week_start": week_start,
        "scraped_at": scraped_at,
        "id": rid,
        "core_recipe_id": menu_recipe.get("core_recipe_id", ""),
        "name": name,
        "is_surcharge": is_surcharge,
        "surcharge_per_portion_gbp": surcharge_per_portion_gbp,
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
        "ingredients_json": ingredients_json,
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
    # Gousto's menu API returns the menu with the latest cutoff <= delivery_date,
    # not the menu containing it. Cutoffs are on Tuesdays. Anchor each week's
    # delivery_date to the next Tuesday strictly after today (the cutoff of the
    # currently-live menu), then step 7 days per weeks_ahead. This makes
    # weeks_ahead=0 always return the menu users are currently ordering from,
    # regardless of what day of the week the scraper runs.
    today = date.today()
    days_to_next_tue = (1 - today.weekday()) % 7 or 7  # weekday: Mon=0..Sun=6
    target = today + timedelta(days=days_to_next_tue + weeks_ahead * 7)
    delivery_date = target.isoformat()
    print(f"\n--- Week +{weeks_ahead}: requesting delivery_date={delivery_date} ---")

    menu_recipes, period = fetch_menu(session, url_template, delivery_date, num_portions)
    week_start = (period.get("when_start", "") or delivery_date)[:10]
    print(f"   menu period: {week_start} -> {(period.get('when_cutoff','') or '')[:10]}")
    print(f"   {len(menu_recipes)} recipes on this menu")

    if not menu_recipes:
        return None, week_start, 0

    rows = []
    matched = 0
    total = len(menu_recipes)
    for i, (rid, menu_r) in enumerate(menu_recipes.items(), 1):
        slug = uuid_to_slug.get(rid)
        detail = fetch_recipe_detail(session, slug) if slug else None
        # Fallback: some menu recipes aren't in the cookbook listing at all
        # (Gousto staples and some new SKUs). Guess the slug from the recipe
        # name — empirically matches Gousto's URL pattern for most of them.
        if not detail:
            guessed = derive_slug_from_name(menu_r.get("name", ""))
            if guessed and guessed != slug:
                detail = fetch_recipe_detail(session, guessed)
                if detail:
                    uuid_to_slug[rid] = guessed  # cache for any future weeks
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
    return out_path, week_start, total


def run():
    scraped_at = date.today().isoformat()
    device_id, url_template = bootstrap()
    if not device_id:
        raise RuntimeError("Bootstrap failed - couldn't capture device_id")
    session = make_session(device_id)
    uuid_to_slug = build_uuid_to_slug_map(session, use_cache=True)

    csv_paths = []
    week_counts = []
    failures = []
    for wk in WEEKS_TO_FETCH:
        try:
            path, week_start, count = scrape_week(
                session, url_template, wk, uuid_to_slug, NUM_PORTIONS, scraped_at
            )
            if path:
                csv_paths.append(path)
                week_counts.append((week_start, count))
        except Exception as e:
            print(f"   FAILED week +{wk}: {e}")
            failures.append((wk, str(e)))

    print(f"\n{'='*60}\nDONE: {len(csv_paths)} CSV(s) created\n{'='*60}")
    for p in csv_paths:
        print(f"  {p}")

    print(f"\n{'='*60}\nPER-WEEK FILTERED COUNTS (compare to gousto.co.uk/menu)\n{'='*60}")
    for week_start, count in week_counts:
        print(f"  {week_start}  ->  {count} recipes")
    if failures:
        print(f"\n{'='*60}\nFAILURES\n{'='*60}")
        for wk, msg in failures:
            print(f"  week +{wk}: {msg}")
        raise RuntimeError(f"{len(failures)} week(s) failed")

    return csv_paths


if __name__ == "__main__":
    try:
        run()
    except Exception as e:
        print(f"FATAL: {e}", file=sys.stderr)
        sys.exit(1)
