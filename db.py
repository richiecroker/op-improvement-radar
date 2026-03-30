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
GCS_DB_PATH = "improvement_radar/measures.duckdb"
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


@st.cache_data(ttl=86400)
def _fetch_measures_df() -> pd.DataFrame:
    res = requests.get(REPO_URL, headers=_github_headers(), timeout=15)
    res.raise_for_status()
    rows = [
        {
            "measure_id": measure_id,
            "name": data.get("name", measure_id),
            "is_percentage": data.get("is_percentage", False),
            "y_label": data.get("y_label", ""),
            "radar_exclude": data.get("radar_exclude", False),
        }
        for item in res.json()
        if item.get("name", "").endswith(".json")
        for measure_id in [measure_id_from_github_url(item.get("html_url"))]
        for data in [requests.get(item["download_url"], timeout=10).json()]
    ]
    df = pd.DataFrame(rows)
    return df[df["radar_exclude"] == False]  # filter out excluded measures


def _normalise_df(df: pd.DataFrame) -> pd.DataFrame:
    for col in df.columns:
        if "date" in str(df[col].dtype).lower():
            df[col] = pd.to_datetime(df[col]).dt.date
    return df


def _latest_bq_month():
    row = list(_bq_client().query(
        "SELECT DATE(MAX(month)) FROM `ebmdatalab.measures.ccg_data_saba`"
    ).result())[0]
    return str(row[0])


def _cached_month(conn):
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

    # Build orgs table
    orgs_sql = """
        SELECT 'pcn' AS org_type, code, name FROM `ebmdatalab.hscic.pcns`
        UNION ALL
        SELECT 'ccg', code, name FROM `ebmdatalab.hscic.ccgs`
        WHERE close_date IS NULL AND org_type = 'CCG'
        UNION ALL
        SELECT 'stp', code, name FROM `ebmdatalab.hscic.stps`
    """
    orgs_df = bq.query(orgs_sql).result().to_dataframe()
    conn.execute("DROP TABLE IF EXISTS orgs")
    conn.register("_tmp_orgs", orgs_df)
    conn.execute("CREATE TABLE orgs AS SELECT * FROM _tmp_orgs")
    conn.unregister("_tmp_orgs")


def _save_to_gcs(bucket):
    tmp = LOCAL_DB + ".upload.tmp"
    shutil.copy2(LOCAL_DB, tmp)
    try:
        bucket.blob(GCS_DB_PATH).upload_from_filename(tmp)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


@st.cache_resource
def get_duckdb_connection():
    bucket = _gcs_client().bucket(BUCKET_NAME)
    latest_bq = _latest_bq_month()

    # 1) Local DB fresh?
    if os.path.exists(LOCAL_DB):
        conn = duckdb.connect(LOCAL_DB)
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
        if TARGET_TABLE in tables and _cached_month(conn) == latest_bq:
            return conn
        conn.close()

    # 2) GCS cache fresh?
    tmp_path = LOCAL_DB + ".tmp"
    try:
        with st.spinner("Downloading cached DB..."):
            bucket.blob(GCS_DB_PATH).download_to_filename(tmp_path)
        os.replace(tmp_path, LOCAL_DB)
        conn = duckdb.connect(LOCAL_DB)
        tables = [r[0] for r in conn.execute("SHOW TABLES").fetchall()]
        if TARGET_TABLE in tables and _cached_month(conn) == latest_bq:
            return conn
        conn.close()
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)

    # 3) Rebuild from BQ
    for ext in ["", ".wal"]:
        p = LOCAL_DB + ext
        if os.path.exists(p):
            os.remove(p)

    with st.spinner("Rebuilding database..."):
        conn = duckdb.connect(LOCAL_DB)
        _rebuild(conn)
        conn.checkpoint()
        conn.close()

    _save_to_gcs(bucket)
    return duckdb.connect(LOCAL_DB)