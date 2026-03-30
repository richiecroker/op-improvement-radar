import streamlit as st
import pandas as pd
import yaml
import os
import requests

from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from urllib.parse import urlparse

from db import get_duckdb_connection

from google.cloud import bigquery, storage
from google.oauth2 import service_account

def _credentials():
    return service_account.Credentials.from_service_account_info(st.secrets["gcp_service_account"])

def _bq_client():
    return bigquery.Client(credentials=_credentials(), project="ebmdatalab")


# ── Page config — must be first Streamlit command ─────────────────────────────

st.set_page_config(layout="wide")

# ── App ────────────────────────────────────────────────────────────────────────

# --- Header ---
base_dir = os.path.dirname(__file__)
#st.image(os.path.join(base_dir, "content", "OpenPrescribing.svg"))
st.info(
    """##### Hello!  This is a **very** early prototype of something.  
Please let us know what you think, and what you'd like to see.  Email us at [bennett@phc.ox.ac.uk](mailto:bennett@phc.ox.ac.uk)"""
)

conn = get_duckdb_connection()

#measures = conn.execute(
#    "SELECT DISTINCT measure FROM measures ORDER BY measure"
#).df()["measure"].tolist()#
#
#selected_measure = st.selectbox("Select a measure", measures)

conn = get_duckdb_connection()
st.write(conn.execute("SELECT * FROM measures LIMIT 5").df())

df = conn.execute(
    """
    WITH base AS (
        SELECT * FROM measures
        WHERE measure = 'aafpercent' AND org_type = 'ccg'
        ORDER BY month
    ),
    ranked AS (
        SELECT *,
            ROW_NUMBER() OVER (PARTITION BY org_id ORDER BY month)       AS rn_asc,
            ROW_NUMBER() OVER (PARTITION BY org_id ORDER BY month DESC)  AS rn_desc
        FROM base
    ),
    agg AS (
        SELECT
            org_id,
            AVG(numerator)                                            AS mean_events,
            AVG(CASE WHEN rn_asc  <= 6 THEN calc_value END)           AS start_rate,
            AVG(CASE WHEN rn_desc <= 6 THEN calc_value END)           AS end_rate,
            AVG(CASE WHEN rn_asc  <= 6 THEN percentile END)           AS start_pct,
            AVG(CASE WHEN rn_desc <= 6 THEN percentile END)           AS end_pct
        FROM ranked
        GROUP BY org_id
    ),
    valid AS (
        SELECT org_id,
            (start_pct - end_pct) AS pct_drop
        FROM agg
        WHERE
            mean_events > ?
            AND start_rate > 0
            AND (start_rate - end_rate) / start_rate >= ? / 100.0
            AND end_rate > 0
            AND start_pct > ?
            AND end_pct   < ?
        ORDER BY pct_drop DESC
        LIMIT ?
    )
    SELECT DISTINCT v.org_id
    FROM valid v;
    """,
    [20, 10, 0.8, 0.4, 10]
).df()

st.dataframe(df)

st.dataframe(df)

st.dataframe(df)


# ── Information ─────────────────────────────────────────────────────────────────

st.divider()

with st.expander("Click here to read our methodology", icon=":material/quick_reference:"):
    with open(os.path.join(base_dir, "content", "methodology.md")) as f:
        st.markdown(f.read())

#with open(os.path.join(base_dir, "content", "changelog.yaml")) as f:
#    changelog = yaml.safe_load(f)

#with st.expander("Click to see changelog", icon=":material/history:"):
 #   for entry in reversed(changelog):
 #       st.markdown(f"**{entry['date']}** — {entry['change']} *({entry['person']})*")

