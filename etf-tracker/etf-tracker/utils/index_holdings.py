"""
Top-10 index constituents (weights, English names, one-line Russian
descriptions) for the section under the P/E table.

Data sources (all free, all verified 2026-11-06):
- CSI indices (000300, 931071, H30015, 000852): official month-end weight
  file of csindex.com.cn (XLS; includes the official English names).
- CNI indices (980017, 980022, 980018): official sample file of
  cnindex.com.cn (XLSX; Chinese names + industry + weights).
- Hong Kong indices: hsi.com.hk refuses plain HTTP requests (403), so the
  top-10 of the tracker ETFs (513180 / 159545 / 159892) is used instead -
  the ACTUAL ETF portfolios from their quarterly reports (Eastmoney F10),
  the same ETFs the P/E table's HK rows are built from.

English names for CNI/HK stocks and Russian descriptions are curated
dictionaries keyed by stock code (codes are stable identifiers). Companies
that enter the top-10 after a rebalancing show their name and weight, but
"—" as description until the dictionaries are updated.

Refresh policy: once a month per index is enough (CSI/CNI weight files are
month-end, ETF reports are quarterly; a monthly check catches both).
Every snapshot is appended to the `holdings_cache` tab of the Google Sheet
(append-only, like pe_cache/index_price_cache; no other tab is ever
written). If the live fetch fails, the last cached snapshot is served.
"""
from __future__ import annotations

import io
import re
import time
from datetime import datetime
from typing import Optional

import pandas as pd
import requests
import streamlit as st

from utils import sheets_db
from utils.index_valuation import INDEX_PE_CONFIG  # same 10 indices + labels

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
}

CACHE_TAB = "holdings_cache"
REFRESH_AFTER_DAYS = 31          # refetch at most once a month
TOP_N = 10

# --------------------------------------------------------------------------- #
# Where the top-10 of each index comes from (keys/labels live in
# index_valuation.INDEX_PE_CONFIG so both tables always stay in sync)
# --------------------------------------------------------------------------- #
HOLDINGS_SOURCES = {
    "csi300":  {"source": "csindex", "code": "000300"},
    "semi":    {"source": "cnindex", "code": "980017"},
    "ai":      {"source": "csindex", "code": "931071"},
    "robot":   {"source": "cnindex", "code": "980022"},
    "sat":     {"source": "cnindex", "code": "980018"},
    "autos":   {"source": "csindex", "code": "H30015"},
    "hk_div":  {"source": "hk_etf",  "etf": "159545"},
    "hk_tech": {"source": "hk_etf",  "etf": "513180"},
    "hk_bio":  {"source": "hk_etf",  "etf": "159892"},
    "csi1000": {"source": "csindex", "code": "000852"},
}

_LABEL_TO_KEY = {cfg["label"]: cfg["key"] for cfg in INDEX_PE_CONFIG}
_KEY_TO_LABEL = {cfg["key"]: cfg["label"] for cfg in INDEX_PE_CONFIG}


def get_index_labels() -> list[str]:
    """Russian labels of the 10 indices, in the same order as the P/E table."""
    return [cfg["label"] for cfg in INDEX_PE_CONFIG]


def get_key_for_label(label: str) -> Optional[str]:
    return _LABEL_TO_KEY.get(label)


# --------------------------------------------------------------------------- #
# Curated English names (CNI + HK stocks; CSI files carry their own)
# --------------------------------------------------------------------------- #
EN_NAMES = {
    # --- CNI Semiconductor (980017) ---
    "688256": "Cambricon Technologies",
    "002371": "NAURA Technology Group",
    "688981": "SMIC",
    "688008": "Montage Technology",
    "603986": "GigaDevice Semiconductor",
    "688041": "Hygon Information Technology",
    "688012": "AMEC",
    "300604": "Hangzhou Changchuan Technology",
    "688072": "Piotech",
    "600584": "JCET Group",
    # --- CNI Robot (980022) ---
    "002472": "Zhejiang Shuanghuan Driveline",
    "002600": "Lingyi iTech",
    "603119": "Zhejiang Rongtai Technology",
    "300024": "Siasun Robot & Automation",
    "002747": "Estun Automation",
    "688017": "Leaderdrive",
    "300953": "Zhenyu Technology",
    "688169": "Roborock",
    "300503": "Haozhi Electric Machinery",
    "002230": "iFlytek",
    "002415": "Hikvision",
    # --- CNI Commercial Satellite (980018) ---
    "688002": "Raytron Technology",
    "600118": "China Spacesat",
    "600879": "Aerospace Electronics Technology",
    "688385": "Shanghai Fudan Microelectronics",
    "603236": "Quectel",
    "300762": "Shanghai Hanxun Wireless Technology",
    "688387": "CICT Mobile Communications",
    "601698": "China Satcom",
    "002465": "Haige Communications",
    # --- Hang Seng High Dividend Low Volatility (via ETF 159545) ---
    "00384": "China Gas Holdings",
    "00152": "Shenzhen International Holdings",
    "03311": "China State Construction International",
    "01368": "Xtep International",
    "00288": "WH Group",
    "02666": "Genertec Universal Medical Group",
    "03360": "Far East Horizon",
    "00390": "China Railway Group",
    "00762": "China Unicom",
    "03877": "CSSC (Hong Kong) Shipping",
    # --- Hang Seng TECH (via ETF 513180) ---
    "09999": "NetEase",
    "00700": "Tencent Holdings",
    "09988": "Alibaba Group",
    "03690": "Meituan",
    "00992": "Lenovo Group",
    "01810": "Xiaomi Corporation",
    "00981": "SMIC",
    "01211": "BYD Company",
    "09618": "JD.com",
    "09888": "Baidu",
    # --- Hang Seng Biotech (via ETF 159892) ---
    "02269": "WuXi Biologics",
    "06160": "BeiGene",
    "01801": "Innovent Biologics",
    "02359": "WuXi AppTec",
    "01093": "CSPC Pharmaceutical Group",
    "09926": "Akeso",
    "01548": "GenScript Biotech",
    "01177": "Sino Biopharmaceutical",
    "03692": "Hansoh Pharmaceutical",
    "06990": "Kelun-Biotech",
    # --- CSI indices: shorter/cleaner display forms (the official files
    #     sometimes concatenate or shout the names, e.g. ZHEJIANGCFMOTOPOWERCO.) ---
    "600519": "Kweichow Moutai",
    "603259": "WuXi AppTec",
    "300308": "Zhongji Innolight",
    "300750": "CATL",
    "601318": "Ping An Insurance",
    "600036": "China Merchants Bank",
    "601899": "Zijin Mining",
    "000333": "Midea Group",
    "600900": "China Yangtze Power",
    "601166": "Industrial Bank",
    "603019": "Sugon (Dawning Information)",
    "000977": "Inspur Information",
    "300502": "Eoptolink Technology",
    "300857": "Sharetronic Data Technology",
    "688111": "Kingsoft Office",
    "300454": "Sangfor Technologies",
    "601360": "Qihoo 360",
    "002594": "BYD",
    "600660": "Fuyao Glass",
    "600104": "SAIC Motor",
    "600066": "Yutong Bus",
    "601689": "Tuopu Group",
    "000625": "Changan Automobile",
    "601127": "Seres Group",
    "601058": "Sailun Group",
    "603129": "Zhejiang CFMOTO Power",
}


# --------------------------------------------------------------------------- #
# One-line Russian descriptions (curated; keyed by stock code)
# --------------------------------------------------------------------------- #
DESCRIPTIONS_RU = {
    # --- CSI 300 ---
    "600519": "Крупнейший в Китае производитель элитного крепкого напитка байцзю.",
    "300308": "Ведущий производитель оптических модулей для дата-центров и ИИ-сетей.",
    "300750": "Крупнейший в мире производитель аккумуляторов для электромобилей.",
    "601318": "Один из крупнейших страховых и финансовых конгломератов Китая.",
    "600036": "Ведущий банк Китая с фокусом на розничный сегмент.",
    "601899": "Один из крупнейших производителей золота и меди в Китае.",
    "000333": "Крупнейший в мире производитель бытовой техники.",
    "600900": "Крупнейший в мире оператор гидроэлектростанций (река Янцзы).",
    "603259": "Контрактная разработка и производство лекарств для фармацевтики.",
    "601166": "Крупный универсальный банк со штаб-квартирой в Фуцзяне.",
    # --- CSI AI (931071) ---
    "603019": "Производитель высокопроизводительных серверов и суперкомпьютеров.",
    "000977": "Один из крупнейших китайских производителей серверов.",
    "002230": "Лидер Китая в распознавании речи и ИИ-платформах.",
    "300502": "Производитель оптических трансиверов для дата-центров.",
    "300857": "Производитель устройств хранения данных и умной электроники.",
    "002415": "Крупнейший в мире производитель систем видеонаблюдения.",
    "688111": "Разработчик офисного пакета WPS Office.",
    "300454": "Разработчик решений кибербезопасности и частных облаков.",
    "601360": "Разработчик антивирусных и браузерных продуктов.",
    # --- CNI Semiconductor (980017) ---
    "688256": "Разработчик чипов для искусственного интеллекта.",
    "002371": "Крупнейший в Китае производитель полупроводникового оборудования.",
    "688981": "Крупнейший контрактный производитель чипов в материковом Китае.",
    "688008": "Разработчик чипов памяти и интерфейсных микросхем.",
    "603986": "Производитель флеш-памяти и микроконтроллеров.",
    "688041": "Разработчик серверных процессоров архитектуры x86.",
    "688012": "Производитель оборудования для травления полупроводников.",
    "300604": "Производитель тестового оборудования для микросхем.",
    "688072": "Производитель оборудования осаждения плёнок для чипов.",
    "600584": "Один из крупнейших в мире сборщиков и тестировщиков чипов.",
    # --- CNI Robot (980022) ---
    "002472": "Производитель шестерён и редукторов для роботов и автомобилей.",
    "002600": "Производитель прецизионных деталей для электроники.",
    "603119": "Производитель изоляционных материалов для батарей электромобилей.",
    "300024": "Один из пионеров промышленной робототехники в Китае.",
    "002747": "Ведущий китайский производитель промышленных роботов.",
    "688017": "Производитель гармонических редукторов для роботов.",
    "300953": "Производитель сердечников электродвигателей для электромобилей.",
    "688169": "Производитель роботов-пылесосов.",
    "300503": "Производитель шпинделей для станков и компонентов роботов.",
    # --- CNI Commercial Satellite (980018) ---
    "688002": "Производитель инфракрасных матриц и тепловизоров.",
    "600118": "Главный китайский производитель малых спутников.",
    "600879": "Производитель электроники для ракет и спутников.",
    "688385": "Разработчик FPGA и чипов безопасности.",
    "603236": "Крупнейший в мире производитель сотовых модулей IoT.",
    "300762": "Разработчик широкополосной связи спецназначения.",
    "688387": "Разработчик оборудования мобильных сетей 5G.",
    "601698": "Оператор спутниковой связи Китая на геостационарной орбите.",
    "002465": "Производитель навигационной аппаратуры BeiDou и спецсвязи.",
    # --- CSI 800 Autos (H30015) ---
    "002594": "Крупнейший в мире производитель электромобилей.",
    "600660": "Крупнейший в мире производитель автомобильного стекла.",
    "600104": "Крупнейший автомобилестроительный концерн Китая.",
    "600066": "Крупнейший производитель автобусов в Китае.",
    "601689": "Производитель автокомпонентов и систем для электромобилей.",
    "000625": "Один из крупнейших автопроизводителей Китая.",
    "601127": "Производитель электромобилей (бренд AITO совместно с Huawei).",
    "601058": "Производитель шин.",
    "603129": "Производитель квадроциклов, мотоциклов и снегоходов.",
    # --- CSI 1000 (000852) ---
    "002428": "Вертикально интегрированный производитель германия.",
    "000636": "Производитель пассивных электронных компонентов.",
    "601208": "Производитель изоляционных плёночных материалов.",
    "688536": "Разработчик высокоточных аналоговых микросхем.",
    "688766": "Производитель чипов памяти NOR Flash и EEPROM.",
    "688110": "Разработчик чипов памяти малой ёмкости.",
    "688519": "Производитель стеклотекстолита для печатных плат.",
    "600330": "Производитель магнитных материалов и сапфировых подложек.",
    "688025": "Производитель лазеров и лазерного оборудования.",
    "301550": "Производитель подшипников и электроприводов.",
    # --- Hang Seng High Dividend Low Volatility (159545) ---
    "00384": "Крупнейший дистрибьютор природного газа в Китае.",
    "00152": "Оператор логистической инфраструктуры и скоростных дорог.",
    "03311": "Подрядчик инфраструктурного строительства.",
    "01368": "Производитель спортивной обуви и одежды.",
    "00288": "Крупнейший в мире производитель свинины.",
    "02666": "Оператор сети больниц и медицинский лизинг.",
    "03360": "Лизинговая компания и оператор больниц.",
    "00390": "Один из крупнейших строительных подрядчиков мира.",
    "00762": "Один из трёх операторов мобильной связи Китая.",
    "03877": "Судовой лизинг под контролем госкомпании CSSC.",
    # --- Hang Seng TECH (513180) ---
    "09999": "Крупный разработчик видеоигр и интернет-сервисов.",
    "00700": "Крупнейшая интернет-компания Китая (WeChat, игры, облако).",
    "09988": "Крупнейшая платформа электронной торговли Китая.",
    "03690": "Платформа доставки еды и локальных сервисов.",
    "00992": "Крупнейший в мире производитель ПК.",
    "01810": "Производитель смартфонов и IoT-устройств.",
    "00981": "Крупнейший контрактный производитель чипов в материковом Китае.",
    "01211": "Крупнейший в мире производитель электромобилей.",
    "09618": "Крупный интернет-ритейлер с собственной логистикой.",
    "09888": "Владелец поисковика и ИИ-сервисов (ERNIE).",
    # --- Hang Seng Biotech (159892) ---
    "02269": "Крупнейшая контрактная платформа производства биопрепаратов.",
    "06160": "Глобальная биофармкомпания с фокусом на онкологию.",
    "01801": "Биотех-компания (онкология, кардиометаболизм).",
    "02359": "Контрактная разработка и производство лекарств.",
    "01093": "Производитель дженериков и инновационных лекарств.",
    "09926": "Биотех, двойные антитела для онкологии.",
    "01548": "Синтез генов и контрактная биотех-разработка.",
    "01177": "Одна из крупнейших фармацевтических групп Китая.",
    "03692": "Инновационная фармацевтическая компания.",
    "06990": "Разработчик ADC-препаратов для онкологии.",
}


# --------------------------------------------------------------------------- #
# Small helpers
# --------------------------------------------------------------------------- #
def _get(url: str, timeout: int = 20, params: dict | None = None,
         extra_headers: dict | None = None) -> Optional[requests.Response]:
    headers = dict(UA)
    if extra_headers:
        headers.update(extra_headers)
    for attempt in range(2):
        try:
            r = requests.get(url, timeout=timeout, params=params, headers=headers)
            if r.status_code == 200:
                return r
        except Exception:
            pass
        time.sleep(1 + attempt)
    return None


def _digits(v) -> str:
    return re.sub(r"\D", "", str(v))


def _norm_a_code(v) -> str:
    d = _digits(v)
    return d.zfill(6) if d else ""


def _norm_hk_code(v) -> str:
    d = _digits(v)
    return d.zfill(5) if d else ""


def _ticker(code: str, is_hk: bool) -> str:
    if is_hk:
        return f"{code}.HK" if code else ""
    if not code:
        return ""
    if code.startswith(("60", "68", "90")):
        return f"{code}.SH"
    if code.startswith(("00", "30")):
        return f"{code}.SZ"
    return f"{code}.BJ"


def _fmt_date(v) -> str:
    """'20261030' / '2026-10-30' / datetime -> '2026-10-30'."""
    s = str(v).strip().split(" ")[0].split("T")[0].replace("-", "")
    if len(s) == 8:
        return f"{s[:4]}-{s[4:6]}-{s[6:8]}"
    return str(v).strip()[:10]


# --------------------------------------------------------------------------- #
# Source 1: CSI official month-end weight files (XLS, with English names)
# --------------------------------------------------------------------------- #
def _fetch_csindex(code: str) -> Optional[dict]:
    r = _get(f"https://oss-ch.csindex.com.cn/static/html/csindex/public/"
             f"uploads/file/autofile/closeweight/{code}closeweight.xls")
    if r is None:
        return None
    try:
        df = pd.read_excel(io.BytesIO(r.content))
        if len(df.columns) != 10:
            return None
        df = df.iloc[:, [0, 4, 5, 6, 9]]
        df.columns = ["date", "code", "cn_name", "en_name", "weight"]
        df["code"] = df["code"].map(_norm_a_code)
        df["weight"] = pd.to_numeric(df["weight"], errors="coerce")
        df = df.dropna(subset=["weight"])
        df = df.sort_values("weight", ascending=False).head(TOP_N)
        if df.empty:
            return None
        rows = [{"code": r_["code"],
                 "en_name": EN_NAMES.get(r_["code"],
                                         str(r_["en_name"]).strip() or str(r_["cn_name"]).strip()),
                 "weight": float(r_["weight"])}
                for _, r_ in df.iterrows()]
        return {"date": _fmt_date(df["date"].iloc[0]), "rows": rows}
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Source 2: CNI official sample files (XLSX, Chinese names + weights)
# --------------------------------------------------------------------------- #
def _fetch_cnindex(code: str) -> Optional[dict]:
    r = _get("https://www.cnindex.com.cn/sample-detail/download-history",
             params={"indexcode": code})
    if r is None:
        return None
    try:
        df = pd.read_excel(io.BytesIO(r.content))
        wcol = [c for c in df.columns if "权重" in str(c)]
        ccol = [c for c in df.columns if "样本代码" in str(c)]
        ncol = [c for c in df.columns if "样本简称" in str(c)]
        dcol = [c for c in df.columns if "日期" in str(c)]
        if not (wcol and ccol and ncol and dcol):
            return None
        df["_date"] = df[dcol[0]].map(_fmt_date)
        latest = df["_date"].max()
        df = df[df["_date"] == latest].copy()
        df["code"] = df[ccol[0]].map(_norm_a_code)
        df["weight"] = pd.to_numeric(df[wcol[0]], errors="coerce")
        df = df.dropna(subset=["weight"])
        df = df.sort_values("weight", ascending=False).head(TOP_N)
        if df.empty:
            return None
        rows = [{"code": r_["code"],
                 "en_name": EN_NAMES.get(r_["code"], str(r_[ncol[0]]).strip()),
                 "weight": float(r_["weight"])}
                for _, r_ in df.iterrows()]
        return {"date": latest, "rows": rows}
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Source 3: HK indices via the top-10 of their tracker ETFs (quarterly
# reports, Eastmoney F10) - hsi.com.hk blocks plain HTTP requests
# --------------------------------------------------------------------------- #
def _fetch_hk_etf(etf: str) -> Optional[dict]:
    for year in (datetime.now().year, datetime.now().year - 1):
        r = _get("https://fundf10.eastmoney.com/FundArchivesDatas.aspx",
                 params={"type": "jjcc", "code": etf, "topline": str(TOP_N),
                         "year": str(year), "month": ""},
                 extra_headers={"Referer": f"https://fundf10.eastmoney.com/ccmx_{etf}.html"})
        if r is None:
            continue
        snap = _parse_jjcc(r.text)
        if snap:
            return snap
    return None


def _parse_jjcc(text: str) -> Optional[dict]:
    """First (newest) holdings table of the FundArchivesDatas response."""
    try:
        m = re.search(r"(\d{4}-\d{2}-\d{2})", text)
        if not m:
            return None
        rows, rank_seen = [], False
        for tr in re.findall(r"<tr>(.*?)</tr>", text, re.S):
            tds = re.findall(r"<td[^>]*>(.*?)</td>", tr, re.S)
            if len(tds) < 7:
                continue
            clean = [re.sub(r"<[^>]+>", "", t).strip() for t in tds]
            try:
                rank = int(clean[0])
            except ValueError:
                continue
            if rank == 1 and rank_seen:
                break  # reached the next (older) report table
            rank_seen = True
            code = _norm_hk_code(clean[1])
            if not code:
                continue
            try:
                weight = float(clean[-3].replace("%", "").replace(",", ""))
            except (ValueError, IndexError):
                continue
            rows.append({"code": code,
                         "en_name": EN_NAMES.get(code, clean[2]),
                         "weight": weight})
            if len(rows) >= TOP_N:
                break
        if len(rows) < 3:
            return None
        return {"date": m.group(1), "rows": rows}
    except Exception:
        return None


# --------------------------------------------------------------------------- #
# Google-Sheets cache (`holdings_cache`, append-only)
# --------------------------------------------------------------------------- #
def _read_cache(index_key: str) -> Optional[dict]:
    is_hk = HOLDINGS_SOURCES.get(index_key, {}).get("source") == "hk_etf"
    try:
        df = sheets_db.read_df(CACHE_TAB)
        if df.empty:
            return None
        df = df[df["index_key"].astype(str).str.strip() == index_key]
        if df.empty:
            return None
        dates = df["date"].astype(str).str.strip()
        latest = dates.max()
        df = df[dates == latest]
        rows = []
        for _, r in df.iterrows():
            code = _norm_hk_code(r["code"]) if is_hk else _norm_a_code(r["code"])
            try:
                w = float(r["weight"])
            except (TypeError, ValueError):
                continue
            if code and w > 0:
                rows.append({"code": code, "en_name": str(r["en_name"]).strip(),
                             "weight": w})
        rows.sort(key=lambda x: x["weight"], reverse=True)
        if not rows:
            return None
        return {"date": latest, "rows": rows[:TOP_N]}
    except Exception:
        return None  # no secrets / no sheet yet -> just fetch live


def _append_cache(index_key: str, snap: dict) -> None:
    is_hk = HOLDINGS_SOURCES.get(index_key, {}).get("source") == "hk_etf"
    try:
        df = sheets_db.read_df(CACHE_TAB)
        existing: set[tuple] = set()
        if not df.empty:
            for _, r in df.iterrows():
                code = _norm_hk_code(r["code"]) if is_hk else _norm_a_code(r["code"])
                existing.add((str(r["index_key"]).strip(),
                              str(r["date"]).strip(), code))
        rows = []
        for r_ in snap["rows"]:
            if (index_key, snap["date"], r_["code"]) in existing:
                continue
            rows.append({"index_key": index_key, "date": snap["date"],
                         "code": r_["code"], "en_name": r_["en_name"],
                         "weight": f"{r_['weight']:.4f}"})
        if rows:
            sheets_db.append_rows(CACHE_TAB, rows)
    except Exception:
        pass  # cache write is best-effort; never break the table


def _fresh_enough(snap: dict) -> bool:
    try:
        d = datetime.strptime(snap["date"], "%Y-%m-%d").date()
    except Exception:
        return False
    return (datetime.now().date() - d).days <= REFRESH_AFTER_DAYS


# --------------------------------------------------------------------------- #
# Public API
# --------------------------------------------------------------------------- #
def _fetch(index_key: str) -> Optional[dict]:
    cfg = HOLDINGS_SOURCES.get(index_key)
    if not cfg:
        return None
    try:
        if cfg["source"] == "csindex":
            return _fetch_csindex(cfg["code"])
        if cfg["source"] == "cnindex":
            return _fetch_cnindex(cfg["code"])
        if cfg["source"] == "hk_etf":
            return _fetch_hk_etf(cfg["etf"])
    except Exception:
        return None
    return None


def _caption(index_key: str, date: str) -> str:
    cfg = HOLDINGS_SOURCES[index_key]
    if cfg["source"] == "csindex":
        src = f"официальный файл csindex.com.cn (веса на {date})"
    elif cfg["source"] == "cnindex":
        src = f"официальный файл cnindex.com.cn (веса на {date})"
    else:
        src = (f"топ-10 портфеля ETF {cfg['etf']} по отчёту на {date} "
               f"(hsi.com.hk не отдаёт данные по прямому запросу)")
    return (f"Веса: {src}. Названия — официальные английские, "
            f"описания — редакционные. Обновляется раз в месяц.")


@st.cache_data(ttl=21600, show_spinner=False)
def get_top10(index_key: str) -> Optional[dict]:
    """
    Returns {"df": DataFrame[№, Компания, Тикер, Вес_%, Описание],
             "date": str, "caption": str} or None.

    Serves the cached snapshot while it is younger than ~a month; otherwise
    refetches from the live source and appends the new snapshot to the
    `holdings_cache` Google-Sheets tab (append-only).
    """
    if index_key not in HOLDINGS_SOURCES:
        return None
    snap = _read_cache(index_key)
    if snap and _fresh_enough(snap):
        return _decorate(index_key, snap)

    fresh = _fetch(index_key)
    if fresh and fresh.get("rows"):
        _append_cache(index_key, fresh)
        return _decorate(index_key, fresh)

    if snap:  # stale snapshot is better than nothing
        return _decorate(index_key, snap)
    return None


def _decorate(index_key: str, snap: dict) -> dict:
    is_hk = HOLDINGS_SOURCES[index_key]["source"] == "hk_etf"
    rows = sorted(snap["rows"], key=lambda x: -x["weight"])[:TOP_N]
    data = []
    for i, r in enumerate(rows, 1):
        data.append({
            "№": i,
            "Компания": r.get("en_name") or "—",
            "Тикер": _ticker(r["code"], is_hk),
            "Вес, %": round(float(r["weight"]), 2),
            "Описание": DESCRIPTIONS_RU.get(r["code"], "—"),
        })
    df = pd.DataFrame(data, columns=["№", "Компания", "Тикер", "Вес, %", "Описание"])
    return {"df": df, "date": snap["date"], "caption": _caption(index_key, snap["date"])}
