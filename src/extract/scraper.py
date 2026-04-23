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


BASE_URL   = "https://www.avito.ma/fr/maroc/immobilier"
MAX_PAGES  = 5
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
    driver.set_page_load_timeout(30)  
    return driver


# ── Helpers ───────────────────────────────────────────────────────────────────

def _safe_text(driver, css: str, default: str = "") -> str:
    try:
        return driver.find_element(By.CSS_SELECTOR, css).text.strip()
    except NoSuchElementException:
        return default


def _get_listing_urls(driver, page_url: str) -> list[str]:
    urls = []
    try:
        driver.get(page_url)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "a[href*='/fr/maroc/']"))  
        )
        seen = set()
        for a in driver.find_elements(By.CSS_SELECTOR, "a[href*='/fr/maroc/']"):  
            href = a.get_attribute("href")
            if href and href not in seen:
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

        record["titre"] = _safe_text(driver, "h1")
        record["prix"]  = _safe_text(driver, "p[font-weight='bold']")  

        
        ville_text = _safe_text(driver, "span[class*='sc-16573058-17']")
        if ville_text:
            parts = ville_text.split(",")
            if len(parts) >= 2:
                record["ville"]    = parts[-1].strip()   
                record["quartier"] = parts[0].strip()    
            else:
                record["ville"] = ville_text.strip()

        
        record["surface"]       = _safe_text(driver, "span[title='Surface totale']")
        record["nb_chambres"]   = _safe_text(driver, "span[title='Chambres']")
        record["nb_salles_bain"]= _safe_text(driver, "span[title='Salle de bain']")
        record["etage"]         = _safe_text(driver, "span[title='Étage']")

        logger.debug(f"Scraped: {record['titre'][:60]}")

    except TimeoutException:
        logger.warning(f"Timeout on listing: {url}")
        record["error"] = "timeout"  

    except Exception as e:
        logger.error(f"Error scraping {url}: {e}")
        record["error"] = str(e)  

    return record


# ── Bronze persistence ────────────────────────────────────────────────────────

def _save_bronze(records: list[dict], page_num: int) -> str:
    today = datetime.utcnow()

    
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

def run_scraper(max_pages: int = MAX_PAGES) -> list[dict]:  
    logger.info("=== Scraper started ===")
    driver = _build_driver()

    total_records  = 0
    valid_records  = 0
    invalid_records = 0
    all_records    = []  

    try:
        for page_num in range(1, max_pages + 1):
            page_url = f"{BASE_URL}?o={page_num}"  
            logger.info(f"── Page {page_num}/{max_pages}")

            listing_urls = _get_listing_urls(driver, page_url)

            if not listing_urls:
                logger.warning("No listings found — stopping pagination.")
                break

            page_records = []  

            for url in listing_urls:
                record = _scrape_listing(driver, url)  
                total_records += 1

                if is_valid_record(record):              
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

            _save_bronze(page_records, page_num)        
            all_records.extend(page_records)            

    except WebDriverException as e:
        logger.error(f"WebDriver fatal error: {e}")

    finally:
        driver.quit()
        logger.info("WebDriver closed.")

    logger.info("=== Scraper finished ===")
    return all_records  