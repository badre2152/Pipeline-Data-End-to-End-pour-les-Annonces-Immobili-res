"""
ML Schema — One Big Table (OBT) / Feature Store.
All features in one flat table. No encoding, scaling, or SMOTE here —
those transformations happen in the ML notebook after extraction.
"""

import pandas as pd
import numpy as np
from src.utils.db import get_connection, execute_query, bulk_insert
from src.utils.logger import get_logger

logger = get_logger("ml_schema")

_DDL_SCHEMA = "CREATE SCHEMA IF NOT EXISTS ml_schema;"

_DDL_TABLE = """
CREATE TABLE IF NOT EXISTS ml_schema.feature_store (
    id                  SERIAL PRIMARY KEY,

    -- Target variable
    prix                NUMERIC,

    -- Raw features
    ville               TEXT,
    quartier            TEXT,
    surface_m2          NUMERIC,
    nb_chambres         INTEGER,
    nb_salles_bain      INTEGER,
    etage               TEXT,
    annee_construction  INTEGER,

    -- Engineered features
    prix_par_m2         NUMERIC,
    age_bien            INTEGER,
    categorie_prix      TEXT,

    -- Metadata (excluded from model training)
    titre               TEXT,
    lien                TEXT UNIQUE,
    scraped_at          TIMESTAMP,
    loaded_at           TIMESTAMP DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_ml_ville ON ml_schema.feature_store(ville);
CREATE INDEX IF NOT EXISTS idx_ml_prix  ON ml_schema.feature_store(prix);
"""

_INSERT = """
INSERT INTO ml_schema.feature_store
    (prix, ville, quartier, surface_m2, nb_chambres, nb_salles_bain,
     etage, annee_construction, prix_par_m2, age_bien, categorie_prix,
     titre, lien, scraped_at)
VALUES %s
ON CONFLICT (lien) DO NOTHING
"""

_COLS = [
    "prix", "ville", "quartier", "surface_m2", "nb_chambres",
    "nb_salles_bain", "etage", "annee_construction", "prix_par_m2",
    "age_bien", "categorie_prix", "titre", "lien", "scraped_at",
]

# PostgreSQL INTEGER range
INT_MIN = -2_147_483_648
INT_MAX =  2_147_483_647

# Columns that must fit in PostgreSQL INTEGER
_INT_COLS = ["nb_chambres", "nb_salles_bain", "annee_construction", "age_bien"]


def _safe_int(val):
    """Convert value to safe PostgreSQL INTEGER or None."""
    if val is None or (isinstance(val, float) and np.isnan(val)):
        return None
    try:
        v = int(val)
        if INT_MIN <= v <= INT_MAX:
            return v
        return None  # out of range → treat as missing
    except (ValueError, TypeError, OverflowError):
        return None


def _fetch_clean() -> pd.DataFrame:
    conn = get_connection()
    try:
        return pd.read_sql("SELECT * FROM clean.annonces", conn)
    finally:
        conn.close()


def run_ml_schema(df: pd.DataFrame | None = None):
    logger.info("=== ML Schema load started ===")

    execute_query(_DDL_SCHEMA)
    for stmt in _DDL_TABLE.strip().split(";"):
        s = stmt.strip()
        if s:
            execute_query(s + ";")
    logger.info("ML Schema DDL applied.")

    if df is None:
        df = _fetch_clean()
        logger.info(f"Loaded {len(df)} rows from clean.annonces")

    # Check all columns exist
    missing = [c for c in _COLS if c not in df.columns]
    if missing:
        logger.error(f"Missing columns in DataFrame: {missing}")
        return

    # Skip if empty
    if df.empty:
        logger.warning("DataFrame is empty — skipping ML schema load.")
        return

    # Check target variable
    null_prix = df["prix"].isna().sum()
    total     = len(df)
    logger.info(f"Target variable (prix): {total - null_prix}/{total} valid values")

    if null_prix == total:
        logger.warning("All prix values are NULL — skipping feature store load.")
        return

    # ✅ FIX: safely cast integer columns to prevent overflow
    df = df.copy()
    for col in _INT_COLS:
        if col in df.columns:
            df[col] = df[col].apply(_safe_int)

    sub  = df[_COLS].where(pd.notna(df[_COLS]), None)
    rows = [tuple(r) for r in sub.itertuples(index=False, name=None)]

    # ✅ FIX: replace any remaining numpy int types with Python native int/None
    safe_rows = []
    for row in rows:
        safe_row = []
        for i, val in enumerate(row):
            col = _COLS[i]
            if col in _INT_COLS:
                safe_row.append(_safe_int(val))
            elif isinstance(val, (np.integer,)):
                safe_row.append(int(val))
            elif isinstance(val, (np.floating,)):
                safe_row.append(None if np.isnan(val) else float(val))
            else:
                safe_row.append(val)
        safe_rows.append(tuple(safe_row))

    bulk_insert(_INSERT, safe_rows)
    logger.info(f"=== ML Schema load finished — {len(safe_rows)} rows in feature_store ===")