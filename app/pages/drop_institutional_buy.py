"""
下跌但法人同步買進分頁(上市＋上櫃，資料來源共用 stock_price_daily +
three_institutional_daily)。
"""

import pandas as pd
import streamlit as st

from app.common.charts import add_ma_columns, plot_ma_signal_candlestick
from app.common.disposition import build_disposition_stock_options, load_disposition_map
from app.common.help_tooltip import apply_help_tooltip_top_right
from app.common.stock_price_data import load_price_history
from app.pages.institutional import T86_ALL_THREE_UNIT, find_t86_signal, load_recent_data_t86


def compute_price_drop_institutional_buy_signal(
    price_df: pd.DataFrame,
    t86_df: pd.DataFrame,
    lookback_m: int,
    drop_pct: float,
    min_avg_vol_5: float,
    buy_n_days: int,
    ma_main_days: int,
    ma_long_days: int,
):
    """回傳 (符合「下跌但法人同步買進」條件的股票清單, 帶均線欄位的完整df)。"""
    empty_result = pd.DataFrame(columns=["股票代號", "股票名稱", "跌幅(%)", "起始收盤", "最新收盤", "近5日均量(張)"])

    if price_df.empty:
        return empty_result, price_df

    df = add_ma_columns(price_df, ma_main_days, ma_long_days, restrict_prefixes=False)
    if df.empty:
        return empty_result, df

    df = df.sort_values(["stock_id", "trade_date"])

    all_dates = sorted(df["trade_date"].dropna().unique())
    if len(all_dates) < lookback_m:
        st.warning(f"交易日資料不足(目前只有 {len(all_dates)} 天)，需先至「站穩均線」分頁下載歷史資料。")
        return empty_result, df

    latest_date = all_dates[-1]
    start_date = all_dates[-lookback_m]

    df_latest = df[df["trade_date"] == latest_date][["stock_id", "stock_name", "close_price", "vma_5"]]
    df_start = df[df["trade_date"] == start_date][["stock_id", "close_price"]].rename(
        columns={"close_price": "start_close"}
    )

    merged = df_start.merge(df_latest, on="stock_id", how="inner")
    merged = merged.dropna(subset=["start_close", "close_price"])
    merged = merged[merged["start_close"] > 0]
    merged["drop_pct"] = (merged["start_close"] - merged["close_price"]) / merged["start_close"] * 100

    price_ok_ids = set(
        merged[
            (merged["drop_pct"] >= drop_pct)
            & merged["vma_5"].notna()
            & (merged["vma_5"] >= min_avg_vol_5)
        ]["stock_id"]
    )

    if not price_ok_ids or t86_df.empty:
        buy_ids = set()
    else:
        t86_result = find_t86_signal(
            t86_df, T86_ALL_THREE_UNIT, "buy", "consecutive", n_days=buy_n_days,
        )
        buy_ids = set(t86_result["stock_id"])

    codes_ok = sorted(price_ok_ids & buy_ids)

    if codes_ok:
        result_df = (
            merged[merged["stock_id"].isin(codes_ok)][
                ["stock_id", "stock_name", "drop_pct", "start_close", "close_price", "vma_5"]
            ]
            .drop_duplicates(subset=["stock_id"])
            .rename(columns={
                "stock_id": "股票代號",
                "stock_name": "股票名稱",
                "drop_pct": "跌幅(%)",
                "start_close": "起始收盤",
                "close_price": "最新收盤",
                "vma_5": "近5日均量(張)",
            })
            .sort_values("跌幅(%)", ascending=False)
        )
        result_df["跌幅(%)"] = result_df["跌幅(%)"].round(2)
        result_df["近5日均量(張)"] = result_df["近5日均量(張)"].round(0)
    else:
        result_df = empty_result

    return result_df, df


def render_price_drop_institutional_buy_tab():
    """渲染「下跌但法人同步買進」分頁(版面與K線圖呈現比照「均線糾結」分頁)。"""
    apply_help_tooltip_top_right()
    st.header(
        "下跌但法人同步買進",
        help=(
            "近期大跌但法人同步買超的股票"
        ),
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        lookback_m = st.number_input(
            "近M天比較跌幅", min_value=2, value=4, key="pdib_m",
        )
    with c2:
        drop_pct = st.number_input(
            "跌幅門檻%", min_value=0.0, value=7.0, step=0.5, key="pdib_drop",
        )
    with c3:
        min_avg_vol_5 = st.number_input(
            "近5日均量門檻(張)", min_value=0, value=1000, key="pdib_minvol",
        )

    c4, c5, c6 = st.columns(3)
    with c4:
        buy_n_days = st.number_input(
            "法人同步買超連續天數", min_value=1, value=2, key="pdib_buy_n",
        )
    with c5:
        ma_main_days = st.number_input("月線天數", min_value=1, value=20, key="pdib_ma_main")
    with c6:
        ma_long_days = st.number_input("季線天數", min_value=1, value=60, key="pdib_ma_long")

    plot_days = st.slider("K線顯示天數", min_value=20, max_value=120, value=60, key="pdib_plotdays")

    # 撈股價資料的回看區間要比季線天數、跌幅比較天數多留一點緩衝
    lookback_days = max(ma_long_days + lookback_m + 30, 150)

    with st.spinner("查詢股價資料庫中..."):
        price_df = load_price_history(lookback_days)

    if price_df.empty:
        st.warning("資料庫目前沒有股價資料，需先至「站穩均線」分頁下載資料。")
        return

    available_dates = sorted(price_df["trade_date"].unique())
    st.info(f"目前股價資料涵蓋 {len(available_dates)} 個交易日：{available_dates[0]} ~ {available_dates[-1]}")

    with st.spinner("查詢三大法人資料中..."):
        t86_df = load_recent_data_t86(buy_n_days)

    if t86_df.empty:
        st.warning("資料庫目前沒有三大法人資料，需先至「三大法人」分頁下載資料。")
        return

    result, full_df = compute_price_drop_institutional_buy_signal(
        price_df, t86_df, lookback_m, drop_pct, min_avg_vol_5, buy_n_days, ma_main_days, ma_long_days,
    )

    st.subheader(f"符合「下跌但法人同步買進」條件的股票(共 {len(result)} 檔)")
    st.dataframe(result, use_container_width=True, hide_index=True)

    csv = result.to_csv(index=False).encode("utf-8-sig")
    st.download_button("下載結果 CSV", csv, file_name="下跌但法人同步買進_清單.csv",
                        mime="text/csv", key="pdib_dl_result")

    st.divider()
    st.subheader("個股K線圖")

    if result.empty:
        st.write("目前沒有符合條件的股票。")
    else:
        disposition_map = load_disposition_map()
        pdib_id_to_name = dict(zip(result["股票代號"], result["股票名稱"]))
        picked_labels, picked_label_to_id = build_disposition_stock_options(
            result["股票代號"].tolist(), disposition_map, pdib_id_to_name
        )
        picked_label = st.selectbox("選擇股票代號", picked_labels, key="pdib_pick_stock")
        picked = picked_label_to_id[picked_label]
        if picked:
            fig = plot_ma_signal_candlestick(
                full_df, picked, plot_days, ma_main_days, ma_long_days,
                disposition_periods=disposition_map.get(picked, []),
            )
            st.plotly_chart(fig, use_container_width=True, key="pdib_candlestick_chart")