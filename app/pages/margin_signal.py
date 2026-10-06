"""
融資減融券增(上市＋上櫃合併頁籤)。

- 上市：連續 N 天「融資賣出 > 買進」且「融券賣出 > 買進」
- 上櫃：連續 N 天「資買-資賣-現償 < 0(融資減)」且「券賣-券買-券償 > 0(融券增)」
- 兩個市場的資料表、下載來源、訊號公式都不同，各自獨立下載，
  分析結果再合併成一張表(用「市場」欄位標示來源)。

對應資料表：margin_trading_daily(上市)、margin_trading_otc_daily(上櫃)
"""

import datetime
import io
import pandas as pd
import requests
import streamlit as st
from sqlalchemy import text

from app.common.charts import add_ma_columns, plot_ma_signal_candlestick
from app.common.constants import STOCK_ID_RE, today_tw
from app.common.db import get_engine, get_latest_date
from app.common.disposition import build_disposition_stock_options, load_disposition_map
from app.common.help_tooltip import apply_help_tooltip_top_right
from app.common.http import get_relaxed_session
from app.common.rate_limit import check_download_rate_limit, warn_if_identical_to_previous_day
from app.common.stock_price_data import load_price_history_single

# ----上市專用----

# 證交所 CSV 實際欄位順序(用來做位置對應，解析用，不能刪減)
TWSE_CSV_COLUMN_ORDER = [
    "stock_id", "stock_name", "margin_buy", "margin_sell", "margin_cash_redemption",
    "margin_prev_balance", "margin_today_balance", "margin_next_limit",
    "short_buy", "short_sell", "short_stock_redemption", "short_prev_balance",
    "short_today_balance", "short_next_limit", "offset_hedge", "note",
]

# 實際要寫入資料庫、給分析邏輯使用的欄位(已清整，只留用得到的，未來可視情況新增欄位，但需先確認資料庫會不會爆掉)
TWSE_COLUMN_ORDER = ["stock_id", "stock_name", "margin_buy", "margin_sell", "short_buy", "short_sell"]


def fetch_from_twse(trade_date: datetime.date) -> pd.DataFrame:
    """從證交所網站下載指定日期的融資融券資料(分類項目：全部)"""
    date_str = trade_date.strftime("%Y%m%d")
    url = (
        "https://www.twse.com.tw/exchangeReport/MI_MARGN"
        f"?response=csv&date={date_str}&selectType=ALL"
    )
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()

    # 證交所 CSV 編碼不固定，兩種都試
    try:
        raw_text = resp.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        raw_text = resp.content.decode("cp950", errors="replace")

    lines = raw_text.splitlines()
    header_idx = next((i for i, line in enumerate(lines) if line.startswith('"代號"')), None)
    if header_idx is None:
        raise ValueError("查無資料，可能是非交易日(例如假日)或該日期尚未公布")

    end_idx = len(lines)
    for i in range(header_idx, len(lines)):
        if lines[i].startswith('"備註'):
            end_idx = i
            break

    main_csv = "\n".join(lines[header_idx:end_idx])
    df = pd.read_csv(io.StringIO(main_csv), engine="python")

    df.iloc[:, 0] = (
        df.iloc[:, 0].astype(str)
        .str.replace('="', '', regex=False)
        .str.replace('"', '', regex=False)
        .str.strip()
    )

    # 先用完整欄位順序做位置對應解析，避免 CSV 欄位錯位
    col_map = dict(zip(df.columns[:len(TWSE_CSV_COLUMN_ORDER)], TWSE_CSV_COLUMN_ORDER))
    df = df.rename(columns=col_map)[TWSE_CSV_COLUMN_ORDER]

    # 只保留股票代號為4碼數字的個股，濾掉 ETF、權證、牛熊證等非一般股票
    # (跟 STOCK_ID_RE 在其他資料源匯入時套用的規則一致)
    df = df[df["stock_id"].str.fullmatch(STOCK_ID_RE)].copy()

    df["trade_date"] = trade_date

    num_cols = [c for c in TWSE_CSV_COLUMN_ORDER if c not in ("stock_id", "stock_name", "note")]
    for col in num_cols:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "", regex=False), errors="coerce"
        )

    # 只保留清整過、實際會用到的欄位再回傳
    return df[["trade_date"] + TWSE_COLUMN_ORDER]


def import_dataframe(df: pd.DataFrame) -> int:
    """把下載好的 DataFrame 寫入資料庫，重複日期+股票自動跳過"""
    with get_engine().begin() as conn:
        df.to_sql("_staging_margin", conn, if_exists="replace", index=False)
        result = conn.execute(text("""
            INSERT INTO margin_trading_daily (
                trade_date, stock_id, stock_name, margin_buy, margin_sell,
                short_buy, short_sell
            )
            SELECT trade_date, stock_id, stock_name, margin_buy, margin_sell,
                   short_buy, short_sell
            FROM _staging_margin
            ON CONFLICT (trade_date, stock_id) DO NOTHING
        """))
        conn.execute(text("DROP TABLE _staging_margin"))
    return result.rowcount


def load_recent_data(n_days: int) -> pd.DataFrame:
    """對外介面不變；先查 margin_trading_daily 最新交易日當版本鍵，資料沒更新就吃快取"""
    version = get_latest_date("margin_trading_daily", "trade_date")
    return _load_recent_data_cached(n_days, version)


@st.cache_data(ttl=21600)  # 6 小時僅為保險網，正常情況靠版本鍵失效
def _load_recent_data_cached(n_days: int, version: str) -> pd.DataFrame:
    query = """
        SELECT stock_id, stock_name, trade_date, margin_buy, margin_sell, short_buy, short_sell
        FROM margin_trading_daily
        WHERE trade_date >= (
            SELECT MIN(trade_date) FROM (
                SELECT DISTINCT trade_date FROM margin_trading_daily
                ORDER BY trade_date DESC LIMIT %(n)s
            ) t
        )
        ORDER BY stock_id, trade_date
    """
    return pd.read_sql(query, get_engine(), params={"n": n_days})


def find_consecutive_signal(df: pd.DataFrame, n_days: int) -> pd.DataFrame:
    cond = (df["margin_sell"] > df["margin_buy"]) & (df["short_sell"] > df["short_buy"])
    hit = (
        cond.groupby(df["stock_id"])
            .rolling(n_days)
            .sum()
            .reset_index(level=0, drop=True)
            .eq(n_days)
    )
    return (
        df.loc[hit, ["stock_id", "stock_name"]]
        .drop_duplicates(subset=["stock_id"])
        .sort_values("stock_id")
    )


# ----上櫃專用----

# 櫃買中心回傳 JSON 表格的實際欄位順序(用來做位置對應，解析用，不能刪減)
OTC_JSON_COLUMN_ORDER = [
    "stock_id", "stock_name", "margin_prev_balance", "margin_buy", "margin_sell",
    "margin_cash_redemption", "margin_balance", "margin_secured_by", "margin_usage_rate",
    "margin_limit", "short_prev_balance", "short_sell", "short_buy", "short_redemption",
    "short_balance", "short_secured_by", "short_usage_rate", "short_limit",
    "offset_hedge", "note",
]

# 實際要寫入資料庫、給分析邏輯使用的欄位(已清整，只留用得到的，未來可視情況新增欄位，但需先確認資料庫會不會爆掉)
OTC_COLUMN_ORDER = [
    "stock_id", "stock_name", "margin_buy", "margin_sell", "margin_cash_redemption",
    "short_buy", "short_sell", "short_redemption",
]


def fetch_from_tpex(trade_date: datetime.date) -> pd.DataFrame:
    """從證券櫃檯買賣中心下載指定日期的上櫃融資融券資料(JSON API)"""
    url = "https://www.tpex.org.tw/www/zh-tw/margin/balance"
    date_str = trade_date.strftime("%Y/%m/%d")
    payload = {"date": date_str, "id": "", "response": "json"}
    session = get_relaxed_session()
    resp = session.post(
        url, json=payload, timeout=15,
        headers={"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"},
    )
    resp.raise_for_status()
    data = resp.json()

    tables = data.get("tables") or []
    if not tables or not tables[0].get("data"):
        raise ValueError("查無資料，可能是非交易日(例如假日)或該日期尚未公布")

    table = tables[0]
    df = pd.DataFrame(table["data"], columns=table["fields"])

    # 只保留代號欄位是純英數字的列，濾掉表尾「合計」等說明文字列
    df = df[df.iloc[:, 0].astype(str).str.match(r"^[A-Za-z0-9]+$")].copy()
    if df.empty:
        raise ValueError("解析後沒有有效資料列，回傳格式可能已變動(回報供校正)")

    # 先用完整欄位順序做位置對應解析，避免 JSON 欄位錯位
    col_map = dict(zip(df.columns[:len(OTC_JSON_COLUMN_ORDER)], OTC_JSON_COLUMN_ORDER))
    df = df.rename(columns=col_map)[OTC_JSON_COLUMN_ORDER]

    # 只保留股票代號為4碼數字的個股，濾掉 ETF、權證、牛熊證等非一般股票
    # (跟 STOCK_ID_RE 在其他資料源匯入時套用的規則一致)
    df = df[df["stock_id"].str.fullmatch(STOCK_ID_RE)].copy()

    df["trade_date"] = trade_date

    num_cols = [c for c in OTC_JSON_COLUMN_ORDER if c not in ("stock_id", "stock_name", "note")]
    for col in num_cols:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "", regex=False), errors="coerce"
        )

    # 只保留清整過、實際會用到的欄位再回傳
    return df[["trade_date"] + OTC_COLUMN_ORDER]


def import_dataframe_otc(df: pd.DataFrame) -> int:
    with get_engine().begin() as conn:
        df.to_sql("_staging_margin_otc", conn, if_exists="replace", index=False)
        result = conn.execute(text(f"""
            INSERT INTO margin_trading_otc_daily (
                trade_date, {", ".join(OTC_COLUMN_ORDER)}
            )
            SELECT trade_date, {", ".join(OTC_COLUMN_ORDER)}
            FROM _staging_margin_otc
            ON CONFLICT (trade_date, stock_id) DO NOTHING
        """))
        conn.execute(text("DROP TABLE _staging_margin_otc"))
    return result.rowcount


def load_recent_data_otc(n_days: int) -> pd.DataFrame:
    """對外介面不變；先查 margin_trading_otc_daily 最新交易日當版本鍵，資料沒更新就吃快取"""
    version = get_latest_date("margin_trading_otc_daily", "trade_date")
    return _load_recent_data_otc_cached(n_days, version)


@st.cache_data(ttl=21600)  # 6 小時僅為保險網，正常情況靠版本鍵失效
def _load_recent_data_otc_cached(n_days: int, version: str) -> pd.DataFrame:
    query = """
        SELECT stock_id, stock_name, trade_date,
               margin_buy, margin_sell, margin_cash_redemption,
               short_buy, short_sell, short_redemption
        FROM margin_trading_otc_daily
        WHERE trade_date >= (
            SELECT MIN(trade_date) FROM (
                SELECT DISTINCT trade_date FROM margin_trading_otc_daily
                ORDER BY trade_date DESC LIMIT %(n)s
            ) t
        )
        ORDER BY stock_id, trade_date
    """
    return pd.read_sql(query, get_engine(), params={"n": n_days})


def find_consecutive_signal_otc(df: pd.DataFrame, n_days: int) -> pd.DataFrame:
    # 融資減：資買 - 資賣 - 現償 < 0 & 融券增：券賣 - 券買 - 券償 > 0
    financing_delta = df["margin_buy"] - df["margin_sell"] - df["margin_cash_redemption"]
    short_delta = df["short_sell"] - df["short_buy"] - df["short_redemption"]
    cond = (financing_delta < 0) & (short_delta > 0)
    hit = (
        cond.groupby(df["stock_id"])
            .rolling(n_days)
            .sum()
            .reset_index(level=0, drop=True)
            .eq(n_days)
    )
    return (
        df.loc[hit, ["stock_id", "stock_name"]]
        .drop_duplicates(subset=["stock_id"])
        .sort_values("stock_id")
    )


# ----頁面畫面----

def render_margin_tab():
    """融資減、融券增(上市＋上櫃合併頁籤，不分市場，同時顯示兩邊篩選結果)。"""
    apply_help_tooltip_top_right()
    st.header("融資融券")

    # ----區塊一：下載資料並匯入----
    st.subheader("下載資料")
    dl_twse_tab, dl_tpex_tab = st.tabs(
        ["上市(證交所)", "上櫃(櫃買中心)"], key="margin_dl_tabs"
    )

    with dl_twse_tab:
        with st.form("margin_twse_download_form"):
            col1, col2 = st.columns([2, 1])
            with col1:
                twse_pick_date = st.date_input(
                    "選擇查詢日期",
                    value=today_tw(),
                    max_value=today_tw(),
                    key="margin_twse_date",
                )
            with col2:
                st.write("")
                st.write("")
                do_download_twse = st.form_submit_button("下載並匯入資料庫", type="primary")

        if do_download_twse:
            limit_msg = check_download_rate_limit("margin_twse", twse_pick_date)
            if limit_msg:
                st.error(limit_msg)
            else:
                with st.spinner(f"下載 {twse_pick_date} 資料中..."):
                    try:
                        fetched_df = fetch_from_twse(twse_pick_date)
                        dup_msg = warn_if_identical_to_previous_day(
                            "margin_trading_daily", "stock_id",
                            ["margin_buy", "margin_sell", "short_buy", "short_sell"],
                            fetched_df, twse_pick_date,
                        )
                        if dup_msg:
                            st.warning(dup_msg)
                        inserted = import_dataframe(fetched_df)
                        st.success(f"共解析 {len(fetched_df)} 筆，新增 {inserted} 筆(重複日期已自動跳過)")
                    except ValueError as e:
                        st.warning(str(e))
                    except Exception as e:
                        st.error(f"下載或匯入失敗：{e}")

    with dl_tpex_tab:
        with st.form("margin_tpex_download_form"):
            col1, col2 = st.columns([2, 1])
            with col1:
                tpex_pick_date = st.date_input(
                    "選擇查詢日期",
                    value=today_tw(),
                    max_value=today_tw(),
                    key="margin_tpex_date",
                )
            with col2:
                st.write("")
                st.write("")
                do_download_tpex = st.form_submit_button("下載並匯入資料庫", type="primary")

        if do_download_tpex:
            limit_msg = check_download_rate_limit("margin_tpex", tpex_pick_date)
            if limit_msg:
                st.error(limit_msg)
            else:
                with st.spinner(f"下載 {tpex_pick_date} 資料中..."):
                    try:
                        fetched_df = fetch_from_tpex(tpex_pick_date)
                        dup_msg = warn_if_identical_to_previous_day(
                            "margin_trading_otc_daily", "stock_id",
                            ["margin_buy", "margin_sell", "margin_cash_redemption",
                             "short_buy", "short_sell", "short_redemption"],
                            fetched_df, tpex_pick_date,
                        )
                        if dup_msg:
                            st.warning(dup_msg)
                        inserted = import_dataframe_otc(fetched_df)
                        st.success(f"共解析 {len(fetched_df)} 筆，新增 {inserted} 筆(重複日期已自動跳過)")
                    except ValueError as e:
                        st.warning(str(e))
                    except Exception as e:
                        st.error(f"下載或匯入失敗：{e}(可改用本機 import_otc_to_supabase.py 手動匯入)")

    st.divider()

    # ----區塊二：分析(上市＋上櫃合併顯示)----
    st.subheader(
        "分析"
    )

    n_days = st.number_input("連續天數", min_value=2, max_value=30, value=3, step=1, key="margin_ndays")

    with st.spinner("查詢資料庫中..."):
        df_twse = load_recent_data(n_days)
        df_otc = load_recent_data_otc(n_days)

    if df_twse.empty and df_otc.empty:
        st.warning(
            "資料庫目前沒有上市或上櫃資料，需先下載，或執行 "
            "import_to_supabase.py／import_otc_to_supabase.py 匯入資料。"
        )
        return

    date_range_msgs = []
    if not df_twse.empty:
        twse_dates = sorted(df_twse["trade_date"].unique())
        date_range_msgs.append(f"上市 {len(twse_dates)} 個交易日：{twse_dates[0]} ~ {twse_dates[-1]}")
    else:
        date_range_msgs.append("上市目前沒有資料")
    if not df_otc.empty:
        otc_dates = sorted(df_otc["trade_date"].unique())
        date_range_msgs.append(f"上櫃 {len(otc_dates)} 個交易日：{otc_dates[0]} ~ {otc_dates[-1]}")
    else:
        date_range_msgs.append("上櫃目前沒有資料")
    st.info("　／　".join(date_range_msgs))

    result_twse = find_consecutive_signal(df_twse, n_days) if not df_twse.empty else pd.DataFrame(
        columns=["stock_id", "stock_name"]
    )
    result_otc = find_consecutive_signal_otc(df_otc, n_days) if not df_otc.empty else pd.DataFrame(
        columns=["stock_id", "stock_name"]
    )
    result_twse = result_twse.assign(market="上市")
    result_otc = result_otc.assign(market="上櫃")
    result = pd.concat([result_twse, result_otc], ignore_index=True).sort_values(["market", "stock_id"])

    st.subheader(f"連續 {n_days} 天符合條件的股票(共 {len(result)} 檔，上市 {len(result_twse)}／上櫃 {len(result_otc)})")
    st.dataframe(
        result.rename(columns={"stock_id": "股票代號", "stock_name": "股票名稱", "market": "市場"}),
        use_container_width=True, hide_index=True,
    )

    csv = result.rename(
        columns={"stock_id": "股票代號", "stock_name": "股票名稱", "market": "市場"}
    ).to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "下載結果 CSV", csv,
        file_name=f"融資減融券增_連續{n_days}天訊號.csv",
        mime="text/csv", key="margin_dl_result",
    )

    st.divider()
    st.subheader("融資減、融券增數量明細")

    if result.empty:
        st.write("目前沒有符合條件的股票。")
    else:
        detail_parts = []
        if not result_twse.empty:
            d = df_twse[df_twse["stock_id"].isin(result_twse["stock_id"])].copy()
            d["融資減少量"] = d["margin_sell"] - d["margin_buy"]
            d["融券增加量"] = d["short_sell"] - d["short_buy"]
            d["market"] = "上市"
            detail_parts.append(d)
        if not result_otc.empty:
            d = df_otc[df_otc["stock_id"].isin(result_otc["stock_id"])].copy()
            d["融資減少量"] = d["margin_buy"] - d["margin_sell"] - d["margin_cash_redemption"]
            d["融券增加量"] = d["short_sell"] - d["short_buy"] - d["short_redemption"]
            d["market"] = "上櫃"
            detail_parts.append(d)

        detail = pd.concat(detail_parts, ignore_index=True)
        detail_table = (
            detail[["market", "stock_id", "stock_name", "trade_date", "融資減少量", "融券增加量"]]
            .rename(columns={
                "market": "市場",
                "stock_id": "股票代號",
                "stock_name": "股票名稱",
                "trade_date": "交易日期",
            })
            .sort_values(["市場", "股票代號", "交易日期"])
        )
        st.dataframe(detail_table, use_container_width=True, hide_index=True)

    # ----區塊三：個股K線圖(資料來源 stock_price_daily，畫法跟「站穩均線」共用)----
    st.divider()
    st.subheader(
        "個股K線圖",
        help=(
            "股票代號存在於資料庫中就可查詢K線圖"
        ),
    )

    chart_ids = sorted(
        sid for sid in result["stock_id"].unique()
        if len(sid) == 4 and sid.isdigit()
    )

    if not chart_ids:
        st.write("目前篩選結果中沒有符合K線圖查詢範圍(4碼股票代號)的股票。")
    else:
        chart_plot_days = st.slider(
            "K線顯示天數", min_value=20, max_value=120, value=60, key="margin_chart_plotdays",
        )
        # 抓多一點回看區間才能算出長期均線(跟站穩均線分頁的作法一致)
        chart_lookback_days = max(chart_plot_days + 100, 150)

        disposition_map = load_disposition_map()
        margin_id_to_name = dict(zip(result["stock_id"], result["stock_name"]))
        chart_labels, chart_label_to_id = build_disposition_stock_options(
            chart_ids, disposition_map, margin_id_to_name
        )
        chart_pick_label = st.selectbox("選擇股票代號", chart_labels, key="margin_chart_pick_stock")
        chart_pick = chart_label_to_id[chart_pick_label]
        if chart_pick:
            with st.spinner("查詢個股價量資料中..."):
                price_df = load_price_history_single(chart_pick, chart_lookback_days)
            price_df = add_ma_columns(price_df, restrict_prefixes=False)
            if price_df.empty or price_df[price_df["stock_id"] == chart_pick].empty:
                st.warning(
                    "資料庫目前沒有這檔股票的 stock_price_daily 資料，"
                    "需先至「站穩均線」分頁下載或匯入。"
                )
            else:
                fig = plot_ma_signal_candlestick(
                    price_df, chart_pick, chart_plot_days,
                    disposition_periods=disposition_map.get(chart_pick, []),
                )
                st.plotly_chart(fig, use_container_width=True, key="margin_candlestick_chart")