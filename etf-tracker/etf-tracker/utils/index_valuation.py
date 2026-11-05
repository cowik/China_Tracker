"""
Index valuation: P/E (TTM) for the 10 benchmark indices shown at the bottom
of the dashboard.

8 of the 10 rows come STRAIGHT from official sources with live queries
(no own calculations, a handful of fast HTTP requests, ~2-3 s total):
- CSI indices (000300, 931071, H30015, 000852): official csindex.com.cn API.
  The `peg` field of /csindex-home/perf/index-perf is the rolling P/E (TTM),
  published daily after the close.
- CNI indices (980017, 980022, 980018): official cnindex.com.cn API
  (POST /index/search). The `peDynamic` field is exactly the number that
  cnindex.com.cn itself displays in its index table under "PE(滚动)"
  (rolling P/E).
- Hang Seng TECH: Danjuan (Xueqiu) index valuation service.

The two remaining Hang Seng indices (High Dividend Low Vol, Biotech) have no
free official P/E feed, so they are COMPUTED - but only once per day:
P/E = sum(total market cap) / sum(TTM earnings) over the full holdings of
their tracker ETFs (159545 / 159892, Eastmoney F10 semi-annual reports),
using Tencent gtimg per-stock P/E TTM. Loss-makers are included (they reduce
the aggregate earnings) - the same convention Wind/Danjuan use. ~1-2 s.

A snapshot of every value is appended (append-only, dedup by day) to the
`pe_cache` tab of the Google Sheet for the last completed trading day.
get_pe_table() serves that snapshot whenever it already exists, so the live
sources are normally hit only once per day, after the 17:30 cache refresh.

Empirical data-format notes (verified):
- Tencent gtimg quotes: for HK stocks the P/E TTM is field 57 (field 39 is
  the dynamic P/E there); field 45 is total market cap. Requests accept
  ~50 symbols at once and answer in <0.1 s.
- Eastmoney FundArchivesDatas.aspx REQUIRES a Referer header from
  fundf10.eastmoney.com, otherwise it returns an empty payload.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta
from typing import Optional

import pandas as pd
import requests
import streamlit as st
import pytz

from utils import sheets_db, data_fetch

BEIJING_TZ = pytz.timezone("Asia/Shanghai")

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}

PE_TTM_FIELD_HK = 57  # gtimg field index for HK-stock P/E (TTM)
MCAP_FIELD = 45       # gtimg field index for total market cap

# --------------------------------------------------------------------------- #
# Configuration: the 10 indices and where each P/E comes from
# --------------------------------------------------------------------------- #
INDEX_PE_CONFIG = [
    {"key": "csi300",  "label": "Возможности Китая",                   "source": "csindex", "code": "000300"},
    {"key": "semi",    "label": "Полупроводники",                      "source": "cnindex", "code": "980017"},
    {"key": "ai",      "label": "ИИ",                                  "source": "csindex", "code": "931071"},
    {"key": "robot",   "label": "Робототехника",                       "source": "cnindex", "code": "980022"},
    {"key": "sat",     "label": "Космические технологии",              "source": "cnindex", "code": "980018"},
    {"key": "autos",   "label": "Автопром",                            "source": "csindex", "code": "H30015"},
    {"key": "hk_div",  "label": "Дивидендные акции Гонконга",          "source": "hk_etf",  "etf": "159545"},
    {"key": "hk_tech", "label": "Техсектор Гонконга",                  "source": "danjuan", "code": "HKHSTECH",
     "etf_fallback": "513180"},
    {"key": "hk_bio",  "label": "Биотехнологии Гонконга",              "source": "hk_etf",  "etf": "159892"},
    {"key": "csi1000", "label": "Китайские акции малой капитализации", "source": "csindex", "code": "000852"},
]


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _get(url: str, timeout: int = 15, extra_headers: dict | None = None) -> Optional[requests.Response]:
    headers = dict(UA)
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(2):
        try:
            r = requests.get(url, timeout=timeout, headers=headers)
            if r.status_code == 200:
                return r
        except Exception:
            pass
        time.sleep(1 + attempt)
    return None


def _after_close() -> bool:
    """True after 17:30 Beijing time (the app's daily cache-refresh moment)."""
    now = datetime.now(BEIJING_TZ)
    return now.hour > 17 or (now.hour == 17 and now.minute >= 30)


# --------------------------------------------------------------------------- #
# Tencent gtimg quotes (P/E TTM + total market cap), big fast batches
# --------------------------------------------------------------------------- #
def _gtimg_quotes(symbols: list[str], pe_field: int) -> dict[str, dict]:
    """
    symbols: ['hk06160', ...]
    pe_field: 57 for HK stocks (P/E TTM).
    Returns {symbol: {'name', 'pe_ttm', 'mcap'}} for whatever it could fetch.
    Verified: a batch of 50 symbols answers in ~0.05 s.
    """
    out: dict[str, dict] = {}
    chunk = 40
    for i in range(0, len(symbols), chunk):
        batch = symbols[i:i + chunk]
        for attempt in range(3):
            got = {}
            try:
                r = requests.get("https://qt.gtimg.cn/q=" + ",".join(batch),
                                 timeout=15, headers=UA)
                r.encoding = "gbk"
                for line in r.text.strip().split(";"):
                    line = line.strip()
                    if "=" not in line or "~" not in line:
                        continue
                    sym = line.split("=")[0].replace("v_", "")
                    if sym not in batch:
                        continue
                    parts = line.split("=")[1].strip('"').split("~")
                    if len(parts) < MCAP_FIELD + 1:
                        continue

                    def flt(idx: int):
                        try:
                            return float(parts[idx]) if parts[idx] else None
                        except (ValueError, IndexError):
                            return None

                    got[sym] = {"name": parts[1], "pe_ttm": flt(pe_field),
                                "mcap": flt(MCAP_FIELD)}
            except Exception:
                pass
            out.update(got)
            if len(got) == len(batch):
                break
            time.sleep(1 + attempt)  # retry only what failed (update merges)
        time.sleep(0.2)  # light pacing between batches
    return out


def _aggregate_pe_ttm(quotes: dict[str, dict]) -> Optional[float]:
    """Index P/E = sum(mcap) / sum(TTM earnings). Loss-makers included,
    stocks without P/E contribute zero earnings. None if aggregate <= 0."""
    tot_m = tot_e = 0.0
    for q in quotes.values():
        if not q.get("mcap"):
            continue
        pe = q.get("pe_ttm")
        tot_m += q["mcap"]
        if pe:
            tot_e += q["mcap"] / pe
    if tot_e <= 0 or tot_m <= 0:
        return None
    return tot_m / tot_e


# --------------------------------------------------------------------------- #
# Source 1: CSI official API (P/E TTM directly)
# --------------------------------------------------------------------------- #
def _csindex_pe(code: str) -> Optional[float]:
    end = datetime.now(BEIJING_TZ).strftime("%Y%m%d")
    start = (datetime.now(BEIJING_TZ) - timedelta(days=20)).strftime("%Y%m%d")
    r = _get("https://www.csindex.com.cn/csindex-home/perf/index-perf"
             f"?indexCode={code}&startDate={start}&endDate={end}")
    if r is None:
        return None
    try:
        data = r.json().get("data") or []
        for row in reversed(data):  # rows are oldest-first -> newest last
            pe = row.get("peg")
            if pe not in (None, 0, "0"):
                return float(pe)
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# Source 2: CNI official API (P/E TTM directly, the "PE(滚动)" of their site)
# --------------------------------------------------------------------------- #
def _cnindex_pe(code: str) -> Optional[float]:
    """Official rolling P/E from cnindex.com.cn - the same number their
    index-series table shows in the 'PE(滚动)' column (field `peDynamic`)."""
    for attempt in range(2):
        try:
            r = requests.post(
                "https://www.cnindex.com.cn/index/search",
                data={"content": code, "rows": 10, "pageNum": 1},
                headers={**UA,
                         "Referer": "https://www.cnindex.com.cn/module/index-series.html",
                         "X-Requested-With": "XMLHttpRequest"},
                timeout=15,
            )
            rows = ((r.json() or {}).get("data") or {}).get("rows") or []
            for row in rows:
                if str(row.get("indexcode", "")).strip() == str(code).strip():
                    pe = row.get("peDynamic")
                    if pe not in (None, "", 0, "0"):
                        return float(pe)
            return None  # index found but no PE published
        except Exception:
            time.sleep(1 + attempt)
    return None


# --------------------------------------------------------------------------- #
# Source 3: Danjuan (Xueqiu's index valuation) - direct P/E for a few indices
# --------------------------------------------------------------------------- #
def _danjuan_pe(code: str) -> Optional[float]:
    r = _get("https://danjuanfunds.com/djapi/index_eva/dj",
             extra_headers={"Referer": "https://danjuanfunds.com/index-value"})
    if r is None:
        return None
    try:
        for item in r.json().get("data", {}).get("items", []):
            if item.get("index_code") == code:
                pe = item.get("pe")
                return float(pe) if pe else None
    except Exception:
        pass
    return None


# --------------------------------------------------------------------------- #
# Source 4: Hang Seng indices via full ETF holdings -> computed P/E
# (only for the two indices that have no free official P/E feed)
# --------------------------------------------------------------------------- #
def _hk_etf_constituents(etf: str) -> list[str]:
    """
    Full stock list of the ETF from Eastmoney F10. Semi-annual reports
    (month=6/12) contain the complete holdings; quarterly reports (month='')
    only the top-10, so they are used as a last resort only.
    NOTE: the endpoint requires a Referer header, otherwise it returns
    an empty payload.
    """
    years = [datetime.now().year, datetime.now().year - 1]
    months = ["6", "12", ""]  # full semi-annual lists first
    codes: list[str] = []
    for year in years:
        for month in months:
            r = _get("https://fundf10.eastmoney.com/FundArchivesDatas.aspx"
                     f"?type=jjcc&code={etf}&topline=200&year={year}&month={month}",
                     extra_headers={"Referer": f"https://fundf10.eastmoney.com/ccmx_{etf}.html"})
            if r is None:
                continue
            for tr in re.findall(r"<tr>(.*?)</tr>", r.text, re.S):
                tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
                if len(tds) < 7:
                    continue
                code = re.sub(r"<[^>]+>", "", tds[1]).strip()
                if re.fullmatch(r"\d{4,6}", code):
                    code = code.zfill(5)
                    if code not in codes:
                        codes.append(code)
            if len(codes) >= 25:  # a full holdings list was found
                return codes
    return codes


def _hk_etf_pe(etf: str) -> Optional[float]:
    codes = _hk_etf_constituents(etf)
    if not codes:
        return None
    quotes = _gtimg_quotes(["hk" + c for c in codes], PE_TTM_FIELD_HK)
    if not quotes:
        return None
    return _aggregate_pe_ttm(quotes)


# --------------------------------------------------------------------------- #
# Orchestration
# --------------------------------------------------------------------------- #
def _fetch_one(cfg: dict) -> Optional[float]:
    try:
        src = cfg["source"]
        if src == "csindex":
            return _csindex_pe(cfg["code"])
        if src == "cnindex":
            return _cnindex_pe(cfg["code"])
        if src == "hk_etf":
            return _hk_etf_pe(cfg["etf"])
        if src == "danjuan":
            pe = _danjuan_pe(cfg["code"])
            if pe:
                return pe
            # fall back to computing from the biggest tracker ETF
            if cfg.get("etf_fallback"):
                return _hk_etf_pe(cfg["etf_fallback"])
            return None
    except Exception:
        return None
    return None


def _read_cached_snapshot(target_date: str) -> dict[str, float]:
    """{index_key: pe} for rows already stored in pe_cache for target_date."""
    try:
        cached = sheets_db.read_df("pe_cache")
        if cached.empty:
            return {}
        cached = cached[cached["date"].astype(str).str.strip() == target_date]
        out = {}
        for _, r in cached.iterrows():
            try:
                pe = float(r["pe"])
            except (ValueError, TypeError, KeyError):
                continue
            if pe > 0:
                out[str(r["index_key"])] = pe
        return out
    except Exception:
        return {}


def _append_snapshot(target_date: str, values: dict) -> None:
    """Append today's values to the `pe_cache` tab (append-only, dedup by day)."""
    try:
        cached = sheets_db.read_df("pe_cache")
        existing: set[tuple] = set()
        if not cached.empty:
            existing = set(zip(cached["date"].astype(str).str.strip(),
                               cached["index_key"].astype(str).str.strip()))
        rows = []
        for cfg in INDEX_PE_CONFIG:
            pe = values.get(cfg["key"])
            if pe is None:
                continue
            if (target_date, cfg["key"]) in existing:
                continue
            rows.append({"date": target_date, "index_key": cfg["key"],
                         "pe": f"{float(pe):.4f}"})
        if rows:
            sheets_db.append_rows("pe_cache", rows)
    except Exception:
        pass  # history snapshot is best-effort; never break the dashboard


@st.cache_data(ttl=3600, show_spinner=False)
def get_pe_table() -> pd.DataFrame:
    """
    Returns a DataFrame: columns [index_key, label, pe] for the 10 indices.

    Serves the snapshot stored in `pe_cache` for the last completed trading
    day whenever it exists; only the missing rows are fetched live. A fresh
    snapshot is appended (append-only) after the market close, so the slow
    part normally runs once per day.
    """
    try:
        target = data_fetch._get_last_trading_day()
    except Exception:
        target = None
    if not target:
        target = datetime.now(BEIJING_TZ).strftime("%Y-%m-%d")

    values: dict[str, Optional[float]] = {}
    if target:
        values.update(_read_cached_snapshot(target))

    for cfg in INDEX_PE_CONFIG:
        if cfg["key"] in values:
            continue
        values[cfg["key"]] = _fetch_one(cfg)

    if _after_close():
        _append_snapshot(target, values)

    rows = [{"index_key": cfg["key"], "label": cfg["label"],
             "pe": values.get(cfg["key"])} for cfg in INDEX_PE_CONFIG]
    return pd.DataFrame(rows, columns=["index_key", "label", "pe"])
