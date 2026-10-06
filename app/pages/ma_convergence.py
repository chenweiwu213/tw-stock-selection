"""
均線糾結功能(上市＋上櫃，資料來源與「站穩均線」共用 stock_price_daily。
"""

import pandas as pd
import streamlit as st

from app.common.charts import add_ma_columns, plot_ma_signal_candlestick
from app.common.disposition import build_disposition_stock_options, load_disposition_map
from app.common.stock_price_data import load_price_history


def compute_ma_knot_signal(
    df: pd.DataFrame,
    knot_lookback_n: int,
    knot_need_m: int,
    strict_last_x: int,
    knot_threshold: float,
    ma_main_days: int,
    ma_long_days: int,
    min_avg_vol_5: float,
):
    """回傳 (符合均線糾結條件的股票清單, 帶均線欄位的完整df)。"""
    if df.empty:
        return pd.DataFrame(columns=["股票代號", "股票名稱"]), df

    df = add_ma_columns(df, ma_main_days, ma_long_days, restrict_prefixes=False)
    if df.empty:
        return pd.DataFrame(columns=["股票代號", "股票名稱"]), df

    # ma_main：月線(天數由 ma_main_days 參數決定，欄位名稱與 add_ma_columns 一致)
    ma_cols = ["ma_5", "ma_10", "ma_main"]
    df["knot_idx"] = df[ma_cols].std(axis=1, ddof=0) / df["close_price"]
    df["is_knot"] = df[ma_cols].notna().all(axis=1) & (df["knot_idx"] <= knot_threshold)

    all_dates = sorted(df["trade_date"].dropna().unique())
    if len(all_dates) < max(knot_lookback_n, strict_last_x):
        st.warning(f"交易日資料不足(目前只有 {len(all_dates)} 天)，需先下載或匯入更多歷史資料。")
        return pd.DataFrame(columns=["股票代號", "股票名稱"]), df

    latest_n_dates = all_dates[-knot_lookback_n:]
    latest_x_dates = all_dates[-strict_last_x:]

    df_n = df[df["trade_date"].isin(latest_n_dates)]
    df_x = df[df["trade_date"].isin(latest_x_dates)]

    cond_m = df_n.groupby("stock_id")["is_knot"].sum() >= knot_need_m
    cond_x = df_x.groupby("stock_id")["is_knot"].all()

    codes_knot = set(cond_m[cond_m].index) & set(cond_x[cond_x].index)

    latest_day = all_dates[-1]
    latest_df = df[df["trade_date"] == latest_day]

    codes_ok = sorted(set(
        latest_df[
            latest_df["stock_id"].isin(codes_knot)
            & (latest_df["volume_lot"] > latest_df["vma_5"])
            & (latest_df["volume_lot"] > latest_df["vma_main"])
            & (latest_df["vma_5"] > min_avg_vol_5)
            & latest_df["vma_5"].notna()
            & latest_df["vma_main"].notna()
        ]["stock_id"]
    ))

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


def render_ma_knot_tab():
    """渲染「均線糾結」分頁。"""
    st.header(
        "均線糾結"
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        knot_lookback_n = st.number_input(
            "糾結觀察天數", min_value=1, value=10, key="ma_knot_n",
        )
        ma_main_days = st.number_input("月線天數", min_value=1, value=20, key="ma_knot_ma_main")
    with c2:
        knot_need_m = st.number_input(
            "至少糾結天數", min_value=1, max_value=int(knot_lookback_n),
            value=min(7, int(knot_lookback_n)), key="ma_knot_m",
        )
        ma_long_days = st.number_input("季線天數", min_value=1, value=60, key="ma_knot_ma_long")
    with c3:
        strict_last_x = st.number_input(
            "最後嚴格糾結天數", min_value=1, max_value=int(knot_lookback_n),
            value=min(3, int(knot_lookback_n)), key="ma_knot_x",
        )
        min_avg_vol_5 = st.number_input(
            "最新5日均量門檻(張)", min_value=0, value=3000, key="ma_knot_minvol",
        )

    knot_threshold = st.number_input(
        "糾結指標門檻", min_value=0.0, max_value=0.2,
        value=0.007, step=0.001, format="%.3f", key="ma_knot_threshold",
    )

    plot_days = st.slider("K線顯示天數", min_value=20, max_value=120, value=60, key="ma_knot_plotdays")

    # 撈資料的回看區間要比季線天數、糾結觀察天數多留一點緩衝
    lookback_days = max(ma_long_days + knot_lookback_n + 30, 150)

    with st.spinner("查詢資料庫中..."):
        df = load_price_history(lookback_days)

    if df.empty:
        st.warning("資料庫目前沒有資料，需下載資料。")
        return

    available_dates = sorted(df["trade_date"].unique())
    st.info(f"目前資料涵蓋 {len(available_dates)} 個交易日：{available_dates[0]} ~ {available_dates[-1]}")

    result, full_df = compute_ma_knot_signal(
        df, knot_lookback_n, knot_need_m, strict_last_x, knot_threshold,
        ma_main_days, ma_long_days, min_avg_vol_5,
    )

    st.subheader(f"符合均線糾結條件的股票(共 {len(result)} 檔)")
    st.dataframe(result, use_container_width=True, hide_index=True)

    csv = result.to_csv(index=False).encode("utf-8-sig")
    st.download_button("下載結果 CSV", csv, file_name="均線糾結_清單.csv",
                        mime="text/csv", key="ma_knot_dl_result")

    st.divider()
    st.subheader("個股K線圖")

    if result.empty:
        st.write("目前沒有符合條件的股票。")
    else:
        disposition_map = load_disposition_map()
        ma_knot_id_to_name = dict(zip(result["股票代號"], result["股票名稱"]))
        picked_labels, picked_label_to_id = build_disposition_stock_options(
            result["股票代號"].tolist(), disposition_map, ma_knot_id_to_name
        )
        picked_label = st.selectbox("選擇股票代號", picked_labels, key="ma_knot_pick_stock")
        picked = picked_label_to_id[picked_label]
        if picked:
            fig = plot_ma_signal_candlestick(
                full_df, picked, plot_days, ma_main_days, ma_long_days,
                disposition_periods=disposition_map.get(picked, []),
            )
            st.plotly_chart(fig, use_container_width=True, key="ma_knot_candlestick_chart")