import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import date, timedelta

from utils import sheets_db, data_fetch, returns

# ----- Page config with collapsed sidebar -----
st.set_page_config(
    page_title="China Portfolio & ETF Tracker",
    layout="wide",
    initial_sidebar_state="collapsed",
)

# ----- Custom CSS -----
st.markdown(
    """
    <style>
        .block-container {
            padding-top: 2.5rem !important;
            padding-bottom: 5rem !important;
        }
        h1 {
            margin-top: 0rem !important;
            margin-bottom: 0.25rem !important;
        }
        .stCaption {
            margin-top: -0.25rem !important;
            margin-bottom: 0.5rem !important;
        }
        h2, h3 {
            margin-top: 0.5rem !important;
            margin-bottom: 0.25rem !important;
        }
        footer {
            margin-bottom: 2rem;
        }
        
        /* --- MOBILE TABLE FIXES --- */
        /* Force table to fit exactly within container width, NO scrollbar */
        [data-testid="stDataFrame"] {
            max-width: 100% !important;
            overflow: hidden !important; 
        }
        /* Force long Russian portfolio names to wrap to the next line */
        [data-testid="stDataFrame"] th, 
        [data-testid="stDataFrame"] td {
            word-break: break-word !important;
            white-space: normal !important;
            min-width: 40px !important; /* Keeps columns narrow enough to fit */
        }
        /* Reduce side padding on mobile screens to maximize width */
        @media (max-width: 768px) {
            .block-container {
                padding-left: 1rem !important;
                padding-right: 1rem !important;
            }
        }
    </style>
    """,
    unsafe_allow_html=True,
)

st.title("📈 China Portfolio & ETF Tracker")
st.caption(
    "All performance figures are **total return** (price change + dividends "
    "reinvested), not just price change."
)

# Fetch portfolios dynamically from Google Sheets
PORTFOLIO_LABELS = sheets_db.get
