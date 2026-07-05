"""
Data fetching with persistent cache + optional force refresh.
"""
from __future__ import annotations
import time
import random
from datetime import datetime, timedelta
from typing import Dict, List

import pandas as pd
import streamlit as st
import yfinance as yf
import pytz

from utils import sheets_db

BEIJING_TZ = pytz.timezone("Asia/Shanghai")


# ---------------------------------------------------------------- helpers --
def _now_beijing() -> datetime:
    return datetime.now(BEIJING_TZ)


def _is_trading_day(dt: datetime) -> bool:
    return dt.weekday() < 5


def _clean_ticker(ticker: str) -> str:
    ticker = str(ticker).strip()
    for suffix in (".SH", ".SZ", ".SS"):
        if ticker.upper().endswith(suffix):
            ticker = ticker[: -len(suffix)]
    return ticker.replace(".", "")


def _to_baostock_ticker(ticker: str) -> str:
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    return f"sh.{ticker}" if ticker[0] in ("5", "6") else f"sz.{ticker}"


def _to_yfinance_ticker(ticker: str) -> str:
    ticker = _clean_ticker(ticker)
    if not ticker:
        return ticker
    return f"{ticker}.SS" if ticker[0] in ("5", "6") else f"{ticker}.SZ"


def _get_last_trading_day() -> str:
    """Return the most recent actual trading day."""
    try:
        import baostock as bs
        today = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")
        start = (datetime.now(BEIJING_TZ) - timedelta(days=10)).strftime("%Y-%m-%d")
        lg = bs.login()
        if lg is not None and lg.error_code == '0':
            rs = bs.query_trade_dates(start_date=start, end_date=today)
            if rs.error_code == '0':
                trading_days = []
                while rs.next():
                    row = rs.get_row_data()
                    if row and len(row) > 0:
                        trading_days.append(row[0])
                bs.logout()
                if trading_days:
                    return max(trading_days)
    except Exception:
        pass
    dt = datetime.now(BEIJING_TZ)
    while dt.weekday() > 4:
        dt -= timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


# ------------------------------------------------------------ raw fetchers --
def _retry_download_baostock(ticker: str, start_date: str, end_date: str, adjustflag="2", retries=3) -> pd.DataFrame:
    import baostock as bs
    for attempt in range(retries):
        try:
            lg = bs.login()
            if lg is None or lg.error_code != "0":
                time.sleep(2)
                continue
            rs = bs.query_history_k_data_plus(
                ticker, "date,close",
                start_date=start_date, end_date=end_date,
                frequency="d", adjustflag=adjustflag,
            )
            if rs.error_code != "0":
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
            df["date"] = pd.to_datetime(df["date"])
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            return df.dropna(subset=["close"])[["date", "close"]]
        except Exception:
            time.sleep(2 * (1 + random.random()))
    return pd.DataFrame()


def _retry_download_yfinance(ticker_yf: str, start: str, end: str, retries=3) -> pd.DataFrame:
    for attempt in range(retries):
        try:
            df = yf.download(
                ticker_yf, start=start, end=end,
                progress=False, timeout=15,
                auto_adjust=True,
            )
            if df.empty:
                return pd.DataFrame()
            close = df["Adj Close"] if "Adj Close" in df.columns else df["Close"]
            out = close.reset_index()
            out.columns = ["date", "close"]
            out["date"] = pd.to_datetime(out["date"])
            return out
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 * (1 + random.random()))
            else:
                return pd.DataFrame()
    return pd.DataFrame()


def _fetch_missing_data(ticker: str, asset_type: str, start_date: str, end_date: str) -> pd.DataFrame:
    if asset_type == "stock":
        bs_ticker = _to_baostock_ticker(ticker)
        df = _retry_download_baostock(bs_ticker, start_date, end_date, adjustflag="2")
        if df.empty:
            df = _retry_download_baostock(bs_ticker, start_date, end_date, adjustflag="3")
        if df.empty:
            yf_ticker = _to_yfinance_ticker(ticker)
            df = _retry_download_yfinance(yf_ticker, start_date, end_date)
        return df
    else:
        yf_ticker = _to_yfinance_ticker(ticker)
        return _retry_download_yfinance(yf_ticker, start_date, end_date)


# --------------------------------------------------------- sheet-backed cache --
@st.cache_data(ttl=3600, show_spinner=False)
def _load_all_price_cache() -> pd.DataFrame:
    return sheets_db.read_df("price_cache")


def _clear_price_cache_memory() -> None:
    _load_all_price_cache.clear()


def _get_cached_series(ticker: str, asset_type: str) -> pd.Series:
    df = _load_all_price_cache()
    if df.empty:
        return pd.Series(dtype=float)
    required = ["ticker", "asset_type", "date", "close"]
    if not all(c in df.columns for c in required):
        return pd.Series(dtype=float)
    mask = (df["ticker"].astype(str).str.strip() == ticker) & (df["asset_type"] == asset_type)
    cached = df[mask].copy()
    if cached.empty:
        return pd.Series(dtype=float)
    cached["date"] = pd.to_datetime(cached["date"])
    return cached.sort_values("date").set_index("date")["close"]


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
    _clear_price_cache_memory()


# --------------------------------------------------------------- batch watchlist --
def get_watchlist_prices(watchlist_df: pd.DataFrame) -> Dict[str, pd.Series]:
    if watchlist_df.empty:
        return {}
    tickers = []
    labels = []
    for _, row in watchlist_df.iterrows():
        ticker = str(row["ticker"]).strip()
        if not ticker:
            continue
        name = str(row.get("name", "")).strip() or ticker
        yf_ticker = _to_yfinance_ticker(ticker)
        tickers.append(yf_ticker)
        labels.append(f"{name} ({ticker})")
    if not tickers:
        return {}
    end_date = _get_last_trading_day()
    start_date = (datetime.now(BEIJING_TZ) - timedelta(days=3650)).strftime("%Y-%m-%d")
    try:
        data = yf.download(
            tickers, start=start_date, end=end_date,
            progress=False, timeout=30, group_by='ticker',
            auto_adjust=True,
        )
        if data.empty:
            return {}
        result = {}
        for i, yf_t in enumerate(tickers):
            label = labels[i]
            if yf_t not in data.columns.levels[0]:
                continue
            sub = data[yf_t]
            if 'Adj Close' in sub.columns:
                close = sub['Adj Close']
            elif 'Close' in sub.columns:
                close = sub['Close']
            else:
                continue
            close = close.dropna()
            if not close.empty:
                result[label] = close / close.iloc[0]
        return result
    except Exception as e:
        st.warning(f"Batch yfinance fetch failed: {e}")
        return {}


# ------------------------------------------------------------------ public --
@st.cache_data(ttl=900, show_spinner=False)
def get_price_series(ticker: str, asset_type: str = "stock", start_date: str = "1990-01-01", force_refresh: bool = False) -> pd.Series:
    """
    Date-indexed close price series.
    If force_refresh=True, bypass cache and fetch directly from source.
    """
    ticker = _clean_ticker(ticker)

    if force_refresh:
        # Direct fetch without cache
        last_trading = _get_last_trading_day()
        df = _fetch_missing_data(ticker, asset_type, start_date, last_trading)
        if df.empty:
            return pd.Series(dtype=float)
        return df.set_index("date")["close"].sort_index()

    # Normal cached path
    cached_series = _get_cached_series(ticker, asset_type)

    if not cached_series.empty:
        start_fetch = (cached_series.index.max() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start_fetch = start_date

    end_fetch = _get_last_trading_day()

    if start_fetch <= end_fetch:
        new_df = _fetch_missing_data(ticker, asset_type, start_fetch, end_fetch)
        if not new_df.empty:
            if not cached_series.empty:
                new_df = new_df[~new_df["date"].isin(cached_series.index)]
            if not new_df.empty:
                _update_cache(ticker, asset_type, new_df)
                cached_series = _get_cached_series(ticker, asset_type)

    if cached_series.empty:
        return pd.Series(dtype=float)
    return cached_series.sort_index()


# ---- backward compatibility ----
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
