"""
BI Schema — Star Schema for Power BI / reporting.
"""

import numpy as np
import pandas as pd
from datetime import datetime

from src.utils.db import get_connection, execute_query, fetch_all
from src.utils.logger import get_logger

logger = get_logger("bi_schema")

_DDL = [
    "CREATE SCHEMA IF NOT EXISTS bi_schema;",

    """CREATE TABLE IF NOT EXISTS bi_schema.dim_localisation (
        id_localisation SERIAL PRIMARY KEY,
        ville           TEXT NOT NULL,
        quartier        TEXT NOT NULL DEFAULT '',
        region_label    TEXT NOT NULL DEFAULT 'Autre',
        is_grande_ville BOOLEAN NOT NULL DEFAULT FALSE,
        UNIQUE (ville, quartier)
    );""",

    """CREATE TABLE IF NOT EXISTS bi_schema.dim_caracteristiques (
        id_caracteristiques SERIAL PRIMARY KEY,
        nb_chambres         BIGINT,
        nb_salles_bain      BIGINT,
        etage               TEXT NOT NULL DEFAULT '',
        annee_construction  BIGINT,
        age_bien            BIGINT,
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
        lien                TEXT UNIQUE,
        loaded_at           TIMESTAMP DEFAULT NOW()
    );""",

    "CREATE INDEX IF NOT EXISTS idx_fact_loc  ON bi_schema.fact_annonce(id_localisation);",
    "CREATE INDEX IF NOT EXISTS idx_fact_car  ON bi_schema.fact_annonce(id_caracteristiques);",
    "CREATE INDEX IF NOT EXISTS idx_fact_time ON bi_schema.fact_annonce(id_temps);",
    "CREATE INDEX IF NOT EXISTS idx_fact_prix ON bi_schema.fact_annonce(prix);",
]

# ✅ MIGRATION: يضيف الأعمدة الجديدة إذا كان الجدول موجوداً بدونها
_DDL_MIGRATIONS = [
    "ALTER TABLE bi_schema.dim_localisation ADD COLUMN IF NOT EXISTS region_label TEXT NOT NULL DEFAULT 'Autre';",
    "ALTER TABLE bi_schema.dim_localisation ADD COLUMN IF NOT EXISTS is_grande_ville BOOLEAN NOT NULL DEFAULT FALSE;",
    "ALTER TABLE bi_schema.fact_annonce ADD COLUMN IF NOT EXISTS lien TEXT;",
    # UNIQUE constraint على lien — يُضاف فقط إذا لم يكن موجوداً
    """DO $$ BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint
            WHERE conname = 'fact_annonce_lien_key'
            AND conrelid = 'bi_schema.fact_annonce'::regclass
        ) THEN
            ALTER TABLE bi_schema.fact_annonce ADD CONSTRAINT fact_annonce_lien_key UNIQUE (lien);
        END IF;
    END $$;""",
]

_VIEWS = [
    """CREATE OR REPLACE VIEW bi_schema.v_annonces_full AS
    SELECT
        f.id_annonce,
        l.ville, l.quartier, l.region_label, l.is_grande_ville,
        c.nb_chambres, c.nb_salles_bain, c.etage,
        c.annee_construction, c.age_bien,
        t.date_jour, t.annee, t.trimestre, t.mois,
        f.titre, f.prix, f.surface_m2, f.prix_par_m2,
        f.categorie_prix, f.lien
    FROM bi_schema.fact_annonce f
    LEFT JOIN bi_schema.dim_localisation     l ON f.id_localisation     = l.id_localisation
    LEFT JOIN bi_schema.dim_caracteristiques c ON f.id_caracteristiques = c.id_caracteristiques
    LEFT JOIN bi_schema.dim_temps            t ON f.id_temps            = t.id_temps;
    """,
    """CREATE OR REPLACE VIEW bi_schema.v_prix_par_ville AS
    SELECT
        l.ville,
        l.region_label,
        COUNT(*)                              AS nb_annonces,
        ROUND(AVG(f.prix)::numeric, 0)        AS prix_moyen,
        ROUND(AVG(f.prix_par_m2)::numeric, 0) AS prix_m2_moyen,
        MIN(f.prix)                           AS prix_min,
        MAX(f.prix)                           AS prix_max
    FROM bi_schema.fact_annonce f
    JOIN bi_schema.dim_localisation l ON f.id_localisation = l.id_localisation
    WHERE f.prix IS NOT NULL
    GROUP BY l.ville, l.region_label
    ORDER BY prix_moyen DESC;
    """,
]


def _upsert_localisation(cur, ville, quartier, region_label, is_grande_ville) -> int:
    cur.execute(
        """
        INSERT INTO bi_schema.dim_localisation
            (ville, quartier, region_label, is_grande_ville)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (ville, quartier)
        DO UPDATE SET
            region_label    = EXCLUDED.region_label,
            is_grande_ville = EXCLUDED.is_grande_ville
        RETURNING id_localisation
        """,
        (ville or "", quartier or "", region_label or "Autre", bool(is_grande_ville)),
    )
    return cur.fetchone()[0]


def _safe_int(v, default=-1):
    if v is None or (isinstance(v, float) and v != v):
        return default
    try:
        return int(v)
    except (ValueError, TypeError, OverflowError):
        return default


def _upsert_caracteristiques(cur, nb_ch, nb_sb, etage, annee, age) -> int:
    nb_ch = _safe_int(nb_ch)
    nb_sb = _safe_int(nb_sb)
    annee = _safe_int(annee)
    age   = None if (age is None or (isinstance(age, float) and age != age)) else int(age)
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


def _validate(inserted_this_run: int):
    logger.info("── Post-load BI validation starting ──")
    warnings = 0

    rows = fetch_all("SELECT COUNT(*) FROM bi_schema.fact_annonce;")
    total_in_db = rows[0][0] if rows else 0
    logger.info(
        f"Validation ℹ fact_annonce: {inserted_this_run} inserted this run "
        f"| {total_in_db} total in DB"
    )

    for dim, col in [("dim_localisation", "id_localisation"),
                     ("dim_caracteristiques", "id_caracteristiques")]:
        rows = fetch_all(f"""
            SELECT COUNT(*) FROM bi_schema.fact_annonce f
            WHERE f.{col} IS NOT NULL
              AND NOT EXISTS (
                  SELECT 1 FROM bi_schema.{dim} d
                  WHERE d.{col} = f.{col}
              );
        """)
        orphans = rows[0][0] if rows else 0
        if orphans:
            logger.warning(f"Validation ❌ {orphans} orphan rows ({dim})")
            warnings += 1
        else:
            logger.info(f"Validation ✅ {dim} FK: no orphans")

    if warnings == 0:
        logger.info("── Post-load BI validation PASSED ✅ ──")
    else:
        logger.warning(f"── Post-load BI validation finished with {warnings} warning(s) ──")


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

    # ✅ MIGRATION
    for migration in _DDL_MIGRATIONS:
        try:
            execute_query(migration)
        except Exception as e:
            logger.debug(f"Migration skipped: {e}")

    logger.info("BI Schema DDL + migrations applied.")

    if df is None:
        df = _fetch_clean()
        logger.info(f"Loaded {len(df)} rows from clean.annonces")

    conn    = get_connection()
    count   = 0  # rows cleanly committed via RELEASE SAVEPOINT
    skipped = 0  # rows rolled back via ROLLBACK TO SAVEPOINT

    def _val(v):
        return None if pd.isna(v) else v

    try:
        with conn:                          # ONE transaction — commits on exit
            cur = conn.cursor()

            for i, (_, row) in enumerate(df.iterrows()):
                savepoint = f"sp_row_{i}"
                try:
                    cur.execute(f"SAVEPOINT {savepoint}")

                    id_loc = _upsert_localisation(
                        cur,
                        row.get("ville"),
                        row.get("quartier"),
                        row.get("region_label"),
                        row.get("is_grande_ville"),
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

                    cur.execute(
                        """
                        INSERT INTO bi_schema.fact_annonce
                            (id_localisation, id_caracteristiques, id_temps,
                             titre, prix, surface_m2, prix_par_m2,
                             categorie_prix, lien)
                        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                        ON CONFLICT (lien) DO NOTHING
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

                    # Row fully inserted — lock it in and move on
                    cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                    count += 1

                except Exception as e:
                    # Roll back only this row — transaction stays alive
                    cur.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                    cur.execute(f"RELEASE SAVEPOINT {savepoint}")
                    skipped += 1
                    logger.warning(f"Row {i} skipped — rolled back cleanly: {e}")

            cur.close()
        # ← with conn: exits here → COMMIT (all released savepoints are persisted)

    finally:
        conn.close()

    logger.info(
        f"=== BI Schema load finished — {count} inserted, {skipped} skipped ==="
    )

    for view_sql in _VIEWS:
        try:
            execute_query(view_sql)
        except Exception as e:
            logger.warning(f"Could not create view: {e}")
    logger.info("Power BI helper views created (v_annonces_full, v_prix_par_ville).")

    _validate(inserted_this_run=count)