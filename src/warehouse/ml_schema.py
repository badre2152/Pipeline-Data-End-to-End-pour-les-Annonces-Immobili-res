"""
ML Schema — One Big Table (OBT) / Feature Store.
All features in one flat table. No encoding, scaling, or SMOTE here —
those transformations happen in the ML notebook after extraction.
"""

import pandas as pd
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
    lien                TEXT,
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
"""

_COLS = [
    "prix", "ville", "quartier", "surface_m2", "nb_chambres",
    "nb_salles_bain", "etage", "annee_construction", "prix_par_m2",
    "age_bien", "categorie_prix", "titre", "lien", "scraped_at",
]


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

    # ✅ FIX 1: check all columns exist before insert
    missing = [c for c in _COLS if c not in df.columns]
    if missing:
        logger.error(f"Missing columns in DataFrame: {missing}")
        return

    # ✅ FIX 2: check target variable (prix) is not all NULL
    null_prix = df["prix"].isna().sum()
    total     = len(df)
    logger.info(f"Target variable (prix): {total - null_prix}/{total} valid values")
    if null_prix == total:
        logger.error("All prix values are NULL — feature store will be useless!")
        return

    sub  = df[_COLS].where(pd.notna(df[_COLS]), None)
    rows = [tuple(r) for r in sub.itertuples(index=False, name=None)]
    bulk_insert(_INSERT, rows)

    logger.info(f"=== ML Schema load finished — {len(rows)} rows in feature_store ===")