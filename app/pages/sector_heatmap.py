"""
類股熱度儀表板功能(資金與熱度)。

資料來源：
- 類股對照表：CMoney股市爆料同學會(即時爬取，整份覆蓋式更新)
- 每日開高低收量：沿用 stock_price_daily
- 今日討論度：stock_mention_daily(由 update_stock_mention.py 排程寫入)

對應資料表：sector_category_master, stock_mention_daily(schema.sql)
"""

import io
import json
import os
import re
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from py_mini_racer import MiniRacer
from sqlalchemy import text
from wordcloud import WordCloud

from app.common.constants import STOCK_ID_RE
from app.common.db import get_engine
from app.common.formatting import style_rows_by_sign
from app.common.help_tooltip import apply_help_tooltip_top_right
from app.common.http import get_relaxed_session

# ----今日討論度文字雲(資金與熱度)用常數----
MENTION_MIN_COUNT = 3  # 最低提及次數門檻(非固定TopN)

STOCK_FORUM_URL_TEMPLATE = "https://www.cmoney.tw/forum/stock/{stock_id}"

# wordcloud畫中文需要CJK字型檔，找不到就退回只顯示股票代號
_CJK_FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/System/Library/Fonts/PingFang.ttc",
    "C:/Windows/Fonts/msjh.ttc",
]


def _find_cjk_font_path() -> str | None:
    for path in _CJK_FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    return None

# 抓「任何一個」類股頁面即可：資料是全站共用的(見下方 fetch_sector_category_full() 說明)
SECTOR_CATEGORY_SOURCE_URL = "https://www.cmoney.tw/forum/category/C11010"

SECTOR_CATEGORY_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    )
}

# 分類門檻
SECTOR_HEAT_BUCKETS = ["大跌", "小跌", "持平", "小漲", "大漲"]


def classify_sector_change(pct: float) -> str:
    # 因為當日成交量為0，所以部分資料為空(開盤價/收盤價/漲跌幅為None或NaN)；
    # NaN 與任何數字比較都是 False，會一路落到最後的 else 被誤判成大漲，故歸為持平
    if pd.isna(pct):
        return "持平"
    if pct <= -5.5:
        return "大跌"
    elif pct <= -2:
        return "小跌"
    elif pct < 2:
        return "持平"
    elif pct < 5.5:
        return "小漲"
    else:
        return "大漲"


def _extract_nuxt_script(html: str) -> str:
    """從 CMoney 類股頁面 HTML 裡取出 window.__NUXT__=(function(...){...})(...) 這段 SSR 狀態資料。"""
    marker = "window.__NUXT__=(function("
    start_idx = html.find(marker)
    if start_idx == -1:
        raise ValueError("找不到 window.__NUXT__ 區塊，CMoney網站可能已改版，需要重新確認 HTML 結構")
    end_idx = html.find("</script>", start_idx)
    if end_idx == -1:
        raise ValueError("找不到對應的 </script> 結尾，CMoney網站可能已改版")
    code = html[start_idx:end_idx]
    code = re.sub(r";\s*$", "", code)
    return code  # "window.__NUXT__=(function(...){...})(...)"


def fetch_sector_category_full() -> pd.DataFrame:
    """抓單一CMoney類股頁面，解析內嵌的NUXT SSR資料，回傳全站類股/股票對照表(詳見README)。"""
    session = get_relaxed_session()  # cmoney.tw 跟 tpex.org.tw 一樣有憑證缺 Subject Key Identifier 的問題
    resp = session.get(SECTOR_CATEGORY_SOURCE_URL, headers=SECTOR_CATEGORY_HEADERS, timeout=15)
    resp.raise_for_status()
    nuxt_code = _extract_nuxt_script(resp.text)

    js_program = (
        "var window = {};\n"
        + nuxt_code + ";\n"
        + "(function() {\n"
        "    function deepFind(obj, key, maxDepth, seen) {\n"
        "        if (!obj || typeof obj !== 'object' || maxDepth < 0) return undefined;\n"
        "        if (seen.indexOf(obj) !== -1) return undefined;\n"
        "        seen.push(obj);\n"
        "        if (Object.prototype.hasOwnProperty.call(obj, key)) return obj[key];\n"
        "        var children;\n"
        "        if (obj instanceof Map) {\n"
        "            children = [];\n"
        "            obj.forEach(function(v) { children.push(v); });\n"
        "        } else if (Array.isArray(obj)) {\n"
        "            children = obj;\n"
        "        } else {\n"
        "            children = Object.keys(obj).map(function(k) { return obj[k]; });\n"
        "        }\n"
        "        for (var i = 0; i < children.length; i++) {\n"
        "            var found = deepFind(children[i], key, maxDepth - 1, seen);\n"
        "            if (found !== undefined) return found;\n"
        "        }\n"
        "        return undefined;\n"
        "    }\n"
        "    if (typeof window.__NUXT__ === 'undefined') {\n"
        "        throw new Error('window.__NUXT__ 不存在，CMoney 頁面結構可能已改版');\n"
        "    }\n"
        "    var classMap = deepFind(window.__NUXT__, 'classStockSortDataMap', 14, []);\n"
        "    var stockDict = deepFind(window.__NUXT__, 'stockDict', 14, []);\n"
        "    if (!classMap) {\n"
        "        var topKeys = Object.keys(window.__NUXT__ || {});\n"
        "        var stateKeys = (window.__NUXT__ && window.__NUXT__.state) ? Object.keys(window.__NUXT__.state) : [];\n"
        "        throw new Error('找不到 classStockSortDataMap，CMoney 資料結構可能已改版。'"
        " + ' __NUXT__頂層鍵值=[' + topKeys.join(',') + ']；state鍵值=[' + stateKeys.join(',') + ']');\n"
        "    }\n"
        "    var iterateEntries = function(m, fn) {\n"
        "        if (m instanceof Map) { m.forEach(function(v, k) { fn(v, k); }); }\n"
        "        else { Object.keys(m).forEach(function(k) { fn(m[k], k); }); }\n"
        "    };\n"
        "    var rows = [];\n"
        "    iterateEntries(classMap, function(categoryInfo, mapKey) {\n"
        "        if (!categoryInfo || typeof categoryInfo !== 'object') return;\n"
        "        var categoryLabel = categoryInfo.name || categoryInfo.Name || mapKey;\n"
        "        var rawCodes = categoryInfo.constituentStocks || categoryInfo.ConstituentStocks || '';\n"
        "        if (!rawCodes) return;\n"
        "        var codes = String(rawCodes).split('\u3001')"
        ".map(function(s) { return s.trim(); }).filter(Boolean);\n"
        "        codes.forEach(function(code) {\n"
        "            var info = stockDict ? (stockDict instanceof Map ? stockDict.get(code) : stockDict[code]) : null;\n"
        "            rows.push({\n"
        "                category_label: categoryLabel,\n"
        "                stock_id: code,\n"
        "                stock_name: info ? (info.name || info.Name || '') : ''\n"
        "            });\n"
        "        });\n"
        "    });\n"
        "    if (rows.length === 0) {\n"
        "        var sampleKeys = [];\n"
        "        iterateEntries(classMap, function(categoryInfo) {\n"
        "            if (sampleKeys.length === 0 && categoryInfo && typeof categoryInfo === 'object') {\n"
        "                sampleKeys = Object.keys(categoryInfo);\n"
        "            }\n"
        "        });\n"
        "        throw new Error('找到 classStockSortDataMap 但解析不出任何股票，"
        "categoryInfo 欄位名稱可能已改版。單筆類股物件鍵值=[' + sampleKeys.join(',') + ']');\n"
        "    }\n"
        "    return JSON.stringify(rows);\n"
        "})();\n"
    )
    ctx = MiniRacer()
    result_json = ctx.eval(js_program)
    rows = json.loads(result_json)

    df = pd.DataFrame(rows, columns=["category_label", "stock_id", "stock_name"])
    if df.empty:
        return df
    # 只保留4碼股票代號的正常個股(排除ETF/權證等雜訊)
    df = df[df["stock_id"].str.fullmatch(STOCK_ID_RE)]
    # CMoney原始資料裡，同一檔股票常常橫跨多種分類：
    # 「大產業-細項」格式(連字號)的正式產業別 vs.「概念股_XXX」格式
    # (底線)的個股標籤式分類(如2327國巨同時掛在概念股_Apple、
    # 概念股_被動元件、電子上游-被動元件…等6種標籤下)。
    # 類股熱度儀表板只需要正式產業別，所以只保留連字號格式、
    # 排除底線格式的概念股標籤，並確保一檔股票只對應一筆分類，
    # 避免同一檔股票的多個標籤重複計入熱度分數。
    df = df[df["category_label"].str.contains("-") & ~df["category_label"].str.contains("_")]
    df = df.drop_duplicates(subset=["stock_id"], keep="first")
    return df.drop_duplicates(subset=["category_label", "stock_id"]).reset_index(drop=True)


def replace_sector_category_master(df: pd.DataFrame) -> int:
    """整份覆蓋 sector_category_master(先清空再寫入)，
    避免類股改組後舊分類的股票殘留在對照表裡造成熱度計算失真。"""
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM sector_category_master"))
        if not df.empty:
            df.to_sql("_staging_sector_category", conn, if_exists="replace", index=False)
            conn.execute(text("""
                INSERT INTO sector_category_master (category_label, stock_id, stock_name, updated_at)
                SELECT category_label, stock_id, stock_name, NOW()
                FROM _staging_sector_category
                ON CONFLICT (category_label, stock_id) DO NOTHING
            """))
            conn.execute(text("DROP TABLE _staging_sector_category"))
    return len(df)


def get_sector_category_master_stats() -> dict:
    row = pd.read_sql("""
        SELECT COUNT(DISTINCT category_label) AS n_categories,
               COUNT(DISTINCT stock_id) AS n_stocks,
               MAX(updated_at) AS updated_at
        FROM sector_category_master
    """, get_engine()).iloc[0]
    return dict(row)


@st.cache_data(ttl=300)
def get_sector_sections() -> list:
    """從對照表目前實際存在的 category_label(格式「大產業-細項」)動態算出大產業別清單，
    用於篩選下拉選單；不寫死類股代碼表，CMoney日後改版新增/調整分類也不用改程式。"""
    df = pd.read_sql("SELECT DISTINCT category_label FROM sector_category_master", get_engine())
    if df.empty:
        return []
    return sorted({label.split("-", 1)[0] for label in df["category_label"] if "-" in label})


@st.cache_data(ttl=300)
def load_stock_price_dates() -> list:
    """stock_price_daily 目前有資料的交易日(新到舊排序)，供類股熱度分頁選日期用。"""
    df = pd.read_sql(
        "SELECT DISTINCT trade_date FROM stock_price_daily ORDER BY trade_date DESC", get_engine(),
    )
    return df["trade_date"].tolist()


@st.cache_data(ttl=300)
def load_sector_heat_input(trade_date) -> pd.DataFrame:
    """撈出指定交易日的個股開高低收量與「最近一個有收盤價的前一交易日」資料，配上類股對照表，
    並用 stock_price_daily 的歷史資料現場算出「近5日平均成交量」(前一日往前算5個交易日)。
    回傳欄位跟 sector_heat_dashboard.py 的 compute_stock_metrics() 輸入格式一致。"""
    engine = get_engine()
    prev_date = pd.read_sql(
        "SELECT MAX(trade_date) AS d FROM stock_price_daily WHERE trade_date < %(d)s",
        engine, params={"d": trade_date},
    )["d"].iloc[0]
    if prev_date is None:
        return pd.DataFrame()

    query = """
        WITH t_today AS (
            SELECT stock_id, stock_name, volume_shares AS volume,
                   open_price AS o_open, high_price AS o_high,
                   low_price AS o_low, close_price AS o_close
            FROM stock_price_daily WHERE trade_date = %(today)s
        ),
        t_yday AS (
            -- 每檔股票各自往前找「收盤價不為空值」的最近一個交易日；
            -- 前一天成交量為0(收盤價為空)就往更早的日期找，直到有收盤價為止
            SELECT DISTINCT ON (stock_id)
                   stock_id, open_price AS y_open, high_price AS y_high,
                   low_price AS y_low, close_price AS y_close
            FROM stock_price_daily
            WHERE trade_date < %(today)s AND close_price IS NOT NULL
            ORDER BY stock_id, trade_date DESC
        ),
        vol5 AS (
            SELECT stock_id, AVG(volume_shares) AS avg_vol5
            FROM (
                SELECT stock_id, volume_shares,
                       ROW_NUMBER() OVER (PARTITION BY stock_id ORDER BY trade_date DESC) AS rn
                FROM stock_price_daily
                WHERE trade_date <= %(yday)s
            ) x
            WHERE rn <= 5
            GROUP BY stock_id
        )
        SELECT
            c.category_label AS "股票類別",
            t.stock_id AS "股票代號",
            COALESCE(t.stock_name, c.stock_name) AS "股票名稱",
            t.volume AS "成交量",
            t.o_open AS "今日開盤價", t.o_high AS "今日最高價",
            t.o_low AS "今日最低價", t.o_close AS "今日收盤價",
            y.y_open AS "昨日開盤價", y.y_high AS "昨日最高價",
            y.y_low AS "昨日最低價", y.y_close AS "昨日收盤價",
            v.avg_vol5 AS "近5日平均成交量"
        FROM sector_category_master c
        JOIN t_today t ON t.stock_id = c.stock_id
        LEFT JOIN t_yday y ON y.stock_id = c.stock_id  -- 資料庫完全沒有更早的收盤價才會是None
        LEFT JOIN vol5 v ON v.stock_id = c.stock_id
    """
    df = pd.read_sql(query, engine, params={"today": trade_date, "yday": prev_date})
    # DB驅動對 AVG()/NUMERIC 欄位可能回傳 Decimal，統一轉成 float64 避免後面
    # 數值運算／select_dtypes(include="number") 判斷欄位型別時出錯
    numeric_cols = [
        "成交量", "今日開盤價", "今日最高價", "今日最低價", "今日收盤價",
        "昨日開盤價", "昨日最高價", "昨日最低價", "昨日收盤價", "近5日平均成交量",
    ]
    for col in numeric_cols:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df["日期"] = trade_date
    return df


def compute_sector_stock_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """為每檔股票計算漲跌幅、振幅、量比、漲跌分類(邏輯沿用 sector_heat_dashboard.py)。"""
    df = df.copy()
    df["漲跌幅"] = (df["今日收盤價"] - df["昨日收盤價"]) / df["昨日收盤價"] * 100
    df["振幅"] = (df["今日最高價"] - df["今日最低價"]) / df["昨日收盤價"] * 100
    df["分類"] = df["漲跌幅"].apply(classify_sector_change)
    df["量比"] = df["成交量"] / df["近5日平均成交量"].replace(0, float("nan"))
    return df


def aggregate_sector_by_category(df: pd.DataFrame, weights: dict) -> pd.DataFrame:
    """依「股票類別」彙總出家數/廣度/強度/量能與綜合熱度分數(邏輯沿用 sector_heat_dashboard.py)。"""
    rows = []
    for category, g in df.groupby("股票類別"):
        n = len(g)
        bucket_counts = g["分類"].value_counts().reindex(SECTOR_HEAT_BUCKETS, fill_value=0)

        net_up_ratio = (
            bucket_counts["小漲"] + bucket_counts["大漲"]
            - bucket_counts["小跌"] - bucket_counts["大跌"]
        ) / n
        extreme_ratio = (bucket_counts["大漲"] + bucket_counts["大跌"]) / n
        avg_change = g["漲跌幅"].mean()
        avg_amplitude = g["振幅"].mean()

        if g["量比"].notna().any():
            volume_score = (g["量比"] > 1.5).mean()
            avg_volume_ratio = g["量比"].mean()
        else:
            volume_score = min(avg_amplitude / 10, 1.0)
            avg_volume_ratio = float("nan")

        rows.append({
            "股票類別": category, "家數": n,
            "大跌家數": bucket_counts["大跌"], "小跌家數": bucket_counts["小跌"],
            "持平家數": bucket_counts["持平"], "小漲家數": bucket_counts["小漲"],
            "大漲家數": bucket_counts["大漲"],
            "淨上漲比例": round(net_up_ratio, 3), "極端家數比例": round(extreme_ratio, 3),
            "平均漲跌幅": round(avg_change, 2), "平均振幅": round(avg_amplitude, 2),
            "量能得分": round(volume_score, 3),
            "平均量比": round(avg_volume_ratio, 2) if pd.notna(avg_volume_ratio) else float("nan"),
        })

    result = pd.DataFrame(rows)
    if result.empty:
        return result

    max_abs_change = result["平均漲跌幅"].abs().max() or 1
    strength_score = result["平均漲跌幅"].abs() / max_abs_change

    result["熱度分數"] = (
        weights["strength"] * strength_score
        + weights["extreme"] * result["極端家數比例"]
        + weights["breadth"] * result["淨上漲比例"].abs()
        + weights["volume"] * result["量能得分"]
    ) * 100
    result["熱度分數"] = result["熱度分數"].round(1)

    return result.sort_values("熱度分數", ascending=False).reset_index(drop=True)


# ----今日討論度文字雲(資金與熱度)----

@st.cache_data(ttl=300)
def load_stock_mentions(min_count: int = MENTION_MIN_COUNT) -> pd.DataFrame:
    """撈出提及次數 >= min_count 的股票，補上股票名稱供顯示用(連同trade_date，供對齊即時查詢用的日期)。"""
    query = """
        SELECT
            m.trade_date,
            m.stock_id,
            m.mention_count,
            COALESCE(
                (SELECT stock_name FROM sector_category_master c WHERE c.stock_id = m.stock_id LIMIT 1),
                (SELECT stock_name FROM stock_price_daily p
                    WHERE p.stock_id = m.stock_id ORDER BY p.trade_date DESC LIMIT 1)
            ) AS stock_name
        FROM stock_mention_daily m
        WHERE m.mention_count >= %(min_count)s
        ORDER BY m.mention_count DESC
    """
    df = pd.read_sql(query, get_engine(), params={"min_count": min_count})
    df["mention_count"] = pd.to_numeric(df["mention_count"], errors="coerce")
    return df


@st.cache_data(ttl=300)
def render_mention_wordcloud_image(df: pd.DataFrame) -> bytes:
    """把 {stock_id, stock_name, mention_count} 畫成文字雲PNG，回傳圖片bytes。"""
    font_path = _find_cjk_font_path()
    if font_path:
        labels = [
            f"{row.stock_id} {row.stock_name}" if row.stock_name else row.stock_id
            for row in df.itertuples()
        ]
    else:
        labels = df["stock_id"].tolist()

    frequencies = dict(zip(labels, df["mention_count"]))
    wc = WordCloud(
        font_path=font_path,
        width=1000,
        height=500,
        background_color="white",
        prefer_horizontal=0.95,
    ).generate_from_frequencies(frequencies)

    buf = io.BytesIO()
    wc.to_image().save(buf, format="PNG")
    return buf.getvalue()


@st.cache_data(ttl=86400)
def fetch_stock_name_live(stock_id: str) -> str:
    """sector_category_master／stock_price_daily都查無股名時的備援：抓個股頁面<title>解析中文股名。"""
    try:
        url = STOCK_FORUM_URL_TEMPLATE.format(stock_id=stock_id)
        session = get_relaxed_session()
        resp = session.get(url, headers=SECTOR_CATEGORY_HEADERS, timeout=10)
        resp.raise_for_status()
        match = re.search(r"<title>([^(<]+)\(" + re.escape(stock_id) + r"\)", resp.text)
        return match.group(1).strip() if match else ""
    except Exception:
        return ""


@st.fragment
def render_mention_wordcloud_fragment(mention_df: pd.DataFrame):
    if _find_cjk_font_path() is None:
        st.caption("目前環境找不到中文字型，文字雲暫時只顯示股票代號(不影響次數統計本身)。")

    with st.spinner("繪製文字雲中..."):
        png_bytes = render_mention_wordcloud_image(mention_df)
    st.image(png_bytes, use_container_width=True)


def render_stock_mention_section():
    """今日討論度文字雲，畫在既有內容之下。"""
    st.divider()
    st.subheader(
        "今日討論度",
        help=(
            "資料來源：CMoney股市討論熱門貼文，每日由排程統計提及次數。"
            "資料庫只保留最近一次排程執行的資料，排程執行前顯示的會是前一次的結果。"
        ),
    )

    mention_df = load_stock_mentions(MENTION_MIN_COUNT)
    if mention_df.empty:
        st.info(
            "目前資料庫裡沒有討論度資料，需先確認 update_stock_mention.py 排程是否已成功執行過。"
        )
        return

    # sector_category_master／stock_price_daily都查無股名時，改即時抓個股頁面標題補上
    missing_name = mention_df["stock_name"].isna() | (mention_df["stock_name"] == "")
    if missing_name.any():
        mention_df.loc[missing_name, "stock_name"] = (
            mention_df.loc[missing_name, "stock_id"].apply(fetch_stock_name_live)
        )

    render_mention_wordcloud_fragment(mention_df)


# ----頁面畫面----

def render_sector_heat_tab():
    apply_help_tooltip_top_right()
    st.header("資金與熱度")

    st.subheader("類股對照表維護")
    stats = get_sector_category_master_stats()
    n_cat, n_stock, updated_at = stats["n_categories"], stats["n_stocks"], stats["updated_at"]
    if n_cat:
        st.caption(f"目前已有 {n_cat} 個類股、{n_stock} 檔股票的對照表，最後更新時間：{updated_at}")
    else:
        st.info("尚未建立類股對照表，需先點下方按鈕從CMoney抓取。")

    if st.button("抓取最新類股清單", key="sector_fetch_btn"):
        with st.spinner("正在從CMoney抓取類股完整資料(只需一個頁面，約幾秒鐘)..."):
            try:
                cat_df = fetch_sector_category_full()
            except Exception as e:
                cat_df = None
                st.error(f"抓取失敗：{e}")
        if cat_df is not None:
            if not cat_df.empty:
                n = replace_sector_category_master(cat_df)
                get_sector_sections.clear()
                load_sector_heat_input.clear()
                st.success(f"抓取完成，共 {n} 筆股票分類資料，已整份覆蓋更新對照表。")
                st.rerun()
            else:
                st.error("抓取失敗，沒有解析出任何資料，CMoney網站可能已改版。")

    st.divider()
    st.subheader("今日類股熱度排行")

    price_dates = load_stock_price_dates()
    if not price_dates:
        st.info("資料庫目前沒有 stock_price_daily 資料，需先至「站穩均線」分頁下載或匯入。")
        return
    if not n_cat:
        st.info("需先在上方抓取類股對照表，才能計算類股熱度。")
        return

    c1, c2 = st.columns(2)
    with c1:
        heat_date = st.selectbox("交易日期", price_dates, index=0, key="sector_heat_date")
    section_options = ["全部產業"] + get_sector_sections()
    with c2:
        heat_industry = st.selectbox("產業別篩選", section_options, key="sector_heat_industry")

    with st.expander("進階：熱度分數權重設定"):
        wc1, wc2, wc3, wc4 = st.columns(4)
        with wc1:
            w_strength = st.number_input("強度權重", 0.0, 1.0, 0.35, 0.05, key="sector_w_strength")
        with wc2:
            w_extreme = st.number_input("極端家數權重", 0.0, 1.0, 0.25, 0.05, key="sector_w_extreme")
        with wc3:
            w_breadth = st.number_input("廣度權重", 0.0, 1.0, 0.20, 0.05, key="sector_w_breadth")
        with wc4:
            w_volume = st.number_input("量能權重", 0.0, 1.0, 0.20, 0.05, key="sector_w_volume")
    weights = {"strength": w_strength, "extreme": w_extreme, "breadth": w_breadth, "volume": w_volume}

    with st.spinner("計算類股熱度中..."):
        raw = load_sector_heat_input(heat_date)
    if raw.empty:
        st.warning(f"{heat_date} 沒有可比對的前一交易日資料，無法計算漲跌幅(可能是該股票代號在 stock_price_daily 裡的第一筆資料)。")
        return

    if heat_industry != "全部產業":
        raw = raw[raw["股票類別"].str.startswith(heat_industry)]
    if raw.empty:
        st.write("篩選後沒有資料。")
        return

    stock_metrics = compute_sector_stock_metrics(raw)
    ranking = aggregate_sector_by_category(stock_metrics, weights)

    # ----指標卡----
    n_categories_tracked = len(ranking)
    n_up = int((ranking["淨上漲比例"] > 0).sum())
    n_down = int((ranking["淨上漲比例"] < 0).sum())
    hottest = ranking.iloc[0]["股票類別"] if not ranking.empty else "—"
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("追蹤類股數", n_categories_tracked)
    m2.metric("普漲類股數", n_up)
    m3.metric("普跌類股數", n_down)
    m4.metric("今日最熱類股", hottest)

    # ----熱度總覽色塊圖----
    # 色塊大小用「熱度分數」當標準，不特別分上漲/下跌兩區，全部類股放在同一層。
    # 熱度分數理論上不會是負值，但仍用一個很小的正數墊底，
    # 避免色塊圖因為 values 為 0 而整塊消失或報錯。
    st.markdown("#### 熱度總覽")
    max_abs = ranking["平均漲跌幅"].abs().max() or 1
    tm_sizes = ranking["熱度分數"].fillna(0.01).clip(lower=0.01)
    tm_text = []
    for _, row in ranking.iterrows():
        vr_text = f"{row['平均量比']:.2f}" if pd.notna(row["平均量比"]) else "—"
        tm_text.append(
            f"熱度分數 {row['熱度分數']}<br>平均漲跌 {row['平均漲跌幅']}%<br>"
            f"平均量比 {vr_text}<br>{row['家數']}家"
        )

    treemap_fig = go.Figure(go.Treemap(
        labels=ranking["股票類別"],
        parents=[""] * len(ranking),
        values=tm_sizes,
        text=tm_text,
        texttemplate="%{label}<br>%{text}",  # 明確指定顯示內容，避免Plotly預設另外多顯示一次數值
        marker=dict(
            colors=ranking["平均漲跌幅"],
            colorscale=[[0, "#1B9E5A"], [0.5, "#EDEDED"], [1, "#F64545"]],
            cmid=0, cmin=-max_abs, cmax=max_abs,
            showscale=True, colorbar=dict(title="平均漲跌幅%"),
        ),
    ))
    treemap_fig.update_layout(margin=dict(t=10, l=10, r=10, b=10), height=700)
    st.plotly_chart(treemap_fig, use_container_width=True, key="sector_heat_treemap")

    # ----熱度排行榜----
    st.markdown("#### 熱度排行榜")
    max_show = len(ranking)
    if max_show <= 1:
        top_n = max_show
    else:
        top_n = st.slider(
            "顯示前N名", min_value=1, max_value=min(30, max_show),
            value=min(10, max_show), key="sector_heat_topn",
        )
    top_df = ranking.head(top_n).iloc[::-1]  # 讓長條圖從上到下由熱到冷
    bar_colors = ["#F64545" if v >= 0 else "#1B9E5A" for v in top_df["平均漲跌幅"]]
    rank_fig = go.Figure(go.Bar(
        x=top_df["熱度分數"], y=top_df["股票類別"], orientation="h",
        marker=dict(color=bar_colors),
        text=[f"{v:.1f}" for v in top_df["熱度分數"]], textposition="outside",
    ))
    rank_fig.update_layout(
        margin=dict(t=10, l=10, r=30, b=10), height=max(300, 32 * len(top_df)),
        xaxis_title="熱度分數",
    )
    st.plotly_chart(rank_fig, use_container_width=True, key="sector_heat_ranking_bar")

    # ----漲跌家數分佈(一般堆疊長條圖，絕對家數、由0開始往右疊)----
    # 用標準 barmode="stack"：全部家數都用絕對值，x軸一律從0開始往右加總，
    # 五段依序由左到右為大跌→小跌→持平→小漲→大漲(Plotly stack 是依「加入 trace 的順序」
    # 由0往外疊，所以先加入的 trace 會落在最左邊)。
    st.markdown("#### 漲跌家數分佈")
    dist_df = ranking.head(top_n).iloc[::-1]

    diverge_fig = go.Figure()
    diverge_fig.add_trace(go.Bar(
        name="大跌", y=dist_df["股票類別"], x=dist_df["大跌家數"], orientation="h",
        marker=dict(color="#1B7A43"), legendrank=1,
        hovertemplate="%{y}<br>大跌：%{x}家<extra></extra>",
    ))
    diverge_fig.add_trace(go.Bar(
        name="小跌", y=dist_df["股票類別"], x=dist_df["小跌家數"], orientation="h",
        marker=dict(color="#8FD9B6"), legendrank=2,
        hovertemplate="%{y}<br>小跌：%{x}家<extra></extra>",
    ))
    diverge_fig.add_trace(go.Bar(
        name="持平", y=dist_df["股票類別"], x=dist_df["持平家數"], orientation="h",
        marker=dict(color="#D9D9D9"), legendrank=3,
        hovertemplate="%{y}<br>持平：%{x}家<extra></extra>",
    ))
    diverge_fig.add_trace(go.Bar(
        name="小漲", y=dist_df["股票類別"], x=dist_df["小漲家數"], orientation="h",
        marker=dict(color="#F6A9A9"), legendrank=4,
        hovertemplate="%{y}<br>小漲：%{x}家<extra></extra>",
    ))
    diverge_fig.add_trace(go.Bar(
        name="大漲", y=dist_df["股票類別"], x=dist_df["大漲家數"], orientation="h",
        marker=dict(color="#F64545"), legendrank=5,
        hovertemplate="%{y}<br>大漲：%{x}家<extra></extra>",
    ))
    diverge_fig.update_layout(
        barmode="stack", margin=dict(t=10, l=10, r=10, b=10),
        height=max(300, 32 * len(dist_df)),
        xaxis=dict(title="家數"),
    )
    st.plotly_chart(diverge_fig, use_container_width=True, key="sector_heat_diverge_bar")

    # ----明細----
    st.markdown("#### 明細")
    drill_category = st.selectbox("選擇類股查看個股明細", ranking["股票類別"], key="sector_heat_drill_category")
    detail = stock_metrics[stock_metrics["股票類別"] == drill_category][
        ["股票代號", "股票名稱", "漲跌幅", "振幅", "今日開盤價", "今日收盤價", "成交量", "分類"]
    ].sort_values("漲跌幅", ascending=False).rename(columns={"成交量": "成交量(張)"})
    # 資料庫裡的成交量單位是「股」，畫面上依台股慣例改用「張」(1張=1000股)顯示
    detail["成交量(張)"] = (detail["成交量(張)"] / 1000).round(0)
    st.dataframe(
        style_rows_by_sign(detail, "漲跌幅"), use_container_width=True, hide_index=True,
        key="sector_heat_drill_table",
    )

    # ----今日討論度文字雲----
    render_stock_mention_section()