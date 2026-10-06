"""
查詢三大法人買賣張數功能(上市＋上櫃合併頁籤)

篩選特定單位(三大法人合計／外資／投信／自營商)連續買超或賣超，
或最近 M 天內至少 K 天符合，並可加上成交量條件。

對應資料表：three_institutional_daily
"""

import datetime
import pandas as pd
import requests
import streamlit as st
from sqlalchemy import text

from app.common.charts import add_ma_columns, plot_ma_signal_candlestick
from app.common.constants import STOCK_ID_RE, STOCK_PRICE_ALLOWED_PREFIXES, today_tw
from app.common.db import get_engine, get_latest_date
from app.common.disposition import build_disposition_stock_options, load_disposition_map
from app.common.help_tooltip import apply_help_tooltip_top_right
from app.common.http import get_relaxed_session
from app.common.parsing import find_field_index
from app.common.rate_limit import check_download_rate_limit, warn_if_identical_to_previous_day
from app.common.stock_price_data import load_price_history_single

# 證交所欄位名稱候選(第一個找不到才退而求其次模糊比對)
T86_CODE_FIELD_CANDIDATES = ["證券代號"]
T86_NAME_FIELD_CANDIDATES = ["證券名稱"]
T86_FOREIGN_FIELD_CANDIDATES = ["外陸資買賣超股數(不含外資自營商)"]
T86_TRUST_FIELD_CANDIDATES = ["投信買賣超股數"]
T86_DEALER_FIELD_CANDIDATES = ["自營商買賣超股數"]
T86_TOTAL_FIELD_CANDIDATES = ["三大法人買賣超股數合計", "三大法人買賣超股數"]


def fetch_from_twse_t86(trade_date: datetime.date) -> pd.DataFrame:
    """從證交所下載指定日期的三大法人買賣超(上市)資料(JSON API)"""
    date_str = trade_date.strftime("%Y%m%d")
    url = "https://www.twse.com.tw/rwd/zh/fund/T86"
    resp = requests.get(
        url, params={"date": date_str, "selectType": "ALL", "response": "json"}, timeout=15
    )
    resp.raise_for_status()
    payload = resp.json()

    if payload.get("stat") != "OK":
        raise ValueError("查無資料，可能是非交易日(例如假日)或該日期尚未公布")

    fields = payload["fields"]
    rows = payload["data"]

    code_idx = find_field_index(fields, T86_CODE_FIELD_CANDIDATES)
    name_idx = find_field_index(fields, T86_NAME_FIELD_CANDIDATES)
    foreign_idx = find_field_index(fields, T86_FOREIGN_FIELD_CANDIDATES)
    trust_idx = find_field_index(fields, T86_TRUST_FIELD_CANDIDATES)
    dealer_idx = find_field_index(fields, T86_DEALER_FIELD_CANDIDATES)
    total_idx = find_field_index(fields, T86_TOTAL_FIELD_CANDIDATES)

    df = pd.DataFrame({
        "stock_id": [str(r[code_idx]).strip() for r in rows],
        "stock_name": [str(r[name_idx]).strip() for r in rows],
        "foreign_net": [r[foreign_idx] for r in rows],
        "trust_net": [r[trust_idx] for r in rows],
        "dealer_net": [r[dealer_idx] for r in rows],
        "institutional_net_total": [r[total_idx] for r in rows],
    })
    df["trade_date"] = trade_date

    num_cols = ["foreign_net", "trust_net", "dealer_net", "institutional_net_total"]
    for col in num_cols:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "", regex=False), errors="coerce"
        )

    # T86 回傳除了一般個股，還會夾雜權證、牛熊證、TDR等代號非4碼數字的列，
    # 該功能只處理一般個股，跟均線／股價分頁一致用 STOCK_ID_RE 篩選掉這些非4碼代號
    df = df[df["stock_id"].str.fullmatch(STOCK_ID_RE)].copy()
    return df


# ----上櫃三大法人：從櫃買中心「三大法人買賣明細資訊」JSON API 直接下載----
# 對應網頁：https://www.tpex.org.tw/zh-tw/mainboard/trading/major-institutional/detail/day.html
# 下載前該頁面要手動把「請選擇類股」由「所有證券」改成
# 「全部(不含權證、牛熊證、可展延牛熊證)」，對應這裡的 sect=EW 參數。
#
# 注意：這支 API 回傳的 fields 陣列裡「買進股數」「賣出股數」「買賣超股數」
# 會重複出現 7 次(分別對應：外資及陸資(不含外資自營商)／外資自營商／
# 外資及陸資／投信／自營商(自行買賣)／自營商(避險)／自營商)，彼此欄位名稱
# 完全相同，沒辦法像 T86 那樣用「名稱比對」找欄位，只能用「位置」比對，
# 位置經實際回傳資料驗證如下(索引從 0 開始，共 24 欄)：
#   0  代號
#   1  名稱
#   2~4   外資及陸資(不含外資自營商)：買進／賣出／買賣超
#   5~7   外資自營商：買進／賣出／買賣超
#   8~10  外資及陸資：買進／賣出／買賣超
#   11~13 投信：買進／賣出／買賣超
#   14~16 自營商(自行買賣)：買進／賣出／買賣超
#   17~19 自營商(避險)：買進／賣出／買賣超
#   20~22 自營商(自行買賣+避險合計)：買進／賣出／買賣超   ← dealer_net 取這裡
#   23    三大法人買賣超股數合計                        ← institutional_net_total
TPEX_BIGD_API_URL = "https://www.tpex.org.tw/www/zh-tw/insti/dailyTrade"
TPEX_BIGD_SECT_ALL_EXCL_WARRANT = "EW"  # 全部(不含權證、牛熊證、可展延牛熊證)
TPEX_BIGD_CODE_IDX = 0
TPEX_BIGD_NAME_IDX = 1
TPEX_BIGD_FOREIGN_IDX = 4    # 外資及陸資(不含外資自營商)-買賣超股數
TPEX_BIGD_TRUST_IDX = 13     # 投信-買賣超股數
TPEX_BIGD_DEALER_IDX = 22    # 自營商-買賣超股數(自行買賣+避險合計)
TPEX_BIGD_TOTAL_IDX = 23     # 三大法人買賣超股數合計
TPEX_BIGD_MIN_FIELDS = 24


def fetch_from_tpex_bigd_api(trade_date: datetime.date) -> pd.DataFrame:
    """從櫃買中心下載指定日期的三大法人買賣超(上櫃)資料(JSON API)"""
    date_str = trade_date.strftime("%Y/%m/%d")
    payload = {
        "type": "Daily",
        "sect": TPEX_BIGD_SECT_ALL_EXCL_WARRANT,
        "date": date_str,
        "id": "",
        "response": "json",
    }
    session = get_relaxed_session()
    resp = session.post(
        TPEX_BIGD_API_URL, data=payload, timeout=15,
        headers={"User-Agent": "Mozilla/5.0"},
    )
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        # 回傳的不是 JSON(可能是網址錯誤、被導向錯誤頁、或櫃買中心改版)，
        # 把實際收到的內容前段印出來，方便對照瀏覽器 DevTools 抓到的網址／格式來修正
        snippet = resp.text[:300].replace("\n", " ")
        raise ValueError(
            f"回傳不是有效的 JSON(HTTP {resp.status_code}，Content-Type: "
            f"{resp.headers.get('Content-Type', '未知')})。"
            f"實際內容開頭：{snippet!r}"
            f"(瀏覽器 F12 → Network 確認正確的 Request URL / Method，"
            f"目前使用的是 POST {TPEX_BIGD_API_URL})"
        )

    tables = data.get("tables") or []
    if not tables or not tables[0].get("data"):
        raise ValueError("查無資料，可能是非交易日(例如假日)或該日期尚未公布")

    table = tables[0]
    fields = table["fields"]
    rows = table["data"]

    if len(fields) < TPEX_BIGD_MIN_FIELDS:
        raise ValueError(f"回傳欄位數不足(實際 {len(fields)} 欄)，格式可能已變動(回報供校正)")

    df = pd.DataFrame({
        "stock_id": [str(r[TPEX_BIGD_CODE_IDX]).strip() for r in rows],
        "stock_name": [str(r[TPEX_BIGD_NAME_IDX]).strip() for r in rows],
        "foreign_net": [r[TPEX_BIGD_FOREIGN_IDX] for r in rows],
        "trust_net": [r[TPEX_BIGD_TRUST_IDX] for r in rows],
        "dealer_net": [r[TPEX_BIGD_DEALER_IDX] for r in rows],
        "institutional_net_total": [r[TPEX_BIGD_TOTAL_IDX] for r in rows],
    })
    df["trade_date"] = trade_date

    num_cols = ["foreign_net", "trust_net", "dealer_net", "institutional_net_total"]
    for col in num_cols:
        df[col] = pd.to_numeric(
            df[col].astype(str).str.replace(",", "", regex=False), errors="coerce"
        )

    # 跟上市T86一樣，上櫃三大法人回傳也會夾雜非4碼一般股票代號的列，一併篩掉
    df = df[df["stock_id"].str.fullmatch(STOCK_ID_RE)].copy()
    return df


def import_dataframe_t86(df: pd.DataFrame) -> int:
    with get_engine().begin() as conn:
        df.to_sql("_staging_t86", conn, if_exists="replace", index=False)
        result = conn.execute(text("""
            INSERT INTO three_institutional_daily (
                trade_date, stock_id, stock_name, foreign_net, trust_net, dealer_net,
                institutional_net_total
            )
            SELECT trade_date, stock_id, stock_name, foreign_net, trust_net, dealer_net,
                   institutional_net_total
            FROM _staging_t86
            ON CONFLICT (trade_date, stock_id) DO NOTHING
        """))
        conn.execute(text("DROP TABLE _staging_t86"))
    return result.rowcount


def load_recent_data_t86(n_days: int) -> pd.DataFrame:
    """對外介面不變；先查 three_institutional_daily 最新交易日當版本鍵，資料沒更新就吃快取"""
    version = get_latest_date("three_institutional_daily", "trade_date")
    return _load_recent_data_t86_cached(n_days, version)


@st.cache_data(ttl=21600)  # 6 小時僅為保險網，正常情況靠版本鍵失效
def _load_recent_data_t86_cached(n_days: int, version: str) -> pd.DataFrame:
    query = """
        SELECT stock_id, stock_name, trade_date,
               foreign_net, trust_net, dealer_net, institutional_net_total
        FROM three_institutional_daily
        WHERE trade_date >= (
            SELECT MIN(trade_date) FROM (
                SELECT DISTINCT trade_date FROM three_institutional_daily
                ORDER BY trade_date DESC LIMIT %(n)s
            ) t
        )
        ORDER BY stock_id, trade_date
    """
    return pd.read_sql(query, get_engine(), params={"n": n_days})


T86_UNIT_OPTIONS = {
    "三大法人合計": "institutional_net_total",
    "外資(不含自營商)": "foreign_net",
    "投信": "trust_net",
    "自營商": "dealer_net",
    "三大法人同時買賣超(三方同買或同賣)": "ALL_THREE",
}

# 「三大法人同時買賣超」只支援連續 N 天模式，不支援「最近 M 天內至少 K 天」
T86_ALL_THREE_UNIT = "ALL_THREE"


def find_t86_signal(
    df: pd.DataFrame,
    unit_col: str,
    direction: str,
    mode: str,
    n_days: int | None = None,
    m_days: int | None = None,
    k_days: int | None = None,
) -> pd.DataFrame:
    """三大法人(或特定單位)買賣超訊號篩選，支援兩種模式：

    - mode="consecutive"：連續 n_days 天同一方向(買超或賣超)
    - mode="at_least"   ：最近 m_days 天中，至少 k_days 天符合同一方向(不需連續，
                           unit_col="ALL_THREE" 時不支援此模式)

    direction: "buy"(買超)或 "sell"(賣超)

    unit_col="ALL_THREE" 時，改用「三方同買/同賣」邏輯：
    - 同買：外資、投信、自營商三者皆 >= 0(沒有任何一方賣超)，且三者不同時為 0
    - 同賣：外資、投信、自營商三者皆 <= 0(沒有任何一方買超)，且三者不同時為 0
    (只要有一方是 0，只要另外兩方沒有反向，仍算同買/同賣；只有三方都是 0
    (完全沒有買賣)時才不列入)
    """
    if unit_col == T86_ALL_THREE_UNIT:
        f, t, d = df["foreign_net"], df["trust_net"], df["dealer_net"]
        all_zero = (f == 0) & (t == 0) & (d == 0)
        if direction == "buy":
            cond = (f >= 0) & (t >= 0) & (d >= 0) & (~all_zero)
        else:
            cond = (f <= 0) & (t <= 0) & (d <= 0) & (~all_zero)
    else:
        cond = df[unit_col] > 0 if direction == "buy" else df[unit_col] < 0

    if mode == "consecutive":
        hit = (
            cond.groupby(df["stock_id"])
                .rolling(n_days)
                .sum()
                .reset_index(level=0, drop=True)
                .eq(n_days)
        )
    else:  # at_least
        recent_dates = sorted(df["trade_date"].unique())[-m_days:]
        if len(recent_dates) < m_days:
            hit = pd.Series(False, index=df.index)
        else:
            sub = df[df["trade_date"].isin(recent_dates)]
            sub_cond = cond.loc[sub.index]
            counts = sub_cond.groupby(sub["stock_id"]).sum()
            days_present = sub.groupby("stock_id")["trade_date"].nunique()
            ok_stocks = set(counts[(counts >= k_days) & (days_present >= m_days)].index)
            hit = df["stock_id"].isin(ok_stocks)

    return (
        df.loc[hit, ["stock_id", "stock_name"]]
        .drop_duplicates(subset=["stock_id"])
        .sort_values("stock_id")
    )


# ----成交量篩選----

def load_recent_volume(n_days: int) -> pd.DataFrame:
    """對外介面不變；先查 stock_price_daily 最新交易日當版本鍵，資料沒更新就吃快取"""
    version = get_latest_date("stock_price_daily", "trade_date")
    return _load_recent_volume_cached(n_days, version)


@st.cache_data(ttl=21600)  # 6 小時僅為保險網，正常情況靠版本鍵失效
def _load_recent_volume_cached(n_days: int, version: str) -> pd.DataFrame:
    """抓最近 n_days 個交易日的成交量資料(來自 stock_price_daily)，
    供三大法人分頁做量能篩選用。volume_shares 單位是股，/1000 才是張。"""
    query = """
        SELECT trade_date, stock_id, volume_shares
        FROM stock_price_daily
        WHERE trade_date >= (
            SELECT MIN(trade_date) FROM (
                SELECT DISTINCT trade_date FROM stock_price_daily
                ORDER BY trade_date DESC LIMIT %(n)s
            ) t
        )
        ORDER BY stock_id, trade_date
    """
    return pd.read_sql(query, get_engine(), params={"n": n_days})


def filter_by_recent_volume(
    candidate_ids,
    vol_df: pd.DataFrame,
    recent_days: int,
    min_vol_lots: float,
) -> set:
    """從候選股票代號中，篩出「最近 recent_days 個交易日，
    每一天成交量都 >= min_vol_lots 張」的股票。
    資料筆數不足 recent_days 天的股票(例如新上市或當天停牌)視為不通過。"""
    candidate_ids = set(candidate_ids)
    if not candidate_ids or vol_df.empty or recent_days <= 0:
        return set()

    vol_df = vol_df[vol_df["stock_id"].isin(candidate_ids)].copy()
    if vol_df.empty:
        return set()

    vol_df["volume_lot"] = vol_df["volume_shares"] / 1000.0
    recent_dates = sorted(vol_df["trade_date"].unique())[-recent_days:]
    if len(recent_dates) < recent_days:
        return set()

    sub = vol_df[vol_df["trade_date"].isin(recent_dates)]
    passed = set()
    for stock_id, g in sub.groupby("stock_id"):
        if len(g) < recent_days:
            continue
        if (g["volume_lot"] >= min_vol_lots).all():
            passed.add(stock_id)
    return passed


# ----頁面畫面----

def render_t86_tab():
    apply_help_tooltip_top_right()
    st.header("三大法人")

    # ----區塊一：從證交所／櫃買中心下載資料並匯入----
    st.subheader("下載資料")
    dl_twse_tab, dl_tpex_tab = st.tabs(
        ["上市(證交所)", "上櫃(櫃買中心)"], key="t86_dl_tabs"
    )

    with dl_twse_tab:
        with st.form("t86_twse_download_form"):
            col1, col2 = st.columns([2, 1])
            with col1:
                pick_date = st.date_input(
                    "選擇查詢日期",
                    value=today_tw(),
                    max_value=today_tw(),
                    key="t86_date",
                )
            with col2:
                st.write("")
                st.write("")
                do_download = st.form_submit_button("下載並匯入資料庫", type="primary")

        if do_download:
            limit_msg = check_download_rate_limit("t86_twse", pick_date)
            if limit_msg:
                st.error(limit_msg)
            else:
                with st.spinner(f"下載 {pick_date} 資料中..."):
                    try:
                        fetched_df = fetch_from_twse_t86(pick_date)
                        dup_msg = warn_if_identical_to_previous_day(
                            "three_institutional_daily", "stock_id",
                            ["foreign_net", "trust_net", "dealer_net", "institutional_net_total"],
                            fetched_df, pick_date,
                        )
                        if dup_msg:
                            st.warning(dup_msg)
                        inserted = import_dataframe_t86(fetched_df)
                        st.success(f"共解析 {len(fetched_df)} 筆，新增 {inserted} 筆(重複日期已自動跳過)")
                    except ValueError as e:
                        st.warning(str(e))
                    except Exception as e:
                        st.error(f"下載或匯入失敗：{e}(可改用本機 import_t86_to_supabase.py 手動匯入)")

    with dl_tpex_tab:
        with st.form("t86_tpex_download_form"):
            col1, col2 = st.columns([2, 1])
            with col1:
                tpex_pick_date = st.date_input(
                    "選擇查詢日期",
                    value=today_tw(),
                    max_value=today_tw(),
                    key="t86_tpex_date",
                )
            with col2:
                st.write("")
                st.write("")
                do_tpex_download = st.form_submit_button("下載並匯入資料庫", type="primary")

        if do_tpex_download:
            limit_msg = check_download_rate_limit("t86_tpex", tpex_pick_date)
            if limit_msg:
                st.error(limit_msg)
            else:
                with st.spinner(f"下載 {tpex_pick_date} 資料中..."):
                    try:
                        fetched_df = fetch_from_tpex_bigd_api(tpex_pick_date)
                        dup_msg = warn_if_identical_to_previous_day(
                            "three_institutional_daily", "stock_id",
                            ["foreign_net", "trust_net", "dealer_net", "institutional_net_total"],
                            fetched_df, tpex_pick_date,
                        )
                        if dup_msg:
                            st.warning(dup_msg)
                        inserted = import_dataframe_t86(fetched_df)
                        st.success(
                            f"共解析 {len(fetched_df)} 筆，新增 {inserted} 筆(重複日期已自動跳過)"
                        )
                    except ValueError as e:
                        st.warning(str(e))
                    except Exception as e:
                        st.error(f"下載或匯入失敗：{e}")
    st.divider()

    # ----區塊二：篩選條件----
    st.subheader("篩選條件", help="篩選特定單位(三大法人合計／外資／投信／自營商)連續買超或賣超，並可加上成交量條件")

    c1, c2 = st.columns(2)
    with c1:
        unit_label = st.selectbox(
            "篩選單位", list(T86_UNIT_OPTIONS.keys()),
            index=list(T86_UNIT_OPTIONS.keys()).index("外資(不含自營商)"),
            key="t86_unit",
        )
        unit_col = T86_UNIT_OPTIONS[unit_label]
    with c2:
        direction_label = st.radio(
            "方向", ["買超", "賣超"], horizontal=True, key="t86_direction",
        )
        direction = "buy" if direction_label == "買超" else "sell"

    is_all_three = unit_col == T86_ALL_THREE_UNIT

    if is_all_three:
        mode = "consecutive"
        n_days = st.number_input(
            "連續天數 N", min_value=2, max_value=30, value=6, step=1, key="t86_ndays_all3",
            help=(
                "「三大法人同時買賣超」只支援連續 N 天模式：三方同買＝外資、投信、自營商皆無反向賣超"
                "(允許某一方為 0)，三方同賣同理；只有三方皆為 0(完全無買賣)才不列入。"
            ),
        )
        m_days, k_days = None, None
        lookback_days = n_days
    else:
        mode_label = st.radio(
            "天數條件模式",
            ["連續 N 天都符合", "最近 M 天內至少 K 天符合(不需連續)"],
            key="t86_mode",
        )

        if mode_label == "連續 N 天都符合":
            mode = "consecutive"
            n_days = st.number_input("連續天數 N", min_value=2, max_value=30, value=6, step=1, key="t86_ndays")
            m_days, k_days = None, None
            lookback_days = n_days
        else:
            mode = "at_least"
            cc1, cc2 = st.columns(2)
            with cc1:
                m_days = st.number_input("最近天數 M", min_value=2, max_value=60, value=10, step=1, key="t86_mdays")
            with cc2:
                k_days = st.number_input(
                    "至少符合天數 K", min_value=1, max_value=int(m_days), value=min(6, int(m_days)),
                    step=1, key="t86_kdays",
                )
            n_days = None
            lookback_days = m_days

    st.markdown("**(選用)成交量條件**")
    use_vol_filter = st.checkbox(
        "同時要求最近幾天成交量都達到門檻",
        value=True,
        key="t86_use_vol",
    )
    vol_recent_days, min_vol_lots = None, None
    if use_vol_filter:
        vc1, vc2 = st.columns(2)
        with vc1:
            vol_recent_days = st.number_input(
                "最近幾天", min_value=1, max_value=30, value=3, step=1, key="t86_vol_days",
            )
        with vc2:
            min_vol_lots = st.number_input(
                "最低成交量門檻(張)", min_value=0, value=1000, step=100, key="t86_vol_min",
            )

    with st.spinner("查詢資料庫中..."):
        df = load_recent_data_t86(lookback_days)

    if df.empty:
        st.warning("資料庫目前沒有三大法人資料，需先下載或執行 import_t86_to_supabase.py 匯入資料。")
        return

    available_dates = sorted(df["trade_date"].unique())
    st.info(f"目前資料涵蓋 {len(available_dates)} 個交易日：{available_dates[0]} ~ {available_dates[-1]}")

    result = find_t86_signal(
        df, unit_col, direction, mode, n_days=n_days, m_days=m_days, k_days=k_days,
    )

    vol_note = ""
    if use_vol_filter and not result.empty:
        with st.spinner("查詢成交量資料中..."):
            vol_df = load_recent_volume(vol_recent_days)
        if vol_df.empty:
            st.warning("資料庫目前沒有 stock_price_daily 成交量資料，已略過成交量條件，需先至「站穩均線」分頁匯入資料。")
        else:
            passed_ids = filter_by_recent_volume(
                result["stock_id"], vol_df, vol_recent_days, min_vol_lots,
            )
            result = result[result["stock_id"].isin(passed_ids)]
            vol_note = f"，且近 {vol_recent_days} 天成交量皆 ≥ {min_vol_lots} 張"

    if mode == "consecutive":
        cond_desc = f"連續 {n_days} 天{direction_label}"
    else:
        cond_desc = f"近 {m_days} 天內至少 {k_days} 天{direction_label}"

    result_display = result.copy()
    if not result_display.empty:
        result_display = result_display.rename(columns={
            "stock_id": "股票代號", "stock_name": "股票名稱",
        })

    st.subheader(f"{unit_label}｜{cond_desc}{vol_note}(共 {len(result)} 檔)")
    st.dataframe(result_display, use_container_width=True, hide_index=True)

    result_csv = result_display.to_csv(index=False).encode("utf-8-sig")
    st.download_button("下載結果 CSV", result_csv, file_name=f"三大法人_{unit_label}_{direction_label}訊號.csv",
                        mime="text/csv", key="t86_dl_result")

    st.divider()
    st.subheader("三大法人買賣超明細")

    if result.empty:
        st.write("目前沒有符合條件的股票。")
    else:
        detail = df[df["stock_id"].isin(result["stock_id"])].copy()
        detail_table = (
            detail[["stock_id", "stock_name", "trade_date",
                     "foreign_net", "trust_net", "dealer_net", "institutional_net_total"]]
            .rename(columns={
                "stock_id": "股票代號",
                "stock_name": "股票名稱",
                "trade_date": "交易日期",
                "foreign_net": "外陸資買賣超",
                "trust_net": "投信買賣超",
                "dealer_net": "自營商買賣超",
                "institutional_net_total": "三大法人合計",
            })
            .sort_values(["股票代號", "交易日期"])
        )
        st.dataframe(detail_table, use_container_width=True, hide_index=True)

    # ----區塊三：個股K線圖(資料來源 stock_price_daily，畫法跟「站穩均線」共用)----
    st.divider()
    st.subheader(
        "個股K線圖",
        help=(
            "只提供代號為4碼的電子股個股K線圖查詢"
            # 沿用「站穩均線」的 ALLOWED_PREFIXES 篩選清單；資料來源為 stock_price_daily
        ),
    )

    chart_ids = sorted(
        sid for sid in result["stock_id"].unique()
        if len(sid) == 4 and sid.isdigit() and sid[:2] in STOCK_PRICE_ALLOWED_PREFIXES
    )

    if not chart_ids:
        st.write("目前篩選結果中沒有符合K線圖查詢範圍(4碼、指定前兩碼)的股票。")
    else:
        chart_plot_days = st.slider(
            "K線顯示天數", min_value=20, max_value=120, value=60, key="t86_chart_plotdays",
        )
        # 抓多一點回看區間才能算出長期均線(跟站穩均線分頁的作法一致)
        chart_lookback_days = max(chart_plot_days + 100, 150)

        disposition_map = load_disposition_map()
        t86_id_to_name = dict(zip(result["stock_id"], result["stock_name"]))
        chart_labels, chart_label_to_id = build_disposition_stock_options(
            chart_ids, disposition_map, t86_id_to_name
        )
        chart_pick_label = st.selectbox("選擇股票代號", chart_labels, key="t86_chart_pick_stock")
        chart_pick = chart_label_to_id[chart_pick_label]
        if chart_pick:
            with st.spinner("查詢個股價量資料中..."):
                price_df = load_price_history_single(chart_pick, chart_lookback_days)
            price_df = add_ma_columns(price_df)
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
                st.plotly_chart(fig, use_container_width=True, key="t86_candlestick_chart")