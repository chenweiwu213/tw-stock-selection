"""
股價資料(stock_price_daily)讀取共用模組：load_price_history() 供跨分頁均線計算共用，單股K線圖用 load_price_history_single()。

快取：stock_price_daily 一天只會寫入一次最新資料，不用固定 TTL 猜測資料何時更新，
改用「版本鍵快取」——每次呼叫先用 get_latest_date() 查現況最新交易日(輕量、不快取)，
沒變就吃快取、變了就自動重查，不需要在資料寫入後手動呼叫 .clear()。
ttl=21600(6小時)僅作為異常情況下的保險網，正常情況下都是靠版本鍵觸發重查。
"""

import pandas as pd
import streamlit as st

from app.common.db import get_engine, get_latest_date

_PRICE_TABLE = "stock_price_daily"
_PRICE_DATE_COLUMN = "trade_date"


def load_price_history(lookback_days: int) -> pd.DataFrame:
    """抓最近 lookback_days 個交易日資料算均線(長期均線需要足夠區間，lookback_days 建議給大一點，例如150)
    對外介面與呼叫方式不變，內部改為版本鍵快取。"""
    version = get_latest_date(_PRICE_TABLE, _PRICE_DATE_COLUMN)
    return _load_price_history_cached(lookback_days, version)


def load_price_history_single(stock_id: str, lookback_days: int) -> pd.DataFrame:
    """跟 load_price_history() 邏輯一致但只回傳單一 stock_id(用於個股K線圖，避免不必要的 egress；若需全市場篩選運算如 compute_ma_signal，改用 load_price_history())
    對外介面與呼叫方式不變，內部改為版本鍵快取。"""
    version = get_latest_date(_PRICE_TABLE, _PRICE_DATE_COLUMN)
    return _load_price_history_single_cached(stock_id, lookback_days, version)


@st.cache_data(ttl=21600)
def _load_price_history_cached(lookback_days: int, version: str) -> pd.DataFrame:
    """version 只用來當快取 key(資料庫最新交易日不變就吃快取)，查詢本身用不到"""
    query = """
        SELECT trade_date, stock_id, stock_name, open_price, high_price, low_price,
               close_price, volume_shares
        FROM stock_price_daily
        WHERE trade_date >= (
            SELECT MIN(trade_date) FROM (
                SELECT DISTINCT trade_date FROM stock_price_daily
                ORDER BY trade_date DESC LIMIT %(n)s
            ) t
        )
        ORDER BY stock_id, trade_date
    """
    return pd.read_sql(query, get_engine(), params={"n": lookback_days})


@st.cache_data(ttl=21600)
def _load_price_history_single_cached(stock_id: str, lookback_days: int, version: str) -> pd.DataFrame:
    """version 只用來當快取 key(資料庫最新交易日不變就吃快取)，查詢本身用不到"""
    query = """
        SELECT trade_date, stock_id, stock_name, open_price, high_price, low_price,
               close_price, volume_shares
        FROM stock_price_daily
        WHERE stock_id = %(stock_id)s
          AND trade_date >= (
            SELECT MIN(trade_date) FROM (
                SELECT DISTINCT trade_date FROM stock_price_daily
                ORDER BY trade_date DESC LIMIT %(n)s
            ) t
        )
        ORDER BY trade_date
    """
    return pd.read_sql(query, get_engine(), params={"stock_id": stock_id, "n": lookback_days})