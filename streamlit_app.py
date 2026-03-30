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

import streamlit as st
import plotly.graph_objects as go
import pandas as pd

from db import get_duckdb_connection
from db import _fetch_measures_df

def _credentials():
    return service_account.Credentials.from_service_account_info(st.secrets["gcp_service_account"])

def _bq_client():
    return bigquery.Client(credentials=_credentials(), project="ebmdatalab")


# ── Page config — must be first Streamlit command ─────────────────────────────

st.set_page_config(layout="wide")

# ── App ────────────────────────────────────────────────────────────────────────

# --- Header ---
base_dir = os.path.dirname(__file__)
st.image(os.path.join(base_dir, "content", "OpenPrescribing.svg"))
st.info(
    """##### Hello!  This is a **very** early prototype of an enhanced version of the Improvement Radar.  
Please let us know what you think, and what you'd like to see.  Email us at [bennett@phc.ox.ac.uk](mailto:bennett@phc.ox.ac.uk)"""
)

PLOT_COLOURS = ["red", "green", "orange", "purple", "brown"]

st.title("Improvement Radar")

conn = get_duckdb_connection()

# --- Selectors ---
measures_df = _fetch_measures_df()
measure_options = dict(zip(measures_df["name"], measures_df["measure_id"]))
selected_name = st.selectbox("Select a measure", sorted(measure_options.keys()))
selected_measure = measure_options[selected_name]

with st.sidebar:
    org_type = st.selectbox(
        "Select organisation type",
        options=["ccg", "pcn", "stp"],
        format_func=lambda x: {"ccg": "Sub-ICB Location (SICBL)", "pcn": "Primary Care Network (PCN)", "stp": "Integrated Care Board"}[x]
    )   

# --- Lookup measure metadata ---
row = measures_df[measures_df["measure_id"] == selected_measure].iloc[0]
is_percentage = row["is_percentage"]
y_label = row["y_label"] if row["y_label"] else "Rate"

# --- Filter orgs ---
filtered_orgs_df = conn.execute(
    """
    WITH base AS (
        SELECT * FROM measures
        WHERE measure = ? AND org_type = ?
        ORDER BY month
    ),
    ranked AS (
        SELECT *,
            ROW_NUMBER() OVER (PARTITION BY org_id ORDER BY month)      AS rn_asc,
            ROW_NUMBER() OVER (PARTITION BY org_id ORDER BY month DESC) AS rn_desc
        FROM base
    ),
    agg AS (
        SELECT
            org_id,
            AVG(numerator)                                          AS mean_events,
            AVG(CASE WHEN rn_asc  <= 6 THEN calc_value END)        AS start_rate,
            AVG(CASE WHEN rn_desc <= 6 THEN calc_value END)        AS end_rate,
            AVG(CASE WHEN rn_asc  <= 6 THEN percentile END)        AS start_pct,
            AVG(CASE WHEN rn_desc <= 6 THEN percentile END)        AS end_pct,
            ARG_MAX(month, percentile)                              AS peak_month
        FROM ranked
        GROUP BY org_id
    ),
    valid AS (
        SELECT org_id,
            (start_pct - end_pct) AS pct_drop,
            peak_month
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
    SELECT DISTINCT v.org_id, v.peak_month, v.pct_drop
    FROM valid v
    """,
    [selected_measure, org_type, 20, 10, 0.8, 0.4, 5]
).df()

filtered_orgs = filtered_orgs_df["org_id"].tolist()

# --- Deciles ---
deciles = conn.execute(
    """
    SELECT
        month,
        PERCENTILE_CONT(0.1) WITHIN GROUP (ORDER BY calc_value) AS d10,
        PERCENTILE_CONT(0.2) WITHIN GROUP (ORDER BY calc_value) AS d20,
        PERCENTILE_CONT(0.3) WITHIN GROUP (ORDER BY calc_value) AS d30,
        PERCENTILE_CONT(0.4) WITHIN GROUP (ORDER BY calc_value) AS d40,
        PERCENTILE_CONT(0.5) WITHIN GROUP (ORDER BY calc_value) AS d50,
        PERCENTILE_CONT(0.6) WITHIN GROUP (ORDER BY calc_value) AS d60,
        PERCENTILE_CONT(0.7) WITHIN GROUP (ORDER BY calc_value) AS d70,
        PERCENTILE_CONT(0.8) WITHIN GROUP (ORDER BY calc_value) AS d80,
        PERCENTILE_CONT(0.9) WITHIN GROUP (ORDER BY calc_value) AS d90
    FROM measures
    WHERE measure = ? AND org_type = ?
    GROUP BY month
    ORDER BY month
    """,
    [selected_measure, org_type]
).df()

# --- Raw data ---
data = conn.execute(
    "SELECT * FROM measures WHERE measure = ? AND org_type = ?",
    [selected_measure, org_type]
).df()


# --- Plot ---
if len(filtered_orgs) == 0:
    st.info("No organisations met the criteria for detecting substantial improvement on this measure.")
else:
    st.write(f"**{len(filtered_orgs)}** organisation(s) with improvement identified")

    fig = go.Figure()

    # Decile lines
    decile_cols = ["d10", "d20", "d30", "d40", "d50", "d60", "d70", "d80", "d90"]
    for col in decile_cols:
        is_median = col == "d50"
        fig.add_trace(go.Scatter(
            x=deciles["month"],
            y=deciles[col],
            showlegend=False,
            mode="lines",
            line=dict(
                color="blue",
                width=2 if is_median else 1.5,
                dash="solid" if is_median else "dot"
            ),
            name="Median" if is_median else col,
            opacity=0.6 if is_median else 0.3
        ))

    # Org lines
    for i, org_id in enumerate(filtered_orgs):
        org_name = conn.execute(
            "SELECT name FROM orgs WHERE org_type = ? AND code = ?",
            [org_type, org_id]
        ).fetchone()
        org_label = org_name[0] if org_name else org_id

        org_data = data[data["org_id"] == org_id].sort_values("month")
        fig.add_trace(go.Scatter(
            x=org_data["month"],
            y=org_data["calc_value"],
            mode="lines",
            line=dict(color=PLOT_COLOURS[i], width=2),
            name=org_label
        ))

    fig.update_layout(
        yaxis_title=y_label,
        yaxis_tickformat=".0%" if is_percentage else None,
        plot_bgcolor="white",
        height=500,
        margin=dict(l=40, r=20, t=20, b=40),
        legend=dict(font=dict(size=12))
    )
    fig.update_xaxes(showgrid=True, title="Month")
    fig.update_yaxes(showgrid=True)

    st.plotly_chart(fig, use_container_width=True)

    # Org table
    st.subheader("Identified organisations")
    table_rows = []
    for _, row in filtered_orgs_df.iterrows():
        org_name = conn.execute(
            "SELECT name FROM orgs WHERE org_type = ? AND code = ?",
            [org_type, row["org_id"]]
        ).fetchone()
        table_rows.append({
            "Name": org_name[0] if org_name else row["org_id"],
            "Improvement started": row["peak_month"].strftime("%b %Y") if pd.notna(row["peak_month"]) else "",
            "Percentile drop": f"{row['pct_drop']:.0%}",
        })
    st.dataframe(pd.DataFrame(table_rows), use_container_width=True, hide_index=True)



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

