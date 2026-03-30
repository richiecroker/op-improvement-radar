import streamlit as st
import pandas as pd
from datetime import datetime
import yaml
import os

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

