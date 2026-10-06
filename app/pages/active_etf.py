"""
主動式ETF買賣功能

追蹤主動式ETF每日持股明細，用前後兩個交易日的持股相減，
判斷主動式ETF本日買賣了哪些標的、買賣多少張，並提供四種綜合訊號分析。

對應資料表：etf_master、etf_holdings_daily(schema 定義在專案根目錄的 schema.sql)
"""

import datetime
import re
import time
import pandas as pd
import streamlit as st
from requests.exceptions import RequestException
from sqlalchemy import text

from app.common.charts import add_ma_columns, plot_ma_signal_candlestick
from app.common.cmoney import fetch_bearer_token
from app.common.db import get_engine
from app.common.disposition import build_disposition_stock_options, load_disposition_map
from app.common.formatting import style_rows_by_sign
from app.common.http import get_relaxed_session
from app.common.help_tooltip import apply_help_tooltip_top_right
from app.common.stock_price_data import load_price_history_single

CMONEY_HOLDINGS_URL = "https://www.cmoney.tw/api/customReport/app/v2/dtno/JsonCsv"
CMONEY_HOLDINGS_DTNO = 59449513
CMONEY_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": "https://www.cmoney.tw",
    "Referer": "https://www.cmoney.tw/",
}
# 回傳欄位依名稱比對(欄名含此關鍵字即可)，不用位置索引，避免CMoney調整欄位順序時資料錯位
HOLDINGS_COLUMN_KEYWORDS = {
    "trade_date_raw": "日期",
    "stock_id": "標的代號",
    "stock_name": "標的名稱",
    "weight_pct": "權重",
    "shares_held": "持有數",
    "unit": "單位",
}

ETF_HOLDINGS_KEEP_TRADE_DATES = 6  # 每檔ETF保留最近幾個交易日資料(緩衝週末/補抓)


def normalize_date_to_yyyymmdd(s: str) -> str:
    """把日期字串統一轉成 yyyymmdd(西元8碼)，支援西元8碼與民國7碼格式。"""
    if s is None:
        return ""
    digits = re.sub(r"\D", "", str(s).strip())
    if len(digits) >= 8 and digits[:4] in ("19", "20"):
        return digits[:8]
    if len(digits) == 7:
        roc_year = int(digits[:3])
        return f"{roc_year + 1911:04d}{digits[3:]}"
    return digits[:8] if len(digits) >= 8 else ""


def describe_api_response(resp, j) -> str:
    """整理API實際回應(HTTP狀態、最上層鍵與型別、body開頭)，用來判斷是改版、尚未公布還是被擋。"""
    if isinstance(j, dict):
        shape = {k: (f"list[{len(v)}]" if isinstance(v, list) else type(v).__name__) for k, v in j.items()}
    else:
        shape = type(j).__name__
    return f"HTTP {resp.status_code}｜最上層={shape}｜body前300字={resp.text[:300]!r}"


def fetch_etf_holdings(etf_id: str, token: str) -> pd.DataFrame:
    """抓取單一主動式ETF的當日持股明細(只留個股資料、單位=股，忽略國外股票、ETF、現金等非個股明細)，回傳可直接寫入DB的DataFrame。"""
    payload = {
        "Dtno": CMONEY_HOLDINGS_DTNO,
        "FilterNo": "0",
        "Params": f"AssignID={etf_id};MTPeriod=0;DTMode=0;DTRange=1;DTOrder=1;MajorTable=M722;",
    }
    headers = {**CMONEY_HEADERS, "Authorization": f"Bearer {token}"}

    try:
        resp = get_relaxed_session().post(CMONEY_HOLDINGS_URL, headers=headers, json=payload, timeout=30)
        if not resp.ok:
            raise RuntimeError(f"{etf_id} HTTP {resp.status_code}：{resp.text[:300]}")
        j = resp.json()
    except RequestException as e:
        raise RuntimeError(f"{etf_id} HTTP請求失敗：{e}") from e

    columns = j.get("columns") if isinstance(j, dict) else None
    rows = j.get("rows") if isinstance(j, dict) else None
    if not isinstance(columns, dict) or not isinstance(rows, list) or len(rows) == 0:
        raise ValueError(f"{etf_id} API回傳異常或為空｜{describe_api_response(resp, j)}")

    col_names = list(columns.keys())
    col_idx = {}
    for field, keyword in HOLDINGS_COLUMN_KEYWORDS.items():
        matched = [i for i, name in enumerate(col_names) if keyword in name]
        if not matched:
            raise ValueError(f"{etf_id} 回傳欄位找不到含「{keyword}」的欄位，實際欄位：{col_names}")
        col_idx[field] = matched[0]

    raw_df = pd.DataFrame(rows)
    df = pd.DataFrame({field: raw_df[i] for field, i in col_idx.items()})
    df["stock_id"] = df["stock_id"].astype(str).str.strip()
    df["weight_pct"] = pd.to_numeric(df["weight_pct"], errors="coerce")
    df["shares_held"] = pd.to_numeric(df["shares_held"], errors="coerce")

    df = df[df["unit"].astype(str).str.strip() == "股"].copy()
    df = df[df["stock_id"].str.match(r"^\d{4}$")].copy()

    if df.empty:
        raise ValueError(f"{etf_id} 過濾後無有效持股資料")

    trade_date_str = normalize_date_to_yyyymmdd(df["trade_date_raw"].iloc[0])
    df["trade_date"] = datetime.datetime.strptime(trade_date_str, "%Y%m%d").date()
    df["etf_code"] = etf_id

    return df[["etf_code", "trade_date", "stock_id", "stock_name", "shares_held", "weight_pct"]]


def upsert_etf_holdings(df: pd.DataFrame) -> None:
    """把單檔ETF單日持股明細寫入資料庫，同一天重複抓取會覆蓋更新。"""
    with get_engine().begin() as conn:
        df.to_sql("_staging_etf_holdings", conn, if_exists="replace", index=False)
        conn.execute(text("""
            INSERT INTO etf_holdings_daily (
                etf_code, trade_date, stock_id, stock_name, shares_held, weight_pct
            )
            SELECT etf_code, trade_date, stock_id, stock_name, shares_held, weight_pct
            FROM _staging_etf_holdings
            ON CONFLICT (etf_code, trade_date, stock_id) DO UPDATE SET
                stock_name  = EXCLUDED.stock_name,
                shares_held = EXCLUDED.shares_held,
                weight_pct  = EXCLUDED.weight_pct
        """))
        conn.execute(text("DROP TABLE _staging_etf_holdings"))


def cleanup_old_etf_snapshots(etf_code: str, keep_days: int = ETF_HOLDINGS_KEEP_TRADE_DATES) -> None:
    """只保留每檔ETF最近N個交易日的持股資料，避免資料無限累積。"""
    with get_engine().begin() as conn:
        conn.execute(text("""
            DELETE FROM etf_holdings_daily
            WHERE etf_code = :etf_code
              AND trade_date NOT IN (
                  SELECT DISTINCT trade_date FROM etf_holdings_daily
                  WHERE etf_code = :etf_code
                  ORDER BY trade_date DESC
                  LIMIT :keep_days
              )
        """), {"etf_code": etf_code, "keep_days": keep_days})


def list_all_etf_master() -> pd.DataFrame:
    return pd.read_sql(
        "SELECT etf_code, is_active FROM etf_master ORDER BY etf_code", get_engine()
    )


def add_etf_to_master(etf_code: str) -> None:
    """新增ETF代號到清單；若代號已存在則重新設為追蹤中。"""
    etf_code = etf_code.strip().upper()
    with get_engine().begin() as conn:
        conn.execute(text("""
            INSERT INTO etf_master (etf_code, is_active)
            VALUES (:etf_code, TRUE)
            ON CONFLICT (etf_code) DO UPDATE SET is_active = TRUE
        """), {"etf_code": etf_code})


def delete_etf_from_master(etf_code: str) -> None:
    """刪除ETF代號，同時清掉該ETF累積的持股資料。"""
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM etf_holdings_daily WHERE etf_code = :etf_code"), {"etf_code": etf_code})
        conn.execute(text("DELETE FROM etf_master WHERE etf_code = :etf_code"), {"etf_code": etf_code})


def fetch_all_active_etfs() -> tuple[list, list]:
    """迴圈抓取 etf_master 中所有 is_active=TRUE 的主動式ETF，回傳(成功清單, 失敗清單)。"""
    active_codes = pd.read_sql(
        "SELECT etf_code FROM etf_master WHERE is_active = TRUE ORDER BY etf_code", get_engine()
    )["etf_code"].tolist()

    try:
        token = fetch_bearer_token()  # 訪客token約25小時過期，每次批次抓取重新取得一次，所有ETF共用
    except Exception as e:
        return [], [(code, f"取得CMoney訪客token失敗：{e}") for code in active_codes]

    success, failed = [], []
    for code in active_codes:
        try:
            df = fetch_etf_holdings(code, token)
            upsert_etf_holdings(df)
            cleanup_old_etf_snapshots(code)
            success.append(code)
        except Exception as e:
            failed.append((code, str(e)))
        time.sleep(0.8)  # 避免短時間內對同一API連續高頻請求

    return success, failed


def get_recent_etf_trade_dates(etf_code: str, limit: int = 4) -> list:
    df = pd.read_sql("""
        SELECT DISTINCT trade_date FROM etf_holdings_daily
        WHERE etf_code = %(etf_code)s
        ORDER BY trade_date DESC
        LIMIT %(limit)s
    """, get_engine(), params={"etf_code": etf_code, "limit": limit})
    return df["trade_date"].tolist()  # [最新, 次新, ...]


def get_etf_holdings_diff(etf_code: str, new_date, old_date) -> pd.DataFrame:
    """算出兩個交易日之間的持股變化，只回傳有變化的標的。"""
    df = pd.read_sql("""
        WITH new_h AS (
            SELECT stock_id, stock_name, shares_held
            FROM etf_holdings_daily WHERE etf_code=%(etf_code)s AND trade_date=%(new_date)s
        ),
        old_h AS (
            SELECT stock_id, stock_name, shares_held
            FROM etf_holdings_daily WHERE etf_code=%(etf_code)s AND trade_date=%(old_date)s
        )
        SELECT
            COALESCE(n.stock_id, o.stock_id) AS "股票代號",
            COALESCE(n.stock_name, o.stock_name) AS "股票名稱",
            COALESCE(n.shares_held, 0) - COALESCE(o.shares_held, 0) AS shares_change
        FROM new_h n
        FULL OUTER JOIN old_h o ON n.stock_id = o.stock_id
        WHERE COALESCE(n.shares_held, 0) - COALESCE(o.shares_held, 0) != 0
        ORDER BY shares_change DESC
    """, get_engine(), params={"etf_code": etf_code, "new_date": new_date, "old_date": old_date})
    df["買賣張數"] = (df["shares_change"] / 1000).round(2)
    return df[["股票代號", "股票名稱", "買賣張數"]]


def load_all_etf_holdings() -> pd.DataFrame:
    """撈出所有ETF目前保留的持股(etf_holdings_daily 每檔ETF只留最近幾天，資料量小，直接整批撈回來在 pandas 裡處理)。"""
    return pd.read_sql(
        "SELECT etf_code, trade_date, stock_id, stock_name, shares_held FROM etf_holdings_daily",
        get_engine(),
    )


def compute_etf_multi_signals() -> dict:
    """整合所有主動式ETF的持股，計算四種跨ETF買賣資訊。"""
    raw = load_all_etf_holdings()
    empty = pd.DataFrame()
    result = {
        "multi_etf_latest": empty,
        "all_etf_net_buy_streak": empty,
        "single_etf_buy_streak": empty,
        "single_day_big_trade": empty,
        "note": "",
    }
    if raw.empty:
        result["note"] = "目前資料庫沒有任何ETF持股資料，需先抓取資料。"
        return result

    raw = raw.copy()
    raw["lots"] = raw["shares_held"] / 1000.0

    date_count = raw.groupby("etf_code")["trade_date"].nunique()
    # rank=1 為該ETF最新的交易日資料，數字越大代表越舊
    raw["date_rank"] = raw.groupby("etf_code")["trade_date"].rank(method="dense", ascending=False).astype(int)
    rank_date_map = raw.groupby(["etf_code", "date_rank"])["trade_date"].max()

    # ----篩選條件1：最新一天，>=3檔ETF同時買進的股票----
    etfs_with_2days = date_count[date_count >= 2].index
    df1 = raw[raw["etf_code"].isin(etfs_with_2days) & raw["date_rank"].isin([1, 2])]
    if not df1.empty:
        pivot1 = df1.pivot_table(
            index=["etf_code", "stock_id", "stock_name"], columns="date_rank", values="lots", aggfunc="first",
        ).reset_index()
        for r in (1, 2):
            if r not in pivot1.columns:
                pivot1[r] = 0.0
        pivot1 = pivot1.rename(columns={1: "latest_lots", 2: "prev_lots"}).fillna({"latest_lots": 0.0, "prev_lots": 0.0})
        pivot1["買賣張數"] = (pivot1["latest_lots"] - pivot1["prev_lots"]).round(2)
        pivot1 = pivot1[pivot1["買賣張數"] != 0].copy()
        pivot1["交易日期"] = pivot1["etf_code"].map(lambda c: rank_date_map.get((c, 1)))

        # ----篩選條件4：最新一天，單一ETF買入或賣出達到100張以上(含)的個股----
        big_trade = pivot1[pivot1["買賣張數"].abs() >= 100].copy()
        if not big_trade.empty:
            big_trade["買賣方向"] = big_trade["買賣張數"].apply(lambda x: "買超" if x > 0 else "賣超")
            detail4 = big_trade[
                ["etf_code", "stock_id", "stock_name", "交易日期", "買賣張數", "買賣方向"]
            ].rename(columns={"etf_code": "ETF代號", "stock_id": "股票代號", "stock_name": "股票名稱"})
            detail4 = detail4.sort_values("買賣張數", key=lambda s: s.abs(), ascending=False)
            result["single_day_big_trade"] = detail4.reset_index(drop=True)

        buy_counts = pivot1[pivot1["買賣張數"] > 0].groupby("stock_id")["etf_code"].nunique()
        target_ids = buy_counts[buy_counts >= 3].index.tolist()
        if target_ids:
            detail = pivot1[pivot1["stock_id"].isin(target_ids)][
                ["stock_id", "stock_name", "etf_code", "交易日期", "買賣張數"]
            ].rename(columns={"stock_id": "股票代號", "stock_name": "股票名稱", "etf_code": "ETF代號"})
            detail = detail.sort_values(["股票代號", "買賣張數"], ascending=[True, False])
            result["multi_etf_latest"] = detail.reset_index(drop=True)

    # ----篩選條件2、3：需要至少4天資料才能算出「連續3天」的每日買賣張數----
    etfs_with_4days = date_count[date_count >= 4].index
    df23 = raw[raw["etf_code"].isin(etfs_with_4days) & raw["date_rank"].isin([1, 2, 3, 4])]
    if not df23.empty:
        pivot23 = df23.pivot_table(
            index=["etf_code", "stock_id", "stock_name"], columns="date_rank", values="lots", aggfunc="first",
        ).reset_index()
        for r in (1, 2, 3, 4):
            if r not in pivot23.columns:
                pivot23[r] = 0.0
        pivot23 = pivot23.fillna(0.0)
        # diff_1：最新一天 vs 次新一天的買賣張數；diff_2：再往前一天；diff_3：再往前一天(共3天)
        pivot23["diff_1"] = (pivot23[1] - pivot23[2]).round(2)
        pivot23["diff_2"] = (pivot23[2] - pivot23[3]).round(2)
        pivot23["diff_3"] = (pivot23[3] - pivot23[4]).round(2)

        day_labels = {r: rank_date_map[rank_date_map.index.get_level_values(1) == r].max() for r in (1, 2, 3, 4)}

        # 篩選條件2：不分ETF，加總所有ETF後，最近3天每天都是淨買超
        # 只以「股票代號」為唯一值加總——同一股票代號在不同ETF資料來源可能對應到
        # 不太一樣的中文名稱(e.g. 「精測」與「中華精測科技」代號同為6510)，
        # 若連 stock_name 一起 groupby 會被誤判成兩檔不同股票，導致加總分裂、
        # 連續3天淨買超的判斷失真。因此只用 stock_id 作為PK，
        # 股票名稱另外用 stock_id 對應到「其中一個股票名稱」。
        stock_name_map = pivot23.groupby("stock_id")["stock_name"].first()
        agg2 = pivot23.groupby("stock_id")[["diff_1", "diff_2", "diff_3"]].sum().round(2).reset_index()
        agg2["stock_name"] = agg2["stock_id"].map(stock_name_map)
        cond2 = (agg2["diff_1"] > 0) & (agg2["diff_2"] > 0) & (agg2["diff_3"] > 0)
        result2 = agg2[cond2].rename(columns={
            "stock_id": "股票代號", "stock_name": "股票名稱",
            "diff_1": f"最新交易日買超張數({day_labels.get(1, '')})",
            "diff_2": f"前1交易日買超張數({day_labels.get(2, '')})",
            "diff_3": f"前2交易日買超張數({day_labels.get(3, '')})",
        })
        # 調整欄位順序：股票代號、股票名稱，再接三天的買超張數
        result2 = result2[[
            "股票代號", "股票名稱",
            f"最新交易日買超張數({day_labels.get(1, '')})",
            f"前1交易日買超張數({day_labels.get(2, '')})",
            f"前2交易日買超張數({day_labels.get(3, '')})",
        ]]
        if not result2.empty:
            result["all_etf_net_buy_streak"] = result2.sort_values(
                by=f"最新交易日買超張數({day_labels.get(1, '')})", ascending=False
            ).reset_index(drop=True)

        # 篩選條件3：單一ETF自己連續3天都買進(該ETF自己的買賣張數連續3天為正)
        cond3 = (pivot23["diff_1"] > 0) & (pivot23["diff_2"] > 0) & (pivot23["diff_3"] > 0)
        result3 = pivot23[cond3][["etf_code", "stock_id", "stock_name", "diff_1", "diff_2", "diff_3"]].rename(
            columns={
                "etf_code": "ETF代號", "stock_id": "股票代號", "stock_name": "股票名稱",
                "diff_1": f"最新交易日買進張數({day_labels.get(1, '')})",
                "diff_2": f"前1交易日買進張數({day_labels.get(2, '')})",
                "diff_3": f"前2交易日買進張數({day_labels.get(3, '')})",
            }
        )
        if not result3.empty:
            result["single_etf_buy_streak"] = result3.sort_values(
                by=f"最新交易日買進張數({day_labels.get(1, '')})", ascending=False
            ).reset_index(drop=True)

    return result


# ----頁面畫面----

def render_active_etf_tab():
    apply_help_tooltip_top_right()
    st.header(
        "主動式ETF買賣",
        help=(
            "主動式ETF本日買賣了哪些標的、買賣多少張。"
        ),
    )

    with st.expander("管理ETF清單(新增／刪除)"):
        master_df = list_all_etf_master()
        st.dataframe(master_df, use_container_width=True, hide_index=True)

        st.markdown("**新增ETF**")
        with st.form("etf_master_add_form", clear_on_submit=True):
            new_code = st.text_input("ETF代號(例如 00400A)", key="etf_master_new_code")
            submitted = st.form_submit_button("新增")
            if submitted:
                if not new_code.strip():
                    st.warning("輸入ETF代號")
                else:
                    add_etf_to_master(new_code)
                    st.success(f"已新增／更新 {new_code.strip().upper()}")
                    st.rerun()

        st.markdown("**刪除ETF**")
        existing_codes = master_df["etf_code"].tolist()
        if existing_codes:
            del_code = st.selectbox("選擇要刪除的ETF", existing_codes, key="etf_master_del_code")
            confirm_del = st.checkbox(f"確認刪除 {del_code}(會一併清除其持股歷史資料)", key="etf_master_del_confirm")
            if st.button("刪除", key="etf_master_del_btn", disabled=not confirm_del):
                delete_etf_from_master(del_code)
                st.success(f"已刪除 {del_code}")
                st.rerun()
        else:
            st.write("目前清單是空的")

    if st.button("抓取今日持股", key="etf_active_fetch_btn"):
        with st.spinner("正在抓取所有主動式ETF持股資料..."):
            success, failed = fetch_all_active_etfs()
        st.success(f"成功抓取 {len(success)} 檔：{', '.join(success)}")
        if failed:
            st.warning(f"抓取失敗 {len(failed)} 檔")
            for code, err in failed:
                st.text(f"　{code}：{err}")

    etf_list = pd.read_sql(
        "SELECT etf_code FROM etf_master WHERE is_active = TRUE ORDER BY etf_code", get_engine()
    )["etf_code"].tolist()

    if not etf_list:
        st.info("etf_master 目前沒有任何ETF代號，需先匯入清單。")
        return

    selected_etf = st.selectbox("選擇ETF", etf_list, key="etf_active_select")

    if selected_etf:
        dates = get_recent_etf_trade_dates(selected_etf, limit=4)
        if len(dates) < 2:
            if dates:
                st.info(
                    f"「{selected_etf}」目前只有 {len(dates)} 筆持股資料({dates[0]})，"
                    "至少需要2個交易日才能算出買賣差異。若資料庫其他ETF已有2天以上資料，"
                    "代表這檔在某次「抓取今日持股」時可能失敗，重新按一次抓取按鈕，"
                    "或檢查上方抓取失敗清單。"
                )
            else:
                st.info(f"「{selected_etf}」目前尚未抓取過任何持股資料，需先按上方「抓取今日持股」。")
            return

        pairs = list(zip(dates[:-1], dates[1:]))[:3]  # 最多近3天diff
        for new_date, old_date in pairs:
            st.markdown(f"**{old_date} → {new_date}**")
            diff_df = get_etf_holdings_diff(selected_etf, new_date, old_date)
            if diff_df.empty:
                st.write("無買賣變化")
            else:
                st.dataframe(
                    diff_df, use_container_width=True, hide_index=True,
                    key=f"etf_diff_{selected_etf}_{old_date}_{new_date}",
                )

    # ----綜合所有主動式ETF資料分析----
    st.divider()
    st.subheader(
        "綜合分析",
    )

    signals = compute_etf_multi_signals()
    if signals["note"]:
        st.info(signals["note"])

    selected_stock_ids = set()

    st.markdown("**① 最新一個交易日，至少3檔ETF同時買進的股票**")
    df_sig1 = signals["multi_etf_latest"]
    if df_sig1.empty:
        st.write("目前沒有符合條件的股票。")
    else:
        st.dataframe(
            style_rows_by_sign(df_sig1, "買賣張數"), use_container_width=True, hide_index=True, key="etf_sig1_multi_buy",
        )
        selected_stock_ids.update(df_sig1["股票代號"].unique().tolist())

    st.markdown("**② 不分ETF加總，最近3天每天皆為買超股票**")
    df_sig2 = signals["all_etf_net_buy_streak"]
    if df_sig2.empty:
        st.write("目前沒有符合條件的股票。")
    else:
        st.dataframe(df_sig2, use_container_width=True, hide_index=True, key="etf_sig2_net_buy_streak")
        selected_stock_ids.update(df_sig2["股票代號"].unique().tolist())

    st.markdown("**③ 單一ETF最近3天連續買進的股票**")
    df_sig3 = signals["single_etf_buy_streak"]
    if df_sig3.empty:
        st.write("目前沒有符合條件的股票。")
    else:
        st.dataframe(df_sig3, use_container_width=True, hide_index=True, key="etf_sig3_single_buy_streak")
        selected_stock_ids.update(df_sig3["股票代號"].unique().tolist())

    st.markdown("**④ 最新一個交易日，單一ETF買入或賣出達到100張以上(含)的股票**")
    df_sig4 = signals["single_day_big_trade"]
    if df_sig4.empty:
        st.write("目前沒有符合條件的股票。")
    else:
        st.dataframe(
            style_rows_by_sign(df_sig4, "買賣張數"), use_container_width=True, hide_index=True, key="etf_sig4_big_trade",
        )
        selected_stock_ids.update(df_sig4["股票代號"].unique().tolist())

    # ----上述①②③④被選到的個股，提供K線圖查詢(畫法與查詢方式比照「站穩均線」分頁)----
    st.divider()
    st.subheader("個股K線圖(上述選出的股票)")

    if not selected_stock_ids:
        st.write("目前沒有符合任一條件的股票可供查詢。")
    else:
        etf_chart_ids = sorted(selected_stock_ids)
        etf_chart_plot_days = st.slider(
            "K線顯示天數", min_value=20, max_value=120, value=60, key="etf_sig_chart_plotdays",
        )
        etf_chart_lookback_days = max(etf_chart_plot_days + 100, 150)
        etf_disposition_map = load_disposition_map()
        etf_id_to_name = {}
        for _df in (df_sig1, df_sig2, df_sig3, df_sig4):
            if not _df.empty and "股票名稱" in _df.columns:
                etf_id_to_name.update(dict(zip(_df["股票代號"], _df["股票名稱"])))
        etf_chart_labels, etf_chart_label_to_id = build_disposition_stock_options(
            etf_chart_ids, etf_disposition_map, etf_id_to_name
        )
        etf_chart_pick_label = st.selectbox("選擇股票代號", etf_chart_labels, key="etf_sig_chart_pick_stock")
        etf_chart_pick = etf_chart_label_to_id[etf_chart_pick_label]
        if etf_chart_pick:
            with st.spinner("查詢個股價量資料中..."):
                etf_price_df = load_price_history_single(etf_chart_pick, etf_chart_lookback_days)
            etf_price_df = add_ma_columns(etf_price_df, restrict_prefixes=False)
            if etf_price_df.empty or etf_price_df[etf_price_df["stock_id"] == etf_chart_pick].empty:
                st.warning(
                    "資料庫目前沒有這檔股票的 stock_price_daily 資料，"
                    "需先至「站穩均線」分頁下載資料。"
                )
            else:
                etf_fig = plot_ma_signal_candlestick(
                    etf_price_df, etf_chart_pick, etf_chart_plot_days,
                    disposition_periods=etf_disposition_map.get(etf_chart_pick, []),
                )
                st.plotly_chart(etf_fig, use_container_width=True, key="etf_sig_candlestick_chart")