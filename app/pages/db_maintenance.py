"""
資料庫維護功能

設計原則：
1. 顆粒度＝distinct trade_date(不是列數)，跟本機端檔案保留邏輯的精神一致。
2. 刪除前一定要先「預覽」，顯示：目前筆數/日期範圍 → 刪除後剩多少筆/日期範圍，
   使用者打勾確認後才能真的按下刪除鍵。
3. etf_holdings_daily(主動式ETF功能) 已有自己的自動清理機制(保留6個snapshot)，不放進這裡。
"""

import streamlit as st
from sqlalchemy import text

from app.common.db import get_engine
# 處置股手動更新功能目前已停用(見 render_db_maintenance_tab 內的註解)，
# 若需要重新啟用，請一併取消下一行的註解
# from app.common.disposition import fetch_disposition_twse, fetch_disposition_tpex, load_disposition_map, upsert_disposition

DB_MAINTENANCE_TABLES = [
    {
        "key": "t86",
        "label": "三大法人(three_institutional_daily)",
        "table": "three_institutional_daily",
        "date_col": "trade_date",
        "unit_label": "交易日",
        "default_keep": 10,
    },
    {
        "key": "margin_twse",
        "label": "融資融券｜上市(margin_trading_daily)",
        "table": "margin_trading_daily",
        "date_col": "trade_date",
        "unit_label": "交易日",
        "default_keep": 10,
    },
    {
        "key": "margin_tpex",
        "label": "融資融券｜上櫃(margin_trading_otc_daily)",
        "table": "margin_trading_otc_daily",
        "date_col": "trade_date",
        "unit_label": "交易日",
        "default_keep": 10,
    },
    {
        "key": "stock_price",
        "label": "均線／K線價量(stock_price_daily)",
        "table": "stock_price_daily",
        "date_col": "trade_date",
        "unit_label": "交易日",
        "default_keep": 120,
    },
    {
        "key": "shareholding",
        "label": "大戶持股比例(shareholding_distribution_weekly)",
        "table": "shareholding_distribution_weekly",
        "date_col": "trade_date",
        "unit_label": "週",
        "default_keep": 2,
    },
    {
        "key": "disposition",
        "label": "處置股清單(disposition_stocks)",
        "table": "disposition_stocks",
        "date_col": "start_date",
        "unit_label": "天(依處置起始日)",
        "default_keep": 120,
    },
]


def get_table_date_stats(table: str, date_col: str) -> dict:
    """回傳指定資料表目前的筆數、不同日期數與日期範圍。"""
    with get_engine().begin() as conn:
        row = conn.execute(text(
            f"SELECT COUNT(*) AS n_rows, COUNT(DISTINCT {date_col}) AS n_dates, "
            f"MIN({date_col}) AS min_date, MAX({date_col}) AS max_date FROM {table}"
        )).mappings().first()
    return dict(row) if row else {"n_rows": 0, "n_dates": 0, "min_date": None, "max_date": None}


def preview_prune_by_date(table: str, date_col: str, keep_n: int) -> dict | None:
    """回傳刪除預覽：若目前日期數已經 <= keep_n，回傳 None(代表不需要刪除)"""
    engine = get_engine()
    with engine.begin() as conn:
        dates = [
            r[0] for r in conn.execute(
                text(f"SELECT DISTINCT {date_col} FROM {table} ORDER BY {date_col} DESC")
            ).fetchall()
        ]
    if len(dates) <= keep_n:
        return None

    cutoff_date = dates[keep_n - 1]  # 保留下來的最舊日期
    with engine.begin() as conn:
        total_rows = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
        remaining_rows = conn.execute(
            text(f"SELECT COUNT(*) FROM {table} WHERE {date_col} >= :cutoff"),
            {"cutoff": cutoff_date},
        ).scalar()
    return {
        "keep_n": keep_n,
        "total_rows": total_rows,
        "delete_rows": total_rows - remaining_rows,
        "remaining_rows": remaining_rows,
        "remaining_min_date": cutoff_date,
        "remaining_max_date": dates[0],
    }


def execute_prune_by_date(table: str, date_col: str, cutoff_date) -> int:
    with get_engine().begin() as conn:
        result = conn.execute(
            text(f"DELETE FROM {table} WHERE {date_col} < :cutoff"),
            {"cutoff": cutoff_date},
        )
    return result.rowcount


def render_db_maintenance_section(cfg: dict):
    key = cfg["key"]
    table = cfg["table"]
    date_col = cfg["date_col"]
    unit_label = cfg["unit_label"]

    st.markdown(f"**{cfg['label']}**")
    stats = get_table_date_stats(table, date_col)

    if not stats["n_rows"]:
        st.write("目前資料庫裡沒有資料。")
        st.divider()
        return

    st.write(
        f"目前共 {stats['n_rows']} 筆資料，涵蓋 {stats['n_dates']} 個不同{unit_label}："
        f"{stats['min_date']} ～ {stats['max_date']}"
    )

    keep_n = st.number_input(
        f"保留最近幾個{unit_label}", min_value=1, max_value=int(stats["n_dates"]),
        value=min(cfg["default_keep"], int(stats["n_dates"])), step=1, key=f"dbmaint_keep_{key}",
    )

    preview_key = f"dbmaint_preview_{key}"

    if st.button("預覽刪除影響", key=f"dbmaint_preview_btn_{key}"):
        preview = preview_prune_by_date(table, date_col, int(keep_n))
        st.session_state[preview_key] = preview

    preview = st.session_state.get(preview_key)

    # 若使用者改了保留數但還沒重新預覽，先不顯示舊的預覽結果，避免誤刪
    if preview is not None and preview["keep_n"] != int(keep_n):
        preview = None
        st.session_state[preview_key] = None

    if preview is None:
        st.caption("需先按「預覽刪除影響」，確認刪除後會剩多少資料，再進行刪除。")
    elif preview["delete_rows"] == 0:
        st.info(f"目前資料未超過保留範圍({keep_n} 個{unit_label})，不需要刪除。")
    else:
        st.warning(
            f"刪除後，「{cfg['label']}」將會刪除 {preview['delete_rows']} 筆資料，"
            f"剩下 {preview['remaining_rows']} 筆資料，"
            f"日期範圍：{preview['remaining_min_date']} ～ {preview['remaining_max_date']}。"
        )
        confirm = st.checkbox("我確定要刪除以上資料", key=f"dbmaint_confirm_{key}")
        if st.button("執行刪除", key=f"dbmaint_delete_btn_{key}", disabled=not confirm, type="primary"):
            deleted = execute_prune_by_date(table, date_col, preview["remaining_min_date"])
            st.session_state[preview_key] = None
            st.cache_data.clear()
            st.success(f"已刪除 {deleted} 筆資料。")
            st.rerun()

    st.divider()


def render_db_maintenance_tab():
    st.header("資料庫維護")

    # ---- 處置股清單手動更新(已停用) ----
    # 處置股清單目前由排程(cron-job.org 觸發 GitHub Actions 的 update_disposition.py)
    # 每個交易日自動更新，不需要提供使用者手動更新。
    # 以下這段程式碼僅供「排程出問題時的緊急使用」：
    # 若排程連續失敗、又需要立刻補上最新處置股，可取消下方整段註解(同時取消檔案開頭
    # 對 app.common.disposition 的 import 註解)，重新部署後即可在此頁手動更新。
    # 問題排除、排程恢復正常後，建議再把這段註解回去。
    #
    # st.subheader(
    #     "處置股清單手動更新",
    # )
    # if st.button("立即更新處置股清單(上市＋上櫃)", key="disposition_manual_update_btn"):
    #     total = 0
    #     with st.spinner("下載上市處置股清單中..."):
    #         try:
    #             twse_df = fetch_disposition_twse()
    #             total += upsert_disposition(twse_df)
    #             st.write(f"上市(TWSE)：抓到 {len(twse_df)} 筆")
    #         except Exception as e:
    #             st.error(f"上市(TWSE)抓取失敗：{e}")
    #     with st.spinner("下載上櫃處置股清單中..."):
    #         try:
    #             tpex_df = fetch_disposition_tpex()
    #             total += upsert_disposition(tpex_df)
    #             st.write(f"上櫃(TPEx)：抓到 {len(tpex_df)} 筆")
    #         except Exception as e:
    #             st.error(f"上櫃(TPEx)抓取失敗：{e}")
    #     load_disposition_map.clear()
    #     st.success(f"完成，共新增/更新 {total} 筆處置股紀錄。")
    #
    # st.divider()

    for cfg in DB_MAINTENANCE_TABLES:
        render_db_maintenance_section(cfg)