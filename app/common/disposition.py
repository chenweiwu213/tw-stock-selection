"""
處置股共用模組：抓取上市(資料來源：證交所)與上櫃(資料來源：櫃買中心)處置股清單並寫入 disposition_stocks，供各分頁K線圖下拉選單使用。
"""

import datetime
import re
import pandas as pd
import requests
import streamlit as st
from sqlalchemy import text

from app.common.constants import STOCK_ID_RE, today_tw
from app.common.db import get_engine
from app.common.http import get_relaxed_session


def _parse_roc_date(s: str) -> datetime.date:
    """把民國年日期字串(如 '115/08/27')轉成西元日期"""
    y, m, d = s.strip().split("/")
    return datetime.date(int(y) + 1911, int(m), int(d))


def _strip_html_tags(s: str) -> str:
    """去除欄位值中可能混入的雜訊：HTML標記(例如 <a href=...> 連結)，
    以及官方資料常見的「名稱(相對路徑或網址)」附註寫法，只保留股票名稱本身"""
    s = re.sub(r"<[^>]+>", "", s)
    # 櫃買中心的欄位會用括號夾帶連結路徑，e.g. "富喬(../../mainboard/...)"，
    # 修正方式：取括號前的部分
    s = re.split(r"[((]", s, maxsplit=1)[0]
    return s.strip()


def _parse_disposition_period(period_str: str):
    """把『115/08/27～115/09/02』這種民國年起訖字串，解析成 (start_date, end_date)"""
    start_str, end_str = re.split(r"[～~]", period_str.strip())
    return _parse_roc_date(start_str), _parse_roc_date(end_str)


def fetch_disposition_twse() -> pd.DataFrame:
    """從證交所下載目前公布中的處置有價證券清單(上市，不需帶日期參數，預設即含明日起生效者)"""
    # 只記錄 stock_id / stock_name / 起訖日期，不記錄處置原因或措施文字
    url = "https://www.twse.com.tw/rwd/zh/announcement/punish"
    resp = requests.get(
        url, params={"response": "json"}, timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    payload = resp.json()
    fields = payload.get("fields") or []
    rows = payload.get("data") or []
    if not fields or not rows:
        return pd.DataFrame(columns=["market", "stock_id", "stock_name", "start_date", "end_date"])

    idx = {name: i for i, name in enumerate(fields)}
    out = []
    for row in rows:
        stock_id = str(row[idx["證券代號"]]).strip()
        if not re.fullmatch(STOCK_ID_RE, stock_id):
            continue
        stock_name = _strip_html_tags(str(row[idx["證券名稱"]]))[:50]
        try:
            start_date, end_date = _parse_disposition_period(str(row[idx["處置起迄時間"]]))
        except Exception:
            continue
        out.append({
            "market": "TWSE", "stock_id": stock_id, "stock_name": stock_name,
            "start_date": start_date, "end_date": end_date,
        })
    return pd.DataFrame(out)


def fetch_disposition_tpex() -> pd.DataFrame:
    """從櫃買中心下載上櫃處置股清單(查詢區間固定過去14天～明天，確保涵蓋目前有效與明日起生效者)"""
    today = today_tw()
    start = today - datetime.timedelta(days=14)
    end = today + datetime.timedelta(days=1)
    url = "https://www.tpex.org.tw/www/zh-tw/bulletin/disposal"
    params = {
        "startDate": start.strftime("%Y/%m/%d"),
        "endDate": end.strftime("%Y/%m/%d"),
        "response": "json",
    }
    session = get_relaxed_session()
    resp = session.get(url, params=params, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
    resp.raise_for_status()
    payload = resp.json()

    # 欄位/資料可能包在 tables[0] 底下(舊格式)，也可能直接放在最外層(改版後可能變成
    # 跟證交所一樣的 fields/data 結構)；兩種都嘗試，取得到的先用
    tables = payload.get("tables") or []
    if tables and tables[0].get("data"):
        fields = tables[0]["fields"]
        rows = tables[0]["data"]
    elif payload.get("data") and payload.get("fields"):
        fields = payload["fields"]
        rows = payload["data"]
    else:
        return pd.DataFrame(columns=["market", "stock_id", "stock_name", "start_date", "end_date"])

    # 欄位用關鍵字比對而非寫死順序，若官方欄位改版導致比對失敗會丟出例外(內含實際欄位清單)，方便及早發現並校正
    def find_col(*keywords):
        for i, name in enumerate(fields):
            if any(k in name for k in keywords):
                return i
        return None

    id_idx = find_col("代號")
    name_idx = find_col("名稱")
    # 起迄／起訖／期間 都是官方可能用過的寫法，一起比對
    period_idx = find_col("起迄", "起訖", "期間")
    # 若沒有單一「起迄」合併欄位，改找拆成兩欄的起始日／結束日
    start_idx = find_col("起始", "開始", "生效")
    end_idx = find_col("迄止", "結束", "終止")

    if id_idx is None or (period_idx is None and (start_idx is None or end_idx is None)):
        raise ValueError(
            "櫃買中心處置股回傳格式已變動，欄位對應失敗，回報以便校正。"
            f"目前回傳的欄位為：{fields}"
        )

    out = []
    for row in rows:
        stock_id = str(row[id_idx]).strip()
        if not re.fullmatch(STOCK_ID_RE, stock_id):
            continue
        stock_name = _strip_html_tags(str(row[name_idx]))[:50] if name_idx is not None else ""
        try:
            if period_idx is not None:
                start_date, end_date = _parse_disposition_period(str(row[period_idx]))
            else:
                start_date = _parse_roc_date(str(row[start_idx]))
                end_date = _parse_roc_date(str(row[end_idx]))
        except Exception:
            continue
        out.append({
            "market": "TPEx", "stock_id": stock_id, "stock_name": stock_name,
            "start_date": start_date, "end_date": end_date,
        })
    return pd.DataFrame(out)


def upsert_disposition(df: pd.DataFrame, db_engine=None) -> int:
    """把處置股 DataFrame upsert 進 disposition_stocks(PK為 market/stock_id/start_date，同一檔股票不同批次處置期間分開存，供K線圖正確標示每段區間)"""
    db_engine = db_engine or get_engine()
    if df.empty:
        return 0
    with db_engine.begin() as conn:
        df.to_sql("_staging_disposition", conn, if_exists="replace", index=False)
        result = conn.execute(text("""
            INSERT INTO disposition_stocks (market, stock_id, stock_name, start_date, end_date)
            SELECT market, stock_id, stock_name, start_date, end_date FROM _staging_disposition
            ON CONFLICT (market, stock_id, start_date) DO UPDATE SET
                stock_name = EXCLUDED.stock_name,
                end_date = EXCLUDED.end_date
        """))
        conn.execute(text("DROP TABLE _staging_disposition"))
    return result.rowcount


@st.cache_data(ttl=300)
def load_disposition_map() -> dict:
    """讀取目前仍相關(尚未結束)的處置股區間，回傳 {stock_id: [(start_date, end_date), ...]}
    (不分市場合併查，因為上市／上櫃股票代號不會重複)"""
    engine = get_engine()
    today = today_tw()
    df = pd.read_sql(
        text(
            "SELECT stock_id, start_date, end_date FROM disposition_stocks "
            "WHERE end_date >= :today ORDER BY stock_id, start_date"
        ),
        engine, params={"today": today},
    )
    result: dict = {}
    for row in df.itertuples(index=False):
        result.setdefault(row.stock_id, []).append((row.start_date, row.end_date))
    return result


def _disposition_label_suffix(stock_id: str, disposition_map: dict, today: datetime.date) -> str:
    for start, end in disposition_map.get(stock_id, []):
        if start <= today <= end:
            return " 🔴處置中"
        if start == today + datetime.timedelta(days=1):
            return " 🟠明日起處置"
        if start > today:
            return f" 🟠{start.month}/{start.day}起處置"
    return ""


def build_disposition_stock_options(stock_ids, disposition_map: dict, id_to_name: dict | None = None):
    """回傳 (顯示用 label 清單, {label: stock_id})；label 固定包含股票代號＋名稱(若有提供對照表)，
    處置中或即將處置的股票，label 後面會加註記(含明日起處置的特別標示)"""
    today = today_tw()
    id_to_name = id_to_name or {}
    labels = []
    label_to_id = {}
    for sid in stock_ids:
        name = id_to_name.get(sid, "")
        base = f"{sid} {name}" if name else sid
        label = f"{base}{_disposition_label_suffix(sid, disposition_map, today)}"
        labels.append(label)
        label_to_id[label] = sid
    return labels, label_to_id