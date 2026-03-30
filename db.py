import logging
import os
import shutil

import duckdb
import pandas as pd
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
SOURCE_TABLE = "ebmdatalab.measures.ccg_data_lpzomnibus"  # just used to check latest date
SOURCE_DATE_COL = "month"

TARGET_TABLE = "measures"


# --- Auth / clients ---
def _credentials():
    return service_account.Credentials.from_service_account_info(
        st.secrets["gcp_service_account"]
    )


def _gcs_client():
    return storage.Client(credentials=_credentials())


def _bq_client():
    return bigquery.Client(credentials=_credentials(), project="ebmdatalab")


# --- Helpers ---
def _latest_bq_date() -> str | None:
    """
    Get latest month across one representative table.
    (Assumes all measure tables are in sync)
    """
    bq = _bq_client()
    try:
        result = bq.query(
            f"SELECT DATE(MAX({SOURCE_DATE_COL})) FROM `{SOURCE_TABLE}`"
        ).result()
        row = list(result)[0]
        return str(row[0]) if row[0] else None
    except Exception as e:
        logger.error("Failed to get latest date: %s", e)
        return None


def _cached_date(conn) -> str | None:
    try:
        result = conn.execute(
            f"SELECT MAX(CAST({SOURCE_DATE_COL} AS DATE)) FROM {TARGET_TABLE}"
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

    # get measures list
    measures_df = bq.query(
        f"SELECT DISTINCT measure_id FROM `{BQ_DATASET}.measure_metadata`"
    ).result().to_dataframe()

    # get existing tables
    existing_tables = {
        table.table_id for table in bq.list_tables(BQ_DATASET)
    }

    # build SQL dynamically
    sql = build_sql(measures_df, existing_tables)

    if not sql.strip():
        raise ValueError("No SQL generated for measures rebuild")

    # run query
    df = _normalise_df(bq.query(sql).result().to_dataframe())

    # write to duckdb
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

    # --- 1) local DB ---
    if os.path.exists(LOCAL_DB):
        try:
            conn = duckdb.connect(LOCAL_DB)

            tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]

            if (
                TARGET_TABLE in tables
                and _cached_date(conn) == latest_bq
            ):
                logger.info("Using local DuckDB")
                return conn

            conn.close()
            logger.info("Local DB stale")

        except Exception as e:
            logger.warning("Local DB unusable: %s", e)

    # --- 2) GCS cache ---
    tmp_path = LOCAL_DB + ".tmp"

    try:
        with st.spinner("Downloading cached DB..."):
            bucket.blob(GCS_DB_PATH).download_to_filename(tmp_path)

        os.replace(tmp_path, LOCAL_DB)

        conn = duckdb.connect(LOCAL_DB)
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]

        if (
            TARGET_TABLE in tables
            and _cached_date(conn) == latest_bq
        ):
            logger.info("Using GCS cached DB")
            return conn

        conn.close()
        logger.info("GCS DB stale")

    except Exception as e:
        logger.info("No usable GCS DB: %s", e)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # --- 3) rebuild ---
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