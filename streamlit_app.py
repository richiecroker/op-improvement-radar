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
st.image(os.path.join(base_dir, "content", "OpenPrescribing.svg"))
st.info(
    """##### Hello!  This is a **very** early prototype of something.  
Please let us know what you think, and what you'd like to see.  Email us at [bennett@phc.ox.ac.uk](mailto:bennett@phc.ox.ac.uk)"""
)

# Secrets
# ----------------------------
github_token = st.secrets.get("github_token")

# ----------------------------
# Fetch measures from GitHub
# ----------------------------
headers = {"Authorization": f"token {github_token}"}
repo_url = (
    "https://api.github.com/repos/"
    "ebmdatalab/openprescribing/contents/"
    "openprescribing/measures/definitions"
)

res = requests.get(repo_url, headers=headers, timeout=15)
if res.status_code != 200:
    st.error("Failed to fetch measure definitions")
    st.stop()

rows = []
for item in res.json():
    if not item.get("name", "").endswith(".json"):
        continue

    github_url = item.get("html_url")
    measure_id = measure_id_from_github_url(github_url)

    try:
        data = requests.get(item["download_url"], timeout=10).json()
    except Exception:
        continue

    authored_by = data.get("authored_by", "")
    if isinstance(authored_by, list):
        authored_by = authored_by[0] if authored_by else ""

    checked_by = data.get("checked_by", "")
    if isinstance(checked_by, list):
        checked_by = checked_by[0] if checked_by else ""

    next_review = data.get("next_review")
    if isinstance(next_review, list):
        next_review = next_review[0]
    if isinstance(next_review, str):
        try:
            next_review = datetime.strptime(next_review, "%Y-%m-%d").date()
        except Exception:
            next_review = None

    rows.append({
        "measure_name": data.get("name", measure_id),
        "measure_id": measure_id,
        "github_url": github_url,
        "authored_by": email_to_name(authored_by),
        "checked_by": email_to_name(checked_by),
        "next_review": next_review,
        "next_review_months": review_months(next_review),
    })

df = pd.DataFrame(rows)

st.dataframe(df)



# ── Information ─────────────────────────────────────────────────────────────────

st.divider()

with st.expander("Click here to read our methodology", icon=":material/quick_reference:"):
    with open(os.path.join(base_dir, "content", "methodology.md")) as f:
        st.markdown(f.read())

with open(os.path.join(base_dir, "content", "changelog.yaml")) as f:
    changelog = yaml.safe_load(f)

with st.expander("Click to see changelog", icon=":material/history:"):
    for entry in reversed(changelog):
        st.markdown(f"**{entry['date']}** — {entry['change']} *({entry['person']})*")

