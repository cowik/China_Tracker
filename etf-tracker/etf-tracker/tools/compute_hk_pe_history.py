# -*- coding: utf-8 -*-
"""
Reconstruct the 5-year daily P/E (TTM) history of the three Hang Seng indices
used by the dashboard (Hang Seng TECH, Hang Seng Biotech, Hang Seng SCHK High
Dividend Low Volatility) and compute their 5-year medians - without Wind.

Why this exists (verified 2026-11-24): hsi.com.hk publishes only the CURRENT
monthly P/E in its factsheets (files are overwritten every month, no archive);
the Index360 fundamentals need registration; Danjuan's index_eva history
requires a phone login and covers only HKHSTECH anyway. So, exactly like the
CNI tool (compute_cni_pe_history.py), the history is rebuilt from official
building blocks - but through HK-specific channels:

  1. Membership timeline - full holdings of the tracker ETFs (513180 / 159892 /
     159545) from Eastmoney F10 semi-annual investment reports, every ~6 months.
     This mirrors how the app itself computes the CURRENT P/E of these rows
     (ETF holdings as the index proxy). Before the ETF's first full report the
     first known list is backcast (159545 exists since 2024-03 -> its HSHYLV
     membership is backcast ~2.5 years; 513180/159892 cover the whole window).
  2. Per-stock P/E (TTM) and total market cap history - Baidu gushitong
     (akshare stock_hk_valuation_baidu, period 近五年 + 近三年 merged for daily
     recent data). Verified: Baidu's last-day P/E and mcap match Tencent gtimg
     quotes EXACTLY (e.g. 02269 PE 30.19 / mcap 1740.77e8), and gtimg is what
     the app's live column uses - so the two columns share one methodology.
  3. Per-stock daily close (raw) - Tencent ifzq kline; used to extend the
     (sparse) Baidu market caps to every trading day: mcap(t) = mcap(s) *
     P(t)/P(s) at the as-of observation s.

Aggregate (整体法, loss-makers INCLUDED - negative earnings reduce the sum,
exactly like the app's live HK rows): P/E(t) = Σ mcap_i(t) / Σ e_i(t),
e_i(s) = mcap_i(s) / pe_i(s) at the same observation date (price cancels, so
the earnings step series is pure). Stocks with mcap but no P/E contribute
their mcap and zero earnings (the same convention as _aggregate_pe_ttm).

Output: data/hk_pe_history.csv  (date, index_key, pe, n_ok, n_members)
plus .hk_cache/hk_summary.json with medians and validation.

Validation anchors (printed at the end):
- the app's live value: the same ETF-holdings gtimg aggregate the dashboard
  shows today (for all three rows);
- Danjuan HKHSTECH current P/E + its published pe_percentile (for the TECH
  row) - our history's percentile of that P/E should be close to Danjuan's;
- HSI factsheet monthly P/E (reference only: their convention differs, most
  visibly for Biotech).

Usage:
  python3 tools/compute_hk_pe_history.py            # full run -> data/hk_pe_history.csv
  python3 tools/compute_hk_pe_history.py --validate # only anchors vs the file on disk
"""
from __future__ import annotations

import argparse
import json
import os
import re
import time
from datetime import datetime

import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

# The three HK rows of INDEX_PE_CONFIG (keys must match utils/index_valuation.py)
CODES = {
    "hk_tech": {"etf": "513180", "name": "Hang Seng TECH"},
    "hk_bio":  {"etf": "159892", "name": "Hang Seng Biotech"},
    "hk_div":  {"etf": "159545", "name": "Hang Seng SCHK High Dividend Low Vol"},
}
MEDIAN_YEARS = 5
STALE_MAX_DAYS = 21        # a Baidu observation may be at most this many days old
PRICE_STALE_DAYS = 10      # kline close may be at most this many days old
COVERAGE_MIN = 0.60        # a day is used if >= 60% of members have data
FULL_LIST_MIN = 25         # jjcc report with >= this many rows = full holdings
GTIMG_CHUNK = 40           # gtimg batch size (symbols per request)
MAX_PE_DAY = 250           # aggregate P/E above this = aggregate earnings ~ 0 -> day dropped

OUT_CSV = os.path.join("data", "hk_pe_history.csv")

# --------------------------------------------------------------------------- #
# Fetchers (idempotent, cached on disk)
# --------------------------------------------------------------------------- #
def fetch_holdings_timeline(etf: str, cache_dir: str) -> list[tuple[pd.Timestamp, list[str]]]:
    """All full-holdings reports of the ETF from Eastmoney F10: [(date, [codes])].
    Semi-annual reports (Q2/Q4) carry the complete list; quarterly ones only
    the top-10 and are filtered out by FULL_LIST_MIN."""
    f = os.path.join(cache_dir, f"jjcc_{etf}.csv")
    if os.path.exists(f):
        df = pd.read_csv(f, dtype={"code": str})
        snap = [(pd.Timestamp(r["date"]), r["code"]) for _, r in df.iterrows()]
        out: dict = {}
        for d, c in snap:
            out.setdefault(d, []).append(c)
        return sorted(out.items())

    out: dict = {}
    for year in range(2021, datetime.now().year + 1):
        for month in ("6", "12"):   # only these responses carry FULL holdings lists
            try:
                r = requests.get(
                    "https://fundf10.eastmoney.com/FundArchivesDatas.aspx",
                    params={"type": "jjcc", "code": etf, "topline": 200,
                            "year": year, "month": month},
                    headers={**UA, "Referer": f"https://fundf10.eastmoney.com/ccmx_{etf}.html"},
                    timeout=30)
            except Exception:
                time.sleep(2)
                continue
            # one <h4>-headed block per report; the report date is in 截止至
            for ch in re.split(r"<h4", r.text)[1:]:
                date_m = re.search(r"截止至.*?(20\d{2}-\d{2}-\d{2})", ch) \
                         or re.search(r"(20\d{2}-\d{2}-\d{2})", ch)
                if not date_m:
                    continue
                codes = []
                for tr in re.findall(r"<tr>(.*?)</tr>", ch, re.S):
                    tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
                    if len(tds) < 7:
                        continue
                    code = re.sub(r"<[^>]+>", "", tds[1]).strip()
                    if re.fullmatch(r"\d{4,6}", code):
                        code = code.zfill(5)
                        if code not in codes:
                            codes.append(code)
                if len(codes) >= FULL_LIST_MIN:
                    d = pd.Timestamp(date_m.group(1))
                    if d not in out or len(codes) > len(out[d]):
                        out[d] = codes
            time.sleep(0.4)
    result = sorted(out.items())
    rows = [{"date": d.strftime("%Y-%m-%d"), "code": c} for d, cs in result for c in cs]
    if rows:
        pd.DataFrame(rows).to_csv(f, index=False)
    return result


def _baidu(symbol: str, indicator: str, period: str, retries: int = 4) -> pd.DataFrame:
    import akshare as ak
    last = None
    for a in range(retries):
        try:
            df = ak.stock_hk_valuation_baidu(symbol=symbol, indicator=indicator, period=period)
            if len(df):
                return df
            last = "empty"
        except Exception as e:
            last = str(e)[:80]
        time.sleep(1.2 * (a + 1))
    print(f"  FAIL baidu {symbol} {indicator} {period}: {last}", flush=True)
    return pd.DataFrame()


def fetch_baidu_series(symbol: str, indicator: str, cache_dir: str) -> pd.Series:
    """近五年 series {date: value}; fallback 全部. Cached on disk."""
    bdir = os.path.join(cache_dir, "baidu")
    os.makedirs(bdir, exist_ok=True)
    f = os.path.join(bdir, f"{symbol}_{indicator}.csv")
    if os.path.exists(f):
        df = pd.read_csv(f, parse_dates=["date"])
        return pd.Series(pd.to_numeric(df["value"], errors="coerce").values,
                         index=pd.DatetimeIndex(df["date"]))
    parts = []
    for period in ("近五年", "全部"):   # 近五年 suffices: the kline price extension
        df = _baidu(symbol, indicator, period)  # gives daily precision to mcap
        if len(df):
            parts.append(df)
            if period == "近五年":
                break
        time.sleep(0.3)
    if not parts:
        return pd.Series(dtype=float)
    allp = pd.concat(parts, ignore_index=True).drop_duplicates("date", keep="first")
    allp.to_csv(f, index=False)
    return pd.Series(pd.to_numeric(allp["value"], errors="coerce").values,
                     index=pd.to_datetime(allp["date"]))


def _clean_pe(s: pd.Series) -> pd.Series:
    """Drop corrupt Baidu placeholder values (~1.0 when TTM earnings ~ 0)."""
    med = s.abs().median()
    bad = s.between(0.9, 1.15) & (s.abs() > 5 * med)
    if bad.any():
        s = s.where(~bad)
    return s


def fetch_kline(symbol: str, cache_dir: str, start: str, end: str) -> pd.Series:
    """Raw daily close from Tencent ifzq (rows are [date, open, close, ...])."""
    kdir = os.path.join(cache_dir, "kline")
    os.makedirs(kdir, exist_ok=True)
    f = os.path.join(kdir, f"{symbol}.csv")
    if os.path.exists(f):
        df = pd.read_csv(f, parse_dates=["date"])
        return pd.Series(pd.to_numeric(df["close"], errors="coerce").values,
                         index=pd.DatetimeIndex(df["date"]))
    for a in range(3):
        try:
            url = ("https://web.ifzq.gtimg.cn/appstock/app/kline/kline"
                   f"?param={symbol},day,{start},{end},2000")
            r = requests.get(url, headers=UA, timeout=30)
            days = ((r.json().get("data") or {}).get(symbol) or {}).get("day") or []
            if days:
                df = pd.DataFrame({"date": [d[0] for d in days],
                                   "close": [float(d[2]) for d in days]})
                df.to_csv(f, index=False)
                return pd.Series(df["close"].values, index=pd.DatetimeIndex(df["date"]))
        except Exception:
            time.sleep(1 + a)
    return pd.Series(dtype=float)


# --------------------------------------------------------------------------- #
# Per-stock aligned series on the HK trading grid
# --------------------------------------------------------------------------- #
def load_stock_series(symbol: str, grid: pd.DatetimeIndex, cache_dir: str) -> pd.Series:
    """Returns (mcap_t, earnings_t) aligned to the grid.

    - obs series: mcap(s), pe(s) -> e(s) = mcap(s)/pe(s) (price cancels)
    - mcap(t) = mcap(s_t) * P(t)/P(s_t)  (as-of obs s_t, kline-extended)
    - staleness: obs may be at most STALE_MAX_DAYS old; kline PRICE_STALE_DAYS
    """
    pe = fetch_baidu_series(symbol, "市盈率(TTM)", cache_dir)
    mc = fetch_baidu_series(symbol, "总市值", cache_dir)
    if len(pe) == 0 or len(mc) == 0:
        return (pd.Series(index=grid, dtype=float), pd.Series(index=grid, dtype=float))
    pe, mc = pe.copy(), mc.copy()
    pe = _clean_pe(pe)
    # align the two series on their common observation dates
    dates = pe.index.intersection(mc.index)
    pe, mc = pe.reindex(dates), mc.reindex(dates)
    e = (mc / pe).where(pe.notna() & (pe != 0) & mc.notna() & (mc > 0))

    kp = fetch_kline("hk" + symbol, cache_dir, grid[0].strftime("%Y-%m-%d"),
                     grid[-1].strftime("%Y-%m-%d"))
    # kline close at each observation date (as-of)
    if len(kp):
        union = dates.union(kp.index)
        kp_at_obs = kp.reindex(union).ffill().reindex(dates)
    else:
        kp_at_obs = pd.Series(index=dates, dtype=float)

    def _asof(series: pd.Series, grid: pd.DatetimeIndex, stale: int, fill_mask=True):
        """ffill onto grid, masked where the as-of observation is too old."""
        if series.empty:
            return pd.Series(index=grid, dtype=float)
        union = grid.union(series.index)
        vals = series.reindex(union).ffill().reindex(grid)
        obs = pd.Series(series.index, index=series.index).reindex(union).ffill().reindex(grid)
        age = (grid.to_series() - obs).dt.days
        return vals.where(age <= stale) if fill_mask else vals

    m_ff = _asof(mc, grid, STALE_MAX_DAYS)
    e_ff = _asof(e, grid, STALE_MAX_DAYS)
    p_ff = _asof(kp, grid, PRICE_STALE_DAYS)
    p_at_obs_ff = _asof(kp_at_obs, grid, STALE_MAX_DAYS * 2)

    ratio = p_ff / p_at_obs_ff
    mcap_t = m_ff * ratio.where(ratio.notna(), 1.0)
    return mcap_t, e_ff


# --------------------------------------------------------------------------- #
# Live anchors (the app's own current values)
# --------------------------------------------------------------------------- #
def gtimg_quotes(symbols: list[str]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), GTIMG_CHUNK):
        batch = symbols[i:i + GTIMG_CHUNK]
        try:
            r = requests.get("https://qt.gtimg.cn/q=" + ",".join(batch),
                             timeout=15, headers=UA)
            r.encoding = "gbk"
            for line in r.text.strip().split(";"):
                line = line.strip()
                if "=" not in line or "~" not in line:
                    continue
                sym = line.split("=")[0].replace("v_", "")
                parts = line.split("=")[1].strip('"').split("~")
                if len(parts) < 58:
                    continue

                def flt(idx):
                    try:
                        return float(parts[idx]) if parts[idx] else None
                    except (ValueError, IndexError):
                        return None
                out[sym] = {"pe_ttm": flt(57), "mcap": flt(45)}
        except Exception:
            pass
        time.sleep(0.2)
    return out


def live_etf_pe(etf: str, timeline) -> tuple:
    """Replicates the app's _hk_etf_pe: latest full holdings + gtimg 整体法."""
    if not timeline:
        return None, []
    codes = timeline[-1][1]
    quotes = gtimg_quotes(["hk" + c for c in codes])
    tot_m = tot_e = 0.0
    for q in quotes.values():
        if not q.get("mcap"):
            continue
        tot_m += q["mcap"]
        if q.get("pe_ttm"):
            tot_e += q["mcap"] / q["pe_ttm"]
    return (tot_m / tot_e if tot_e > 0 else None), codes


def danjuan_hk_tech() -> dict:
    try:
        r = requests.get("https://danjuanfunds.com/djapi/index_eva/dj",
                         headers={**UA, "Referer": "https://danjuanfunds.com/index-value"},
                         timeout=20)
        for it in r.json().get("data", {}).get("items", []):
            if it.get("index_code") == "HKHSTECH":
                return {"pe": float(it.get("pe") or 0) or None,
                        "pe_percentile": it.get("pe_percentile")}
    except Exception:
        pass
    return {"pe": None, "pe_percentile": None}


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #
def run(cache_dir: str, out_csv: str, validate_only: bool = False):
    today = pd.Timestamp(datetime.now().date())
    window_start = today - pd.Timedelta(days=365 * MEDIAN_YEARS)

    # --- trading grid from the HSTECH index kline (HKEX calendar) ---
    hstech_k = fetch_kline("hkHSTECH", cache_dir,
                           (window_start - pd.Timedelta(days=10)).strftime("%Y-%m-%d"),
                           today.strftime("%Y-%m-%d"))
    if hstech_k.empty:
        print("FATAL: HSTECH index kline unavailable - no trading grid", flush=True)
        return
    grid = pd.DatetimeIndex([d for d in hstech_k.index
                             if window_start <= d <= today]).sort_values()
    print(f"window: {grid[0].date()} -> {grid[-1].date()} ({len(grid)} HK trading days)", flush=True)

    # --- membership timelines ---
    timelines = {k: fetch_holdings_timeline(v["etf"], cache_dir) for k, v in CODES.items()}
    for k, tl in timelines.items():
        first = tl[0][0].date() if tl else "-"
        print(f"{k} ({CODES[k]['name']}, ETF {CODES[k]['etf']}): {len(tl)} full-list "
              f"snapshots {first}..{tl[-1][0].date() if tl else '-'}", flush=True)

    if validate_only:
        _validate(grid, timelines, out_csv, cache_dir)
        return

    # --- per-stock series (parallel: network-bound) ---
    from concurrent.futures import ThreadPoolExecutor
    universe = sorted({c for tl in timelines.values() for _, cs in tl for c in cs})
    print(f"per-stock universe: {len(universe)} HK stocks", flush=True)
    mcap_d, e_d = {}, {}

    def _one(sym):
        try:
            return sym, load_stock_series(sym, grid, cache_dir)
        except Exception as ex:
            print(f"  ERR {sym}: {str(ex)[:90]}", flush=True)
            return sym, (pd.Series(index=grid, dtype=float),
                         pd.Series(index=grid, dtype=float))

    with ThreadPoolExecutor(max_workers=6) as ex:
        for i, (sym, pair) in enumerate(ex.map(_one, universe)):
            mcap_d[sym], e_d[sym] = pair
            if (i + 1) % 25 == 0:
                print(f"  [{i+1}/{len(universe)}] series aligned", flush=True)
    mc_df = pd.DataFrame(mcap_d, index=grid)
    e_df = pd.DataFrame(e_d, index=grid)

    # --- membership as-of frames ---
    def members_at(tl, d):
        chosen = tl[0][1]
        for date, cs in tl:
            if date <= d:
                chosen = cs
            else:
                break
        return chosen

    # --- aggregate ---
    print(f"\n=== full history, 整体法 with loss-makers (the app's live convention) ===", flush=True)
    out_rows, summary = [], {}
    for key, tl in timelines.items():
        pe_list = []
        for d in grid:
            members = members_at(tl, d)
            if not members:
                continue
            m = mc_df.loc[d].reindex(members)
            e = e_df.loc[d].reindex(members)
            n_members = len(members)
            n_m = int(m.notna().sum())
            n_ok = int((m.notna() & e.notna()).sum())
            tot_m = float(m.sum(min_count=1)) if n_m else float("nan")
            tot_e = float(e.sum(min_count=1)) if n_ok else float("nan")
            if n_m < COVERAGE_MIN * n_members or pd.isna(tot_m) or pd.isna(tot_e) or tot_e <= 0:
                continue
            pe_day = tot_m / tot_e
            if pe_day > MAX_PE_DAY:
                continue          # earnings ~ 0: P/E undefined (2022 HK biotech)
            pe_list.append((d, pe_day, n_ok, n_members))
        hist = pd.DataFrame(pe_list, columns=["date", "pe", "n_ok", "n_members"])
        med = float(hist["pe"].median())
        cur = float(hist["pe"].iloc[-1])
        summary[key] = {"name": CODES[key]["name"],
                        "etf": CODES[key]["etf"],
                        "median": round(med, 2),
                        "n_obs": int(len(hist)),
                        "first": str(hist["date"].iloc[0].date()),
                        "last": str(hist["date"].iloc[-1].date()),
                        "computed_now": round(cur, 2)}
        print(f"  {key} {CODES[key]['name']}: median={med:.2f} obs={len(hist)} "
              f"({hist['date'].iloc[0].date()}..{hist['date'].iloc[-1].date()}) "
              f"computed_now={cur:.2f}", flush=True)
        hist.insert(1, "index_key", key)
        out_rows.append(hist)

    full = pd.concat(out_rows, ignore_index=True) if out_rows else pd.DataFrame()
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    full.to_csv(out_csv, index=False)
    with open(os.path.join(cache_dir, "hk_summary.json"), "w") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print(f"\nsaved {out_csv} ({len(full)} rows) and hk_summary.json", flush=True)

    _validate(grid, timelines, out_csv, cache_dir)


# --------------------------------------------------------------------------- #
# Validation
# --------------------------------------------------------------------------- #
def _validate(grid, timelines, out_csv: str, cache_dir: str):
    try:
        hist = pd.read_csv(out_csv, parse_dates=["date"])
    except Exception:
        hist = pd.DataFrame()
    print("\n=== VALIDATION ===", flush=True)
    dj = danjuan_hk_tech()
    for key, tl in timelines.items():
        live, codes = live_etf_pe(CODES[key]["etf"], tl)
        h = hist[hist["index_key"] == key].sort_values("date") if not hist.empty else pd.DataFrame()
        last = float(h["pe"].iloc[-1]) if len(h) else None
        med = float(h["pe"].median()) if len(h) else None
        line = (f"  {key} ({CODES[key]['name']}): history_now={last and round(last, 2)} "
                f"| live_etf_anchor={live and round(live, 2)}")
        if key == "hk_tech" and dj.get("pe"):
            pct = (h["pe"] < dj["pe"]).mean() if len(h) else None
            line += (f" | danjuan_now={round(dj['pe'], 2)} "
                     f"(our_pct_of_danjuan_pe={pct and round(pct, 3)} "
                     f"vs danjuan pe_percentile={dj.get('pe_percentile')})")
        line += f" | median={med and round(med, 2)}"
        print(line, flush=True)
        if len(h):
            h_sorted = h.sort_values("date")
            big = h_sorted[h_sorted["pe"].pct_change().abs() > 0.25]
            print(f"     days with |Δ P/E| > 25%: {len(big)} "
                  f"({', '.join(str(d.date()) for d in big['date'].head(6))})", flush=True)
    print("  reference (HSI factsheets, own convention): 2026-10-30: HSTECH 25.43, "
          "HSBIO 51.93, HSHYLV 6.87", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default=".hk_cache")
    ap.add_argument("--out", default=OUT_CSV)
    ap.add_argument("--validate", action="store_true")
    args = ap.parse_args()
    run(args.cache_dir, args.out, validate_only=args.validate)
