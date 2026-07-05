"""
Data fetching with persistent Google Sheets cache.

- Stocks: BaoStock (adjustflag='2') -> BaoStock unadjusted (adjustflag='3')
  -> yfinance, in that order, first non-empty result wins.
- ETFs: yfinance only.
- Historical data is cached in the `price_cache` Google Sheet tab, so a
  cold start (app restart / redeploy / waking from Streamlit Cloud sleep)
  doesn't have to re-download full history for every ticker. After the
  first full fetch, only dates missing from the cache are ever fetched.
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
    # Simplified: Mon-Fri. Public holidays just come back empty from the
    # data source, so they don't need special-casing here.
    return dt.weekday() < 5


def _should_fetch_today() -> bool:
    """True once today's close should exist: a trading day, 15:20+ Beijing."""
    now = _now_beijing()
    if not _is_trading_day(now):
        return False
    return (now.hour > 15) or (now.hour == 15 and now.minute >= 20)


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
    """Same source precedence as before: BaoStock adjusted -> BaoStock
    unadjusted -> yfinance for stocks; yfinance only for ETFs."""
    if asset_type == "stock":
        bs_ticker = _to_baostock_ticker(ticker)
        df = _retry_download_baostock(bs_ticker, start_date, end_date, adjustflag="2")
        if df.empty:
            df = _retry_download_baostock(bs_ticker, start_date, end_date, adjustflag="3")
        if df.empty:
            yf_ticker = _to_yfinance_ticker(ticker)
            df = _retry_download_yfinance(yf_ticker, start_date, end_date)
        return df
    else:  # etf
        yf_ticker = _to_yfinance_ticker(ticker)
        return _retry_download_yfinance(yf_ticker, start_date, end_date)


# --------------------------------------------------------- sheet-backed cache --
@st.cache_data(ttl=32400, show_spinner=False)
def _load_all_price_cache() -> pd.DataFrame:
    """Read the whole price_cache tab once per hour. Cheap in-memory reuse
    across every ticker lookup until it expires or is explicitly cleared."""
    return sheets_db.read_df("price_cache")


def _clear_price_cache_memory() -> None:
    """Invalidate ONLY the in-memory price cache. Appending a new price
    shouldn't force positions/backtests/watchlist reads to re-hit Sheets too -
    that's what sheets_db.clear_caches() is for, and it's overkill here."""
    _load_all_price_cache.clear()


def _get_cached_series(ticker: str, asset_type: str) -> pd.Series:
    df = _load_all_price_cache()
    if df.empty:
        return pd.Series(dtype=float)

    # ----- FIX: ensure required columns exist -----
    required_cols = ["ticker", "asset_type", "date", "close"]
    if not all(col in df.columns for col in required_cols):
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
    """
    Fetch all watchlist ETFs in a single yfinance batch request.
    Returns dict: label -> normalized price series (starting at 1.0).
    """
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

    # Use a reasonable date range for the watchlist (last 10 years)
    end_date = _now_beijing().date()
    start_date = (end_date - timedelta(days=3650)).strftime("%Y-%m-%d")
    end_date_str = end_date.strftime("%Y-%m-%d")

    try:
        data = yf.download(
            tickers, start=start_date, end=end_date_str,
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
                # Normalize to start at 1.0
                result[label] = close / close.iloc[0]
        return result
    except Exception as e:
        st.warning(f"Batch yfinance fetch failed: {e}")
        return {}


# ------------------------------------------------------------------ public --
@st.cache_data(ttl=14400, show_spinner=False)
def get_price_series(ticker: str, asset_type: str = "stock", start_date: str = "1990-01-01") -> pd.Series:
    """
    Date-indexed close price series, backed by the price_cache sheet.
    Only dates missing from the cache are ever fetched live - after the
    first full fetch for a ticker, this is normally a sheet read plus at
    most one small incremental fetch for the latest day(s).
    """
    ticker = _clean_ticker(ticker)
    cached_series = _get_cached_series(ticker, asset_type)

    if not cached_series.empty:
        start_fetch = (cached_series.index.max() + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    else:
        start_fetch = start_date

    today = _now_beijing().date()
    if _should_fetch_today():
        end_fetch = today.strftime("%Y-%m-%d")
    else:
        # Today's close isn't available yet (pre-market-close or a
        # non-trading day) - cap at yesterday so we don't keep re-asking
        # the API for a bar that doesn't exist, on every single rerun.
        end_fetch = (today - timedelta(days=1)).strftime("%Y-%m-%d")

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
