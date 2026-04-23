"""
BI Schema — Star Schema for Power BI / reporting.

    fact_annonce
        ├── dim_localisation      (ville, quartier)
        ├── dim_caracteristiques  (chambres, sdb, étage, année, âge)
        └── dim_temps             (date, jour, mois, trimestre, année)
"""

import numpy as np
import pandas as pd
from datetime import datetime

from src.utils.db import get_connection, execute_query
from src.utils.logger import get_logger

logger = get_logger("bi_schema")

# ── DDL ───────────────────────────────────────────────────────────────────────

_DDL = [
    "CREATE SCHEMA IF NOT EXISTS bi_schema;",

    """CREATE TABLE IF NOT EXISTS bi_schema.dim_localisation (
        id_localisation SERIAL PRIMARY KEY,
        ville           TEXT NOT NULL,
        quartier        TEXT NOT NULL DEFAULT '',
        UNIQUE (ville, quartier)
    );""",

    """CREATE TABLE IF NOT EXISTS bi_schema.dim_caracteristiques (
        id_caracteristiques SERIAL PRIMARY KEY,
        nb_chambres         INTEGER,
        nb_salles_bain      INTEGER,
        etage               TEXT NOT NULL DEFAULT '',
        annee_construction  INTEGER,
        age_bien            INTEGER,
        UNIQUE (nb_chambres, nb_salles_bain, etage, annee_construction)
    );""",

    """CREATE TABLE IF NOT EXISTS bi_schema.dim_temps (
        id_temps     SERIAL PRIMARY KEY,
        date_jour    DATE NOT NULL UNIQUE,
        annee        INTEGER,
        trimestre    INTEGER,
        mois         INTEGER,
        jour         INTEGER,
        jour_semaine INTEGER
    );""",

    """CREATE TABLE IF NOT EXISTS bi_schema.fact_annonce (
        id_annonce          SERIAL PRIMARY KEY,
        id_localisation     INTEGER REFERENCES bi_schema.dim_localisation(id_localisation),
        id_caracteristiques INTEGER REFERENCES bi_schema.dim_caracteristiques(id_caracteristiques),
        id_temps            INTEGER REFERENCES bi_schema.dim_temps(id_temps),
        titre               TEXT,
        prix                NUMERIC,
        surface_m2          NUMERIC,
        prix_par_m2         NUMERIC,
        categorie_prix      TEXT,
        lien                TEXT,
        loaded_at           TIMESTAMP DEFAULT NOW()
    );""",

    "CREATE INDEX IF NOT EXISTS idx_fact_loc  ON bi_schema.fact_annonce(id_localisation);",
    "CREATE INDEX IF NOT EXISTS idx_fact_car  ON bi_schema.fact_annonce(id_caracteristiques);",
    "CREATE INDEX IF NOT EXISTS idx_fact_time ON bi_schema.fact_annonce(id_temps);",
]

# ── Upsert helpers ────────────────────────────────────────────────────────────

def _upsert_localisation(cur, ville: str, quartier: str) -> int:
    cur.execute(
        """
        INSERT INTO bi_schema.dim_localisation (ville, quartier)
        VALUES (%s, %s)
        ON CONFLICT (ville, quartier) DO UPDATE SET ville = EXCLUDED.ville
        RETURNING id_localisation
        """,
        (ville or "", quartier or ""),
    )
    return cur.fetchone()[0]


def _upsert_caracteristiques(cur, nb_ch, nb_sb, etage, annee, age) -> int:
    # ✅ FIX: replace NULL with -1 so ON CONFLICT works in PostgreSQL
    nb_ch = nb_ch if nb_ch is not None else -1
    nb_sb = nb_sb if nb_sb is not None else -1
    annee = annee if annee is not None else -1

    cur.execute(
        """
        INSERT INTO bi_schema.dim_caracteristiques
            (nb_chambres, nb_salles_bain, etage, annee_construction, age_bien)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT (nb_chambres, nb_salles_bain, etage, annee_construction)
        DO UPDATE SET age_bien = EXCLUDED.age_bien
        RETURNING id_caracteristiques
        """,
        (nb_ch, nb_sb, etage or "", annee, age),
    )
    return cur.fetchone()[0]


def _upsert_temps(cur, scraped_at) -> int:
    if scraped_at is None or (isinstance(scraped_at, float) and np.isnan(scraped_at)):
        d = datetime.utcnow().date()
    elif isinstance(scraped_at, datetime):
        d = scraped_at.date()
    elif isinstance(scraped_at, str):
        d = datetime.fromisoformat(scraped_at).date()
    else:
        d = datetime.utcnow().date()

    cur.execute(
        """
        INSERT INTO bi_schema.dim_temps
            (date_jour, annee, trimestre, mois, jour, jour_semaine)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (date_jour) DO NOTHING
        RETURNING id_temps
        """,
        (d, d.year, (d.month - 1) // 3 + 1, d.month, d.day, d.weekday()),
    )
    row = cur.fetchone()
    if row:
        return row[0]
    cur.execute("SELECT id_temps FROM bi_schema.dim_temps WHERE date_jour = %s", (d,))
    return cur.fetchone()[0]


# ── Main ──────────────────────────────────────────────────────────────────────

def _fetch_clean() -> pd.DataFrame:
    conn = get_connection()
    try:
        return pd.read_sql("SELECT * FROM clean.annonces", conn)
    finally:
        conn.close()


def run_bi_schema(df: pd.DataFrame | None = None):
    logger.info("=== BI Schema load started ===")

    for stmt in _DDL:
        execute_query(stmt)
    logger.info("BI Schema DDL applied.")

    if df is None:
        df = _fetch_clean()
        logger.info(f"Loaded {len(df)} rows from clean.annonces")

    conn    = get_connection()
    count   = 0

    try:
        with conn:
            for _, row in df.iterrows():
                try:
                    with conn.cursor() as cur:
                        id_loc = _upsert_localisation(
                            cur, row.get("ville"), row.get("quartier")
                        )
                        id_car = _upsert_caracteristiques(
                            cur,
                            row.get("nb_chambres"),
                            row.get("nb_salles_bain"),
                            row.get("etage"),
                            row.get("annee_construction"),
                            row.get("age_bien"),
                        )
                        id_tps = _upsert_temps(cur, row.get("scraped_at"))

                        def _val(v):
                            return None if pd.isna(v) else v

                        cur.execute(
                            """
                            INSERT INTO bi_schema.fact_annonce
                                (id_localisation, id_caracteristiques, id_temps,
                                 titre, prix, surface_m2, prix_par_m2,
                                 categorie_prix, lien)
                            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                            """,
                            (
                                id_loc, id_car, id_tps,
                                row.get("titre"),
                                _val(row.get("prix")),
                                _val(row.get("surface_m2")),
                                _val(row.get("prix_par_m2")),
                                row.get("categorie_prix"),
                                row.get("lien"),
                            ),
                        )
                    count += 1
                except Exception as e:
                    logger.warning(f"Skipping row due to error: {e}")  # ✅ FIX: skip failed row only
                    continue
    finally:
        conn.close()

    logger.info(f"=== BI Schema load finished — {count} fact rows ===")