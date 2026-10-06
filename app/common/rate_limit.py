"""
下載防呆機制共用模組：限制同一功能每日下載查詢日期數，避免資料庫爆掉，
並比對下載結果是否跟前一交易日完全相同(有時候資料尚未更新會下載到前一天的資料)。
"""

import pandas as pd

from app.common.constants import today_tw
from app.common.db import get_engine

_DOWNLOAD_DATE_TRACKER: dict = {}


def check_download_rate_limit(feature_key: str, query_date, max_dates_per_day: int = 20) -> str | None:
    """同一功能(feature_key)同一天最多下載 max_dates_per_day 個不同查詢日期，超過上限回傳警告文字，否則回傳 None 並記錄本次查詢日期"""
    today = today_tw()
    entry = _DOWNLOAD_DATE_TRACKER.get(feature_key)
    if entry is None or entry["day"] != today:
        entry = {"day": today, "dates": set()}
        _DOWNLOAD_DATE_TRACKER[feature_key] = entry

    if query_date in entry["dates"]:
        return None  # 同一查詢日期重複下載(例如重試)不算新增次數

    if len(entry["dates"]) >= max_dates_per_day:
        return (
            f"今天此功能已累計下載 {max_dates_per_day} 個不同查詢日期的資料，"
            "為避免對來源網站造成過大負擔，明天再繼續下載其餘日期的資料。"
        )

    entry["dates"].add(query_date)
    return None


def warn_if_identical_to_previous_day(
    table_name: str, id_col: str, compare_cols: list, new_df: pd.DataFrame, query_date, date_col: str = "trade_date",
) -> str | None:
    """比較本次下載資料與資料庫中前一個交易日資料是否完全相同，相同則回傳警告文字，否則(含查無前一日資料、比對出錯)回傳 None"""
    engine = get_engine()
    try:
        prev_date_row = pd.read_sql(
            f"SELECT MAX({date_col}) AS d FROM {table_name} WHERE {date_col} < %(qd)s",
            engine, params={"qd": query_date},
        )
        prev_date = prev_date_row["d"].iloc[0]
        if prev_date is None:
            return None

        prev_df = pd.read_sql(
            f"SELECT {id_col}, {', '.join(compare_cols)} FROM {table_name} WHERE {date_col} = %(pd)s",
            engine, params={"pd": prev_date},
        )
        if prev_df.empty:
            return None

        cur = new_df[[id_col] + compare_cols].copy()
        cur[id_col] = cur[id_col].astype(str)
        prev_df[id_col] = prev_df[id_col].astype(str)

        # 除了融資融券的資料表是上市+上櫃共用，前一日資料需先篩選成
        # 「這次下載市場」對應的股票代號，才能公平比對，避免誤判成不同
        prev_df = prev_df[prev_df[id_col].isin(cur[id_col])].copy()
        if len(prev_df) != len(cur):
            return None  # 股票清單跟前一天不同(有新增/下市等)，不算完全相同

        for c in compare_cols:
            cur[c] = pd.to_numeric(cur[c], errors="coerce")
            prev_df[c] = pd.to_numeric(prev_df[c], errors="coerce")

        cur = cur.sort_values(id_col).reset_index(drop=True)
        prev_df = prev_df.sort_values(id_col).reset_index(drop=True)

        if cur.equals(prev_df):
            return (
                f"本次下載({query_date})的資料跟前一個交易日({prev_date})完全相同"
                "(除日期外所有數值皆一致)，可能是來源網站尚未更新當日資料，確認後再決定是否匯入。"
            )
    except Exception:
        return None
    return None