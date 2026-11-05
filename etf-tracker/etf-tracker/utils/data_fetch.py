"""
Data fetching with Google Sheets price cache.
- Stocks: BaoStock (adjustflag='2') -> yfinance fallback.
- ETFs: yfinance only.
- Caches prices directly in the Google Sheet 'price_cache' tab.
"""
from __future__ import annotations
import time
import random
import os
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

@st.cache_data(ttl=3600, show_spinner=False)
def _get_last_trading_day() -> str:
    now_beijing = _now_beijing()
    cutoff_time = now_beijing.replace(hour=17, minute=30, second=0, microsecond=0)
    if now_beijing < cutoff_time:
        now_beijing -= timedelta(days=1)
        
    today = now_beijing.strftime("%Y-%m-%d")
    start = (now_beijing - timedelta(days=10)).strftime("%Y-%m-%d")
    
    try:
        import baostock as bs
        lg = bs.login()
        if lg is not None and lg.error_code == '0':
            rs = bs.query_trade_dates(start_date=start, end_date=today)
            if rs.error_code == '0':
                trading_days = []
                while rs.next():
                    row = rs.get_row_data()
                    if row and len(row) >= 2 and row[1] == "1":
                        trading_days.append(row[0])
                bs.logout()
                if trading_days:
                    return max(trading_days)
    except Exception:
        pass
        
    dt = _now_beijing()
    if dt.hour < 17:
        dt -= timedelta(days=1)
    while dt.weekday() > 4:
        dt -= timedelta(days=1)
    return dt.strftime("%Y-%m-%d")

# ------------------------------------------------------------ raw fetchers --
def _retry_download_yfinance(ticker_yf: str, start: str, end: str, retries=3) -> pd.DataFrame:
    try:
        end_date_exclusive = (pd.Timestamp(end) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
    except Exception:
        end_date_exclusive = end
        
    for attempt in range(retries):
        try:
            df = yf.download(ticker_yf, start=start, end=end_date_exclusive, progress=False, timeout=15, auto_adjust=True)
            if df.empty:
                return pd.DataFrame()
            
            if isinstance(df.columns, pd.MultiIndex):
                if "Adj Close" in df.columns.get_level_values(0):
                    close = df["Adj Close"].iloc[:, 0]
                elif "Close" in df.columns.get_level_values(0):
                    close = df["Close"].iloc[:, 0]
                else:
                    return pd.DataFrame()
            else:
                close = df["Adj Close"] if "Adj Close" in df.columns else df["Close"]
                
            out = close.reset_index()
            out.columns = ["date", "close"]
            out["date"] = pd.to_datetime(out["date"]).dt.tz_localize(None)
            out = out[out["date"].dt.normalize() <= pd.Timestamp(end)]
            return out
        except Exception:
            if attempt < retries - 1:
                time.sleep(2 * (1 + random.random()))
            else:
                return pd.DataFrame()
    return pd.DataFrame()

def _fetch_missing_data_batch(requests: list[tuple[str, str, str, str]]) -> dict[str, pd.DataFrame]:
    import baostock as bs
    results = {}
    
    if not requests:
        return results
        
    stock_reqs = [(t, at, s, e) for t, at, s, e in requests if at == "stock"]
    etf_reqs = [(t, at, s, e) for t, at, s, e in requests if at != "stock"]
    
    session_ok = False
    if stock_reqs:
        try:
            lg = bs.login()
            session_ok = lg is not None and lg.error_code == "0"
            
            for ticker, asset_type, start_date, end_date in stock_reqs:
                bs_ticker = _to_baostock_ticker(ticker)
                df = pd.DataFrame()
                
                try:
                    rs = bs.query_history_k_data_plus(bs_ticker, "date,close", start_date=start_date, end_date=end_date, frequency="d", adjustflag="2")
                    if rs.error_code == "0":
                        data_list = []
                        while rs.next():
                            data_list.append(rs.get_row_data())
                        if data_list:
                            df = pd.DataFrame(data_list, columns=rs.fields)
                            df["date"] = pd.to_datetime(df["date"])
                            df["close"] = pd.to_numeric(df["close"], errors="coerce")
                            df = df.dropna(subset=["close"])[["date", "close"]]
                except Exception:
                    pass
                    
                if df.empty:
                    try:
                        rs = bs.query_history_k_data_plus(bs_ticker, "date,close", start_date=start_date, end_date=end_date, frequency="d", adjustflag="3")
                        if rs.error_code == "0":
                            data_list = []
                            while rs.next():
                                data_list.append(rs.get_row_data())
                            if data_list:
                                df = pd.DataFrame(data_list, columns=rs.fields)
                                df["date"] = pd.to_datetime(df["date"])
                                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                                df = df.dropna(subset=["close"])[["date", "close"]]
                    except Exception:
                        pass
                        
                if df.empty:
                    yf_ticker = _to_yfinance_ticker(ticker)
                    df = _retry_download_yfinance(yf_ticker, start_date, end_date)
                    
                if not df.empty:
                    results[ticker] = df
        except Exception:
            pass
        finally:
            if session_ok:
                bs.logout()
                
    for ticker, asset_type, start_date, end_date in etf_reqs:
        yf_ticker = _to_yfinance_ticker(ticker)
        df = _retry_download_yfinance(yf_ticker, start_date, end_date)
        if not df.empty:
            results[ticker] = df
            
    return results

# --------------------------------------------------------------- batch watchlist --
@st.cache_data(ttl=3600, show_spinner=False)
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
        tickers.append(ticker)
        labels.append(f"{name} ({ticker})")
        
    if not tickers:
        return {}
        
    end_date = _get_last_trading_day()
    start_date = (datetime.now(BEIJING_TZ) - timedelta(days=3650)).strftime("%Y-%m-%d")
    
    cache_df = sheets_db.read_df("price_cache")
    results = {}
    missing_requests = []
    missing_labels = []
    
    for i, t in enumerate(tickers):
        if not cache_df.empty:
            mask = (cache_df["ticker"] == t) & (cache_df["asset_type"] == 'etf')
            cached = cache_df[mask].copy()
        else:
            cached = pd.DataFrame()
            
        if not cached.empty:
            cached["date"] = pd.to_datetime(cached["date"])
            cached["close"] = pd.to_numeric(cached["close"], errors="coerce")
            cached_series = cached.sort_values("date").set_index("date")["close"]
            results[labels[i]] = cached_series / cached_series.iloc[0]
            
            max_cached_date = cached_series.index.max()
            start_fetch = (max_cached_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            if start_fetch <= end_date:
                missing_requests.append((t, 'etf', start_fetch, end_date))
                missing_labels.append(labels[i])
        else:
            missing_requests.append((t, 'etf', start_date, end_date))
            missing_labels.append(labels[i])
            
    if missing_requests:
        fetched_data = _fetch_missing_data_batch(missing_requests)
        rows_to_append = []
        
        for i, req in enumerate(missing_requests):
            ticker = req[0]
            label = missing_labels[i]
            
            if ticker in fetched_data and not fetched_data[ticker].empty:
                df = fetched_data[ticker].copy()
                df["date"] = pd.to_datetime(df["date"])
                df["close"] = pd.to_numeric(df["close"], errors="coerce")
                df = df.dropna(subset=["close"])
                df = df[df["date"].dt.normalize() <= pd.Timestamp(end_date)]
                
                if not df.empty:
                    old_series = results.get(label, pd.Series(dtype=float))
                    new_series = df.set_index("date")["close"]
                    combined_series = pd.concat([old_series, new_series]).sort_index()
                    results[label] = combined_series / combined_series.iloc[0]
                    
                    for dt, price in zip(df["date"], df["close"]):
                        rows_to_append.append({"ticker": ticker, "asset_type": "etf", "date": dt.strftime("%Y-%m-%d"), "close": price})
                        
        if rows_to_append:
            sheets_db.append_rows("price_cache", rows_to_append)
            sheets_db.clear_caches()
            
    return results

# --------------------------------------------------------------- index cache --
# CSI 300 (and potentially other indices) price history, cached in its OWN
# Google Sheet tab (`index_price_cache`). This tab is written append-only and
# is completely independent from the `price_cache` tab used by holdings/ETFs.
INDEX_CACHE_START = "2005-01-01"  # full CSI 300 history since inception

_KLINE_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                           "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}


def _index_symbol_tencent(index_code: str) -> str:
    return ("sh" if index_code.startswith(("000", "950")) else "sz") + index_code


def _fetch_index_kline_tencent(index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Daily closes from Tencent (ifzq). The API returns the LAST `count` bars
    inside [start, end] (count is capped at 640), so we walk forward with
    rolling windows of ~850 calendar days (~600 trading days < 640) - about
    10 quick requests for the whole 2005->now history.
    Row format: [date, open, close, high, low, volume]."""
    import requests as _requests
    symbol = _index_symbol_tencent(index_code)
    rows: list[dict] = []
    cur = start_date
    while cur <= end_date:
        win_end = min((pd.Timestamp(cur) + pd.Timedelta(days=850)).strftime("%Y-%m-%d"),
                      end_date)
        try:
            r = _requests.get("https://web.ifzq.gtimg.cn/appstock/app/fqkline/get",
                              params={"param": f"{symbol},day,{cur},{win_end},640,"},
                              headers=_KLINE_UA, timeout=20)
            data = ((r.json() or {}).get("data") or {}).get(symbol) or {}
            days = data.get("day") or data.get("qfqday") or []
        except Exception:
            days = []
        if not days:
            break
        for d in days:
            if len(d) >= 3 and start_date <= d[0] <= end_date:
                rows.append({"date": d[0], "close": float(d[2])})
        nxt = (pd.Timestamp(days[-1][0]) + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
        if nxt <= cur:
            break
        cur = nxt
        time.sleep(0.3)  # light pacing while paging through history
    return pd.DataFrame(rows)


def _fetch_index_kline_baostock(index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Daily closes from BaoStock (the same feed the app already uses for
    stock history). Full CSI 300 history from 2005-01-04, verified."""
    import baostock as bs
    rows: list[dict] = []
    try:
        lg = bs.login()
        if lg is None or getattr(lg, "error_code", "1") != "0":
            return pd.DataFrame()
        code = ("sh." if index_code.startswith(("000", "950")) else "sz.") + index_code
        rs = bs.query_history_k_data_plus(code, "date,close",
                                          start_date=start_date, end_date=end_date,
                                          frequency="d", adjustflag="3")
        while rs.next():
            r = rs.get_row_data()
            if len(r) >= 2 and r[0] and r[1]:
                rows.append({"date": r[0], "close": float(r[1])})
    except Exception:
        return pd.DataFrame(rows)
    finally:
        try:
            bs.logout()
        except Exception:
            pass
    return pd.DataFrame(rows)


def _fetch_index_kline_eastmoney(index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Daily closes from Eastmoney (price index, no adjustment).
    index_code: e.g. '000300' -> secid 1.000300 (CSI 300, Shanghai market).
    Fetches the full history in one request (beg=0&lmt) and filters locally."""
    import requests as _requests
    secid = f"1.{index_code}"
    url = (
        "https://push2his.eastmoney.com/api/qt/stock/kline/get"
        f"?secid={secid}&fields1=f1,f2,f3,f4,f5,f6"
        "&fields2=f51,f52,f53,f54,f55,f56,f57&klt=101&fqt=0"
        "&beg=0&end=20500101&lmt=10000"
    )
    for attempt in range(3):
        try:
            r = _requests.get(url, timeout=30, headers=_KLINE_UA)
            data = (r.json() or {}).get("data") or {}
            klines = data.get("klines") or []
            rows = []
            for k in klines:
                parts = k.split(",")
                # kline format: date, open, close, high, low, volume, amount
                if len(parts) >= 3 and parts[0] >= start_date and parts[0] <= end_date:
                    rows.append({"date": parts[0], "close": float(parts[2])})
            if rows:
                return pd.DataFrame(rows)
            if klines:  # history exists but nothing in the requested window
                return pd.DataFrame()
        except Exception:
            pass
        time.sleep(2 * (attempt + 1))
    return pd.DataFrame()


def _fetch_index_kline(index_code: str, start_date: str, end_date: str) -> pd.DataFrame:
    """Daily close prices for a China index from the first source that answers:
    Tencent -> BaoStock -> Eastmoney (all three verified independently)."""
    for fetcher in (_fetch_index_kline_tencent,
                    _fetch_index_kline_baostock,
                    _fetch_index_kline_eastmoney):
        try:
            df = fetcher(index_code, start_date, end_date)
        except Exception:
            df = pd.DataFrame()
        if not df.empty:
            return df
    return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def get_index_price_history(index_code: str = "000300") -> pd.Series:
    """
    Daily close-price series for a China index (price return, NOT total return),
    served from the dedicated `index_price_cache` Google Sheet tab whenever
    possible. Only ever APPENDS to that tab - existing rows (and every other
    tab) are never modified.
    """
    cache_df = sheets_db.read_df("index_price_cache")
    end_fetch = _get_last_trading_day()

    series = pd.Series(dtype=float)
    if not cache_df.empty:
        mask = cache_df["ticker"] == index_code
        cached = cache_df[mask].copy()
        if not cached.empty:
            cached["date"] = pd.to_datetime(cached["date"])
            cached["close"] = pd.to_numeric(cached["close"], errors="coerce")
            cached = cached.dropna(subset=["close"]).sort_values("date")
            cached = cached.drop_duplicates(subset=["date"], keep="last")
            series = pd.Series(cached["close"].values, index=cached["date"])

    start_fetch = INDEX_CACHE_START
    if not series.empty:
        max_cached = series.index.max()
        start_fetch = (max_cached + pd.Timedelta(days=1)).strftime("%Y-%m-%d")

    if start_fetch <= end_fetch:
        df = _fetch_index_kline(index_code, start_fetch, end_fetch)
        if not df.empty:
            rows_to_append = [
                {"ticker": index_code, "date": d, "close": c}
                for d, c in zip(df["date"], df["close"])
            ]
            sheets_db.append_rows("index_price_cache", rows_to_append)
            # merge the new rows into the in-memory series directly - no cache
            # wipe and no full re-read of the sheet
            add = pd.Series(pd.to_numeric(df["close"], errors="coerce").values,
                            index=pd.to_datetime(df["date"]))
            add = add.dropna()
            if not add.empty:
                series = add if series.empty else pd.concat([series, add])
                series = series[~series.index.duplicated(keep="last")].sort_index()

    return series

# ------------------------------------------------------------------ public --
@st.cache_data(ttl=3600, show_spinner=False)
def get_prices_batch(holdings: list[dict]) -> dict[str, pd.Series]:
    cache_df = sheets_db.read_df("price_cache")
    end_fetch = _get_last_trading_day()
    missing_requests = []
    results = {}
    
    for h in holdings:
        ticker = _clean_ticker(h["ticker"])
        asset_type = h.get("asset_type", "stock")
        
        # OPTIMIZATION: Full 10-year history for ETFs, but only from inception_date for Stocks
        if asset_type == "etf":
            start_date = (datetime.now(BEIJING_TZ) - timedelta(days=3650)).strftime("%Y-%m-%d")
        else:
            # FIX: Safely handle missing/NaT purchase dates to prevent ValueError crash
            inc_date = h.get("inception_date")
            if pd.isna(inc_date):
                start_date = (datetime.now(BEIJING_TZ) - timedelta(days=3650)).strftime("%Y-%m-%d")
            else:
                start_date = pd.Timestamp(inc_date).strftime("%Y-%m-%d")
        
        if not cache_df.empty:
            mask = (cache_df["ticker"] == ticker) & (cache_df["asset_type"] == asset_type)
            cached = cache_df[mask].copy()
        else:
            cached = pd.DataFrame()
            
        if not cached.empty:
            cached["date"] = pd.to_datetime(cached["date"])
            cached["close"] = pd.to_numeric(cached["close"], errors="coerce")
            cached_series = cached.sort_values("date").set_index("date")["close"]
            results[ticker] = cached_series
            
            max_cached_date = cached_series.index.max()
            start_fetch = (max_cached_date + pd.Timedelta(days=1)).strftime("%Y-%m-%d")
            if start_fetch <= end_fetch:
                missing_requests.append((ticker, asset_type, start_fetch, end_fetch))
        else:
            missing_requests.append((ticker, asset_type, start_date, end_fetch))
            
    if not missing_requests:
        return results
        
    fetched_data = _fetch_missing_data_batch(missing_requests)
    rows_to_append = []
    
    for ticker, asset_type, _, _ in missing_requests:
        if ticker in fetched_data and not fetched_data[ticker].empty:
            df = fetched_data[ticker].copy()
            df["date"] = pd.to_datetime(df["date"])
            df["close"] = pd.to_numeric(df["close"], errors="coerce")
            df = df.dropna(subset=["close"])
            df = df[df["date"].dt.normalize() <= pd.Timestamp(end_fetch)]
            
            for dt, price in zip(df["date"], df["close"]):
                rows_to_append.append({"ticker": ticker, "asset_type": asset_type, "date": dt.strftime("%Y-%m-%d"), "close": price})
                
    if rows_to_append:
        sheets_db.append_rows("price_cache", rows_to_append)
        sheets_db.clear_caches()
        
    cache_df = sheets_db.read_df("price_cache")
    for h in holdings:
        ticker = _clean_ticker(h["ticker"])
        asset_type = h.get("asset_type", "stock")
        if not cache_df.empty:
            mask = (cache_df["ticker"] == ticker) & (cache_df["asset_type"] == asset_type)
            cached = cache_df[mask].copy()
            if not cached.empty:
                cached["date"] = pd.to_datetime(cached["date"])
                cached["close"] = pd.to_numeric(cached["close"], errors="coerce")
                results[ticker] = cached.sort_values("date").set_index("date")["close"]
                
    return results

# ---- backward compatibility ----
def get_stock_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    s = get_prices_batch([{"ticker": ticker, "asset_type": "stock", "inception_date": start_date}]).get(ticker)
    if s is None or s.empty:
        return pd.DataFrame()
    return s.reset_index().rename(columns={"index": "date"})

def get_etf_hist(ticker: str, start_date: str = "1990-01-01", end_date: str = "2050-01-01") -> pd.DataFrame:
    s = get_prices_batch([{"ticker": ticker, "asset_type": "etf", "inception_date": start_date}]).get(ticker)
    if s is None or s.empty:
        return pd.DataFrame()
    return s.reset_index().rename(columns={"index": "date"})

def get_dividends(ticker: str, asset_type: str) -> pd.DataFrame:
    return pd.DataFrame()
