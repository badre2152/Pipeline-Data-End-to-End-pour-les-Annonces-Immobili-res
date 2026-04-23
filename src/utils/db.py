import os
import psycopg2
from psycopg2.extras import execute_values
from dotenv import load_dotenv
from src.utils.logger import get_logger

load_dotenv()
logger = get_logger("db")


def get_connection():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=int(os.getenv("DB_PORT", 5432)),
        dbname=os.getenv("DB_NAME", "avito_db"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD"),
    )


def execute_query(query: str, params=None):
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(query, params)
        logger.debug("Query executed.")
    except Exception as e:
        logger.error(f"Query failed: {e}")
        raise
    finally:
        conn.close()


def bulk_insert(query: str, rows: list):
    if not rows:
        logger.warning("bulk_insert called with empty rows — skipping.")
        return
    conn = get_connection()
    try:
        with conn:
            with conn.cursor() as cur:
                execute_values(cur, query, rows)
        logger.info(f"Bulk insert: {len(rows)} rows.")
    except Exception as e:
        logger.error(f"Bulk insert failed: {e}")
        raise
    finally:
        conn.close()


def fetch_all(query: str, params=None) -> list:
    conn = get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(query, params)
            return cur.fetchall()
    except Exception as e:
        logger.error(f"fetch_all failed: {e}")
        raise
    finally:
        conn.close()
