"""
Data fetching with persistent Google Sheets cache and trading day filtering.
- Uses baostock.query_trade_dates() for accurate trading day calendar
- Stocks: cache last 3 years only
- ETFs: cache full history
- Filters out non-trading days before fetching
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

BEIJING_TZ = pytz.timezone('Asia/Shanghai')

# Cache retention policy
STOCK_CACHE_YEARS = 3   # Stocks: keep last 3 years
ETF_CACHE_FULL = True   # ETFs: keep full history


def _get_trading_days(start_date: str, end_date: str) -> list:
    """
    Get list of trading days from baostock between start_date and end_date.
    Returns list of date strings in 'YYYY-MM-DD' format.
    """
    import baostock as bs
    try:
        lg = bs.login()
        if lg is None or lg.error_code != '0':
            return []

        rs = bs.query_trade_dates(start_date=start_date, end_date=end_date)
        if rs.error_code != '0':
            bs.logout()
            return []

        trading_days = []
        while rs.next():
            row = rs.get_row_data()
            if row and len(row) > 0:
                trading_days.append(row[0])
        bs.logout()
        return trading_days
    except Exception:
        return []


def _is_trading_day(date_str: str) -> bool:
    trading_days = _get_trading_days(date_str, date_str)
    return len(trading_days) > 0


def _get_last_trading_day(date_str: str) -> str:
    target = datetime.strptime(date_str, "%Y-%m-%d")
    for i in range(7):
        check = (target - timedelta(days=i)).strftime("%Y-%m-%d")
        if _is_trading_day(check):
            return check
    return date_str  # fallback


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


@st.cache_data(ttl=3600, show_spinner=False)
def _load_all_price_cache() -> pd.DataFrame:
    return sheets_db.read_df("price_cache")


def _get_cached_series(ticker: str, asset_type: str) -> pd.Series:
    df = _load_all_price_cache()
    if df.empty:
        return pd.Series(dtype=float)
    mask = (df["ticker"].astype(str).str.strip() == ticker) & (df["asset_type"] == asset_type)
    cached = df[mask].copy()
    if cached.empty:
        return pd.Series(dtype=float)
    cached["date"] = pd.to_datetime(cached["date"])
    cached = cached.sort_values("date")
    return cached.set_index("date")["close"]


def _update_cache(ticker: str, asset_type: str, new_data: pd.DataFrame) -> None:
    if new_data.empty:
        return
    new_data = new_data.copy()
    new_data["ticker"] = ticker
    new_data["asset_type"] = asset_type
    new_data["date"] = pd.to_datetime(new_data["date"]).dt.strftime("%Y-%m-%d")
    new_data["close"] = new_data["close"].astype(float)
    rows = new_data[["ticker", "date", "close", "asset_type"]].to_dict(orient="records")
    sheets_db.append_rows("price_cache", rows)
    st.cache_data.clear()


def _filter_trading_days(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    dates = df['date'].dt.strftime('%Y-%m-%d').unique().tolist()
    if not dates:
        return df
    trading_days = _get_trading_days(min(dates), max(dates))
    if not trading_days:
        # Fallback: weekends filtering
        df['is_weekend'] = df['date'].dt.weekday >= 5
        return df[~df['is_weekend']].copy().drop(columns=['is_weekend'])
    trading_set = set(trading_days)
    df['date_str'] = df['date'].dt.strftime('%Y-%m-%d')
    filtered = df[df['date_str'].isin(trading_set)].copy()
    return filtered.drop(columns=['date_str'])


def _trim_cache_to_policy(ticker: str, asset_type: str) -> None:
    df = _load_all_price_cache()
    if df.empty:
        return
    mask = (df["ticker"].astype(str).str.strip() == ticker) & (df["asset_type"] == asset_type)
    if not mask.any():
        return
    df_ticker = df[mask].copy()
    if df_ticker.empty:
        return
    df_ticker["date_dt"] = pd.to_datetime(df_ticker["date"])
    if asset_type == 'stock':
        cutoff = datetime.now() - timedelta(days=STOCK_CACHE_YEARS * 365)
        df_ticker = df_ticker[df_ticker["date_dt"] >= cutoff]
    # ETFs: keep all
    df = df[~((df["ticker"].astype(str).str.strip() == ticker) & (df["asset_type"] == asset_type))]
    if not df_ticker.empty:
        df_ticker = df_ticker.drop(columns=["date_dt"])
        df = pd.concat([df, df_ticker], ignore_index=True)
    sheets_db.write_df("price_cache", df)
    st.cache_data.clear()


def _fetch_missing_data(ticker: str, asset_type: str, start_date: str, end_date: str) -> pd.DataFrame:
    if asset_type == 'stock':
        bs_ticker = _to_baostock_ticker(ticker)
        df = _retry_download_baostock(bs_ticker, start_date, end_date, adjustflag='2')
        if df.empty:
            df = _retry_download_baostock(bs_ticker, start_date, end_date, adjustflag='3')
        return df
    else:  # etf
        yf_ticker = _to_yfinance_ticker(ticker)
        try:
            return _retry_download_yfinance(yf_ticker, start_date, end_date)
        except Exception:
            return pd.DataFrame()


def get_price_series(ticker: str, asset_type: str = "stock", start_date: str = "1990-01-01") -> pd.Series:
    ticker = _clean_ticker(ticker)
    cached_series = _get_cached_series(ticker, asset_type)

    # Determine last cached date
    if not cached_series.empty:
        last_cached_date = cached_series.index.max()
        start_fetch = (last_cached_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start_fetch = start_date

    today_beijing = datetime.now(BEIJING_TZ).date()
    today_str = today_beijing.strftime("%Y-%m-%d")

    if start_fetch <= today_str:
        last_trading = _get_last_trading_day(today_str)
        if start_fetch <= last_trading:
            st.info(f"📡 Fetching missing data for {ticker} from {start_fetch} to {last_trading}...")
            new_df = _fetch_missing_data(ticker, asset_type, start_fetch, last_trading)
            if not new_df.empty:
                new_df = _filter_trading_days(new_df)
                if not cached_series.empty:
                    new_df = new_df[~new_df["date"].isin(cached_series.index)]
                if not new_df.empty:
                    _update_cache(ticker, asset_type, new_df)
                    st.cache_data.clear()
                    cached_series = _get_cached_series(ticker, asset_type)

    # Apply retention policy
    _trim_cache_to_policy(ticker, asset_type)
    st.cache_data.clear()
    cached_series = _get_cached_series(ticker, asset_type)

    if cached_series.empty:
        return pd.Series(dtype=float)
    return cached_series.sort_index()


# Backward compatibility
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
    return pd.DataFrame()
