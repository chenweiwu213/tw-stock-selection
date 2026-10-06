"""
大戶持股比例功能。

資料來源：集保戶股權分散表(臺灣集中保管結算所，data.gov.tw 資料集 11452)
原始欄位固定為：資料日期、證券代號、持股分級、人數、股數、占集保庫存數比例%
持股分級 1~15 為實際級距(15 = 100萬股以上，即「大戶」的最高級距)，
16 是極小的補充級距(實務上通常為0或接近0)，17 是「合計」列(=100%，
不可與其他級距一起加總，否則會重複計算)，因此匯入時一律排除 17。

對應資料表：shareholding_distribution_weekly
"""

import io
import pandas as pd
import requests
import streamlit as st
from sqlalchemy import text

from app.common.charts import add_ma_columns, plot_ma_signal_candlestick
from app.common.constants import STOCK_ID_RE
from app.common.db import get_engine
from app.common.disposition import build_disposition_stock_options, load_disposition_map
from app.common.formatting import style_rows_by_sign
from app.common.help_tooltip import apply_help_tooltip_top_right
from app.common.stock_price_data import load_price_history_single

# 集保結算所開放資料網址：只提供「當週」全市場資料，每週最後一個營業日收盤後更新，
# 無法回溯查詢歷史(要保留歷史資料需自行每週下載備份，再用「上傳本機CSV」匯入)
TDCC_SHAREHOLDING_URL = "https://opendata.tdcc.com.tw/getOD.ashx?id=1-5"
TDCC_HEADERS = {"User-Agent": "Mozilla/5.0"}

SHAREHOLDING_LEVEL_TOTAL = 17  # 「合計」列，匯入時排除


def read_csv_auto_bytes(data: bytes) -> pd.DataFrame:
    """讀取上傳CSV的bytes內容，依序嘗試 utf-8-sig / utf-8 / cp950 編碼。"""
    for enc in ("utf-8-sig", "utf-8", "cp950"):
        try:
            return pd.read_csv(io.BytesIO(data), encoding=enc)
        except Exception:
            continue
    return pd.read_csv(io.BytesIO(data), encoding="utf-8", encoding_errors="replace", engine="python")


def parse_shareholding_df(raw: pd.DataFrame) -> pd.DataFrame:
    """把集保股權分散表原始 DataFrame(不論來自本機CSV或集保官網API，欄位名稱相同)
    整理成可直接寫入 shareholding_distribution_weekly 的格式。欄位用名稱比對，不用位置索引。"""
    df = raw.copy()
    df.columns = [str(c).strip() for c in df.columns]
    df = df.rename(columns={"證券代號": "股票代號"})  # 集保官網欄位名為「證券代號」，內部與畫面統一用「股票代號」

    required = {"資料日期", "股票代號", "持股分級", "人數", "股數", "占集保庫存數比例%"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"缺少必要欄位：{missing}(實際欄位：{list(df.columns)})")

    out = pd.DataFrame({
        "trade_date_raw": df["資料日期"].astype(str).str.strip(),
        "stock_id": (
            df["股票代號"].astype(str)
            .str.replace('="', "", regex=False)
            .str.replace('"', "", regex=False)
            .str.replace("=", "", regex=False)
            .str.strip()
        ),
        "holding_level": pd.to_numeric(df["持股分級"], errors="coerce"),
        "holders_count": pd.to_numeric(
            df["人數"].astype(str).str.replace(",", "", regex=False), errors="coerce"
        ),
        "shares": pd.to_numeric(
            df["股數"].astype(str).str.replace(",", "", regex=False), errors="coerce"
        ),
        "pct_of_custody": pd.to_numeric(
            df["占集保庫存數比例%"].astype(str).str.replace(",", "", regex=False), errors="coerce"
        ),
    })

    # 只留4碼純數字證券代號(濾掉權證、公司債、興櫃代號等)，跟其他分頁的 STOCK_ID_RE 規則一致
    # 0050、0056 等舊制4碼ETF仍會被保留(規則跟站穩均線等分頁一致，若不需要可自行於查詢時排除)
    out = out[out["stock_id"].str.fullmatch(STOCK_ID_RE, na=False)].copy()

    out = out[out["holding_level"].notna()].copy()
    out["holding_level"] = out["holding_level"].astype(int)
    out = out[out["holding_level"] != SHAREHOLDING_LEVEL_TOTAL].copy()  # 排除「合計」列，避免重複計算

    out["trade_date"] = pd.to_datetime(out["trade_date_raw"], format="%Y%m%d", errors="coerce").dt.date
    out = out[out["trade_date"].notna()].copy()

    if out.empty:
        return out

    return out[["trade_date", "stock_id", "holding_level", "holders_count", "shares", "pct_of_custody"]]


def upsert_shareholding_distribution(df: pd.DataFrame) -> int:
    """寫入 shareholding_distribution_weekly，同一週重複匯入會覆蓋更新。回傳寫入筆數。"""
    if df.empty:
        return 0
    with get_engine().begin() as conn:
        df.to_sql("_staging_shareholding", conn, if_exists="replace", index=False)
        conn.execute(text("""
            INSERT INTO shareholding_distribution_weekly (
                trade_date, stock_id, holding_level, holders_count, shares, pct_of_custody
            )
            SELECT trade_date, stock_id, holding_level, holders_count, shares, pct_of_custody
            FROM _staging_shareholding
            ON CONFLICT (trade_date, stock_id, holding_level) DO UPDATE SET
                holders_count  = EXCLUDED.holders_count,
                shares         = EXCLUDED.shares,
                pct_of_custody = EXCLUDED.pct_of_custody
        """))
        conn.execute(text("DROP TABLE _staging_shareholding"))
    return len(df)


def fetch_shareholding_from_tdcc() -> pd.DataFrame:
    """從集保結算所開放資料下載『目前最新一週』的集保戶股權分散表(全市場，含上市、上櫃、興櫃)。
    此網址只提供當週資料，historical資料需自行每週下載備份CSV，再用「上傳本機CSV」匯入資料庫。"""
    resp = requests.get(TDCC_SHAREHOLDING_URL, headers=TDCC_HEADERS, timeout=60)
    resp.raise_for_status()
    resp.encoding = "utf-8-sig"
    raw = pd.read_csv(io.StringIO(resp.text))
    return parse_shareholding_df(raw)


def load_shareholding_dates() -> list:
    return pd.read_sql(
        "SELECT DISTINCT trade_date FROM shareholding_distribution_weekly ORDER BY trade_date DESC",
        get_engine(),
    )["trade_date"].tolist()


def load_stock_name_map() -> pd.DataFrame:
    """shareholding表本身沒有股票名稱欄位，借用 stock_price_daily 現有的證券代號/股票名稱對照做顯示用，
    對照不到的股票名稱留空即可，不影響大戶比例計算本身。"""
    try:
        return pd.read_sql(
            """
            SELECT DISTINCT ON (stock_id) stock_id, stock_name
            FROM stock_price_daily
            ORDER BY stock_id, trade_date DESC
            """,
            get_engine(),
        )
    except Exception:
        return pd.DataFrame(columns=["stock_id", "stock_name"])


def calc_big_holder_ratio(df: pd.DataFrame, level_min: int, level_max: int) -> pd.DataFrame:
    """加總 [level_min, level_max] 級距的『占集保庫存數比例%』，得到每檔股票的『大戶持股比例%』。"""
    cond = df["holding_level"].between(level_min, level_max) & df["pct_of_custody"].notna()
    g = (
        df.loc[cond]
        .groupby("stock_id", as_index=False)["pct_of_custody"]
        .sum()
        .rename(columns={"pct_of_custody": "大戶持股比例%"})
    )
    g["大戶持股比例%"] = g["大戶持股比例%"].round(3)
    return g


def compute_big_holder_change(
    old_date, new_date, level_min: int, level_max: int,
    change_min_pct: float, change_max_pct: float,
) -> pd.DataFrame:
    """比較兩週的大戶持股比例%變化，篩出變動幅度落在 [change_min_pct, change_max_pct) 的股票。"""
    raw = pd.read_sql(
        """
        SELECT trade_date, stock_id, holding_level, pct_of_custody
        FROM shareholding_distribution_weekly
        WHERE trade_date IN (%(old_date)s, %(new_date)s)
        """,
        get_engine(), params={"old_date": old_date, "new_date": new_date},
    )
    if raw.empty:
        return pd.DataFrame()

    old_ratio = calc_big_holder_ratio(raw[raw["trade_date"] == old_date], level_min, level_max)
    new_ratio = calc_big_holder_ratio(raw[raw["trade_date"] == new_date], level_min, level_max)

    old_col = f"大戶持股比例%({old_date})"
    new_col = f"大戶持股比例%({new_date})"
    old_ratio = old_ratio.rename(columns={"大戶持股比例%": old_col})
    new_ratio = new_ratio.rename(columns={"大戶持股比例%": new_col})

    merged = old_ratio.merge(new_ratio, on="stock_id", how="outer")
    merged[old_col] = merged[old_col].fillna(0.0)
    merged[new_col] = merged[new_col].fillna(0.0)

    merged["變動(新-舊)%"] = (merged[new_col] - merged[old_col]).round(3)
    merged["變動方向"] = merged["變動(新-舊)%"].apply(lambda x: "增加" if x >= 0 else "減少")
    merged["變動幅度(絕對值)%"] = merged["變動(新-舊)%"].abs()

    result = merged[
        (merged["變動幅度(絕對值)%"] >= change_min_pct) &
        (merged["變動幅度(絕對值)%"] < change_max_pct)
    ].copy()

    name_map = load_stock_name_map()
    result = result.merge(name_map, on="stock_id", how="left")
    result = result.rename(columns={"stock_id": "股票代號", "stock_name": "股票名稱"})

    cols = ["股票代號", "股票名稱", old_col, new_col, "變動(新-舊)%", "變動方向", "變動幅度(絕對值)%"]
    result = result[[c for c in cols if c in result.columns]]
    result = result.sort_values(["變動幅度(絕對值)%", "股票代號"], ascending=[False, True])
    return result.reset_index(drop=True)


def load_shareholding_ratio_trend(stock_id: str, level_min: int, level_max: int) -> pd.DataFrame:
    """撈出單一股票歷史每週的大戶持股比例%，供趨勢圖使用。"""
    raw = pd.read_sql(
        """
        SELECT trade_date, holding_level, pct_of_custody
        FROM shareholding_distribution_weekly
        WHERE stock_id = %(stock_id)s AND holding_level BETWEEN %(level_min)s AND %(level_max)s
        ORDER BY trade_date
        """,
        get_engine(), params={"stock_id": stock_id, "level_min": level_min, "level_max": level_max},
    )
    if raw.empty:
        return raw
    g = (
        raw.groupby("trade_date", as_index=False)["pct_of_custody"]
        .sum()
        .rename(columns={"pct_of_custody": "大戶持股比例%"})
    )
    g["大戶持股比例%"] = g["大戶持股比例%"].round(3)
    return g


# ----頁面畫面----

def render_big_holder_ratio_tab():
    apply_help_tooltip_top_right()
    st.header("大戶持股比例")

    # ----區塊一：匯入資料----
    st.subheader("匯入資料")
    imp_web_tab, imp_file_tab = st.tabs(
        ["從集保官網匯入最新一週", "上傳本機CSV匯入"], key="big_holder_import_tabs"
    )

    with imp_web_tab:
        if st.button("從集保官網抓取最新一週資料", key="shd_fetch_web_btn"):
            fetch_ok = True
            with st.spinner("正在從集保官網下載資料..."):
                try:
                    parsed = fetch_shareholding_from_tdcc()
                except Exception as e:
                    st.error(f"下載或解析失敗：{e}")
                    parsed = pd.DataFrame()
                    fetch_ok = False
            if not parsed.empty:
                n = upsert_shareholding_distribution(parsed)
                fetched_date = parsed["trade_date"].iloc[0]
                st.success(f"已匯入 {fetched_date} 資料，共 {n} 筆({parsed['stock_id'].nunique()} 檔證券)。")
            elif fetch_ok:
                st.warning("下載成功但解析後沒有有效資料，需確認官網欄位格式是否有變動。")

    with imp_file_tab:
        uploaded_files = st.file_uploader(
            "選擇CSV檔案", type=["csv"], accept_multiple_files=True, key="shd_upload_files",
            help=(
                "上傳集保戶股權分散表CSV(欄位需含：資料日期、股票代號、持股分級、人數、股數、占集保庫存數比例%，"
                "與集保官網下載的格式相同)。可一次選取多個檔案，例如過去每週自行存檔的歷史資料。"
            ),
        )
        if uploaded_files and st.button("開始匯入", key="shd_upload_import_btn"):
            total_rows = 0
            imported_dates = []
            for f in uploaded_files:
                try:
                    raw = read_csv_auto_bytes(f.getvalue())
                    parsed = parse_shareholding_df(raw)
                    if parsed.empty:
                        st.warning(f"{f.name}：解析後沒有有效資料，已略過。")
                        continue
                    n = upsert_shareholding_distribution(parsed)
                    total_rows += n
                    imported_dates.append(str(parsed["trade_date"].iloc[0]))
                except Exception as e:
                    st.error(f"{f.name} 匯入失敗：{e}")
            if total_rows:
                st.success(f"匯入完成，共 {total_rows} 筆，涵蓋交易週：{', '.join(sorted(set(imported_dates)))}")

    # ----區塊二：大戶持股比例變動查詢----
    st.divider()
    st.subheader("大戶持股比例變動查詢")

    available_dates = load_shareholding_dates()  # 新到舊排序
    if len(available_dates) < 2:
        st.info("資料庫目前累積不到2週資料無法比較變動，需先匯入更多週別資料。")
        return

    # 若資料庫目前最新的週別和上次看到的不一樣(例如剛匯入了新一週資料)，
    # 自動把「新／舊週別」選單跳回最新兩週，不需要使用者手動重選。
    # 用 session_state 記錄「上次看到的最新週別」來偵測變化；
    # 一旦偵測到變化，就在 selectbox 建立前先覆寫 session_state 的值。
    latest_date, second_latest_date = available_dates[0], available_dates[1]
    if st.session_state.get("shd_last_seen_latest") != latest_date:
        st.session_state["shd_new_date"] = latest_date
        st.session_state["shd_old_date"] = second_latest_date
        st.session_state["shd_last_seen_latest"] = latest_date

    c1, c2 = st.columns(2)
    with c1:
        new_date = st.selectbox("新週別", available_dates, key="shd_new_date")
    with c2:
        old_options = [d for d in available_dates if d != new_date]
        # 若目前 session_state 存的舊週別因為新週別改變而不在候選清單中了，
        # 回退成 old_options 的第一筆，避免 selectbox 因為 state 值不在 options 裡而噴錯。
        if st.session_state.get("shd_old_date") not in old_options:
            st.session_state["shd_old_date"] = old_options[0]
        old_date = st.selectbox("舊週別", old_options, key="shd_old_date")

    c3, c4, c5 = st.columns(3)
    with c3:
        level_min = st.number_input(
            "大戶分級門檻下限", min_value=1, max_value=15, value=10, key="shd_level_min",
        )
    with c4:
        level_max = st.number_input(
            "大戶分級門檻上限", min_value=int(level_min), max_value=15, value=15, key="shd_level_max",
        )
    with c5:
        direction = st.selectbox("變動方向", ["全部", "只看增加", "只看減少"], key="shd_direction")

    c6, c7 = st.columns(2)
    with c6:
        change_min = st.number_input(
            "變動幅度下限%(絕對值，含)", min_value=0.0, value=6.0, step=0.5, key="shd_change_min",
        )
    with c7:
        change_max = st.number_input(
            "變動幅度上限%(絕對值，不含)", min_value=float(change_min), value=15.0, step=0.5, key="shd_change_max",
        )

    result = compute_big_holder_change(
        old_date, new_date, int(level_min), int(level_max), float(change_min), float(change_max),
    )

    if direction == "只看增加":
        result = result[result["變動方向"] == "增加"]
    elif direction == "只看減少":
        result = result[result["變動方向"] == "減少"]

    st.write(f"{old_date} → {new_date}，符合條件的股票共 {len(result)} 檔")
    st.dataframe(
        style_rows_by_sign(result, "變動(新-舊)%"), use_container_width=True, hide_index=True, key="shd_result_table",
    )

    csv = result.to_csv(index=False).encode("utf-8-sig")
    st.download_button(
        "下載結果 CSV", csv, file_name="大戶持股比例變動.csv", mime="text/csv", key="shd_dl_result",
    )

    # ----區塊三：K線圖----
    st.divider()
    st.subheader("K線圖")

    if result.empty:
        st.write("目前沒有符合條件的股票可供查詢。")
    else:
        disposition_map = load_disposition_map()
        shd_id_to_name = dict(zip(result["股票代號"], result["股票名稱"]))
        picked_labels, picked_label_to_id = build_disposition_stock_options(
            result["股票代號"].tolist(), disposition_map, shd_id_to_name
        )
        picked_label = st.selectbox("選擇股票代號", picked_labels, key="shd_pick_stock")
        picked = picked_label_to_id[picked_label]
        if picked:
            chart_plot_days = st.slider(
                "K線顯示天數", min_value=20, max_value=120, value=60, key="shd_chart_plotdays",
            )
            chart_lookback_days = max(chart_plot_days + 100, 150)
            with st.spinner("查詢個股價量資料中..."):
                price_df = load_price_history_single(picked, chart_lookback_days)
            price_df = add_ma_columns(price_df, restrict_prefixes=False)
            if price_df.empty or price_df[price_df["stock_id"] == picked].empty:
                st.warning(
                    "資料庫目前沒有這檔股票的 stock_price_daily 資料，"
                    "需先至「站穩均線」分頁下載或匯入。"
                )
            else:
                fig = plot_ma_signal_candlestick(
                    price_df, picked, chart_plot_days,
                    disposition_periods=disposition_map.get(picked, []),
                )
                st.plotly_chart(fig, use_container_width=True, key="shd_candlestick_chart")