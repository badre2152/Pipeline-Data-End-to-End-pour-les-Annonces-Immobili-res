"""
Scraper for Avito.ma — immobilier listings.
Collects ONLY non-personal, publicly visible real-estate data.
No names, phone numbers, or emails are ever scraped.
Polite crawling: random delay 2–4s between requests.
"""

import re
import time
import json
import os
import random
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
)

from src.utils.logger import get_logger

logger = get_logger("scraper")

# 🔴 FIX 1: BASE_URL corrected to Avito.ma (was books.toscrape.com)
BASE_URL   = "https://www.avito.ma/fr/maroc/immobilier"
MAX_PAGES  = 1
DELAY_MIN  = 2.0
DELAY_MAX  = 4.0
BRONZE_DIR = os.path.join(os.path.dirname(__file__), "../../data/bronze")


# ── Driver ────────────────────────────────────────────────────────────────────

def _build_driver() -> webdriver.Chrome:
    options = Options()
    options.add_argument("--headless")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1920,1080")
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
    driver.set_page_load_timeout(30)  # ✅ FIX: prevent infinite hang
    return driver


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_text(driver, css: str, default: str = "") -> str:
    try:
        return driver.find_element(By.CSS_SELECTOR, css).text.strip()
    except NoSuchElementException:
        return default


# ── Immobilier URL keywords ───────────────────────────────────────────────────

IMMOBILIER_KEYWORDS = [
    "immobilier", "appartement", "maison",
    "villa", "terrain", "bureau", "riad", "local", "ferme"
]


def _get_listing_urls(driver, page_url: str) -> list[str]:
    urls = []
    try:
        driver.get(page_url)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href$='.htm']"))  # ✅ FIX: listing URLs end with .htm
        )
        seen = set()
        for a in driver.find_elements(By.CSS_SELECTOR, "a[href$='.htm']"):  # ✅ FIX
            href = a.get_attribute("href")
            if href and href not in seen:
                if any(kw in href for kw in IMMOBILIER_KEYWORDS):  # ✅ FIX: immobilier only
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
        # 🔴 FIX 2: error field added to track timeout/failures clearly
        "error":              None,
    }
    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "h1"))
        )

        record["titre"] = _safe_text(driver, "h1")
        record["prix"]  = _safe_text(driver, "p[font-weight='bold']")  # ✅ FIX: stable attribute

        # Location — "Toute la ville, Martil" or "Hay Riad, Rabat"
        ville_text = _safe_text(driver, "span[class*='sc-16573058-17']")
        if ville_text:
            parts = ville_text.split(",")
            if len(parts) >= 2:
                record["ville"]    = parts[-1].strip()   # ✅ "Martil"
                record["quartier"] = parts[0].strip()    # ✅ "Toute la ville" or "Hay Riad"
            else:
                record["ville"] = ville_text.strip()

        # Attribute items — using stable title attributes ✅ FIX
        record["surface"]       = _safe_text(driver, "span[title='Surface totale']")
        record["nb_chambres"]   = _safe_text(driver, "span[title='Chambres']")
        record["nb_salles_bain"]= _safe_text(driver, "span[title='Salle de bain']")
        record["etage"]         = _safe_text(driver, "span[title='Étage']")

        logger.debug(f"Scraped: {record['titre'][:60]}")

    except TimeoutException:
        logger.warning(f"Timeout on listing: {url}")
        record["error"] = "timeout"  # 🔴 FIX 2: mark clearly instead of silent fail

    except Exception as e:
        logger.error(f"Error scraping {url}: {e}")
        record["error"] = str(e)  # 🔴 FIX 2: capture the error message

    return record


# ── Bronze persistence ────────────────────────────────────────────────────────

def _save_bronze(records: list[dict], page_num: int) -> str:
    today = datetime.utcnow()

    # 🔴 FIX 3: use BRONZE_DIR consistently (was hardcoded "data/bronze/...")
    path = os.path.join(
        BRONZE_DIR,
        f"{today.year}/{today.month:02d}/{today.day:02d}"
    )
    os.makedirs(path, exist_ok=True)

    file_path = os.path.join(path, f"page_{page_num}.json")

    with open(file_path, "w", encoding="utf-8") as f:
        json.dump(records, f, ensure_ascii=False, indent=2)

    logger.info(f"Bronze saved → {file_path} ({len(records)} records)")
    return file_path


# ── Validation ────────────────────────────────────────────────────────────────

REQUIRED_FIELDS = ["titre", "prix", "lien"]


def check_schema(record: dict) -> bool:
    return all(field in record for field in REQUIRED_FIELDS)


def check_content(record: dict) -> bool:
    # 🔴 FIX 5: named exception instead of bare except
    try:
        if not record.get("titre") or len(record["titre"]) < 5:
            return False
        if not record.get("prix"):
            return False
        if not record.get("lien"):
            return False
        return True
    except Exception as e:
        logger.error(f"check_content error: {e}")
        return False


def check_business_rules(record: dict) -> bool:
    # 🔴 FIX 4: stricter price validation using regex (min 3 digits)
    # 🔴 FIX 5: named exception instead of bare except
    try:
        prix = str(record.get("prix", ""))
        if not re.search(r'\d{3,}', prix):
            return False
        return True
    except Exception as e:
        logger.error(f"check_business_rules error: {e}")
        return False


def is_valid_record(record: dict) -> bool:
    return (
        check_schema(record)
        and check_content(record)
        and check_business_rules(record)
    )


# ── Entry point ───────────────────────────────────────────────────────────────

def run_scraper(max_pages: int = MAX_PAGES) -> list[dict]:  # 🔴 FIX 6: will now return data
    logger.info("=== Scraper started ===")
    driver = _build_driver()

    total_records  = 0
    valid_records  = 0
    invalid_records = 0
    all_records    = []  # 🔴 FIX 6: collect all pages to return at the end

    try:
        for page_num in range(1, max_pages + 1):
            page_url = f"{BASE_URL}?o={page_num}"  # Avito uses ?o= for pagination
            logger.info(f"── Page {page_num}/{max_pages}")

            listing_urls = _get_listing_urls(driver, page_url)

            if not listing_urls:
                logger.warning("No listings found — stopping pagination.")
                break

            page_records = []  # reset per page

            for url in listing_urls:
                record = _scrape_listing(driver, url)  # create first
                total_records += 1

                if is_valid_record(record):              # then validate
                    page_records.append(record)
                    valid_records += 1
                else:
                    invalid_records += 1
                    logger.warning(f"❌ Invalid record skipped: {record.get('lien')}")

                time.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

            logger.info(
                f"\n📊 PAGE {page_num} STATS:\n"
                f"--------------------------------\n"
                f"Total records:   {total_records}\n"
                f"Valid records:   {valid_records}\n"
                f"Invalid records: {invalid_records}\n"
                f"--------------------------------"
            )

            _save_bronze(page_records, page_num)        # save after each page
            all_records.extend(page_records)            # 🔴 FIX 6: accumulate

    except WebDriverException as e:
        logger.error(f"WebDriver fatal error: {e}")

    finally:
        driver.quit()
        logger.info("WebDriver closed.")

    logger.info("=== Scraper finished ===")
    return all_records  # 🔴 FIX 6: actually return the collected data