# -*- coding: utf-8 -*-
"""
Reconstruct the 5-year daily P/E (TTM) history of the CNI indices used by the
dashboard, and compute their 5-year medians - without Wind.

Why this exists: cnindex.com.cn publishes only the CURRENT PE(滚动) of its
indices; the daily historical files are OHLCV-only, so there is no free
official P/E history for CNI thematic indices. This tool rebuilds it from
official building blocks:

  1. Membership timeline - cnindex `sample-detail/download-adjustment`
     (every review, every stock, with membership windows).
  2. Per-stock daily P/E (TTM) and total market cap history - Baidu
     gushitong (via akshare `stock_zh_valuation_baidu`); values verified
     to match BaoStock's peTTM exactly on overlapping dates.
  3. Per-stock market cap is re-anchored to the cnindex basis (A-share cap)
     using the ratio cnindex_mcap / baidu_mcap on the constituents-snapshot
     date - this removes the A+H combined market cap that Baidu reports for
     dual-listed companies (e.g. SMIC).

Index P/E = sum(mcap_i) / sum(mcap_i / pe_i) over the members of the day
(the standard 整体法). The treatment of loss-makers (include / exclude /
zero earnings) is chosen by matching the official current peDynamic of all
four indices on the same day - see `--validate`.

Output: data/cni_pe_history.csv  (columns date, code, pe, n_ok, n_members)
plus a printed summary with the 5-year medians.

Usage:
  python compute_cni_pe_history.py --cache-dir .cni_cache --out data/cni_pe_history.csv
  python compute_cni_pe_history.py --validate          # only run the convention check
  python compute_cni_pe_history.py --refresh-stocks     # force re-download of per-stock series
"""
from __future__ import annotations

import argparse
import io
import json
import os
import time
from datetime import datetime, timedelta

import pandas as pd
import requests

UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

CODES = ["980017", "980022", "980018", "399266"]  # CNI indices of the dashboard
MEDIAN_YEARS = 5
COVERAGE_MIN = 0.70        # a day is used if >= 70% of members have P/E data
STALE_MAX_DAYS = 6         # per-stock value may be at most 6 calendar days old

CNINDEX_SEARCH = "https://www.cnindex.com.cn/index/search"
CNINDEX_ADJ = "http://www.cnindex.com.cn/sample-detail/download-adjustment"
CNINDEX_CONS = "http://www.cnindex.com.cn/sample-detail/download-history"


# --------------------------------------------------------------------------- #
# Fetchers (idempotent, cached on disk)
# --------------------------------------------------------------------------- #
def _read_excel_bytes(content: bytes) -> pd.DataFrame:
    return pd.read_excel(io.BytesIO(content))


def fetch_constituents(code: str, cache_dir: str) -> pd.DataFrame:
    f = os.path.join(cache_dir, f"cons_{code}.csv")
    r = requests.get(CNINDEX_CONS, params={"indexcode": code}, headers=UA, timeout=60)
    df = _read_excel_bytes(r.content)
    df["样本代码"] = df["样本代码"].astype(str).str.zfill(6)
    df.to_csv(f, index=False)
    return df


def fetch_adjustments(code: str, cache_dir: str) -> pd.DataFrame:
    f = os.path.join(cache_dir, f"adj_{code}.csv")
    r = requests.get(CNINDEX_ADJ, params={"indexcode": code}, headers=UA, timeout=60)
    df = _read_excel_bytes(r.content)
    df["样本代码"] = df["样本代码"].astype(str).str.zfill(6)
    df.to_csv(f, index=False)
    return df


def fetch_official_meta(code: str) -> dict:
    """Current peDynamic / peStatic / totalMarketValue from cnindex."""
    for attempt in range(3):
        try:
            r = requests.post(
                CNINDEX_SEARCH, data={"content": code, "rows": 5, "pageNum": 1},
                headers={**UA, "Referer": "https://www.cnindex.com.cn/module/index-series.html",
                         "X-Requested-With": "XMLHttpRequest"}, timeout=20)
            rows = ((r.json() or {}).get("data") or {}).get("rows") or []
            for row in rows:
                if str(row.get("indexcode", "")).strip() == code:
                    return {"name": row.get("indexname"),
                            "peDynamic": float(row.get("peDynamic") or 0),
                            "peStatic": float(row.get("peStatic") or 0),
                            "totalMV": (row.get("totalMarketValue") or 0) / 1e8,
                            "samplesize": row.get("samplesize")}
        except Exception:
            time.sleep(1 + attempt)
    return {}


def baidu_valuation(symbol: str, indicator: str, period: str = "近五年",
                    retries: int = 4) -> pd.DataFrame:
    import akshare as ak
    last = None
    for a in range(retries):
        try:
            df = ak.stock_zh_valuation_baidu(symbol=symbol, indicator=indicator, period=period)
            if len(df):
                return df
            last = "empty"
        except Exception as e:
            last = str(e)[:80]
        time.sleep(1.2 * (a + 1))
    print(f"  FAIL {symbol} {indicator}: {last}", flush=True)
    return pd.DataFrame()


def stock_universe(cache_dir: str) -> list[str]:
    stocks = set()
    for code in CODES:
        adj = pd.read_csv(os.path.join(cache_dir, f"adj_{code}.csv"), dtype={"样本代码": str})
        adj["样本代码"] = adj["样本代码"].astype(str).str.zfill(6)
        adj = adj[adj["调整类型"] != "备选"]
        stocks |= set(adj["样本代码"])
        cons = pd.read_csv(os.path.join(cache_dir, f"cons_{code}.csv"), dtype={"样本代码": str})
        stocks |= set(cons["样本代码"].astype(str).str.zfill(6))
    return sorted(s for s in stocks if s.isdigit() and len(s) == 6)


def fetch_all_stock_series(cache_dir: str, refresh: bool = False) -> None:
    """Baidu mcap + BaoStock peTTM per stock.
    Baidu's own PE series is NOT used: it carries corrupt ~1.0x placeholder
    values on days when a stock's TTM earnings are close to zero; BaoStock's
    peTTM is exact there (verified 2026-11-24: 688027 shows ~11 000 in
    BaoStock where Baidu shows 1.05)."""
    bdir = os.path.join(cache_dir, "baidu5y")
    os.makedirs(bdir, exist_ok=True)
    stocks = stock_universe(cache_dir)
    print(f"per-stock universe: {len(stocks)} stocks", flush=True)
    n = 0
    for i, stk in enumerate(stocks):
        pe_f, mc_f = (os.path.join(bdir, f"{stk}_pe.csv"),
                      os.path.join(bdir, f"{stk}_mcap.csv"))
        if (not refresh) and os.path.exists(pe_f) and os.path.exists(mc_f):
            continue
        pe = baidu_valuation(stk, "市盈率(TTM)")
        time.sleep(0.25)
        mc = baidu_valuation(stk, "总市值")
        time.sleep(0.25)
        if len(pe) and len(mc):
            pe.to_csv(pe_f, index=False)
            mc.to_csv(mc_f, index=False)
            n += 1
        if (i + 1) % 25 == 0:
            print(f"  [{i+1}/{len(stocks)}] new this run: {n}", flush=True)
    print(f"per-stock series ready (new this run: {n})", flush=True)


def fetch_all_baostock_pe(cache_dir: str) -> None:
    """Per-stock daily peTTM history from BaoStock (free, exact)."""
    import baostock as bs
    bdir = os.path.join(cache_dir, "bst")
    os.makedirs(bdir, exist_ok=True)
    stocks = stock_universe(cache_dir)
    lg = bs.login()
    if not (lg is not None and lg.error_code == "0"):
        print("BaoStock login failed", flush=True)
        return
    n = 0
    for i, stk in enumerate(stocks):
        out_f = os.path.join(bdir, f"{stk}.csv")
        if os.path.exists(out_f):
            continue
        ticker = f"sh.{stk}" if stk[0] in ("5", "6", "9") else f"sz.{stk}"
        rows = []
        try:
            rs = bs.query_history_k_data_plus(
                ticker, "date,peTTM",
                start_date="2021-11-01", end_date=datetime.now().strftime("%Y-%m-%d"),
                frequency="d", adjustflag="3")
            while rs.next():
                rows.append(rs.get_row_data())
        except Exception:
            pass
        if rows:
            df = pd.DataFrame(rows, columns=["date", "peTTM"])
            df["peTTM"] = pd.to_numeric(df["peTTM"], errors="coerce")
            df.to_csv(out_f, index=False)
            n += 1
        if (i + 1) % 40 == 0:
            print(f"  baostock [{i+1}/{len(stocks)}] new={n}", flush=True)
    bs.logout()
    print(f"BaoStock peTTM ready (new this run: {n})", flush=True)


# --------------------------------------------------------------------------- #
# Membership timeline from the adjustment file
# --------------------------------------------------------------------------- #
def membership_intervals(adj: pd.DataFrame) -> dict[str, list[tuple]]:
    """
    {stock: [(start, end), ...]} - valid membership intervals.

    Rows: one per review window per stock.
      'OLD' / '+' rows: the stock IS a member over [开始日期, 结束日期].
      '-' rows: the stock was REMOVED at the review of 开始日期 (the row is
      bracketed with the *following* window, but membership ended before it).
    A removal date that falls inside an OLD/+ bracket (interim special
    reviews) truncates that bracket.
    Stocks carried over at the first review in the file ('OLD') are extended
    backwards to the start of the history window (backcast of the
    pre-publication period).
    """
    adj = adj[adj["调整类型"] != "备选"].copy()
    adj["开始日期"] = pd.to_datetime(adj["开始日期"])
    adj["结束日期"] = pd.to_datetime(adj["结束日期"])
    first_start = adj["开始日期"].min()
    out: dict[str, list[tuple]] = {}
    for stk, g in adj.groupby("样本代码"):
        removals = sorted(set(g.loc[g["调整类型"] == "-", "开始日期"]))
        member_rows = g[g["调整类型"].isin(["OLD", "+"])].sort_values("开始日期")
        ivs, first_kind = [], None
        for _, r in member_rows.iterrows():
            s, e = r["开始日期"], r["结束日期"]
            if first_kind is None:
                first_kind = r["调整类型"]
            for rm in removals:
                if s < rm <= e:          # interim review removed it mid-bracket
                    e = rm - pd.Timedelta(days=1)
                    break
            if s <= e:
                ivs.append((s, e))
        if first_kind == "OLD" and ivs:
            s0, e0 = ivs[0]
            out[stk] = [(pd.Timestamp("1900-01-01"), e0)] + ivs[1:]
        else:
            out[stk] = ivs
    return out


def membership_frame(grid: pd.DatetimeIndex, intervals: dict) -> pd.DataFrame:
    """bool DataFrame (grid x stocks)."""
    cols = {}
    for stk, ivs in intervals.items():
        col = pd.Series(False, index=grid)
        for s, e in ivs:
            col[(grid >= s) & (grid <= e)] = True
        cols[stk] = col
    return pd.DataFrame(cols, index=grid)


# --------------------------------------------------------------------------- #
# Per-stock series aligned to the trading grid (as-of, freshness-limited)
# --------------------------------------------------------------------------- #
def load_stock_series(cache_dir: str, stocks: list[str], grid: pd.DatetimeIndex):
    """PE: BaoStock daily peTTM (exact). MCAP: Baidu 总市值 series."""
    bdir = os.path.join(cache_dir, "baidu5y")
    bstdir = os.path.join(cache_dir, "bst")
    pe_d, mc_d = {}, {}
    for stk in stocks:
        # ---- market cap: baidu (2-day grid, weekend-filled) ----
        f = os.path.join(bdir, f"{stk}_mcap.csv")
        if os.path.exists(f):
            df = pd.read_csv(f, parse_dates=["date"])
            s = pd.Series(pd.to_numeric(df["value"], errors="coerce").values,
                          index=pd.DatetimeIndex(df["date"]))
            s = s[~s.index.duplicated(keep="last")].sort_index()
        else:
            s = pd.Series(dtype=float)
        if len(s) == 0:
            mc_d[stk] = pd.Series(index=grid, dtype=float)
        else:
            union = grid.union(s.index)
            vals = s.reindex(union).ffill().reindex(grid)
            obs = pd.Series(s.index, index=s.index).reindex(union).ffill().reindex(grid)
            age = (grid.to_series() - obs).dt.days
            mc_d[stk] = vals.where(age <= STALE_MAX_DAYS)
        # ---- P/E: baostock peTTM (true daily) ----
        f = os.path.join(bstdir, f"{stk}.csv")
        if os.path.exists(f):
            df = pd.read_csv(f, parse_dates=["date"])
            s = pd.Series(pd.to_numeric(df["peTTM"], errors="coerce").values,
                          index=pd.DatetimeIndex(df["date"]))
            s = s[~s.index.duplicated(keep="last")].sort_index()
        else:
            s = pd.Series(dtype=float)
        if len(s) == 0:
            pe_d[stk] = pd.Series(index=grid, dtype=float)
        else:
            union = grid.union(s.index)
            vals = s.reindex(union).ffill().reindex(grid)
            obs = pd.Series(s.index, index=s.index).reindex(union).ffill().reindex(grid)
            age = (grid.to_series() - obs).dt.days
            pe_d[stk] = vals.where(age <= STALE_MAX_DAYS)
    return (pd.DataFrame(pe_d, index=grid), pd.DataFrame(mc_d, index=grid))


# --------------------------------------------------------------------------- #
# Aggregation
# --------------------------------------------------------------------------- #
def aggregate(pe_row, mc_row, convention: str):
    """pe_row/mc_row: Series over the day's members. Returns (pe, n_ok)."""
    m = mc_row.dropna()
    p = pe_row.reindex(m.index)
    ok = m.index[p.notna() & (p != 0)]
    if len(ok) == 0:
        return None, 0
    mm = m.reindex(ok)
    pp = p.reindex(ok)
    if convention == "all":                      # losses included (Wind 整体法 style)
        inv_sum = (mm / pp).sum()
        tot_m = m.sum()
    elif convention == "pos":                   # loss-makers excluded entirely
        keep = pp > 0
        mm, pp = mm[keep], pp[keep]
        if not len(mm):
            return None, 0
        inv_sum = (mm / pp).sum()
        tot_m = mm.sum()
    elif convention == "zero":                  # loss-makers' earnings = 0, mcap kept
        inv_sum = (mm / pp).clip(lower=0).sum()
        tot_m = m.sum()
    else:
        raise ValueError(convention)
    if inv_sum is None or inv_sum <= 0:
        return None, len(ok)
    return tot_m / inv_sum, len(ok)


def scale_factors(cons: pd.DataFrame, cache_dir: str) -> dict[str, float]:
    """cnindex mcap / baidu mcap on the snapshot date (A-share anchoring)."""
    bdir = os.path.join(cache_dir, "baidu5y")
    mcap_col = [c for c in cons.columns if "总市值" in c][0]
    snap_date = pd.to_datetime(cons["日期"].iloc[0])
    out = {}
    for _, row in cons.iterrows():
        stk = str(row["样本代码"]).zfill(6)
        f = os.path.join(bdir, f"{stk}_mcap.csv")
        if not os.path.exists(f):
            continue
        df = pd.read_csv(f, parse_dates=["date"])
        cn = pd.to_numeric(row[mcap_col], errors="coerce")
        if not cn or cn <= 0:
            continue
        s = df[df["date"] <= snap_date]
        if len(s):
            bd = float(s["value"].iloc[-1])
            if bd > 0:
                out[stk] = float(cn) / bd
    return out


# --------------------------------------------------------------------------- #
# Main pipeline
# --------------------------------------------------------------------------- #
def run(cache_dir: str, out_csv: str, validate_only: bool = False,
        convention: str | None = None, window_years: int = MEDIAN_YEARS):
    # --- inputs ---
    cons_all, adj_all, meta = {}, {}, {}
    for code in CODES:
        cons_all[code] = fetch_constituents(code, cache_dir)
        adj_all[code] = fetch_adjustments(code, cache_dir)
        meta[code] = fetch_official_meta(code)
        print(f"{code} {meta[code].get('name')}: peDynamic={meta[code].get('peDynamic')} "
              f"samplesize={meta[code].get('samplesize')}", flush=True)
    fetch_all_stock_series(cache_dir)
    fetch_all_baostock_pe(cache_dir)

    today = datetime.now().date()
    window_start = pd.Timestamp(today) - pd.Timedelta(days=365 * window_years)
    tdays_f = os.path.join(cache_dir, "trading_days.txt")
    if os.path.exists(tdays_f):
        tdays = [pd.Timestamp(x) for x in open(tdays_f).read().split()]
    else:
        import baostock as bs
        lg = bs.login()
        rs = bs.query_trade_dates(
            start_date=(window_start - timedelta(days=10)).strftime("%Y-%m-%d"),
            end_date=today.strftime("%Y-%m-%d"))
        rows = []
        while rs.next():
            rows.append(rs.get_row_data())
        df = pd.DataFrame(rows, columns=rs.fields)
        tdays = [pd.Timestamp(x) for x in df[df["is_trading_day"] == "1"]["calendar_date"]]
        open(tdays_f, "w").write("\n".join(str(x.date()) for x in tdays))
        bs.logout()
    grid = pd.DatetimeIndex([d for d in tdays if window_start <= d <= pd.Timestamp(today)])
    print(f"window: {grid[0].date()} -> {grid[-1].date()} ({len(grid)} trading days)", flush=True)

    # --- per-stock aligned series ---
    stocks = stock_universe(cache_dir)
    pe_df, mc_df = load_stock_series(cache_dir, stocks, grid)

    # --- membership frames ---
    member_frames = {code: membership_frame(grid, membership_intervals(adj_all[code]))
                     for code in CODES}
    for code in CODES:
        mf = member_frames[code]
        print(f"{code}: members on grid: first day {int(mf.iloc[0].sum())}, "
              f"last day {int(mf.iloc[-1].sum())}", flush=True)

    # --- scales (A-share anchoring) ---
    scales = {code: scale_factors(cons_all[code], cache_dir) for code in CODES}
    for code in CODES:
        big = {k: round(v, 3) for k, v in scales[code].items() if abs(v - 1) > 0.2}
        print(f"{code}: {len(scales[code])} scale factors, |dev|>20%: {big}", flush=True)

    # ---------------- validation: lock the convention ----------------
    latest = grid[-1]
    print("\n=== convention validation (latest day) ===", flush=True)
    val_rows = []
    for conv in ["all", "pos", "zero"]:
        errs = []
        for code in CODES:
            mf = member_frames[code].loc[latest]
            members = list(mf[mf].index)
            sc = scales[code]
            pe_row = pe_df.loc[latest].reindex(members)
            mc_row = mc_df.loc[latest].reindex(members)
            mc_row = mc_row * pd.Series({s: sc.get(s, 1.0) for s in members})
            pe_v, n_ok = aggregate(pe_row, mc_row, conv)
            off = meta[code].get("peDynamic") or 0
            tot_m = mc_row.dropna().sum()
            if pe_v and off:
                errs.append(pe_v / off - 1)
            val_rows.append((code, conv, pe_v, off, tot_m, meta[code].get("totalMV")))
        mean_err = sum(errs) / len(errs) if errs else float("nan")
        print(f"  convention {conv!r}: mean rel. err = {mean_err*100:+.1f}%", flush=True)
    for r in val_rows:
        print(f"    {r[0]} {r[1]}: computed={r[2] and round(r[2], 2)} "
              f"official={r[3] and round(r[3], 2)} | Σmcap={r[4] and round(r[4])}亿 "
              f"official={r[5] and round(r[5])}亿", flush=True)
    if validate_only:
        return
    if convention is None:
        convention = "pos"          # locked by the validation of 2026-11-24
        # (cnindex PE excludes loss-makers; mean err +0.8%)

    # ---------------- full history ----------------
    print(f"\n=== full history, convention {convention!r} ===", flush=True)
    out_rows, summary = [], {}
    for code in CODES:
        mf = member_frames[code]
        sc = scales[code]
        mcs = mc_df.copy()
        for stk, f_ in sc.items():
            if stk in mcs.columns:
                mcs[stk] = mcs[stk] * f_
        pe_list = []
        for d in grid:
            members = list(mf.columns[mf.loc[d]])
            if not members:
                continue
            pe_v, n_ok = aggregate(pe_df.loc[d].reindex(members),
                                   mcs.loc[d].reindex(members), convention)
            n_members = len(members)
            if pe_v is not None and n_ok >= COVERAGE_MIN * n_members:
                pe_list.append((d, pe_v, n_ok, n_members))
        hist = pd.DataFrame(pe_list, columns=["date", "pe", "n_ok", "n_members"])
        med = float(hist["pe"].median())
        cur = float(hist["pe"].iloc[-1])
        pct_rank = float((hist["pe"] < meta[code]["peDynamic"]).mean())
        summary[code] = {"name": meta[code].get("name"),
                         "median": round(med, 2),
                         "n_obs": int(len(hist)),
                         "first": str(hist["date"].iloc[0].date()),
                         "last": str(hist["date"].iloc[-1].date()),
                         "computed_now": round(cur, 2),
                         "official_now": round(meta[code]["peDynamic"], 2),
                         "official_percentile_in_history": round(pct_rank, 3)}
        print(f"  {code} {meta[code].get('name')}: median={med:.2f} obs={len(hist)} "
              f"({hist['date'].iloc[0].date()}..{hist['date'].iloc[-1].date()}) "
              f"computed_now={cur:.2f} official_now={meta[code]['peDynamic']:.2f} "
              f"percentile_of_official={pct_rank:.1%}", flush=True)
        hist.insert(1, "code", code)
        out_rows.append(hist)
    full = pd.concat(out_rows, ignore_index=True)
    os.makedirs(os.path.dirname(out_csv) or ".", exist_ok=True)
    full.to_csv(out_csv, index=False)
    with open(os.path.join(cache_dir, "cni_summary.json"), "w") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2)
    print(f"\nsaved {out_csv} ({len(full)} rows) and cni_summary.json", flush=True)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache-dir", default=".cni_cache")
    ap.add_argument("--out", default="data/cni_pe_history.csv")
    ap.add_argument("--validate", action="store_true")
    ap.add_argument("--convention", default=None, choices=["all", "pos", "zero"])
    ap.add_argument("--refresh-stocks", action="store_true")
    args = ap.parse_args()
    run(args.cache_dir, args.out, validate_only=args.validate, convention=args.convention)
