import time
import json
import os
import random
import re
from datetime import datetime

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
    InvalidSessionIdException,
    StaleElementReferenceException,
)

from src.utils.logger import get_logger

logger = get_logger("scraper")

BASE_URL   = "https://www.avito.ma/fr/maroc/immobilier"
MAX_PAGES  = 1
DELAY_MIN  = 2.0
DELAY_MAX  = 4.0
BRONZE_DIR = os.path.join(os.path.dirname(__file__), "../../data/bronze")

IMMOBILIER_KEYWORDS = [
    "appartement", "maison", "villa", "terrain",
    "bureau", "riad", "local", "ferme", "immobilier",
    "villas", "terrains", "appartements",
]

# ── FIX 1: Expanded false-positive exclusion list for location parsing ────────
LOCATION_FALSE_POSITIVES = [
    "vendre", "louer", "categorie", "annonce",
    "immobilier", "appartement", "appartements",
    "maison", "maisons", "villa", "villas",
    "terrain", "terrains", "bureau", "riad",
    "local", "ferme", "résidentiel", "commercial",
]


# ── Driver ────────────────────────────────────────────────────────────────────

def _build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
    options.add_argument("--disable-extensions")
    options.add_argument("--js-flags=--max-old-space-size=512")
    options.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )

    chromium_bin = "/usr/bin/chromium"
    if os.path.exists(chromium_bin):
        options.binary_location = chromium_bin
        driver = webdriver.Chrome(
            service=Service("/usr/bin/chromedriver"), options=options
        )
    else:
        from webdriver_manager.chrome import ChromeDriverManager
        driver = webdriver.Chrome(
            service=Service(ChromeDriverManager().install()), options=options
        )

    driver.set_page_load_timeout(30)
    return driver


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_text(driver, css: str, default: str = "") -> str:
    try:
        return driver.find_element(By.CSS_SELECTOR, css).text.strip()
    except (NoSuchElementException, StaleElementReferenceException):
        return default


def _get_listing_urls(driver, page_url: str) -> list[str]:
    urls = []
    try:
        driver.get(page_url)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href$='.htm']"))
        )
        seen = set()
        for a in driver.find_elements(By.CSS_SELECTOR, "a[href$='.htm']"):
            try:
                href = a.get_attribute("href")
            except StaleElementReferenceException:
                continue
            if href and href not in seen:
                if any(kw in href for kw in IMMOBILIER_KEYWORDS):
                    seen.add(href)
                    urls.append(href)
        logger.info(f"Page {page_url} → {len(urls)} listings found.")
    except TimeoutException:
        logger.warning(f"Timeout on results page: {page_url}")
    except Exception as e:
        logger.error(f"Error fetching results page {page_url}: {e}")
    return urls


def _scrape_listing(driver, url: str) -> dict:
    record = {
        "titre":              "",
        "prix":               "",
        "ville":              "",
        "quartier":           "",
        "surface":            "",
        "nb_chambres":        "",
        "nb_salles_bain":     "",
        "etage":              "",
        "annee_construction": "",
        "lien":               url,
        "scraped_at":         datetime.utcnow().isoformat(),
        "error":              None,
    }
    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "h1"))
        )

        # ── Titre ──────────────────────────────────────────────────────────
        record["titre"] = _safe_text(driver, "h1")

        # ── Prix ───────────────────────────────────────────────────────────
        # Real format: '2 330 000 DH' — appears in multiple spans
        # We grab all short texts and find the one matching price pattern
        for el in driver.find_elements(By.CSS_SELECTOR, "span, p"):
            try:
                txt = el.text.strip()
            except StaleElementReferenceException:
                continue
            # Match: digits + optional spaces/narrow-spaces + DH
            if re.search(r'[\d\s\u202f]+DH', txt) and len(txt) < 30:
                # Skip the monthly financing line
                if "mois" not in txt.lower():
                    record["prix"] = txt
                    break

        # ── Localisation ───────────────────────────────────────────────────
        # FIX 1: Expanded exclusion list + both sides of comma must be non-empty
        # Real format: 'Guéliz, Marrakech' in a single element
        for el in driver.find_elements(By.CSS_SELECTOR, "span, p, a"):
            try:
                txt = el.text.strip()
            except StaleElementReferenceException:
                continue
            if "," in txt and 3 < len(txt) < 50:
                if not any(w in txt.lower() for w in LOCATION_FALSE_POSITIVES):
                    parts = [p.strip() for p in txt.split(",")]
                    if len(parts) == 2 and all(parts):  # both sides non-empty
                        record["quartier"] = parts[0]
                        record["ville"]    = parts[1]
                        break

        # ── Attributs ──────────────────────────────────────────────────────
        # FIX 2: Strip empty parts from newline split before checking length
        # This fixes annee_construction = 0% caused by trailing newlines
        # e.g. "2018\nAnnée de construction\n " was producing 3 parts → skipped
        for el in driver.find_elements(By.CSS_SELECTOR, "span, div, p"):
            try:
                txt = el.text.strip()
            except StaleElementReferenceException:
                continue

            if "\n" not in txt or len(txt) > 60:
                continue

            # Filter out empty/whitespace-only parts before checking count
            parts = [p.strip() for p in txt.split("\n") if p.strip()]
            if len(parts) != 2:
                continue

            value, label = parts[0], parts[1].lower()

            if "surface" in label:
                record["surface"] = value
            elif "chambre" in label or "pièce" in label:
                record["nb_chambres"] = value
            elif "salle" in label or "bain" in label:
                record["nb_salles_bain"] = value
            elif "étage" in label or "etage" in label:
                record["etage"] = value
            elif "année" in label or "construction" in label:
                record["annee_construction"] = value

        logger.debug(
            f"Scraped: {record['titre'][:50]} | "
            f"prix={record['prix']} | ville={record['ville']}"
        )

    except TimeoutException:
        logger.warning(f"Timeout on listing: {url}")
        record["error"] = "timeout"
    except InvalidSessionIdException:
        raise
    except Exception as e:
        logger.error(f"Error scraping {url}: {e}")
        record["error"] = str(e)

    return record


# ── Bronze persistence ────────────────────────────────────────────────────────

def _save_bronze(records: list[dict]) -> str:
    os.makedirs(BRONZE_DIR, exist_ok=True)
    ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(BRONZE_DIR, f"avito_raw_{ts}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)
    logger.info(f"Bronze saved → {path}  ({len(records)} records)")
    return path


# ── Fill rate monitor ─────────────────────────────────────────────────────────

def _log_fill_rates(records: list[dict]) -> None:
    """FIX 3: Warn early when key fields have low fill rates before data
    reaches the warehouse. Catches scraping regressions immediately."""
    if not records:
        return
    total = len(records)
    key_fields = [
        "prix", "ville", "quartier", "surface",
        "nb_chambres", "nb_salles_bain", "annee_construction",
    ]
    for field in key_fields:
        filled = sum(1 for r in records if r.get(field) not in ("", None))
        pct = (filled / total) * 100
        if pct < 80:
            logger.warning(f"⚠️  Low fill rate: {field} = {pct:.1f}%  ({filled}/{total})")
        else:
            logger.info(f"✅ Fill rate: {field} = {pct:.1f}%")


# ── Entry point ───────────────────────────────────────────────────────────────

def run_scraper(max_pages: int = MAX_PAGES) -> list[dict]:
    logger.info("=== Scraper started ===")
    driver      = _build_driver()
    all_records = []

    try:
        for page_num in range(1, max_pages + 1):
            page_url = f"{BASE_URL}?page={page_num}"
            logger.info(f"── Results page {page_num}/{max_pages}")

            listing_urls = []
            for attempt in range(3):
                listing_urls = _get_listing_urls(driver, page_url)
                if listing_urls:
                    break
                logger.warning(f"Attempt {attempt + 1}: no URLs found, retrying…")
                time.sleep(5)

            if not listing_urls:
                logger.warning("No listings found — stopping pagination.")
                break

            for url in listing_urls:
                try:
                    record = _scrape_listing(driver, url)

                except InvalidSessionIdException:
                    logger.warning("⚠️ Chrome crashed — restarting driver...")
                    try:
                        driver.quit()
                    except Exception:
                        pass
                    driver = _build_driver()
                    logger.info("✅ Driver restarted — retrying URL...")
                    try:
                        record = _scrape_listing(driver, url)
                    except Exception as retry_exc:
                        logger.error(f"Retry failed for {url}: {retry_exc}")
                        record = {
                            "error": str(retry_exc), "lien": url,
                            "titre": "", "prix": "", "ville": "",
                            "quartier": "", "surface": "",
                            "nb_chambres": "", "nb_salles_bain": "",
                            "etage": "", "annee_construction": "",
                            "scraped_at": datetime.utcnow().isoformat(),
                        }

                except Exception as e:
                    logger.error(f"Unexpected error on {url}: {e}")
                    record = {
                        "error": str(e), "lien": url,
                        "titre": "", "prix": "", "ville": "",
                        "quartier": "", "surface": "",
                        "nb_chambres": "", "nb_salles_bain": "",
                        "etage": "", "annee_construction": "",
                        "scraped_at": datetime.utcnow().isoformat(),
                    }

                if not record.get("error"):
                    all_records.append(record)

                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    except WebDriverException as e:
        logger.error(f"WebDriver fatal error: {e}")
    finally:
        try:
            driver.quit()
        except Exception:
            pass
        logger.info("WebDriver closed.")

    _save_bronze(all_records)

    # FIX 3: Log fill rates so regressions are caught before hitting the warehouse
    _log_fill_rates(all_records)

    logger.info(f"=== Scraper finished — {len(all_records)} records ===")
    return all_records