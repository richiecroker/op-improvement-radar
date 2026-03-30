
Copy

import os
import shutil
 
import duckdb
import pandas as pd
import requests
import streamlit as st
 
from google.cloud import bigquery, storage
from google.oauth2 import service_account
 
from build_measures_sql import PREFIXES, build_sql, measure_id_from_github_url
 
 
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
 
 
@st.cache_data(ttl=3600)
def _fetch_measures_df() -> pd.DataFrame:
    res = requests.get(REPO_URL, headers=_github_headers(), timeout=15)
    res.raise_for_status()
    rows = [
        {"measure_name": data.get("name", measure_id), "measure_id": measure_id}
        for item in res.json()
        if item.get("name", "").endswith(".json")
        for measure_id in [measure_id_from_github_url(item.get("html_url"))]
        for data in [requests.get(item["download_url"], timeout=10).json()]
    ]
    return pd.DataFrame(rows)
 
 
def _normalise_df(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        if "date" in str(df[col].dtype).lower():
            df[col] = pd.to_datetime(df[col]).dt.date
    return df
 
 
def _latest_bq_month() -> str | None:
    bq = _bq_client()
    measures_df = _fetch_measures_df()
    existing = {t.table_id for t in bq.list_tables(BQ_DATASET)}
    source = next(
        (f"{p}_data_{row['measure_id']}" for _, row in measures_df.iterrows()
         for p in PREFIXES if f"{p}_data_{row['measure_id']}" in existing),
        None,
    )
    if not source:
        return None
    try:
        row = list(bq.query(f"SELECT DATE(MAX(month)) FROM `{BQ_DATASET}.{source}`").result())[0]
        return str(row[0]) if row[0] else None
    except Exception:
        return None
 
 
def _cached_month(conn) -> str | None:
    try:
        result = conn.execute(f"SELECT MAX(CAST(month AS DATE)) FROM {TARGET_TABLE}").fetchone()
        return str(result[0]) if result and result[0] else None
    except Exception:
        return None
 
 
def _rebuild(conn):
    bq = _bq_client()
    measures_df = _fetch_measures_df()
    existing = {t.table_id for t in bq.list_tables(BQ_DATASET)}
    sql = build_sql(measures_df, existing)
    if not sql.strip():
        raise ValueError("No SQL generated for measures rebuild")
    df = _normalise_df(bq.query(sql).result().to_dataframe())
    conn.execute(f"DROP TABLE IF EXISTS {TARGET_TABLE}")
    conn.register("_tmp", df)
    conn.execute(f"CREATE TABLE {TARGET_TABLE} AS SELECT * FROM _tmp")
    conn.unregister("_tmp")
 
 
def _save_to_gcs(bucket):
    tmp = LOCAL_DB + ".upload.tmp"
    shutil.copy2(LOCAL_DB, tmp)
    try:
        blob = bucket.blob(GCS_DB_PATH)
        blob.upload_from_filename(tmp)
        blob.reload()
        if not blob.exists():
            raise RuntimeError("Upload reported success but blob is missing")
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
 
 
@st.cache_resource
def _open_connection():
    return duckdb.connect(LOCAL_DB)
 
 
def get_duckdb_connection():
    bucket = _gcs_client().bucket(BUCKET_NAME)
    blob = bucket.blob(GCS_DB_PATH)
    latest_bq = _latest_bq_month()
 
    # 1) Local DB fresh?
    if os.path.exists(LOCAL_DB):
        try:
            conn = duckdb.connect(LOCAL_DB)
            tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
            if TARGET_TABLE in tables and _cached_month(conn) == latest_bq:
                conn.close()
                if not blob.exists():
                    _save_to_gcs(bucket)
                return _open_connection()
            conn.close()
        except Exception:
            pass
 
    # 2) GCS cache fresh?
    tmp_path = LOCAL_DB + ".tmp"
    try:
        with st.spinner("Downloading cached DB..."):
            blob.download_to_filename(tmp_path)
        os.replace(tmp_path, LOCAL_DB)
        conn = duckdb.connect(LOCAL_DB)
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
        if TARGET_TABLE in tables and _cached_month(conn) == latest_bq:
            conn.close()
            return _open_connection()
        conn.close()
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
 
    # 3) Rebuild from BQ
    if os.path.exists(LOCAL_DB):
        os.remove(LOCAL_DB)
    with st.spinner("Rebuilding database..."):
        conn = duckdb.connect(LOCAL_DB)
        _rebuild(conn)
        conn.checkpoint()
        conn.close()
 
    _save_to_gcs(bucket)
    return _open_connection()
 