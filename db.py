import logging
import os
import shutil
from urllib.parse import urlparse

import duckdb
import pandas as pd
import requests
import streamlit as st

from google.cloud import bigquery, storage
from google.oauth2 import service_account

from build_measures_sql import build_sql

logger = logging.getLogger(__name__)

# --- Constants ---
BUCKET_NAME = "ebmdatalab"
GCS_DB_PATH = "measures_app/measures.duckdb"
LOCAL_DB = "/tmp/measures.duckdb"

BQ_DATASET = "ebmdatalab.measures"
TARGET_TABLE = "measures"

PREFIXES = ["ccg", "pcn", "stp"]

BASE_COLS = ["month", "numerator", "denominator", "percentile"]

ORG_ID_COL = {
    "ccg": "pct_id",
    "pcn": "pcn_id",
    "stp": "stp_id",
}

REPO_URL = (
    "https://api.github.com/repos/"
    "ebmdatalab/openprescribing/contents/"
    "openprescribing/measures/definitions"
)

SQL_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queries")


# --- Auth / clients ---
def _credentials():
    return service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"]
    )


def _gcs_client():
    return storage.Client(credentials=_credentials())


def _bq_client():
    return bigquery.Client(credentials=_credentials(), project="ebmdatalab")


def _github_headers():
    return {"Authorization": f"token {st.secrets['github_token']}"}


# --- Helpers ---
def measure_id_from_github_url(url):
    if not url:
        return None
    try:
        return os.path.splitext(os.path.basename(urlparse(url).path))[0]
    except Exception:
        return None


def _fetch_measures_df_from_github() -> pd.DataFrame:
    """
    Fetch measure definitions from GitHub and return a dataframe with:
    - measure_name
    - measure_id
    """
    res = requests.get(REPO_URL, headers=_github_headers(), timeout=15)
    res.raise_for_status()

    rows = [
        {
            "measure_name": data.get("name", measure_id),
            "measure_id": measure_id,
        }
        for item in res.json()
        if item.get("name", "").endswith(".json")
        for measure_id in [measure_id_from_github_url(item.get("html_url"))]
        for data in [requests.get(item["download_url"], timeout=10).json()]
    ]

    return pd.DataFrame(rows)


def _latest_bq_date() -> str | None:
 
    bq = _bq_client()
    try:
        result = bq.query(
            f"""
            SELECT DATE(MAX(month))
            FROM `{BQ_DATASET}.ccg_data_lpzomnibus`
            """
        ).result()
        row = list(result)[0]
        return str(row[0]) if row[0] else None
    except Exception as e:
        logger.error("Failed to get latest BQ date: %s", e)
        return None


def _cached_date(conn) -> str | None:
    try:
        result = conn.execute(
            f"SELECT MAX(CAST(month AS DATE)) FROM {TARGET_TABLE}"
        ).fetchone()
        return str(result[0]) if result and result[0] else None
    except Exception:
        return None


def _normalise_df(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        if "date" in str(df[col].dtype).lower():
            df[col] = pd.to_datetime(df[col]).dt.date
    return df


def _rebuild_measures_table(conn):
    bq = _bq_client()

    measures_df = _fetch_measures_df_from_github()

    existing_tables = {
        table.table_id for table in bq.list_tables(BQ_DATASET)
    }

    sql = build_sql(measures_df, existing_tables)

    logger.info("Generated SQL length: %s", len(sql))
    logger.info("First 2000 chars of SQL:\n%s", sql[:2000])

    if not sql.strip():
        raise ValueError("No SQL generated for measures rebuild")

    df = _normalise_df(bq.query(sql).result().to_dataframe())

    conn.execute(f"DROP TABLE IF EXISTS {TARGET_TABLE}")
    conn.register("_tmp", df)
    conn.execute(f"CREATE TABLE {TARGET_TABLE} AS SELECT * FROM _tmp")
    conn.unregister("_tmp")


def _save_db_to_gcs(bucket):
    tmp = LOCAL_DB + ".upload.tmp"
    shutil.copy2(LOCAL_DB, tmp)

    try:
        bucket.blob(GCS_DB_PATH).upload_from_filename(tmp)
    except Exception as e:
        logger.warning("Failed to upload DB to GCS: %s", e)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# --- Main entry point ---
@st.cache_resource
def get_duckdb_connection():
    storage_client = _gcs_client()
    bucket = storage_client.bucket(BUCKET_NAME)

    latest_bq = _latest_bq_date()
    logger.info("Latest BQ date: %s", latest_bq)

    # 1) local DB
    if os.path.exists(LOCAL_DB):
        try:
            conn = duckdb.connect(LOCAL_DB)
            tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]

            if TARGET_TABLE in tables and _cached_date(conn) == latest_bq:
                logger.info("Using local DuckDB")
                return conn

            conn.close()
            logger.info("Local DB stale")
        except Exception as e:
            logger.warning("Local DB unusable: %s", e)

    # 2) GCS cache
    tmp_path = LOCAL_DB + ".tmp"

    try:
        with st.spinner("Downloading cached DB..."):
            bucket.blob(GCS_DB_PATH).download_to_filename(tmp_path)

        os.replace(tmp_path, LOCAL_DB)
        conn = duckdb.connect(LOCAL_DB)
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]

        if TARGET_TABLE in tables and _cached_date(conn) == latest_bq:
            logger.info("Using GCS cached DB")
            return conn

        conn.close()
        logger.info("GCS DB stale")

    except Exception as e:
        logger.info("No usable GCS DB: %s", e)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # 3) rebuild
    if os.path.exists(LOCAL_DB):
        os.remove(LOCAL_DB)

    with st.spinner("Rebuilding database..."):
        conn = duckdb.connect(LOCAL_DB)
        _rebuild_measures_table(conn)
        conn.checkpoint()
        conn.close()

    if not os.path.exists(LOCAL_DB):
        logger.error("DB not created!")
        return duckdb.connect(LOCAL_DB)

    _save_db_to_gcs(bucket)

    return duckdb.connect(LOCAL_DB)