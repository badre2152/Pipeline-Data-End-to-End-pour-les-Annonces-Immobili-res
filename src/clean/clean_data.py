"""
Clean layer — reads from staging.raw_annonces, applies full cleaning
+ feature engineering, writes to clean.annonces and data/silver/.
"""

import re
import os
from datetime import datetime

import numpy as np
import pandas as pd

from src.utils.db import get_connection, execute_query, bulk_insert
from src.utils.logger import get_logger

logger    = get_logger("clean")
SILVER_DIR = os.path.join(os.path.dirname(__file__), "../../data/silver")

# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL_SCHEMA = "CREATE SCHEMA IF NOT EXISTS clean;"

_DDL_TABLE = """
CREATE TABLE IF NOT EXISTS clean.annonces (
    id                  SERIAL PRIMARY KEY,
    titre               TEXT,
    prix                NUMERIC,
    ville               TEXT,
    quartier            TEXT,
    surface_m2          NUMERIC,
    nb_chambres         INTEGER,
    nb_salles_bain      INTEGER,
    etage               TEXT,
    annee_construction  INTEGER,
    lien                TEXT,
    scraped_at          TIMESTAMP,
    -- Engineered features
    prix_par_m2         NUMERIC,
    age_bien            INTEGER,
    categorie_prix      TEXT,
    loaded_at           TIMESTAMP DEFAULT NOW()
);
"""

_INSERT = """
INSERT INTO clean.annonces
    (titre, prix, ville, quartier, surface_m2, nb_chambres,
     nb_salles_bain, etage, annee_construction, lien, scraped_at,
     prix_par_m2, age_bien, categorie_prix)
VALUES %s
"""

# ── Parsing helpers ───────────────────────────────────────────────────────────

def _extract_number(text) -> float | None:
    if not isinstance(text, str):
        return None
    text = (
        text.replace("\u202f", "")
            .replace("\xa0", "")
            .replace(" ", "")
            .replace(",", ".")
    )
    m = re.search(r"\d+\.?\d*", text)  # ✅ FIX: avoid float("3.5.2") crash
    return float(m.group()) if m else None


def _clean_prix(v) -> float | None:
    return _extract_number(str(v)) if pd.notna(v) else None

def _clean_surface(v) -> float | None:
    return _extract_number(str(v)) if pd.notna(v) else None

def _clean_int(v) -> int | None:
    n = _extract_number(str(v)) if pd.notna(v) else None
    return int(n) if n is not None else None


_VILLE_MAP = {
    "casablanca": "Casablanca", "casa": "Casablanca",
    "rabat": "Rabat",
    "marrakech": "Marrakech", "marrakesh": "Marrakech",
    "fes": "Fès", "fès": "Fès", "fez": "Fès",
    "tanger": "Tanger",
    "agadir": "Agadir",
    "meknes": "Meknès", "meknès": "Meknès",
    "oujda": "Oujda",
    "kenitra": "Kénitra", "kénitra": "Kénitra",
    "tetouan": "Tétouan", "tétouan": "Tétouan",
    "safi": "Safi",
    "mohammedia": "Mohammedia",
    "beni mellal": "Beni Mellal", "béni mellal": "Beni Mellal",
    "el jadida": "El Jadida",
    "nador": "Nador",
    "settat": "Settat",
}

def _standardize_ville(v) -> str:
    if not isinstance(v, str):
        return ""
    return _VILLE_MAP.get(v.strip().lower(), v.strip().title())


def _categorize_prix(prix) -> str:
    if prix is None or (isinstance(prix, float) and np.isnan(prix)):
        return "Inconnu"
    if prix < 300_000:   return "Bas"
    if prix < 800_000:   return "Moyen"
    if prix < 2_000_000: return "Élevé"
    return "Luxe"


# ── Pipeline ──────────────────────────────────────────────────────────────────

def _fetch_staging() -> pd.DataFrame:
    conn = get_connection()
    try:
        df = pd.read_sql("SELECT * FROM staging.raw_annonces ORDER BY loaded_at", conn)
        logger.info(f"Fetched {len(df)} rows from staging.")
        return df
    finally:
        conn.close()


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    n0 = len(df)

    # 1. Deduplicate by URL
    df = df.drop_duplicates(subset=["lien"], keep="last")
    logger.info(f"Dedup: {n0} → {len(df)} rows ({n0 - len(df)} removed)")

    # 2. Parse numerics
    df["prix"]          = df["prix"].apply(_clean_prix)
    df["surface_m2"]    = df["surface"].apply(_clean_surface)
    df["nb_chambres"]   = df["nb_chambres"].apply(_clean_int)
    df["nb_salles_bain"]= df["nb_salles_bain"].apply(_clean_int)
    df["annee_construction"] = df["annee_construction"].apply(_clean_int)

    # 3. Standardize location
    df["ville"]    = df["ville"].apply(_standardize_ville)
    df["quartier"] = df["quartier"].fillna("").str.strip().str.title()

    # 4. Clean text
    df["titre"]    = df["titre"].fillna("").str.strip()
    df["etage"]    = df["etage"].fillna("").str.strip()

    # 5. Parse date
    df["scraped_at"] = pd.to_datetime(df["scraped_at"], errors="coerce")

    # 6. Remove extreme outliers (1st–99th percentile)
    for col in ["prix", "surface_m2"]:
        if df[col].notna().sum() < 10:  # ✅ FIX: skip if too few values
            logger.warning(f"Skipping outlier filter for [{col}] — not enough data")
            continue
        q_lo = df[col].quantile(0.01)
        q_hi = df[col].quantile(0.99)
        n_before = len(df)
        df = df[df[col].isna() | ((df[col] >= q_lo) & (df[col] <= q_hi))]
        logger.info(f"Outlier filter [{col}]: removed {n_before - len(df)} rows")

    # 7. Feature engineering
    current_year = datetime.now().year

    df["prix_par_m2"] = np.where(
        df["surface_m2"].notna() & (df["surface_m2"] > 0) & df["prix"].notna(),
        (df["prix"] / df["surface_m2"]).round(2),
        np.nan,
    )

    df["age_bien"] = np.where(
        df["annee_construction"].notna(),
        current_year - df["annee_construction"],
        np.nan,
    )
    df["age_bien"] = df["age_bien"].apply(
        lambda x: int(x) if pd.notna(x) else None
    )

    df["categorie_prix"] = df["prix"].apply(_categorize_prix)

    logger.info(f"Cleaning done. Shape: {df.shape}")
    return df


def _save_silver(df: pd.DataFrame):
    os.makedirs(SILVER_DIR, exist_ok=True)
    ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SILVER_DIR, f"avito_clean_{ts}.csv")
    df.to_csv(path, index=False, encoding="utf-8")
    logger.info(f"Silver CSV saved → {path}")


def _load_to_db(df: pd.DataFrame):
    execute_query(_DDL_SCHEMA)
    execute_query(_DDL_TABLE)

    cols = [
        "titre", "prix", "ville", "quartier", "surface_m2",
        "nb_chambres", "nb_salles_bain", "etage", "annee_construction",
        "lien", "scraped_at", "prix_par_m2", "age_bien", "categorie_prix",
    ]

    # ✅ FIX: check all columns exist before insert
    missing = [c for c in cols if c not in df.columns]
    if missing:
        logger.error(f"Missing columns in DataFrame: {missing}")
        return

    sub = df[cols].where(pd.notna(df[cols]), None)
    rows = [tuple(r) for r in sub.itertuples(index=False, name=None)]
    bulk_insert(_INSERT, rows)


def run_clean() -> pd.DataFrame:
    logger.info("=== Clean layer started ===")
    df_raw   = _fetch_staging()
    df_clean = _clean(df_raw)
    _save_silver(df_clean)
    _load_to_db(df_clean)
    logger.info("=== Clean layer finished ===")
    return df_clean