"""
融資減融券增(上市＋上櫃合併頁籤)。

- 上市：連續 N 天「融資賣出 > 買進」且「融券賣出 > 買進」
- 上櫃：連續 N 天「資買-資賣-現償 < 0(融資減)」且「券賣-券買-券償 > 0(融券增)」
- 兩個市場的資料表、下載來源、訊號公式都不同，各自獨立下載，
  分析結果再合併成一張表(用「市場」欄位標示來源)。

- 個股區塊：融資斷頭價估算(依「融資淨增加」加權推估散戶融資買進區間，再乘上融資成數與維持率130%，並判斷跌破後融資是否死撐) + K線圖

對應資料表：margin_trading_daily(上市)、margin_trading_otc_daily(上櫃)
"""

import datetime
import io
import re

import numpy as np
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
    "margin_prev_balance", "margin_balance", "margin_next_limit",
    "short_buy", "short_sell", "short_stock_redemption", "short_prev_balance",
    "short_today_balance", "short_next_limit", "offset_hedge", "note",
]

# 實際要寫入資料庫、給分析邏輯使用的欄位(已清整，只留用得到的，未來可視情況新增欄位，但需先確認資料庫會不會爆掉)
TWSE_COLUMN_ORDER = [
    "stock_id", "stock_name", "margin_buy", "margin_sell", "margin_cash_redemption",
    "margin_balance", "short_buy", "short_sell",
]


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
    """把下載好的 DataFrame 寫入資料庫；已存在的日期+股票只在「融資餘額尚為空值」時補上現償與餘額，其餘自動跳過"""
    with get_engine().begin() as conn:
        df.to_sql("_staging_margin", conn, if_exists="replace", index=False)
        result = conn.execute(text("""
            INSERT INTO margin_trading_daily (
                trade_date, stock_id, stock_name, margin_buy, margin_sell,
                margin_cash_redemption, margin_balance, short_buy, short_sell
            )
            SELECT trade_date, stock_id, stock_name, margin_buy, margin_sell,
                   margin_cash_redemption, margin_balance, short_buy, short_sell
            FROM _staging_margin
            ON CONFLICT (trade_date, stock_id) DO UPDATE SET
                margin_cash_redemption = EXCLUDED.margin_cash_redemption,
                margin_balance = EXCLUDED.margin_balance
            WHERE margin_trading_daily.margin_balance IS NULL
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
    "margin_balance", "short_buy", "short_sell", "short_redemption",
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
    """寫入上櫃融資融券；已存在的日期+股票只在「融資餘額尚為空值」時補上餘額，其餘自動跳過"""
    with get_engine().begin() as conn:
        df.to_sql("_staging_margin_otc", conn, if_exists="replace", index=False)
        result = conn.execute(text(f"""
            INSERT INTO margin_trading_otc_daily (
                trade_date, {", ".join(OTC_COLUMN_ORDER)}
            )
            SELECT trade_date, {", ".join(OTC_COLUMN_ORDER)}
            FROM _staging_margin_otc
            ON CONFLICT (trade_date, stock_id) DO UPDATE SET
                margin_balance = EXCLUDED.margin_balance
            WHERE margin_trading_otc_daily.margin_balance IS NULL
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


# ----融資斷頭價估算----

MAINTENANCE_RATIO = 1.3  # 融資維持率低於130%會被追繳
DEFAULT_MARGIN_RATIO_PCT = {"上市": 60, "上櫃": 50}
BUY_ZONE_QUANTILES = (0.25, 0.75)  # 融資買進區間取加權分布的25%~75%百分位
LIMIT_DOWN_PCT = 0.10
# 斷頭價低於現價「連續兩個跌停」(約 -19%)就不畫在K線圖上
LIQUIDATION_DRAW_FLOOR_FACTOR = (1 - LIMIT_DOWN_PCT) ** 2

MARGIN_LOOKBACK_DAYS = 60  # 融資回看天數固定為最近60個交易日
HOLDING_MAX_DROP_PCT = 5.0  # 跌破買進區間後融資餘額減少不到5%，視為死撐(套牢中)

LIQUIDATION_HELP = (
    "以最近 60 個交易日內「融資淨增加(融資買進 − 融資賣出 − 現金償還)為正」的交易日，"
    "依淨增加張數與當日收盤價加權，取 25%～75% 百分位作為融資買進區間(可手動調整)。"
    "斷頭價 = 融資買進價 × 融資成數 × 維持率 130%。\n\n"
    "此為估算值：未計入融資利息、券商整戶維持率與期間中途的融資還款；"
    "處置股、警示股的融資成數可能不同。K線圖上只畫出不低於現價兩個跌停(約 -19%)的斷頭價。"
)

HOLDING_HELP = (
    "收盤價跌破融資買進區間下限後，比較最新融資餘額與跌破前一天的餘額(下方百分比為餘額變化)："
    f"減少不到 {HOLDING_MAX_DROP_PCT:g}% 為套牢中(韭菜死撐中)，減少 {HOLDING_MAX_DROP_PCT:g}% 以上為已退場(被割韭菜了)。"
)
HOLDING_HELP_FROM_START = (
    "。跌破發生在回看期間之前，改以期間起點的融資餘額比較"
)

# 五個指標的數值字級縮小，避免「斷頭價區間」這類較長的數字在欄寬不足時換行；只作用在這一組指標
LIQUIDATION_METRIC_CSS = """
<style>
.st-key-margin_liq_metrics [data-testid="stMetricValue"],
.st-key-margin_liq_metrics [data-testid="stMetricValue"] * {
    font-size: 1.15rem !important;
    line-height: 1.4 !important;
}
</style>
"""


def load_margin_history_single(stock_id: str, n_days: int) -> tuple[str | None, pd.DataFrame]:
    """單一股票最近 n_days 個交易日的融資資料，回傳(市場, df)；先查上市、查無再查上櫃，兩邊都沒有回傳(None, 空表)。
    先查兩張表的最新交易日當版本鍵，資料沒更新就吃快取。"""
    version = (
        f"{get_latest_date('margin_trading_daily', 'trade_date')}|"
        f"{get_latest_date('margin_trading_otc_daily', 'trade_date')}"
    )
    return _load_margin_history_single_cached(stock_id, n_days, version)


@st.cache_data(ttl=21600)  # 6 小時僅為保險網，正常情況靠版本鍵失效
def _load_margin_history_single_cached(
    stock_id: str, n_days: int, version: str
) -> tuple[str | None, pd.DataFrame]:
    for market, table in (("上市", "margin_trading_daily"), ("上櫃", "margin_trading_otc_daily")):
        query = f"""
            SELECT trade_date, margin_buy, margin_sell, margin_cash_redemption, margin_balance
            FROM {table}
            WHERE stock_id = %(stock_id)s
              AND trade_date >= (
                SELECT MIN(trade_date) FROM (
                    SELECT DISTINCT trade_date FROM {table}
                    ORDER BY trade_date DESC LIMIT %(n)s
                ) t
            )
            ORDER BY trade_date
        """
        df = pd.read_sql(query, get_engine(), params={"stock_id": stock_id, "n": n_days})
        if not df.empty:
            return market, df
    return None, pd.DataFrame()


def _weighted_quantile(values, weights, q: float) -> float:
    """加權百分位數(q 介於 0~1)，每筆資料的位置取其權重區間的中點"""
    order = np.argsort(values)
    v = np.asarray(values, dtype=float)[order]
    w = np.asarray(weights, dtype=float)[order]
    pos = (np.cumsum(w) - w / 2) / w.sum()
    return float(np.interp(q, pos, v))


def estimate_buy_zone(margin_df: pd.DataFrame, price_df: pd.DataFrame) -> dict:
    """以融資淨增加(買進-賣出-現償)為正的交易日，淨增加張數為權重，對當日收盤價算加權百分位與加權平均成本。
    缺現償或缺股價的日子排除不計。回傳 window_days / used_days / missing_days / increase_total，
    以及 low / high / avg(沒有融資淨增加的日子時為 None)"""
    m = margin_df.copy()
    m["trade_date"] = pd.to_datetime(m["trade_date"]).dt.date
    p = price_df[["trade_date", "close_price"]].copy()
    p["trade_date"] = pd.to_datetime(p["trade_date"]).dt.date
    m = m.merge(p, on="trade_date", how="left")

    need = ["margin_buy", "margin_sell", "margin_cash_redemption", "close_price"]
    m[need] = m[need].apply(pd.to_numeric, errors="coerce")
    valid = m.dropna(subset=need).copy()
    valid["net_increase"] = (
        valid["margin_buy"] - valid["margin_sell"] - valid["margin_cash_redemption"]
    )

    result = {
        "window_days": len(m),
        "used_days": len(valid),
        "missing_days": len(m) - len(valid),
        "increase_total": 0.0,
        "low": None,
        "high": None,
        "avg": None,
    }
    inc = valid[valid["net_increase"] > 0]
    if inc.empty:
        return result

    w = inc["net_increase"]
    c = inc["close_price"]
    result.update(
        increase_total=float(w.sum()),
        low=round(_weighted_quantile(c, w, BUY_ZONE_QUANTILES[0]), 2),
        high=round(_weighted_quantile(c, w, BUY_ZONE_QUANTILES[1]), 2),
        avg=round(float((w * c).sum() / w.sum()), 2),
    )
    return result


def liquidation_price(buy_price: float, margin_ratio_pct: float) -> float:
    """斷頭警戒價 = 融資買進價 × 融資成數 × 維持率130%"""
    return buy_price * margin_ratio_pct / 100 * MAINTENANCE_RATIO


def liquidation_status(price: float, liq_low: float, liq_high: float) -> str:
    if price > liq_high:
        return "尚未觸及"
    if price >= liq_low:
        return "位於區間內"
    return "已跌破區間下限"


def check_margin_holding(margin_df: pd.DataFrame, price_df: pd.DataFrame, platform_low: float) -> dict:
    """收盤價跌破融資買進區間下限(平台下緣)後，檢查融資餘額是否沒減少(死撐)。
    跌破日 = 目前這一段連續收在下限之下的第一天，拿最新融資餘額跟「跌破前一天」的餘額比較；
    減少不到 HOLDING_MAX_DROP_PCT% 為「套牢中」，達到則為「已退場」。
    回傳 status(尚未跌破／資料不足／套牢中／已退場)、change_pct(餘額變化百分比，算不出來為 None)、
    from_window_start(跌破發生在回看期間之前，改跟期間起點的餘額比較)"""
    result = {"status": "資料不足", "change_pct": None, "from_window_start": False}

    m = margin_df[["trade_date", "margin_balance"]].copy()
    m["trade_date"] = pd.to_datetime(m["trade_date"]).dt.date
    p = price_df[["trade_date", "close_price"]].copy()
    p["trade_date"] = pd.to_datetime(p["trade_date"]).dt.date
    p["close_price"] = pd.to_numeric(p["close_price"], errors="coerce")
    p = p.dropna(subset=["close_price"]).sort_values("trade_date")
    p = p[p["trade_date"] >= m["trade_date"].min()]
    t = p.merge(m, on="trade_date", how="left").reset_index(drop=True)
    if t.empty:
        return result

    below = (t["close_price"] < platform_low).tolist()
    if not below[-1]:
        result["status"] = "尚未跌破"
        return result

    # 往回找最近一個沒有跌破的交易日(跌破前一天)，找不到代表跌破發生在回看期間之前
    ref_pos = next((i for i in range(len(below) - 1, -1, -1) if not below[i]), None)
    balance = pd.to_numeric(t["margin_balance"], errors="coerce")
    valid_idx = balance.dropna().index
    if ref_pos is None:
        result["from_window_start"] = True
        ref_idx = valid_idx[0] if len(valid_idx) else None
    else:
        earlier = valid_idx[valid_idx <= ref_pos]
        ref_idx = earlier[-1] if len(earlier) else None
    latest_idx = valid_idx[-1] if len(valid_idx) else None
    # 跌破之後完全沒有融資餘額資料，無法判斷
    if ref_idx is None or latest_idx is None or latest_idx <= ref_idx:
        return result

    ref_balance = float(balance[ref_idx])
    if ref_balance <= 0:
        return result
    change_pct = (float(balance[latest_idx]) - ref_balance) / ref_balance * 100
    result["change_pct"] = change_pct
    result["status"] = "套牢中" if -change_pct < HOLDING_MAX_DROP_PCT else "已退場"
    return result


def select_drawable_liquidation(price: float, liq_low: float, liq_high: float, liq_avg: float) -> dict:
    """挑出要畫在K線圖上的斷頭價：低於現價兩個跌停的不畫；上下限都入選且不相等才畫色帶"""
    floor = price * LIQUIDATION_DRAW_FLOOR_FACTOR
    named = [("斷頭價下限", liq_low), ("斷頭價上限", liq_high), ("平均成本斷頭價", liq_avg)]
    lines, skipped, seen = [], [], set()
    for label, value in named:
        if value < floor:
            skipped.append(label)
        elif round(value, 2) not in seen:
            seen.add(round(value, 2))
            lines.append((value, f"{label} {value:.2f}"))
    band = None
    if liq_low >= floor and liq_high >= floor and round(liq_low, 2) < round(liq_high, 2):
        band = (liq_low, liq_high)
    return {"band": band, "lines": lines, "skipped": skipped}


def _tick_size(price: float) -> float:
    """台股升降單位(依價位區間)，用來當買進區間輸入框的 +/- 步進"""
    if price < 10:
        return 0.01
    if price < 50:
        return 0.05
    if price < 100:
        return 0.1
    if price < 500:
        return 0.5
    if price < 1000:
        return 1.0
    return 5.0


def render_liquidation_block(stock_id: str, price_df: pd.DataFrame) -> dict | None:
    """畫融資斷頭價估算區塊；回傳要畫在K線圖上的價位(select_drawable_liquidation 的結果)，無法估算或都不用畫時回傳 None"""
    st.subheader("融資斷頭價估算", help=LIQUIDATION_HELP)

    stock_prices = price_df[price_df["stock_id"] == stock_id].sort_values("trade_date")
    closes = pd.to_numeric(stock_prices["close_price"], errors="coerce").dropna()
    if closes.empty:
        st.info("這檔股票沒有收盤價資料，無法估算。")
        return None
    current_price = float(closes.iloc[-1])

    col_twse, col_otc = st.columns(2)
    with col_twse:
        ratio_twse = st.number_input(
            "上市融資成數(%)", min_value=10, max_value=100,
            value=DEFAULT_MARGIN_RATIO_PCT["上市"], step=5, key="margin_liq_ratio_twse",
        )
    with col_otc:
        ratio_otc = st.number_input(
            "上櫃融資成數(%)", min_value=10, max_value=100,
            value=DEFAULT_MARGIN_RATIO_PCT["上櫃"], step=5, key="margin_liq_ratio_otc",
        )

    market, margin_df = load_margin_history_single(stock_id, MARGIN_LOOKBACK_DAYS)
    if market is None:
        st.info("資料庫沒有這檔股票的融資資料(可能不是融資標的，或尚未下載)，無法估算。")
        return None

    zone = estimate_buy_zone(margin_df, stock_prices)
    if zone["low"] is None:
        st.info("回看期間內沒有融資淨增加的交易日，無法估算融資買進區間。")
        return None

    # key 帶入建議值，換股票或資料更新造成建議值改變時，輸入框會自動重設為新的建議值
    key_suffix = f"{stock_id}_{zone['low']}_{zone['high']}"
    step = _tick_size(current_price)
    col_low, col_high = st.columns(2)
    with col_low:
        buy_low = st.number_input(
            "融資買進區間下限", min_value=0.01, value=zone["low"], step=step, format="%.2f",
            key=f"margin_liq_low_{key_suffix}",
        )
    with col_high:
        buy_high = st.number_input(
            "融資買進區間上限", min_value=0.01, value=zone["high"], step=step, format="%.2f",
            key=f"margin_liq_high_{key_suffix}",
        )
    if buy_low > buy_high:
        st.error("買進區間下限不能大於上限。")
        return None

    ratio_pct = ratio_twse if market == "上市" else ratio_otc
    liq_low = liquidation_price(buy_low, ratio_pct)
    liq_high = liquidation_price(buy_high, ratio_pct)
    liq_avg = liquidation_price(zone["avg"], ratio_pct)

    holding = check_margin_holding(margin_df, stock_prices, buy_low)
    holding_delta = f"{holding['change_pct']:+.1f}%" if holding["change_pct"] is not None else None
    holding_help = HOLDING_HELP + (HOLDING_HELP_FROM_START if holding["from_window_start"] else "")

    with st.container(key="margin_liq_metrics"):
        st.html(LIQUIDATION_METRIC_CSS)
        c1, c2, c3, c4, c5 = st.columns(5)
        c1.metric("現價", f"{current_price:g}")
        c2.metric("斷頭價區間", f"{liq_low:.2f}～{liq_high:.2f}")
        c3.metric("平均成本斷頭價", f"{liq_avg:.2f}")
        c4.metric("狀態", liquidation_status(current_price, liq_low, liq_high))
        c5.metric("融資狀態", holding["status"], delta=holding_delta, delta_color="off", help=holding_help)

    drawable = select_drawable_liquidation(current_price, liq_low, liq_high, liq_avg)
    if drawable["skipped"]:
        st.caption("未畫在K線圖上(低於現價兩個跌停)：" + "、".join(drawable["skipped"]))
    return drawable if drawable["lines"] else None


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
                        st.success(f"共解析 {len(fetched_df)} 筆，新增或補齊 {inserted} 筆(資料已完整的日期自動跳過)")
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
                        st.success(f"共解析 {len(fetched_df)} 筆，新增或補齊 {inserted} 筆(資料已完整的日期自動跳過)")
                    except ValueError as e:
                        st.warning(str(e))
                    except Exception as e:
                        st.error(f"下載或匯入失敗：{e}")

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
            "資料庫目前沒有上市或上櫃資料，需先下載"
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

    # ----區塊三：個股融資斷頭價估算 + K線圖(K線圖資料來源 stock_price_daily，畫法跟「站穩均線」共用)----
    st.divider()
    st.subheader(
        "選擇個股",
        help=(
            "下拉選單列出上方篩選結果的股票；也可以在輸入框直接輸入4碼股票代號，"
            "查詢資料庫中的任一檔股票(輸入框有值時以輸入框為準)。"
        ),
    )

    chart_ids = sorted(
        sid for sid in result["stock_id"].unique()
        if len(sid) == 4 and sid.isdigit()
    )
    disposition_map = load_disposition_map()
    margin_id_to_name = dict(zip(result["stock_id"], result["stock_name"]))

    chart_pick = None
    col_pick, col_typed = st.columns(2)
    with col_pick:
        if chart_ids:
            chart_labels, chart_label_to_id = build_disposition_stock_options(
                chart_ids, disposition_map, margin_id_to_name
            )
            chart_pick_label = st.selectbox("選擇股票代號", chart_labels, key="margin_chart_pick_stock")
            chart_pick = chart_label_to_id[chart_pick_label]
        else:
            st.caption("目前篩選結果中沒有符合條件的股票，可在右側直接輸入股票代號。")
    with col_typed:
        typed_id = st.text_input("直接輸入股票代號", max_chars=4, key="margin_chart_typed_id").strip()
    if typed_id:
        if re.fullmatch(STOCK_ID_RE, typed_id):
            chart_pick = typed_id
        else:
            chart_pick = None
            st.warning("輸入股票代號的4碼數字。")

    if not chart_pick:
        return

    chart_plot_days = st.slider(
        "K線顯示天數", min_value=20, max_value=120, value=60, key="margin_chart_plotdays",
    )
    # 抓多一點回看區間才能算出長期均線(跟站穩均線分頁的作法一致)；至少150天，也足夠涵蓋融資回看天數上限(120)
    chart_lookback_days = max(chart_plot_days + 100, 150)

    with st.spinner("查詢個股價量資料中..."):
        price_df = load_price_history_single(chart_pick, chart_lookback_days)
    price_df = add_ma_columns(price_df, restrict_prefixes=False)
    if price_df.empty or price_df[price_df["stock_id"] == chart_pick].empty:
        st.warning(
            "資料庫目前沒有這檔股票的 stock_price_daily 資料，"
            "需先至「站穩均線」分頁下載或匯入。"
        )
        return

    liquidation = render_liquidation_block(chart_pick, price_df)

    st.subheader("個股K線圖")
    fig = plot_ma_signal_candlestick(
        price_df, chart_pick, chart_plot_days,
        disposition_periods=disposition_map.get(chart_pick, []),
        liquidation_band=liquidation["band"] if liquidation else None,
        liquidation_lines=liquidation["lines"] if liquidation else None,
    )
    st.plotly_chart(fig, use_container_width=True, key="margin_candlestick_chart")