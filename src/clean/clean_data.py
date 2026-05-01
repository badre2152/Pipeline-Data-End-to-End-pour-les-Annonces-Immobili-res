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
    prix_par_m2         NUMERIC,
    age_bien            INTEGER,
    categorie_prix      TEXT,
    region_label        TEXT,
    is_grande_ville     BOOLEAN,
    loaded_at           TIMESTAMP DEFAULT NOW()
);
"""

# ✅ MIGRATION: يضيف الأعمدة الجديدة إذا كان الجدول موجوداً بدونها
_DDL_MIGRATIONS = [
    "ALTER TABLE clean.annonces ADD COLUMN IF NOT EXISTS region_label TEXT;",
    "ALTER TABLE clean.annonces ADD COLUMN IF NOT EXISTS is_grande_ville BOOLEAN;",
    "ALTER TABLE clean.annonces ADD COLUMN IF NOT EXISTS prix_par_m2 NUMERIC;",
    "ALTER TABLE clean.annonces ADD COLUMN IF NOT EXISTS age_bien INTEGER;",
    "ALTER TABLE clean.annonces ADD COLUMN IF NOT EXISTS categorie_prix TEXT;",
]

_INSERT = """
INSERT INTO clean.annonces
    (titre, prix, ville, quartier, surface_m2, nb_chambres,
     nb_salles_bain, etage, annee_construction, lien, scraped_at,
     prix_par_m2, age_bien, categorie_prix,
     region_label, is_grande_ville)
VALUES %s
"""

# ── City / region reference ───────────────────────────────────────────────────

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
    "sale": "Salé", "salé": "Salé",
    "temara": "Témara", "témara": "Témara",
    "berrechid": "Berrechid",
    "khouribga": "Khouribga",
    "dakhla": "Dakhla",
    "laayoune": "Laâyoune",
}

_GRANDES_VILLES = {
    "Casablanca", "Rabat", "Marrakech", "Fès", "Tanger",
    "Agadir", "Meknès", "Oujda", "Kénitra", "Tétouan",
}

_REGION_MAP = {
    "Casablanca": "Casablanca-Settat",
    "Mohammedia": "Casablanca-Settat",
    "Berrechid":  "Casablanca-Settat",
    "Settat":     "Casablanca-Settat",
    "El Jadida":  "Casablanca-Settat",
    "Rabat":      "Rabat-Salé-Kénitra",
    "Salé":       "Rabat-Salé-Kénitra",
    "Témara":     "Rabat-Salé-Kénitra",
    "Kénitra":    "Rabat-Salé-Kénitra",
    "Marrakech":  "Marrakech-Safi",
    "Safi":       "Marrakech-Safi",
    "Fès":        "Fès-Meknès",
    "Meknès":     "Fès-Meknès",
    "Tanger":     "Tanger-Tétouan-Al Hoceïma",
    "Tétouan":    "Tanger-Tétouan-Al Hoceïma",
    "Agadir":     "Souss-Massa",
    "Oujda":      "L'Oriental",
    "Nador":      "L'Oriental",
    "Beni Mellal":"Béni Mellal-Khénifra",
    "Khouribga":  "Béni Mellal-Khénifra",
    "Dakhla":     "Dakhla-Oued Ed-Dahab",
    "Laâyoune":   "Laâyoune-Sakia El Hamra",
}


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
    m = re.search(r"\d+\.?\d*", text)
    return float(m.group()) if m else None


def _clean_prix(v) -> float | None:
    return _extract_number(str(v)) if pd.notna(v) else None

def _clean_surface(v) -> float | None:
    return _extract_number(str(v)) if pd.notna(v) else None

def _clean_int(v, max_val: int = 32767) -> int | None:
    n = _extract_number(str(v)) if pd.notna(v) else None
    if n is None:
        return None
    n = int(n)
    return n if n <= max_val else None


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


def _apply_missing_value_strategy(df: pd.DataFrame) -> pd.DataFrame:
    n0 = len(df)
    df = df[df["prix"].notna()]
    logger.info(f"Missing-value strategy: dropped {n0 - len(df)} rows with null prix")
    n1 = len(df)
    df = df[df["ville"].notna() & (df["ville"] != "")]
    logger.info(f"Missing-value strategy: dropped {n1 - len(df)} rows with empty ville")
    df["etage"]    = df["etage"].fillna("Non précisé").replace("", "Non précisé")
    df["titre"]    = df["titre"].fillna("Sans titre").replace("", "Sans titre")
    df["quartier"] = df["quartier"].fillna("").str.strip()
    df["scraped_at"] = df["scraped_at"].fillna(pd.Timestamp.utcnow())
    logger.info(f"Missing-value strategy applied. Remaining rows: {len(df)}")
    return df


def _clean(df: pd.DataFrame) -> pd.DataFrame:
    n0 = len(df)
    df = df.drop_duplicates(subset=["lien"], keep="last")
    logger.info(f"Dedup: {n0} → {len(df)} rows ({n0 - len(df)} removed)")

    df["prix"]               = df["prix"].apply(_clean_prix)
    df["surface_m2"]         = df["surface"].apply(_clean_surface)
    df["nb_chambres"]        = df["nb_chambres"].apply(_clean_int)
    df["nb_salles_bain"]     = df["nb_salles_bain"].apply(_clean_int)
    df["annee_construction"] = df["annee_construction"].apply(_clean_int)

    df["ville"]    = df["ville"].apply(_standardize_ville)
    df["quartier"] = df["quartier"].fillna("").str.strip().str.title()
    df["titre"]    = df["titre"].fillna("").str.strip()
    df["etage"]    = df["etage"].fillna("").str.strip()
    df["scraped_at"] = pd.to_datetime(df["scraped_at"], errors="coerce")

    df = _apply_missing_value_strategy(df)

    for col in ["prix", "surface_m2"]:
        if df[col].notna().sum() < 10:
            logger.warning(f"Skipping outlier filter for [{col}] — not enough data")
            continue
        q_lo = df[col].quantile(0.01)
        q_hi = df[col].quantile(0.99)
        n_before = len(df)
        df = df[df[col].isna() | ((df[col] >= q_lo) & (df[col] <= q_hi))]
        logger.info(f"Outlier filter [{col}]: removed {n_before - len(df)} rows")

    current_year = datetime.now().year

    df["prix_par_m2"] = np.where(
        df["surface_m2"].notna() & (df["surface_m2"] > 0) & df["prix"].notna(),
        (df["prix"] / df["surface_m2"]).round(2),
        np.nan,
    )

    df["age_bien"] = df["annee_construction"].apply(
        lambda x: int(x) if pd.notna(x) and x <= 200 else None
    )
    df["annee_construction"] = None
    df["categorie_prix"]  = df["prix"].apply(_categorize_prix)
    df["region_label"]    = df["ville"].map(_REGION_MAP).fillna("Autre")
    df["is_grande_ville"] = df["ville"].isin(_GRANDES_VILLES)

    logger.info(f"Cleaning done. Shape: {df.shape}")
    return df


def _ml_readiness_report(df: pd.DataFrame):
    feature_cols = [
        "prix", "surface_m2", "nb_chambres", "nb_salles_bain",
        "etage", "annee_construction", "ville", "quartier",
        "prix_par_m2", "age_bien", "categorie_prix",
        "region_label", "is_grande_ville",
    ]
    n = len(df)
    if n == 0:
        logger.warning("ML Readiness: 0 rows — cannot compute.")
        return
    lines = [f"\n🤖 ML READINESS REPORT — {n} rows"]
    for col in feature_cols:
        if col not in df.columns:
            lines.append(f"  ❓ {col:<22}: column not found")
            continue
        null_count = df[col].isna().sum()
        fill_pct   = 100 * (n - null_count) // n
        status = "✅" if fill_pct >= 80 else ("⚠️" if fill_pct >= 40 else "❌")
        lines.append(f"  {status} {col:<22}: {n - null_count}/{n} filled ({fill_pct}%)")
    logger.info("\n".join(lines))


def _save_silver(df: pd.DataFrame):
    os.makedirs(SILVER_DIR, exist_ok=True)
    ts   = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(SILVER_DIR, f"avito_clean_{ts}.csv")
    df.to_csv(path, index=False, encoding="utf-8")
    logger.info(f"Silver CSV saved → {path}")


def _load_to_db(df: pd.DataFrame):
    execute_query(_DDL_SCHEMA)
    execute_query(_DDL_TABLE)
    # ✅ MIGRATION: يضيف الأعمدة الجديدة إذا كان الجدول موجوداً بدونها
    for migration in _DDL_MIGRATIONS:
        try:
            execute_query(migration)
        except Exception as e:
            logger.debug(f"Migration skipped (already exists): {e}")

    cols = [
        "titre", "prix", "ville", "quartier", "surface_m2",
        "nb_chambres", "nb_salles_bain", "etage", "annee_construction",
        "lien", "scraped_at", "prix_par_m2", "age_bien", "categorie_prix",
        "region_label", "is_grande_ville",
    ]

    missing = [c for c in cols if c not in df.columns]
    if missing:
        logger.error(f"Missing columns in DataFrame: {missing}")
        return

    sub = df[cols].where(pd.notna(df[cols]), None)
    INT_COLS = {'nb_chambres', 'nb_salles_bain', 'annee_construction', 'age_bien'}

    def safe_row(row):
        result = []
        for col, val in zip(cols, row):
            if col in INT_COLS and val is not None:
                try:
                    v = int(val)
                    result.append(v if v <= 32767 else None)
                except (ValueError, TypeError):
                    result.append(None)
            else:
                result.append(val)
        return tuple(result)

    rows = [safe_row(r) for r in sub.itertuples(index=False, name=None)]
    bulk_insert(_INSERT, rows)


def run_clean() -> pd.DataFrame:
    logger.info("=== Clean layer started ===")
    df_raw   = _fetch_staging()
    df_clean = _clean(df_raw)
    _ml_readiness_report(df_clean)
    _save_silver(df_clean)
    _load_to_db(df_clean)
    logger.info("=== Clean layer finished ===")
    return df_clean