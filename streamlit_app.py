import streamlit as st
import pandas as pd
import yaml
import os
import requests

from datetime import datetime, date
from dateutil.relativedelta import relativedelta
from urllib.parse import urlparse




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

df = pd.DataFrame(rows)
st.dataframe(df)



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

