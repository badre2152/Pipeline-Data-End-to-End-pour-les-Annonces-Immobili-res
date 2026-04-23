"""
Staging layer — loads raw scraped records into staging.raw_annonces.
Data is stored as-is; no transformation happens here.
"""

import json
import os
from src.utils.db import execute_query, bulk_insert
from src.utils.logger import get_logger

logger = get_logger("staging")

BRONZE_DIR = os.path.join(os.path.dirname(__file__), "../../data/bronze")

_DDL_SCHEMA = "CREATE SCHEMA IF NOT EXISTS staging;"

_DDL_TABLE = """
CREATE TABLE IF NOT EXISTS staging.raw_annonces (
    id                  SERIAL PRIMARY KEY,
    titre               TEXT,
    prix                TEXT,
    ville               TEXT,
    quartier            TEXT,
    surface             TEXT,
    nb_chambres         TEXT,
    nb_salles_bain      TEXT,
    etage               TEXT,
    annee_construction  TEXT,
    lien                TEXT,
    scraped_at          TEXT,
    loaded_at           TIMESTAMP DEFAULT NOW()
);
"""

_INSERT = """
INSERT INTO staging.raw_annonces
    (titre, prix, ville, quartier, surface, nb_chambres,
     nb_salles_bain, etage, annee_construction, lien, scraped_at)
VALUES %s
"""


def _latest_bronze_file() -> str | None:
    if not os.path.exists(BRONZE_DIR):
        return None
    files = []
    for root, dirs, filenames in os.walk(BRONZE_DIR):  # ✅ FIX: recursive search
        for f in filenames:
            if f.endswith(".json"):
                files.append(os.path.join(root, f))
    files = sorted(files, reverse=True)
    return files[0] if files else None


def run_staging(records: list[dict] | None = None):
    logger.info("=== Staging load started ===")

    execute_query(_DDL_SCHEMA)
    execute_query(_DDL_TABLE)
    logger.info("staging.raw_annonces — schema/table ready.")

    if records is None:
        path = _latest_bronze_file()
        if not path:
            logger.error("No bronze file found — aborting staging.")
            return
        logger.info(f"Reading bronze file: {path}")
        with open(path, encoding="utf-8") as f:
            records = json.load(f)

    if not records:
        logger.warning("Empty record list — nothing to insert.")
        return

    rows = [
        (
            r.get("titre"),
            r.get("prix"),
            r.get("ville"),
            r.get("quartier"),
            r.get("surface"),
            r.get("nb_chambres"),
            r.get("nb_salles_bain"),
            r.get("etage"),
            r.get("annee_construction"),
            r.get("lien"),
            r.get("scraped_at"),
        )
        for r in records
        if r.get("error") is None  # ✅ FIX: skip failed records
    ]

    bulk_insert(_INSERT, rows)
    logger.info(f"=== Staging load finished — {len(rows)} rows inserted ===")