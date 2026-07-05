"""
Data fetching:
- Stocks: BaoStock primary (adjusted), fallback to unadjusted, then yfinance.
- ETFs: yfinance primary (no fallback to BaoStock).
"""
from __future__ import annotations
import time
import random
import pandas as pd
import streamlit as st
import yfinance as yf


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
            # Use Adj Close for total return
            if 'Adj Close' in df.columns:
                close = df['Adj Close']
            else:
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


def get_price_series(ticker: str, asset_type: str = "stock", start_date: str = "1990-01-01") -> pd.Series:
    """
    Returns date-indexed close price series.
    - Stocks: BaoStock adjusted → BaoStock unadjusted → yfinance (fallback).
    - ETFs: yfinance only (no fallback to BaoStock).
    """
    ticker = _clean_ticker(ticker)
    df = pd.DataFrame()

    if asset_type == 'stock':
        bs_ticker = _to_baostock_ticker(ticker)

        # 1. BaoStock adjusted (total return)
        df = _retry_download_baostock(bs_ticker, start_date, "2050-01-01", adjustflag='2')
        if df.empty:
            # 2. BaoStock unadjusted
            df = _retry_download_baostock(bs_ticker, start_date, "2050-01-01", adjustflag='3')

        if df.empty:
            # 3. yfinance fallback
            yf_ticker = _to_yfinance_ticker(ticker)
            try:
                df = _retry_download_yfinance(yf_ticker, start_date, "2050-01-01")
            except Exception:
                pass

        if df.empty:
            st.warning(f"❌ No data for {ticker} (stock)")
            return pd.Series(dtype=float)

        return df.set_index("date")["close"].sort_index()

    else:  # 'etf'
        yf_ticker = _to_yfinance_ticker(ticker)
        try:
            df = _retry_download_yfinance(yf_ticker, start_date, "2050-01-01")
        except Exception:
            pass

        if df.empty:
            st.warning(f"❌ No data for {ticker} (ETF)")
            return pd.Series(dtype=float)

        return df.set_index("date")["close"].sort_index()


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
