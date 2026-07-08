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
        
        /* --- DATAFRAME NATIVE FIXES --- */
        /* Left-align the first column (Portfolio names) */
        [data-testid="stDataFrame"] th:first-child,
        [data-testid="stDataFrame"] td:first-child {
            text-align: left !important;
        }
        /* Center the numeric columns */
        [data-testid="stDataFrame"] th:not(:first-child),
        [data-testid="stDataFrame"] td:not(:first-child) {
            text-align: center !important;
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

PORTFOLIO_LABELS = sheets_db.get_portfolios()

def load_holdings(tab_name: str) -> list[dict]:
    df = sheets_db.read_df(tab_name)
    holdings = []
    for _, row in df.iterrows():
        try:
            holdings.append({
                "ticker": str(row["ticker"]).strip(),
                "asset_type": str(row.get("asset_type", "stock")).strip().lower() or "stock",
                "weight": float(row["weight"]) / 100.0,
                "inception_date": pd.to_datetime(row["purchase_date"]),
            })
        except (KeyError, ValueError, TypeError):
            continue
    return holdings

def load_backtest(portfolio_label: str) -> pd.Series:
    df = sheets_db.read_df("backtest_history")
    if df.empty:
        return pd.Series(dtype=float)
    df = df[df["portfolio"] == portfolio_label].copy()
    if df.empty:
        return pd.Series(dtype=float)
    df["date"] = pd.to_datetime(df["date"])
    df = df.sort_values("date")
    return pd.Series(pd.to_numeric(df["index_value"], errors="coerce").values, index=df["date"])

@st.cache_data(ttl=300, show_spinner=False)
def compute_portfolio_index(tab_name: str, portfolio_label: str, holdings: list[dict]) -> pd.Series:
    price_data = data_fetch.get_prices_batch(holdings)
    backtest_index_values = load_backtest(portfolio_label)
    rebalance_freq = sheets_db.get_rebalance_frequency(portfolio_label)
    live_start_date = backtest_index_values.index[-1] if not backtest_index_values.empty else None

    live_index = returns.compute_live_index(
        holdings, price_data,
        rebalance_frequency=rebalance_freq,
        live_start_date=live_start_date,
    )

    if live_index.empty and holdings:
        live_index = returns.compute_live_index(
            holdings, price_data,
            rebalance_frequency=rebalance_freq,
            live_start_date=None,
        )
        if not live_index.empty and live_start_date is not None:
            live_index = live_index[live_index.index >= live_start_date]

    return returns.chain_link_backtest(backtest_index_values, live_index)

def load_watchlist() -> pd.DataFrame:
    return sheets_db.read_df("watchlist_etfs")

with st.spinner("Loading your data..."):
    backtest_df = sheets_db.read_df("backtest_history")
    series_options = {}

    for tab_name, label in PORTFOLIO_LABELS.items():
        holdings = load_holdings(tab_name)
        if holdings or not backtest_df[backtest_df["portfolio"] == label].empty:
            series_options[label] = compute_portfolio_index(tab_name, label, holdings)

    watchlist_df = load_watchlist()
    if not watchlist_df.empty:
        watchlist_prices = data_fetch.get_watchlist_prices(watchlist_df)
        series_options.update(watchlist_prices)

    # Sort series_options based on saved display order
    order_map = sheets_db.get_display_order()
    sorted_keys = sorted(series_options.keys(), key=lambda x: order_map.get(x, 9999))
    series_options = {k: series_options[k] for k in sorted_keys}

if not series_options:
    st.info(
        "No portfolios or watchlist ETFs set up yet. Go to the **Manage** page "
        "(left sidebar) to add your positions and ETFs."
    )
    st.stop()

@st.fragment(run_every=timedelta(minutes=5))
def render_dashboard(series_options: dict):
    st.subheader("Performance chart")
    
    col1, col2 = st.columns([3, 2])
    with col1:
        choice = st.selectbox("Choose what to chart:", list(series_options.keys()))
    with col2:
        period = st.radio(
            "Period",
            options=["5D", "1M", "3M", "6M", "YTD", "1Y", "3Y", "5Y", "Max"],
            index=8,
            horizontal=True,
            key="period_selector"
        )

    chart_series = series_options.get(choice)
    
    if chart_series is None or chart_series.dropna().empty:
        st.warning("No price data available yet for this selection.")
    else:
        chart_series = chart_series.dropna()
        last_date = chart_series.index.max()
        
        if period == "5D":
            start_date = last_date - pd.Timedelta(days=5)
        elif period == "1M":
            start_date = last_date - pd.DateOffset(months=1)
        elif period == "3M":
            start_date = last_date - pd.DateOffset(months=3)
        elif period == "6M":
            start_date = last_date - pd.DateOffset(months=6)
        elif period == "YTD":
            start_date = pd.Timestamp(year=last_date.year, month=1, day=1)
        elif period == "1Y":
            start_date = last_date - pd.DateOffset(years=1)
        elif period == "3Y":
            start_date = last_date - pd.DateOffset(years=3)
        elif period == "5Y":
            start_date = last_date - pd.DateOffset(years=5)
        else:
            start_date = chart_series.index.min()

        view_series = chart_series[chart_series.index >= start_date]

        if len(view_series) < 2:
            st.warning(f"Not enough data to display for the selected {period} period.")
        else:
            rebased_series = (view_series / view_series.iloc[0] - 1) * 100

            fig = go.Figure()
            fig.add_trace(go.Scatter(
                x=rebased_series.index, 
                y=rebased_series.values,
                mode="lines", 
                name=choice, 
                line=dict(width=2),
            ))
            fig.update_layout(
                yaxis_title="Total return (%)",
                margin=dict(l=10, r=10, t=30, b=10),
                height=450,
                dragmode=False,
                hovermode="x",
            )
            
            fig.update_xaxes(showspikes=True, spikethickness=1, spikecolor="gray", spikemode="across")
            fig.update_yaxes(showspikes=True, spikethickness=1, spikecolor="gray", spikemode="across")
            
            plotly_config = {
                'displayModeBar': False,
                'displaylogo': False,
                'scrollZoom': False,     
                'doubleClick': 'reset',  
            }
            
            st.plotly_chart(fig, use_container_width=True, config=plotly_config)

    st.subheader("Comparison table")
    rows = []
    for label, s in series_options.items():
        if s.dropna().empty:
            continue
        last_trading_day = s.dropna().index.max() 
        row = returns.comparison_row(s, last_trading_day)
        row["Name"] = label
        rows.append(row)

    if rows:
        table_df = pd.DataFrame(rows).set_index("Name")[["1D", "1W", "1M", "3M", "6M", "1Y"]]
        table_df.index.name = None  # Removes the "Name" header above the portfolio names

        def color_pct(v):
            if pd.isna(v):
                return ""
            return f"color: {'#0a7a2f' if v >= 0 else '#c02020'}"

        styled = table_df.style.format("{:+.2f}%", na_rep="—").map(color_pct)
        st.dataframe(styled, use_container_width=True)
    else:
        st.info("Not enough data yet to build the comparison table.")

render_dashboard(series_options)
