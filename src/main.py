"""
Pipeline orchestrator — runs all steps in sequence with automatic retry.

Order:
  1. Extract   (scraping → data/bronze/)
  2. Staging   (bronze → staging.raw_annonces)
  3. Clean     (staging → clean.annonces + data/silver/)
  4. BI Schema (clean  → bi_schema star schema)
  5. ML Schema (clean  → ml_schema.feature_store)
  6. Cleanup   (truncate staging)
"""

import sys
import time
import os

# Allow running as  python src/main.py  from project root
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from src.extract.scraper        import run_scraper
from src.staging.load_staging   import run_staging
from src.clean.clean_data       import run_clean
from src.warehouse.bi_schema    import run_bi_schema
from src.warehouse.ml_schema    import run_ml_schema
from src.utils.db               import execute_query
from src.utils.logger           import get_logger

logger       = get_logger("pipeline")
MAX_RETRIES  = 3
RETRY_DELAY  = 10   # seconds between retries


# ── Retry wrapper ─────────────────────────────────────────────────────────────

def _run(step_name: str, fn, *args, **kwargs):
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"[{step_name}] ── attempt {attempt}/{MAX_RETRIES}")
            result = fn(*args, **kwargs)
            logger.info(f"[{step_name}] ✓ success")
            return result
        except Exception as exc:
            logger.error(f"[{step_name}] ✗ attempt {attempt} failed: {exc}")
            if attempt < MAX_RETRIES:
                logger.info(f"[{step_name}] retrying in {RETRY_DELAY}s…")
                time.sleep(RETRY_DELAY)
            else:
                logger.critical(
                    f"[{step_name}] all {MAX_RETRIES} attempts failed — aborting."
                )
                raise


def _cleanup_staging():
    try:
        execute_query("TRUNCATE TABLE staging.raw_annonces RESTART IDENTITY;")
        logger.info("Staging table truncated.")
    except Exception as exc:
        logger.warning(f"Staging cleanup failed (non-fatal): {exc}")


# ── Main ──────────────────────────────────────────────────────────────────────

def run_pipeline():
    logger.info("━" * 55)
    logger.info("  AVITO.MA DATA PIPELINE — START")
    logger.info("━" * 55)
    t0 = time.time()

    try:
        # 1 — Extract
        raw = _run("EXTRACT", run_scraper, max_pages=1)

        # 2 — Staging
        _run("STAGING", run_staging, raw)

        # 3 — Clean + Feature Engineering
        df_clean = _run("CLEAN", run_clean)

        # 4 — BI Schema (Star Schema → Power BI)
        _run("BI_SCHEMA", run_bi_schema, df_clean)

        # 5 — ML Schema (OBT → Feature Store)
        _run("ML_SCHEMA", run_ml_schema, df_clean)

        # 6 — Cleanup staging
        _cleanup_staging()

    except Exception as exc:
        logger.critical(f"Pipeline aborted: {exc}")
        sys.exit(1)

    elapsed = round(time.time() - t0, 1)
    logger.info("━" * 55)
    logger.info(f"  PIPELINE COMPLETE — {elapsed}s")
    logger.info("━" * 55)


if __name__ == "__main__":
    run_pipeline()
