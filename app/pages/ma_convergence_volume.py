"""
均線糾結爆大量功能(上市＋上櫃，資料來源與「站穩均線」「均線糾結」共用stock_price_daily)。

在「均線糾結」糾結判斷的基礎上，再加上最新一天的價格突破與成交量爆量條件。
"""

import pandas as pd
import streamlit as st

from app.common.charts import add_ma_columns, plot_ma_signal_candlestick
from app.common.disposition import build_disposition_stock_options, load_disposition_map
from app.common.stock_price_data import load_price_history


def compute_ma_knot_volume_spike_signal(
    df: pd.DataFrame,
    knot_lookback_n: int,
    knot_need_m: int,
    strict_last_x: int,
    knot_threshold: float,
    ma_main_days: int,
    ma_long_days: int,
    min_avg_vol_5_excl_last: float,
    breakout_pct: float,
    volume_spike_pct: float,
):
    """回傳 (符合均線糾結爆大量條件的股票清單, 帶均線欄位的完整df)。"""
    if df.empty:
        return pd.DataFrame(columns=["股票代號", "股票名稱"]), df

    df = add_ma_columns(df, ma_main_days, ma_long_days, restrict_prefixes=False)
    if df.empty:
        return pd.DataFrame(columns=["股票代號", "股票名稱"]), df

    df = df.sort_values(["stock_id", "trade_date"])
    g = df.groupby("stock_id", sort=False)

    # ma_main：月線(天數由 ma_main_days 參數決定，欄位名稱與 add_ma_columns 一致)
    ma_cols = ["ma_5", "ma_10", "ma_main"]
    df["knot_idx"] = df[ma_cols].std(axis=1, ddof=0) / df["close_price"]
    df["is_knot"] = df[ma_cols].notna().all(axis=1) & (df["knot_idx"] <= knot_threshold)

    # 前一日高/低(判斷突破用)
    df["prev_high"] = g["high_price"].shift(1)
    df["prev_low"] = g["low_price"].shift(1)

    # 不含最新一天的 5 日／10 日平均量(判斷量能用)
    df["vma_5_excl_last"] = g["volume_lot"].transform(lambda x: x.shift(1).rolling(5).mean())
    df["vma_10_excl_last"] = g["volume_lot"].transform(lambda x: x.shift(1).rolling(10).mean())

    all_dates = sorted(df["trade_date"].dropna().unique())
    if len(all_dates) < max(knot_lookback_n, strict_last_x + 1):
        st.warning(f"交易日資料不足(目前只有 {len(all_dates)} 天)，需先至「站穩均線」分頁下載資料。")
        return pd.DataFrame(columns=["股票代號", "股票名稱"]), df

    latest_n_dates = all_dates[-knot_lookback_n:]
    # STRICT_LAST_X 不含最新一天：取倒數 (X+1) 天 ~ 倒數第2天
    latest_x_dates = all_dates[-(strict_last_x + 1):-1]

    df_n = df[df["trade_date"].isin(latest_n_dates)]
    df_x = df[df["trade_date"].isin(latest_x_dates)]

    cond_m = df_n.groupby("stock_id")["is_knot"].sum() >= knot_need_m
    cond_x = df_x.groupby("stock_id")["is_knot"].all()

    codes_knot = set(cond_m[cond_m].index) & set(cond_x[cond_x].index)

    latest_day = all_dates[-1]
    latest_df = df[df["trade_date"] == latest_day]

    price_break = (
        (latest_df["close_price"] > latest_df["prev_high"] * (1 + breakout_pct / 100.0))
        | (latest_df["close_price"] < latest_df["prev_low"] * (1 - breakout_pct / 100.0))
    )
    vol_spike = latest_df["volume_lot"] > latest_df["vma_10_excl_last"] * (1 + volume_spike_pct / 100.0)
    vol_gt_ma5_main = (latest_df["volume_lot"] > latest_df["vma_5"]) & (latest_df["volume_lot"] > latest_df["vma_main"])
    avg_vol_ok = latest_df["vma_5_excl_last"] > min_avg_vol_5_excl_last

    codes_ok = sorted(set(
        latest_df[
            latest_df["stock_id"].isin(codes_knot)
            & price_break
            & vol_spike
            & vol_gt_ma5_main
            & avg_vol_ok
            & latest_df["prev_high"].notna()
            & latest_df["prev_low"].notna()
            & latest_df["vma_10_excl_last"].notna()
            & latest_df["vma_5_excl_last"].notna()
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


def render_ma_knot_volume_spike_tab():
    """渲染「均線糾結爆大量」分頁(版面與K線圖呈現比照「均線糾結」分頁)。"""
    st.header(
        "均線糾結爆大量",
    )

    c1, c2, c3 = st.columns(3)
    with c1:
        knot_lookback_n = st.number_input(
            "糾結觀察天數", min_value=2, value=10, key="ma_knot_vs_n",
        )
        ma_main_days = st.number_input("月線天數", min_value=1, value=20, key="ma_knot_vs_ma_main")
    with c2:
        knot_need_m = st.number_input(
            "至少糾結天數", min_value=1, max_value=int(knot_lookback_n),
            value=min(3, int(knot_lookback_n)), key="ma_knot_vs_m",
        )
        ma_long_days = st.number_input("季線天數", min_value=1, value=60, key="ma_knot_vs_ma_long")
    with c3:
        strict_last_x = st.number_input(
            "最後嚴格糾結天數(不含最新一天)",
            min_value=1, max_value=max(1, int(knot_lookback_n) - 1),
            value=min(1, max(1, int(knot_lookback_n) - 1)), key="ma_knot_vs_x",
        )
        min_avg_vol_5 = st.number_input(
            "近5日均量門檻(張，不含最新一天)", min_value=0, value=3000, key="ma_knot_vs_minvol",
        )

    c4, c5, c6 = st.columns(3)
    with c4:
        knot_threshold = st.number_input(
            "糾結指標門檻", min_value=0.0, max_value=0.2,
            value=0.01, step=0.001, format="%.3f", key="ma_knot_vs_threshold",
        )
    with c5:
        breakout_pct = st.number_input(
            "收盤突破幅度 X%(相對前一日高/低點)", min_value=0.0, value=1.0, step=0.5,
            key="ma_knot_vs_xpct",
        )
    with c6:
        volume_spike_pct = st.number_input(
            "當日爆量幅度 Y%(相對前10日均量，不含當日)", min_value=0.0, value=50.0, step=10.0,
            key="ma_knot_vs_ypct",
        )

    plot_days = st.slider("K線顯示天數", min_value=20, max_value=120, value=60, key="ma_knot_vs_plotdays")

    # 撈資料的回看區間要比季線天數、糾結觀察天數多留一點緩衝
    lookback_days = max(ma_long_days + knot_lookback_n + 30, 150)

    with st.spinner("查詢資料庫中..."):
        df = load_price_history(lookback_days)

    if df.empty:
        st.warning("資料庫目前沒有資料，需先至「站穩均線」分頁下載或匯入資料。")
        return

    available_dates = sorted(df["trade_date"].unique())
    st.info(f"目前資料涵蓋 {len(available_dates)} 個交易日：{available_dates[0]} ~ {available_dates[-1]}")

    result, full_df = compute_ma_knot_volume_spike_signal(
        df, knot_lookback_n, knot_need_m, strict_last_x, knot_threshold,
        ma_main_days, ma_long_days, min_avg_vol_5, breakout_pct, volume_spike_pct,
    )

    st.subheader(f"符合均線糾結爆大量條件的股票(共 {len(result)} 檔)")
    st.dataframe(result, use_container_width=True, hide_index=True)

    csv = result.to_csv(index=False).encode("utf-8-sig")
    st.download_button("下載結果 CSV", csv, file_name="均線糾結爆大量_清單.csv",
                        mime="text/csv", key="ma_knot_vs_dl_result")

    st.divider()
    st.subheader("個股K線圖")

    if result.empty:
        st.write("目前沒有符合條件的股票。")
    else:
        disposition_map = load_disposition_map()
        ma_knot_vs_id_to_name = dict(zip(result["股票代號"], result["股票名稱"]))
        picked_labels, picked_label_to_id = build_disposition_stock_options(
            result["股票代號"].tolist(), disposition_map, ma_knot_vs_id_to_name
        )
        picked_label = st.selectbox("選擇股票代號", picked_labels, key="ma_knot_vs_pick_stock")
        picked = picked_label_to_id[picked_label]
        if picked:
            fig = plot_ma_signal_candlestick(
                full_df, picked, plot_days, ma_main_days, ma_long_days,
                disposition_periods=disposition_map.get(picked, []),
            )
            st.plotly_chart(fig, use_container_width=True, key="ma_knot_vs_candlestick_chart")