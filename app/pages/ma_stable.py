"""
站穩均線功能

篩選連續N天站穩均線(允許誤差) + 最新M天嚴格站上 + 量能條件同時符合的股票，
可加上三大法人買超條件。

本功能也是 stock_price_daily 資料表的下載入口——其他所有K線圖分頁
(融資減融券增、三大法人、均線糾結、均線糾結爆大量、下跌但法人同步買進、
主動式ETF買賣、大戶持股比例)都依賴這裡匯入的股價資料。

對應資料表：stock_price_daily
"""

import datetime
import re
import pandas as pd
import requests
import streamlit as st
from sqlalchemy import text

from app.common.charts import add_ma_columns, plot_ma_signal_candlestick
from app.common.constants import STOCK_ID_RE, today_tw
from app.common.db import get_engine
from app.common.disposition import build_disposition_stock_options, load_disposition_map
from app.common.help_tooltip import apply_help_tooltip_top_right
from app.common.http import get_relaxed_session
from app.common.parsing import find_field_index
from app.common.rate_limit import check_download_rate_limit, warn_if_identical_to_previous_day
from app.common.stock_price_data import load_price_history
from app.pages.institutional import (
    T86_ALL_THREE_UNIT,
    T86_UNIT_OPTIONS,
    find_t86_signal,
    load_recent_data_t86,
)

# 實際要寫入資料庫、給分析邏輯使用的欄位(已清整，只留用得到的：
# 均線/K線訊號只需要開高低收與成交量。不分上市/上櫃都寫進同一組欄位，
# 不額外記錄 market 或均價，若未來需要加入更多欄位，需確認資料庫容量)
STOCK_PRICE_COLUMN_ORDER = [
    "stock_id", "stock_name", "volume_shares",
    "open_price", "high_price", "low_price", "close_price",
]

# 欄位用名稱比對(而非位置索引)，跟 T86 同一套規則，
# 比較不怕證交所未來調整欄位順序(只保留上面清整後會用到的欄位對應)
MI_INDEX_FIELD_MAP = {
    "stock_id": "證券代號",
    "stock_name": "證券名稱",
    "volume_shares": "成交股數",
    "open_price": "開盤價",
    "high_price": "最高價",
    "low_price": "最低價",
    "close_price": "收盤價",
}


def fetch_from_twse_mi_index(trade_date: datetime.date) -> pd.DataFrame:
    """從證交所下載指定日期的個股日成交資訊
    (分類：全部(不含權證、牛熊證、可展延牛熊證)，type=ALLBUT0999)"""
    date_str = trade_date.strftime("%Y%m%d")
    url = "https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX"
    resp = requests.get(
        url, params={"date": date_str, "type": "ALLBUT0999", "response": "json"}, timeout=20
    )
    resp.raise_for_status()
    payload = resp.json()

    tables = payload.get("tables") or []
    target = next((t for t in tables if "每日收盤行情" in t.get("title", "")), None)
    if target is None or not target.get("data"):
        raise ValueError("查無資料，可能是非交易日(例如假日)或該日期尚未公布")

    fields = target["fields"]
    rows = target["data"]

    idx_map = {}
    for col, cn in MI_INDEX_FIELD_MAP.items():
        if cn not in fields:
            raise ValueError(f"找不到欄位：{cn}(實際欄位：{fields}，回傳格式可能已變動)")
        idx_map[col] = fields.index(cn)

    df = pd.DataFrame({col: [r[idx_map[col]] for r in rows] for col in STOCK_PRICE_COLUMN_ORDER})

    df["stock_id"] = (
        df["stock_id"].astype(str)
        .str.replace('="', "", regex=False)
        .str.replace('"', "", regex=False)
        .str.replace("=", "", regex=False)
        .str.strip()
    )
    df = df[df["stock_id"].str.fullmatch(STOCK_ID_RE)].copy()
    df["stock_name"] = df["stock_name"].astype(str).str.strip()

    def clean_num(s: pd.Series) -> pd.Series:
        s = s.astype(str).str.replace(r"<[^>]*>", "", regex=True).str.strip()
        s = s.str.replace(",", "", regex=False)
        s = s.replace({"": None, "--": None, "X": None, "+": None, "-": None})
        return pd.to_numeric(s, errors="coerce")

    num_cols = [c for c in STOCK_PRICE_COLUMN_ORDER if c not in ("stock_id", "stock_name")]
    for col in num_cols:
        df[col] = clean_num(df[col])

    df["trade_date"] = trade_date
    return df


# 站穩均線(上櫃)：欄位用名稱比對(沿用 T86 的 find_field_index)而非位置索引，
# 找不到欄位會直接噴錯，依錯誤訊息列出的實際欄位名稱回來修正下方 CANDIDATES 清單。
TPEX_PRICE_API_URL = "https://www.tpex.org.tw/www/zh-tw/afterTrading/dailyQuotes"
TPEX_PRICE_CODE_CANDIDATES = ["代號"]
TPEX_PRICE_NAME_CANDIDATES = ["名稱"]
TPEX_PRICE_OPEN_CANDIDATES = ["開盤"]
TPEX_PRICE_HIGH_CANDIDATES = ["最高"]
TPEX_PRICE_LOW_CANDIDATES = ["最低"]
TPEX_PRICE_CLOSE_CANDIDATES = ["收盤"]
TPEX_PRICE_VOLUME_CANDIDATES = ["成交股數"]


def fetch_from_tpex_price(trade_date: datetime.date) -> pd.DataFrame:
    """從證券櫃檯買賣中心下載指定日期的上櫃個股日成交資訊(JSON API)"""
    date_str = trade_date.strftime("%Y/%m/%d")
    session = get_relaxed_session()
    resp = session.get(
        TPEX_PRICE_API_URL,
        params={"response": "json", "date": date_str},
        timeout=20,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    data = resp.json()

    tables = data.get("tables") or []
    if not tables or not tables[0].get("data"):
        raise ValueError("查無資料，可能是非交易日(例如假日)或該日期尚未公布")

    table = tables[0]
    fields = table["fields"]
    rows = table["data"]

    code_idx = find_field_index(fields, TPEX_PRICE_CODE_CANDIDATES)
    name_idx = find_field_index(fields, TPEX_PRICE_NAME_CANDIDATES)
    open_idx = find_field_index(fields, TPEX_PRICE_OPEN_CANDIDATES)
    high_idx = find_field_index(fields, TPEX_PRICE_HIGH_CANDIDATES)
    low_idx = find_field_index(fields, TPEX_PRICE_LOW_CANDIDATES)
    close_idx = find_field_index(fields, TPEX_PRICE_CLOSE_CANDIDATES)
    volume_idx = find_field_index(fields, TPEX_PRICE_VOLUME_CANDIDATES)

    stock_ids = [str(r[code_idx]).strip() for r in rows]
    # 只保留代號為4碼純數字的列，濾掉ETF(00XXX)、權證、表尾「合計」等說明文字列
    keep_mask = [bool(re.fullmatch(STOCK_ID_RE, sid)) for sid in stock_ids]

    df = pd.DataFrame({
        "stock_id": stock_ids,
        "stock_name": [str(r[name_idx]).strip() for r in rows],
        "volume_shares": [r[volume_idx] for r in rows],
        "open_price": [r[open_idx] for r in rows],
        "high_price": [r[high_idx] for r in rows],
        "low_price": [r[low_idx] for r in rows],
        "close_price": [r[close_idx] for r in rows],
    })
    df = df[keep_mask].copy()
    if df.empty:
        raise ValueError("解析後沒有有效資料列，回傳格式可能已變動(回報供校正)")

    num_cols = [c for c in STOCK_PRICE_COLUMN_ORDER if c not in ("stock_id", "stock_name")]
    for col in num_cols:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "", regex=False).str.strip()
            .replace({"": None, "--": None, "X": None, "+": None, "-": None}),
            errors="coerce",
        )

    df["trade_date"] = trade_date
    return df[["trade_date"] + STOCK_PRICE_COLUMN_ORDER]


def import_dataframe_stock_price(df: pd.DataFrame) -> int:
    with get_engine().begin() as conn:
        df.to_sql("_staging_stock_price", conn, if_exists="replace", index=False)
        result = conn.execute(text(f"""
            INSERT INTO stock_price_daily (
                trade_date, {", ".join(STOCK_PRICE_COLUMN_ORDER)}
            )
            SELECT trade_date, {", ".join(STOCK_PRICE_COLUMN_ORDER)}
            FROM _staging_stock_price
            ON CONFLICT (trade_date, stock_id) DO NOTHING
        """))
        conn.execute(text("DROP TABLE _staging_stock_price"))
    return result.rowcount


def compute_ma_signal(
    df: pd.DataFrame,
    n_days: int,
    last_days_on_ma: int,
    tol_ratio: float,
    ma_main_days: int,
    ma_long_days: int,
    min_avg_vol_5: float,
):
    """回傳 (符合站穩均線條件的股票清單, 帶均線欄位的完整df)。"""
    if df.empty:
        return pd.DataFrame(columns=["股票代號", "股票名稱"]), df

    df = add_ma_columns(df, ma_main_days, ma_long_days)
    if df.empty:
        return pd.DataFrame(columns=["股票代號", "股票名稱"]), df

    th5 = df["ma_5"] * (1 - tol_ratio)
    th10 = df["ma_10"] * (1 - tol_ratio)
    th_main = df["ma_main"] * (1 - tol_ratio)
    df["pass_tol"] = (df["close_price"] >= th5) & (df["close_price"] >= th10) & (df["close_price"] >= th_main)
    df["pass_strict"] = (
        (df["close_price"] > df["ma_5"])
        & (df["close_price"] > df["ma_10"])
        & (df["close_price"] > df["ma_main"])
    )

    all_dates = sorted(df["trade_date"].dropna().unique())
    if len(all_dates) < max(n_days, last_days_on_ma):
        st.warning(f"交易日資料不足(目前只有 {len(all_dates)} 天)，需先下載或匯入更多歷史資料。")
        return pd.DataFrame(columns=["股票代號", "股票名稱"]), df

    latest_strict_dates = all_dates[-last_days_on_ma:]
    df_last = df[df["trade_date"].isin(latest_strict_dates)]
    codes_strict = df_last.groupby("stock_id").apply(
        lambda x: x["pass_strict"].all() and x["ma_main"].notna().all()
    )
    codes_strict = set(codes_strict[codes_strict].index)

    latest_tol_dates = all_dates[-n_days:]
    df_tol = df[df["trade_date"].isin(latest_tol_dates)]
    codes_tol = df_tol.groupby("stock_id").apply(
        lambda x: x["pass_tol"].all() and x["ma_main"].notna().all()
    )
    codes_tol = set(codes_tol[codes_tol].index)

    codes_ok = codes_tol & codes_strict

    latest_day = all_dates[-1]
    latest_df = df[df["trade_date"] == latest_day]
    vol_ok = latest_df[
        (latest_df["stock_id"].isin(codes_ok))
        & (latest_df["volume_lot"] > latest_df["vma_5"])
        & (latest_df["volume_lot"] > latest_df["vma_main"])
        & (latest_df["vma_5"] > min_avg_vol_5)
        & latest_df["vma_5"].notna()
        & latest_df["vma_main"].notna()
    ]["stock_id"].unique()

    codes_ok = sorted(set(vol_ok))
    if codes_ok:
        result_df = (
            latest_df[latest_df["stock_id"].isin(codes_ok)][["stock_id", "stock_name"]]
            .drop_duplicates(subset=["stock_id"])
            .rename(columns={"stock_id": "股票代號", "stock_name": "股票名稱"})
            .sort_values("股票代號")
        )
    else:
        result_df = pd.DataFrame(columns=["股票代號", "股票名稱"])

    return result_df, df


# ----頁面畫面----

def render_ma_signal_tab():
    apply_help_tooltip_top_right()
    st.header("站穩均線")

    # ----區塊一：從證交所／櫃買中心下載資料並匯入----
    st.subheader("下載資料")
    dl_twse_tab, dl_tpex_tab = st.tabs(
        ["上市(證交所)", "上櫃(櫃買中心)"], key="ma_signal_dl_tabs"
    )

    with dl_twse_tab:
        with st.form("ma_signal_twse_download_form"):
            col1, col2 = st.columns([2, 1])
            with col1:
                pick_date = st.date_input(
                    "選擇查詢日期",
                    value=today_tw(),
                    max_value=today_tw(),
                    key="ma_signal_date",
                )
            with col2:
                st.write("")
                st.write("")
                do_download_twse = st.form_submit_button("下載並匯入資料庫", type="primary")

        if do_download_twse:
            limit_msg = check_download_rate_limit("price_twse", pick_date)
            if limit_msg:
                st.error(limit_msg)
            else:
                with st.spinner(f"下載 {pick_date} 資料中..."):
                    try:
                        fetched_df = fetch_from_twse_mi_index(pick_date)
                        dup_msg = warn_if_identical_to_previous_day(
                            "stock_price_daily", "stock_id",
                            ["volume_shares", "open_price", "high_price", "low_price", "close_price"],
                            fetched_df, pick_date,
                        )
                        if dup_msg:
                            st.warning(dup_msg)
                        inserted = import_dataframe_stock_price(fetched_df)
                        st.success(f"共解析 {len(fetched_df)} 筆，新增 {inserted} 筆(重複日期已自動跳過)")
                        # load_price_history / load_price_history_single 已改為版本鍵快取(見 stock_price_data.py)，
                        # 寫入新資料後 get_latest_date() 會自動偵測到最新交易日變化並重查，不再需要手動 .clear()
                    except ValueError as e:
                        st.warning(str(e))
                    except Exception as e:
                        st.error(f"下載或匯入失敗：{e}")

    with dl_tpex_tab:
        with st.form("ma_signal_tpex_download_form"):
            col1, col2 = st.columns([2, 1])
            with col1:
                tpex_pick_date = st.date_input(
                    "選擇查詢日期",
                    value=today_tw(),
                    max_value=today_tw(),
                    key="ma_signal_tpex_date",
                )
            with col2:
                st.write("")
                st.write("")
                do_download_tpex = st.form_submit_button("下載並匯入資料庫", type="primary")

        if do_download_tpex:
            limit_msg = check_download_rate_limit("price_tpex", tpex_pick_date)
            if limit_msg:
                st.error(limit_msg)
            else:
                with st.spinner(f"下載 {tpex_pick_date} 資料中..."):
                    try:
                        fetched_df = fetch_from_tpex_price(tpex_pick_date)
                        dup_msg = warn_if_identical_to_previous_day(
                            "stock_price_daily", "stock_id",
                            ["volume_shares", "open_price", "high_price", "low_price", "close_price"],
                            fetched_df, tpex_pick_date,
                        )
                        if dup_msg:
                            st.warning(dup_msg)
                        inserted = import_dataframe_stock_price(fetched_df)
                        st.success(f"共解析 {len(fetched_df)} 筆，新增 {inserted} 筆(重複日期已自動跳過)")
                        # 同上：版本鍵快取會自動偵測新資料，不再需要手動 .clear()
                    except ValueError as e:
                        st.warning(str(e))
                    except Exception as e:
                        st.error(f"下載或匯入失敗：{e}(可改用本機匯入腳本手動匯入；若持續失敗，可能是櫃買中心回傳的欄位名稱跟程式裡預期的不同，需要回報校正)")

    st.divider()

    # ----區塊二：分析----
    st.subheader("分析")

    c1, c2, c3 = st.columns(3)
    with c1:
        n_days = st.number_input("連續站穩天數", min_value=1, value=7, key="ma_signal_ndays")
        ma_main_days = st.number_input("月線", min_value=1, value=20, key="ma_signal_ma_main")
    with c2:
        last_days_on_ma = st.number_input("最新嚴格站上天數", min_value=1, value=2, key="ma_signal_last_days")
        ma_long_days = st.number_input("季線", min_value=1, value=60, key="ma_signal_ma_long")
    with c3:
        tol_ratio = st.number_input(
            "容許誤差", min_value=0.0, max_value=0.2,
            value=0.025, step=0.005, format="%.3f", key="ma_signal_tol",
        )
        min_avg_vol_5 = st.number_input(
            "最新5日均量門檻(張)", min_value=0, value=3000, key="ma_signal_minvol",
        )

    plot_days = st.slider("K線顯示天數", min_value=20, max_value=120, value=60, key="ma_signal_plotdays")

    st.markdown("**(選用)三大法人買超條件**")
    use_t86_filter = st.checkbox(
        "同時要求三大法人買超",
        key="ma_use_t86",
    )
    t86_unit_label, t86_unit_col, t86_mode = None, None, None
    t86_n_days, t86_m_days, t86_k_days = None, None, None
    if use_t86_filter:
        t86_unit_label = st.selectbox("篩選單位", list(T86_UNIT_OPTIONS.keys()), key="ma_t86_unit")
        t86_unit_col = T86_UNIT_OPTIONS[t86_unit_label]
        is_all_three_ma = t86_unit_col == T86_ALL_THREE_UNIT

        if is_all_three_ma:
            t86_mode = "consecutive"
            t86_n_days = st.number_input(
                "三大法人連續買超天數 N", min_value=1, value=3, key="ma_t86_ndays_all3",
                help=(
                    "「三大法人同時買賣超」只支援連續 N 天模式：三方同買＝外資、投信、自營商皆無反向賣超"
                    "(允許某一方為 0)；只有三方皆為 0(完全無買賣)才不列入。"
                ),
            )
        else:
            t86_mode_label = st.radio(
                "天數條件模式",
                ["連續 N 天都買超", "最近 M 天內至少 K 天買超(不需連續)"],
                key="ma_t86_mode",
            )
            if t86_mode_label == "連續 N 天都買超":
                t86_mode = "consecutive"
                t86_n_days = st.number_input(
                    "連續買超天數 N", min_value=1, value=3, key="ma_t86_ndays",
                )
            else:
                t86_mode = "at_least"
                tc1, tc2 = st.columns(2)
                with tc1:
                    t86_m_days = st.number_input(
                        "最近天數 M", min_value=2, value=10, key="ma_t86_mdays",
                    )
                with tc2:
                    t86_k_days = st.number_input(
                        "至少符合天數 K", min_value=1, max_value=int(t86_m_days),
                        value=min(6, int(t86_m_days)), key="ma_t86_kdays",
                    )

    # 撈資料的回看區間要比季線天數多留一點緩衝，確保 MA_60 算得出來
    lookback_days = max(ma_long_days + n_days + 30, 150)

    with st.spinner("查詢資料庫中..."):
        df = load_price_history(lookback_days)

    if df.empty:
        st.warning("資料庫目前沒有資料，需先下載資料。")
        return

    available_dates = sorted(df["trade_date"].unique())
    st.info(f"目前資料涵蓋 {len(available_dates)} 個交易日：{available_dates[0]} ~ {available_dates[-1]}")

    result, full_df = compute_ma_signal(
        df, n_days, last_days_on_ma, tol_ratio, ma_main_days, ma_long_days, min_avg_vol_5,
    )

    t86_note = ""
    if use_t86_filter and not result.empty:
        t86_lookback = t86_n_days if t86_mode == "consecutive" else t86_m_days
        with st.spinner("查詢三大法人資料中..."):
            t86_df = load_recent_data_t86(t86_lookback)
        if t86_df.empty:
            st.warning("資料庫目前沒有三大法人資料，已略過此條件，需先至「三大法人」分頁下載或匯入資料。")
        else:
            t86_result = find_t86_signal(
                t86_df, t86_unit_col, "buy", t86_mode,
                n_days=t86_n_days, m_days=t86_m_days, k_days=t86_k_days,
            )
            passed_ids = set(t86_result["stock_id"])
            result = result[result["股票代號"].isin(passed_ids)]
            t86_note = f"，且符合「{t86_unit_label}買超」條件"

    st.subheader(f"符合站穩均線條件的股票{t86_note}(共 {len(result)} 檔)")
    st.dataframe(result, use_container_width=True, hide_index=True)

    csv = result.to_csv(index=False).encode("utf-8-sig")
    st.download_button("下載結果 CSV", csv, file_name="站穩均線_清單.csv",
                        mime="text/csv", key="ma_signal_dl_result")

    st.divider()
    st.subheader("個股K線圖",
        help=(
            "只提供代號為4碼的電子股個股K線圖查詢"
        ),
    )

    if result.empty:
        st.write("目前沒有符合條件的股票。")
    else:
        disposition_map = load_disposition_map()
        ma_signal_id_to_name = dict(zip(result["股票代號"], result["股票名稱"]))
        picked_labels, picked_label_to_id = build_disposition_stock_options(
            result["股票代號"].tolist(), disposition_map, ma_signal_id_to_name
        )
        picked_label = st.selectbox("選擇股票代號", picked_labels, key="ma_signal_pick_stock")
        picked = picked_label_to_id[picked_label]
        if picked:
            fig = plot_ma_signal_candlestick(
                full_df, picked, plot_days, ma_main_days, ma_long_days,
                disposition_periods=disposition_map.get(picked, []),
            )
            st.plotly_chart(fig, use_container_width=True, key="ma_signal_candlestick_chart")