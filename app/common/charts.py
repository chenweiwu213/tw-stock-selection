"""
K 線圖與均線計算共用模組

add_ma_columns() 與 plot_ma_signal_candlestick() 被以下所有分頁共用：
站穩均線、均線糾結、均線糾結爆大量、下跌但法人同步買進、主動式ETF買賣、
大戶持股比例、三大法人(個股K線圖)。
"""

import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

from app.common.constants import STOCK_ID_RE, STOCK_PRICE_ALLOWED_PREFIXES


def add_ma_columns(
    df: pd.DataFrame,
    ma_main_days: int = 20,
    ma_long_days: int = 60,
    restrict_prefixes: bool = True,
) -> pd.DataFrame:
    """幫 stock_price_daily 的 df 加上 volume_lot 及均線(MA5/10/主均線/長期均線，預設20/60，可自行調整天數)、量能均線欄位，
    供「站穩均線」訊號判斷、「三大法人」分頁的K線圖查詢、「均線糾結」分頁共用同一套邏輯。
    一律只保留代號為4碼數字的股票(濾掉ETF、權證等)。
    restrict_prefixes=True(預設)時，再額外只保留前兩碼屬於 STOCK_PRICE_ALLOWED_PREFIXES 的股票
    (沿用「站穩均線」本機程式的 ALLOWED_PREFIXES 清單)。"""
    if df.empty:
        return df

    df = df.copy()
    df["volume_lot"] = df["volume_shares"] / 1000.0

    df = df[df["stock_id"].str.fullmatch(STOCK_ID_RE)].copy()
    if restrict_prefixes:
        df = df[df["stock_id"].str[:2].isin(STOCK_PRICE_ALLOWED_PREFIXES)].copy()
    if df.empty:
        return df

    df = df.sort_values(["stock_id", "trade_date"])
    g = df.groupby("stock_id", sort=False)

    df["ma_5"] = g["close_price"].transform(lambda x: x.rolling(5).mean())
    df["ma_10"] = g["close_price"].transform(lambda x: x.rolling(10).mean())
    df["ma_main"] = g["close_price"].transform(lambda x: x.rolling(ma_main_days).mean())
    df["ma_long"] = g["close_price"].transform(lambda x: x.rolling(ma_long_days).mean())

    df["vma_5"] = g["volume_lot"].transform(lambda x: x.rolling(5).mean())
    df["vma_main"] = g["volume_lot"].transform(lambda x: x.rolling(ma_main_days).mean())
    return df


def plot_ma_signal_candlestick(
    df: pd.DataFrame,
    stock_id: str,
    plot_days: int = 60,
    ma_main_days: int = 20,
    ma_long_days: int = 60,
    disposition_periods: list | None = None,
) -> go.Figure:
    """
    個股 K 線圖：
    - 上方：K 線 + MA5 / MA10 / 主均線(預設20) / 長期均線(預設60)
    - 下方：成交量(張)
    - X 軸用連續整數位置，假日與無資料日期不留空白
    - 不使用 Plotly 會在 X 軸顯示索引值的 hover 行為
    - ma_main_days / ma_long_days 僅用來決定圖例文字(例如 "MA20")，
      實際欄位數值由呼叫端傳入的 df(經 add_ma_columns 計算)決定，
      須確保這兩個參數跟算欄位時用的天數一致，避免圖例文字跟實際天數對不上
    - disposition_periods：處置期間清單 [(start_date, end_date), ...](可為 None)。
      因為 X 軸是連續整數位置而非真實日期，色塊必須換算成對應的 x_pos 區間，
      不能直接用日期當座標(否則色塊會完全對不上圖表可視範圍)
    """
    d = (
        df[df["stock_id"] == stock_id]
        .sort_values("trade_date")
        .tail(plot_days)
        .copy()
    )

    if d.empty:
        return go.Figure()

    name = d["stock_name"].iloc[-1]

    price_cols = ["open_price", "high_price", "low_price", "close_price"]
    d = d[d[price_cols].notna().any(axis=1)].copy()

    if d.empty:
        return go.Figure()

    d = d.reset_index(drop=True)

    x_pos = list(range(len(d)))
    date_text = pd.to_datetime(d["trade_date"]).dt.strftime("%Y-%m-%d").tolist()

    fig = make_subplots(
        rows=2,
        cols=1,
        shared_xaxes=True,
        vertical_spacing=0.04,
        row_heights=[0.72, 0.28],
    )

    # -------成交量(張)-------
    if "volume_lot" in d.columns:
        volume = pd.to_numeric(d["volume_lot"], errors="coerce")
    else:
        volume = pd.to_numeric(d["volume_shares"], errors="coerce") / 1000.0

    volume_colors = [
        "red" if close >= open_ else "green"
        for open_, close in zip(d["open_price"], d["close_price"])
    ]

    # -------實際 K 線：關閉原生 hover--------
    fig.add_trace(
        go.Candlestick(
            x=x_pos,
            open=d["open_price"],
            high=d["high_price"],
            low=d["low_price"],
            close=d["close_price"],
            increasing_line_color="red",
            decreasing_line_color="green",
            name="K線",
            hoverinfo="skip",
        ),
        row=1,
        col=1,
    )

    # --------均線：關閉各自 hover，由後面的透明 scatter 統一顯示--------
    for col, color, label in [
        ("ma_5", "blue", "MA5"),
        ("ma_10", "orange", "MA10"),
        ("ma_main", "purple", f"MA{ma_main_days}"),
        ("ma_long", "brown", f"MA{ma_long_days}"),
    ]:
        fig.add_trace(
            go.Scatter(
                x=x_pos,
                y=d[col],
                mode="lines",
                line=dict(color=color, width=1.5),
                name=label,
                hoverinfo="skip",
            ),
            row=1,
            col=1,
        )

    # ---------股價提示文字---------

    # -------每日漲跌(與前一交易日收盤價比較)--------
    prev_close = d["close_price"].shift(1)
    change_amt = d["close_price"] - prev_close
    change_pct = (change_amt / prev_close) * 100

    price_hover_text = []
    for i, row in d.iterrows():
        parts = [
            f"日期={date_text[i]}",
            f"開={row['open_price']:g}" if pd.notna(row["open_price"]) else "開=—",
            f"高={row['high_price']:g}" if pd.notna(row["high_price"]) else "高=—",
            f"低={row['low_price']:g}" if pd.notna(row["low_price"]) else "低=—",
            f"收={row['close_price']:g}" if pd.notna(row["close_price"]) else "收=—",
        ]

        amt = change_amt.iloc[i]
        pct = change_pct.iloc[i]
        if pd.notna(amt) and pd.notna(pct):
            if amt > 0:
                parts.append(f"<span style='color:red'>漲{amt:g}({pct:.2f}%)</span>")
            elif amt < 0:
                parts.append(f"<span style='color:green'>跌{abs(amt):g}({abs(pct):.2f}%)</span>")
            else:
                parts.append("平盤(0.00%)")

        for col, label in [
            ("ma_5", "MA5"),
            ("ma_10", "MA10"),
            ("ma_main", f"MA{ma_main_days}"),
            ("ma_long", f"MA{ma_long_days}"),
        ]:
            value = row[col]
            if pd.notna(value):
                parts.append(f"{label}={value:.2f}")

        price_hover_text.append("<br>".join(parts))

    # ---------成交量提示文字(日期＋成交量)，只在成交量區顯示-----------
    volume_hover_text = [
        f"日期={date_text[i]}<br>成交量={volume.iloc[i]:,.0f} 張"
        if pd.notna(volume.iloc[i])
        else f"日期={date_text[i]}<br>成交量=—"
        for i in range(len(d))
    ]

    # 用透明 scatter marker 取代透明 bar，避免 X 軸出現灰色索引小框
    # hover 點放在每根 K 棒的中間價位附近
    price_hover_y = []
    for _, row in d.iterrows():
        vals = [row["open_price"], row["high_price"], row["low_price"], row["close_price"]]
        vals = [v for v in vals if pd.notna(v)]
        if vals:
            price_hover_y.append(sum(vals) / len(vals))
        else:
            price_hover_y.append(None)

    fig.add_trace(
        go.Scatter(
            x=x_pos,
            y=price_hover_y,
            mode="markers",
            marker=dict(
                symbol="square",
                size=26,
                color="rgba(0,0,0,0)",
                line=dict(width=0),
            ),
            text=price_hover_text,
            hovertemplate="%{text}<extra></extra>",
            showlegend=False,
            name="",
        ),
        row=1,
        col=1,
    )

    # -------成交量：實際柱狀圖不顯示 hover---------

    fig.add_trace(
        go.Bar(
            x=x_pos,
            y=volume,
            marker_color=volume_colors,
            name="成交量",
            hoverinfo="skip",
        ),
        row=2,
        col=1,
    )

    # 用透明 scatter marker 取代 hover bar，避免 X 軸出現灰色索引小框
    # 成交量區只顯示日期＋成交量，不帶入股價／均線資訊
    volume_hover_y = []
    for v in volume:
        if pd.notna(v):
            volume_hover_y.append(v / 2 if v > 0 else 0)
        else:
            volume_hover_y.append(None)

    fig.add_trace(
        go.Scatter(
            x=x_pos,
            y=volume_hover_y,
            mode="markers",
            marker=dict(
                symbol="square",
                size=24,
                color="rgba(0,0,0,0)",
                line=dict(width=0),
            ),
            text=volume_hover_text,
            hovertemplate="%{text}<extra></extra>",
            showlegend=False,
            name="",
        ),
        row=2,
        col=1,
    )

    # X 軸日期標籤
    tick_step = max(1, len(d) // 8)
    tickvals = x_pos[::tick_step]
    ticktext = [date_text[i] for i in tickvals]

    if tickvals[-1] != x_pos[-1]:
        tickvals.append(x_pos[-1])
        ticktext.append(date_text[-1])

    fig.update_xaxes(
        type="linear",
        tickmode="array",
        tickvals=tickvals,
        ticktext=ticktext,
        rangeslider_visible=False,
        showspikes=False,
        row=1,
        col=1,
    )

    fig.update_xaxes(
        type="linear",
        tickmode="array",
        tickvals=tickvals,
        ticktext=ticktext,
        title_text="交易日期",
        showspikes=False,
        row=2,
        col=1,
    )

    fig.update_xaxes(range=[-0.7, len(d) - 0.3], row=1, col=1)
    fig.update_xaxes(range=[-0.7, len(d) - 0.3], row=2, col=1)

    fig.update_yaxes(title_text="股價", row=1, col=1)
    fig.update_yaxes(title_text="成交量(張)", rangemode="tozero", row=2, col=1)

    # ----------------------------
    # 處置期間色塊(若有)
    # ----------------------------
    # X 軸是連續整數位置(x_pos)，不是真實日期，所以色塊範圍要換算成
    # 「處置期間內、落在目前可視交易日範圍中」的第一筆與最後一筆 x_pos，
    # 不能直接拿日期當座標套用，否則色塊會完全偏離圖表可視範圍
    if disposition_periods:
        trade_date_list = pd.to_datetime(d["trade_date"]).dt.date.tolist()
        for p_start, p_end in disposition_periods:
            idxs = [i for i, td in enumerate(trade_date_list) if p_start <= td <= p_end]
            if not idxs:
                continue
            vx0 = idxs[0] - 0.5
            vx1 = idxs[-1] + 0.5
            for row_i in (1, 2):
                fig.add_vrect(
                    x0=vx0, x1=vx1,
                    fillcolor="#ff7f0e", opacity=0.15, line_width=0,
                    row=row_i, col=1,
                )

    fig.update_layout(
        title=f"{stock_id} {name}｜最近{len(d)}個交易日",
        height=650,
        hovermode="closest",
        hoverdistance=60,
        bargap=0.15,
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=1.02,
            xanchor="left",
            x=0,
        ),
        margin=dict(l=40, r=20, t=80, b=40),
    )

    return fig