import logging
import os
import shutil

import duckdb
import pandas as pd
import requests
import streamlit as st

from google.cloud import bigquery, storage
from google.oauth2 import service_account

from build_measures_sql import PREFIXES, build_sql, measure_id_from_github_url


logger = logging.getLogger(__name__)
logging.basicConfig(level=logging.INFO)

# --- Constants ---
BUCKET_NAME = "ebmdatalab"
GCS_DB_PATH = "measures_app/measures.duckdb"
LOCAL_DB = "/tmp/measures.duckdb"

BQ_DATASET = "ebmdatalab.measures"
TARGET_TABLE = "measures"

REPO_URL = (
    "https://api.github.com/repos/"
    "ebmdatalab/openprescribing/contents/"
    "openprescribing/measures/definitions"
)


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
@st.cache_data(ttl=3600)
def _fetch_measures_df_from_github() -> pd.DataFrame:
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


def _normalise_df(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        if hasattr(df[col].dtype, "name") and "date" in str(df[col].dtype).lower():
            df[col] = pd.to_datetime(df[col]).dt.date
    return df


def _first_source_table(measures_df: pd.DataFrame, existing_tables: set[str]) -> str | None:
    for _, row in measures_df.iterrows():
        measure_id = row["measure_id"]
        if not measure_id:
            continue

        for prefix in PREFIXES:
            table_name = f"{prefix}_data_{measure_id}"
            if table_name in existing_tables:
                return table_name

    return None


def _latest_bq_month() -> str | None:
    bq = _bq_client()
    measures_df = _fetch_measures_df_from_github()
    existing_tables = {t.table_id for t in bq.list_tables(BQ_DATASET)}

    source_table = _first_source_table(measures_df, existing_tables)
    if not source_table:
        logger.warning("No source table found in BigQuery to check freshness.")
        return None

    try:
        result = bq.query(
            f"SELECT DATE(MAX(month)) FROM `{BQ_DATASET}.{source_table}`"
        ).result()
        row = list(result)[0]
        return str(row[0]) if row[0] else None
    except Exception as e:
        logger.error("Failed to get latest month from %s: %s", source_table, e)
        return None


def _cached_month(conn) -> str | None:
    try:
        result = conn.execute(
            f"SELECT MAX(CAST(month AS DATE)) FROM {TARGET_TABLE}"
        ).fetchone()
        return str(result[0]) if result and result[0] else None
    except Exception:
        return None


def _rebuild_measures_table(conn):
    bq = _bq_client()

    measures_df = _fetch_measures_df_from_github()
    existing_tables = {table.table_id for table in bq.list_tables(BQ_DATASET)}

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
        blob = bucket.blob(GCS_DB_PATH)
        logger.info("Uploading %s to gs://%s/%s", tmp, BUCKET_NAME, GCS_DB_PATH)

        blob.upload_from_filename(tmp)
        blob.reload()

        logger.info(
            "Upload complete: exists=%s size=%s updated=%s",
            blob.exists(),
            blob.size,
            blob.updated,
        )

        if not blob.exists():
            raise RuntimeError(
                f"Upload reported success but blob is missing: gs://{BUCKET_NAME}/{GCS_DB_PATH}"
            )

        for b in bucket.list_blobs(prefix="measures_app/"):
            logger.info("GCS blob present: %s", b.name)

    except Exception:
        logger.exception("Failed to upload DB to GCS")
        raise
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


# --- Main entry point ---
@st.cache_resource
def get_duckdb_connection():
    storage_client = _gcs_client()
    bucket = storage_client.bucket(BUCKET_NAME)
    blob = bucket.blob(GCS_DB_PATH)

    latest_bq = _latest_bq_month()
    logger.info("Latest BQ month: %s", latest_bq)

    # --- 1) Try local DB ---
    if os.path.exists(LOCAL_DB):
        try:
            conn = duckdb.connect(LOCAL_DB)
            tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]

            if TARGET_TABLE in tables and _cached_month(conn) == latest_bq:
                logger.info("Using local DuckDB")

                if not blob.exists():
                    logger.info("GCS DB missing — uploading local DB")
                    _save_db_to_gcs(bucket)

                return conn

            conn.close()
            logger.info("Local DB stale")

        except Exception as e:
            logger.warning("Local DB unusable: %s", e)

    # --- 2) Try GCS cache ---
    tmp_path = LOCAL_DB + ".tmp"

    try:
        with st.spinner("Downloading cached DB..."):
            blob.download_to_filename(tmp_path)

        os.replace(tmp_path, LOCAL_DB)

        conn = duckdb.connect(LOCAL_DB)
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]

        if TARGET_TABLE in tables and _cached_month(conn) == latest_bq:
            logger.info("Using GCS cached DB")
            return conn

        conn.close()
        logger.info("GCS DB stale")

    except Exception as e:
        logger.info("No usable GCS DB: %s", e)
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # --- 3) Rebuild ---
    if os.path.exists(LOCAL_DB):
        os.remove(LOCAL_DB)

    with st.spinner("Rebuilding database..."):
        conn = duckdb.connect(LOCAL_DB)
        _rebuild_measures_table(conn)
        conn.checkpoint()
        conn.close()

    logger.info(
        "Rebuild finished, local DB exists=%s size=%s",
        os.path.exists(LOCAL_DB),
        os.path.getsize(LOCAL_DB) if os.path.exists(LOCAL_DB) else None,
    )

    if not os.path.exists(LOCAL_DB):
        logger.error("DB not created!")
        return duckdb.connect(LOCAL_DB)

    _save_db_to_gcs(bucket)

    return duckdb.connect(LOCAL_DB)