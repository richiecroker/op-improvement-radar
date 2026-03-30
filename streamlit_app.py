import streamlit as st
import pandas as pd
import yaml
import os
import requests

from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from urllib.parse import urlparse

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

# Secrets
# ----------------------------
github_token = st.secrets.get("github_token")


def measure_id_from_github_url(url):
    if not url:
        return None
    try:
        return os.path.splitext(os.path.basename(urlparse(url).path))[0]
    except Exception:
        return None

# Fetch measure definitions from GitHub
REPO_URL = (
    "https://api.github.com/repos/"
    "ebmdatalab/openprescribing/contents/"
    "openprescribing/measures/definitions"
)
headers = {"Authorization": f"token {github_token}"}

res = requests.get(REPO_URL, headers=headers, timeout=15)
if res.status_code != 200:
    st.error("Failed to fetch measure definitions")
    st.stop()

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

measures_df = pd.DataFrame(rows)
st.dataframe(measures_df)



PREFIXES = ["ccg", "pcn", "stp"]

bq = _bq_client()
existing_tables = {table.table_id for table in bq.list_tables("ebmdatalab.measures")}

base_cols = ["month", "numerator", "denominator", "percentile"]

id = {
    "ccg": "pct_id",
    "pcn": "pcn_id",
    "stp": "stp_id",
}

parts = []

for _, row in measures_df.iterrows():
    measure_id = row["measure_id"]
    if not measure_id:
        continue

    for prefix in PREFIXES:
        table_name = f"{prefix}_data_{measure_id}"
        if table_name not in existing_tables:
            continue

        source_col = id[prefix]

        select_sql = ", ".join(
            base_cols + [
                f"{id} AS org_id",
                f"'{prefix}' AS org_type",
                f"'{measure_id}' AS measure",
            ]
        )

        from_sql = f"`ebmdatalab.measures.{table_name}`"

        parts.append(f"SELECT {select_sql} FROM {from_sql}")

sql = "\nUNION ALL\n".join(parts)
df = bq.query(sql).result().to_dataframe()


# ── Information ─────────────────────────────────────────────────────────────────

st.divider()

#with st.expander("Click here to read our methodology", icon=":material/quick_reference:"):
#    with open(os.path.join(base_dir, "content", "methodology.md")) as f:
#        st.markdown(f.read())

#with open(os.path.join(base_dir, "content", "changelog.yaml")) as f:
#    changelog = yaml.safe_load(f)

#with st.expander("Click to see changelog", icon=":material/history:"):
 #   for entry in reversed(changelog):
 #       st.markdown(f"**{entry['date']}** — {entry['change']} *({entry['person']})*")

