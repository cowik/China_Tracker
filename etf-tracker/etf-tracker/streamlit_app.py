import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from datetime import date, timedelta

from utils import sheets_db, data_fetch, returns

st.set_page_config(page_title="My Portfolio & ETF Tracker", layout="wide")
st.title("📈 My Portfolio & ETF Tracker")
st.caption(
    "All performance figures are **total return** (price change + dividends "
    "reinvested), not just price change."
)

# ----- Russian portfolio labels -----
PORTFOLIO_LABELS = {
    "portfolio1_positions": "Возможности Китая",
    "portfolio2_positions": "Возможности Китая. Специальная 2",
}


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


@st.cache_data(ttl=600, show_spinner=False)
def compute_portfolio_index(tab_name: str, portfolio_label: str, holdings: list[dict]) -> pd.Series:
    price_data = {}
    for h in holdings:
        # Fetch only from inception date (saves massive fetching time)
        price_data[h["ticker"]] = data_fetch.get_price_series(
            h["ticker"], h["asset_type"],
            start_date=h["inception_date"].strftime("%Y-%m-%d")
        )

    backtest_index_values = load_backtest(portfolio_label)
    rebalance_freq = sheets_db.get_rebalance_frequency(portfolio_label)
    live_start_date = backtest_index_values.index[-1] if not backtest_index_values.empty else None

    live_index = returns.compute_live_index(
        holdings, price_data,
        rebalance_frequency=rebalance_freq,
        live_start_date=live_start_date,
    )
    return returns.chain_link_backtest(backtest_index_values, live_index)


def load_watchlist() -> pd.DataFrame:
    return sheets_db.read_df("watchlist_etfs")


with st.spinner("Loading your data..."):
    p1_holdings = load_holdings("portfolio1_positions")
    p2_holdings = load_holdings("portfolio2_positions")
    watchlist_df = load_watchlist()

    backtest_df = sheets_db.read_df("backtest_history")

    series_options = {}

    # Portfolio 1
    if p1_holdings or not backtest_df[backtest_df["portfolio"] == PORTFOLIO_LABELS["portfolio1_positions"]].empty:
        series_options[PORTFOLIO_LABELS["portfolio1_positions"]] = compute_portfolio_index(
            "portfolio1_positions", PORTFOLIO_LABELS["portfolio1_positions"], p1_holdings
        )

    # Portfolio 2
    if p2_holdings or not backtest_df[backtest_df["portfolio"] == PORTFOLIO_LABELS["portfolio2_positions"]].empty:
        series_options[PORTFOLIO_LABELS["portfolio2_positions"]] = compute_portfolio_index(
            "portfolio2_positions", PORTFOLIO_LABELS["portfolio2_positions"], p2_holdings
        )

    # Watchlist ETFs (batch fetch)
    if not watchlist_df.empty:
        watchlist_prices = data_fetch.get_watchlist_prices(watchlist_df)
        series_options.update(watchlist_prices)

if not series_options:
    st.info(
        "No portfolios or watchlist ETFs set up yet. Go to the **Manage** page "
        "(left sidebar) to add your positions and ETFs."
    )
    st.stop()

# --- Chart ---
st.subheader("Performance chart")

# Choose series
choice = st.selectbox("Choose what to chart:", list(series_options.keys()))
chart_series = series_options[choice].dropna()

if chart_series.empty:
    st.warning("No price data available yet for this selection.")
else:
    # ---- Period selector (re-base chart) ----
    period_options = ["5D", "1M", "3M", "6M", "YTD", "1Y", "3Y", "5Y", "Max"]
    # Map period to number of days (YTD and Max are special)
    period_map = {
        "5D": 5,
        "1M": 30,
        "3M": 91,
        "6M": 182,
        "YTD": None,  # special: start of year
        "1Y": 365,
        "3Y": 1095,
        "5Y": 1825,
        "Max": None,
    }
    default_index = period_options.index("5Y")  # default to 5Y
    selected_period = st.selectbox("Chart period", period_options, index=default_index)

    # Compute start date for the selected period
    today = pd.Timestamp(date.today())
    if selected_period == "YTD":
        start_date = pd.Timestamp(year=today.year, month=1, day=1)
    elif selected_period == "Max":
        start_date = chart_series.index.min()
    else:
        days = period_map[selected_period]
        start_date = today - pd.Timedelta(days=days)

    # Filter series to the selected period
    filtered_series = chart_series[chart_series.index >= start_date]
    if filtered_series.empty:
        st.warning("Not enough data for this period.")
        st.stop()

    # Normalize to 0% at the start of the period
    start_value = filtered_series.iloc[0]
    normalized_series = (filtered_series / start_value - 1) * 100

    # Create figure
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=normalized_series.index,
        y=normalized_series,
        mode="lines",
        name=choice,
        line=dict(width=2),
    ))
    fig.update_layout(
        yaxis_title=f"Return (%) from {selected_period} start",
        margin=dict(l=10, r=10, t=30, b=10),
        height=450,
        hovermode="x",
    )
    # Set x-axis range to the selected period (so the chart doesn't show earlier data)
    fig.update_xaxes(
        range=[start_date, today],
        rangeselector=dict(
            buttons=[
                dict(count=5, label="5D", step="day", stepmode="backward"),
                dict(count=1, label="1M", step="month", stepmode="backward"),
                dict(count=3, label="3M", step="month", stepmode="backward"),
                dict(count=6, label="6M", step="month", stepmode="backward"),
                dict(step="year", stepmode="todate", label="YTD"),
                dict(count=1, label="1Y", step="year", stepmode="backward"),
                dict(count=3, label="3Y", step="year", stepmode="backward"),
                dict(count=5, label="5Y", step="year", stepmode="backward"),
                dict(step="all", label="Max"),
            ]
        ),
        rangeslider=dict(visible=False),
    )

    st.plotly_chart(fig, use_container_width=True)

# --- Comparison table ---
st.subheader("Comparison table")
today = pd.Timestamp(date.today())
rows = []
for label, s in series_options.items():
    if s.dropna().empty:
        continue
    row = returns.comparison_row(s, today)
    row["Name"] = label
    rows.append(row)

if rows:
    table_df = pd.DataFrame(rows).set_index("Name")[["1D", "1W", "1M", "3M", "6M", "1Y"]]

    def color_pct(v):
        if pd.isna(v):
            return ""
        return f"color: {'#0a7a2f' if v >= 0 else '#c02020'}"

    styled = table_df.style.format("{:+.2f}%", na_rep="—").map(color_pct)
    st.dataframe(styled, use_container_width=True)
else:
    st.info("Not enough data yet to build the comparison table.")
