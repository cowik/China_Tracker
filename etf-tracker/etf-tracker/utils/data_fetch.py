"""
Data fetching with persistent Google Sheets cache.
- Checks price_cache tab for existing data.
- Fetches only missing dates from BaoStock (stocks) or yfinance (ETFs).
- Auto-updates today's close after 15:20 Beijing time on trading days.
"""
from __future__ import annotations
import time
import random
import pandas as pd
import streamlit as st
import yfinance as yf
from datetime import datetime, timedelta
import pytz

from utils import sheets_db

# ---- Timezone utilities ----
BEIJING_TZ = pytz.timezone('Asia/Shanghai')


def _now_beijing() -> datetime:
    """Return current datetime in Beijing timezone."""
    return datetime.now(BEIJING_TZ)


def _is_trading_day(dt: datetime) -> bool:
    """Check if a given date (in Beijing time) is likely a trading day (Mon-Fri)."""
    # Simplified: we assume Mon-Fri are trading days (holidays are handled by empty data)
    return dt.weekday() < 5


def _should_fetch_today() -> bool:
    """Return True if we should attempt to fetch today's close (after 15:20 Beijing on a trading day)."""
    now = _now_beijing()
    if not _is_trading_day(now):
        return False
    # Market closes at 15:00; we give a 20-minute buffer
    return now.hour >= 15 and now.minute >= 20


# ---- Helper functions ----
def _clean_ticker(ticker: str) -> str:
    ticker = str(ticker).strip()
    for suffix in ['.SH', '.SZ', '.SS']:
        if ticker.upper().endswith(suffix):
            ticker = ticker[:-len(suffix)]
    return ticker.replace('.', '')


def _to_baostock_ticker(ticker: str) -> str:
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    if ticker[0] in ('5', '6'):
        return f"sh.{ticker}"
    else:
        return f"sz.{ticker}"


def _to_yfinance_ticker(ticker: str) -> str:
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    if ticker[0] in ('5', '6'):
        return f"{ticker}.SS"
    else:
        return f"{ticker}.SZ"


def _retry_download_baostock(ticker: str, start_date: str, end_date: str, adjustflag='2', retries=3):
    import baostock as bs
    for attempt in range(retries):
        try:
            lg = bs.login()
            if lg is None or lg.error_code != '0':
                time.sleep(2)
                continue
            rs = bs.query_history_k_data_plus(
                ticker,
                "date,close",
                start_date=start_date,
                end_date=end_date,
                frequency="d",
                adjustflag=adjustflag
            )
            if rs.error_code != '0':
                bs.logout()
                time.sleep(2)
                continue
            data_list = []
            while rs.next():
                data_list.append(rs.get_row_data())
            bs.logout()
            if not data_list:
                return pd.DataFrame()
            df = pd.DataFrame(data_list, columns=rs.fields)
            df['date'] = pd.to_datetime(df['date'])
            df['close'] = pd.to_numeric(df['close'], errors='coerce')
            df = df.dropna(subset=['close'])
            return df[['date', 'close']]
        except Exception:
            time.sleep(2 * (1 + random.random()))
    return pd.DataFrame()


def _retry_download_yfinance(ticker_yf: str, start: str, end: str, retries=3):
    for attempt in range(retries):
        try:
            df = yf.download(ticker_yf, start=start, end=end, progress=False, timeout=15)
            if df.empty:
                return pd.DataFrame()
            close = df['Close']
            out = close.reset_index()
            out.columns = ['date', 'close']
            out['date'] = pd.to_datetime(out['date'])
            return out
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 * (1 + random.random()))
            else:
                raise
    return pd.DataFrame()


# ---- Cache management ----
@st.cache_data(ttl=3600, show_spinner=False)
def _load_all_price_cache() -> pd.DataFrame:
    """Read the entire price_cache sheet once per hour."""
    return sheets_db.read_df("price_cache")


def _get_cached_series(ticker: str, asset_type: str) -> pd.Series:
    """Return cached close prices for the given ticker as a Series index by date."""
    df = _load_all_price_cache()
    if df.empty:
        return pd.Series(dtype=float)
    # Filter by ticker and asset_type
    mask = (df["ticker"].astype(str).str.strip() == ticker) & (df["asset_type"] == asset_type)
    cached = df[mask].copy()
    if cached.empty:
        return pd.Series(dtype=float)
    cached["date"] = pd.to_datetime(cached["date"])
    cached = cached.sort_values("date")
    return cached.set_index("date")["close"]


def _update_cache(ticker: str, asset_type: str, new_data: pd.DataFrame) -> None:
    """Append new_data to the price_cache sheet (only if non-empty)."""
    if new_data.empty:
        return
    new_data = new_data.copy()
    new_data["ticker"] = ticker
    new_data["asset_type"] = asset_type
    new_data["date"] = pd.to_datetime(new_data["date"]).dt.strftime("%Y-%m-%d")
    new_data["close"] = new_data["close"].astype(float)
    # Convert to list of dicts for append_rows
    rows = new_data[["ticker", "date", "close", "asset_type"]].to_dict(orient="records")
    sheets_db.append_rows("price_cache", rows)
    # Clear the cached read so next load picks up new data
    st.cache_data.clear()


def _fetch_missing_data(ticker: str, asset_type: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Fetch data for the given date range from the appropriate source."""
    if asset_type == 'stock':
        bs_ticker = _to_baostock_ticker(ticker)
        df = _retry_download_baostock(bs_ticker, start_date, end_date, adjustflag='2')
        if df.empty:
            # fallback to unadjusted
            df = _retry_download_baostock(bs_ticker, start_date, end_date, adjustflag='3')
        return df
    else:  # etf
        yf_ticker = _to_yfinance_ticker(ticker)
        try:
            return _retry_download_yfinance(yf_ticker, start_date, end_date)
        except Exception:
            return pd.DataFrame()


def get_price_series(ticker: str, asset_type: str = "stock", start_date: str = "1990-01-01") -> pd.Series:
    """
    Returns a date-indexed close price series, using cached data where possible.
    Fetches only missing dates and updates the cache.
    """
    ticker = _clean_ticker(ticker)
    # Load existing cached data
    cached_series = _get_cached_series(ticker, asset_type)

    # Determine the last cached date (if any)
    if not cached_series.empty:
        last_cached_date = cached_series.index.max()
        start_fetch = (last_cached_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start_fetch = start_date

    # Get today's date in Beijing
    today_beijing = _now_beijing().date()
    today_str = today_beijing.strftime("%Y-%m-%d")

    # If we need to fetch today's data (after market close), extend end_date to today
    if _should_fetch_today():
        end_fetch = today_str
    else:
        end_fetch = today_str  # we still fetch up to today, but if today's data not available, it will be empty

    # Only fetch if there is a range to fetch
    if start_fetch <= end_fetch:
        st.info(f"📡 Fetching missing data for {ticker} from {start_fetch} to {end_fetch}...")
        new_df = _fetch_missing_data(ticker, asset_type, start_fetch, end_fetch)
        if not new_df.empty:
            # Remove any rows that are already in cache (duplicates protection)
            if not cached_series.empty:
                new_df = new_df[~new_df["date"].isin(cached_series.index)]
            if not new_df.empty:
                _update_cache(ticker, asset_type, new_df)
                # Reload cache to include new data
                st.cache_data.clear()
                cached_series = _get_cached_series(ticker, asset_type)

    # Now return the full series (cached + newly fetched)
    if cached_series.empty:
        return pd.Series(dtype=float)
    return cached_series.sort_index()


# ---- Backward compatibility ----
def get_stock_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    s = get_price_series(ticker, asset_type="stock", start_date=start_date)
    if s.empty:
        return pd.DataFrame()
    return s.reset_index().rename(columns={"index": "date"})


def get_etf_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    s = get_price_series(ticker, asset_type="etf", start_date=start_date)
    if s.empty:
        return pd.DataFrame()
    return s.reset_index().rename(columns={"index": "date"})


def get_dividends(ticker: str, asset_type: str) -> pd.DataFrame:
    # (unchanged – optional)
    return pd.DataFrame()
