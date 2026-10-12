"""
股票分析 - 網頁版(入口檔)

這個檔案只負責:頁面設定、RWD/漢堡選單CSS、側邊欄選單、頁面路由分派。
所有實際的資料抓取、分析邏輯、畫面渲染都拆到app/ 底下：

    app/
      schema.sql           資料庫建表SQL(新專案需先手動建立資料庫)
      common/              跨分頁共用模組(DB連線、K線圖、處置股、格式化…)
      pages/               每個分頁一個檔案(融資減融券增、三大法人…)

使用前準備：
1. pip install -r requirements.txt(安裝套件與版本)
2. 在專案根目錄(跟analysis同一層)建立 .env 檔(參考.env.example)，
   填入 Supabase 的 Session Pooler 連線字串到 DATABASE_URL
   (.env 不要上傳到 GitHub )
3. 到 Supabase 專案的 SQL Editor，貼上並執行 app/schema.sql，建立所有資料表
4. 部署到Streamlit Cloud時，改用 App 設定裡的 Secrets 功能設定 DATABASE_URL，不需要 .env 檔

執行方式：
    streamlit run analysis_app.py
"""

import streamlit as st

from app.pages.active_etf import render_active_etf_tab
from app.pages.db_maintenance import render_db_maintenance_tab
from app.pages.drop_institutional_buy import render_price_drop_institutional_buy_tab
from app.pages.institutional import render_t86_tab
from app.pages.ma_convergence import render_ma_knot_tab
from app.pages.ma_convergence_volume import render_ma_knot_volume_spike_tab
from app.pages.ma_stable import render_ma_signal_tab
from app.pages.margin_signal import render_margin_tab
from app.pages.pnl_calculator import render_pnl_calculator_tab
from app.pages.sector_heatmap import render_sector_heat_tab
from app.pages.shareholding import render_big_holder_ratio_tab

st.set_page_config(page_title="股票分析", layout="wide", initial_sidebar_state="collapsed")

# --------RWD(響應式網頁設計)樣式----------
# Streamlit本身對 st.columns()在窄螢幕下會自動改成垂直堆疊，
# 但頁籤列、表格、圖表等在手機上仍容易被壓縮或需要左右滑動，
# 因此加入以下 CSS，讓整體排版在手機或平板上也不會跑掉：
# 1. 頁籤列改為可橫向滑動，避免分頁被擠壓到文字重疊或無法點擊
# 2. 表格(st.dataframe)在內容過寬時提供橫向捲動，而不是被裁切
# 3. 窄螢幕下縮小整體字級、按鈕與輸入框的內距，減少版面擁擠
st.markdown(
    """
    <style>
    /* 頁籤列：允許橫向捲動，避免分頁擠壓變形 */
    div[data-baseweb="tab-list"] {
        overflow-x: auto;
        overflow-y: hidden;
        flex-wrap: nowrap !important;
        -webkit-overflow-scrolling: touch;
        scrollbar-width: thin;
    }
    div[data-baseweb="tab-list"] button[data-baseweb="tab"] {
        white-space: nowrap;
        flex-shrink: 0;
    }

    /* 表格容器：內容過寬時可橫向捲動查看，而非被裁切 */
    div[data-testid="stDataFrame"], div[data-testid="stTable"] {
        overflow-x: auto;
    }

    /* st.metric預設是 white-space: nowrap + text-overflow: ellipsis，
       字數一多(例如較長的類股名稱)就會被裁成「...」。若只蓋到最外層
       的stMetricValue，但實際裁切樣式是套用在它底下的子元素(一個內層
       div/span)上，且該欄位(stColumn)本身也有overflow限制，
       所以文字還是被裁掉。改成連同子元素、所在欄位一起蓋掉，
       強制不裁切、可換行完整顯示全部文字。 */
    div[data-testid="stMetric"],
    div[data-testid="stMetric"] * ,
    div[data-testid="column"]:has(div[data-testid="stMetric"]) {
        overflow: visible !important;
    }
    div[data-testid="stMetricValue"],
    div[data-testid="stMetricValue"] > div,
    div[data-testid="stMetricValue"] * {
        white-space: normal !important;
        overflow-wrap: break-word !important;
        word-break: break-word !important;
        text-overflow: unset !important;
        -webkit-line-clamp: unset !important;
        line-height: 1.3 !important;
    }

    /* 手機(寬度 <= 768px)：縮小字級與間距，讓版面更緊湊好操作 */
    @media (max-width: 768px) {
        .block-container {
            padding-left: 0.8rem;
            padding-right: 0.8rem;
            padding-top: 1.2rem;
        }
        h1 { font-size: 1.4rem !important; }
        h2 { font-size: 1.2rem !important; }
        h3 { font-size: 1.05rem !important; }
        div[data-baseweb="tab-list"] button[data-baseweb="tab"] {
            font-size: 0.85rem;
            padding: 0.4rem 0.7rem;
        }
        div[data-testid="stMetricValue"] {
            font-size: 1.1rem;
        }
        .stButton button, .stDownloadButton button {
            width: 100%;
        }
    }

    /* 更小螢幕(寬度 <= 480px)：再進一步縮減 */
    @media (max-width: 480px) {
        .block-container {
            padding-left: 0.5rem;
            padding-right: 0.5rem;
        }
        div[data-baseweb="tab-list"] button[data-baseweb="tab"] {
            font-size: 0.78rem;
            padding: 0.35rem 0.55rem;
        }
    }

    /* ======== 漢堡選單樣式 ========
       Streamlit內建的sidebar收合按鈕(左上角箭頭)。 */
    button[data-testid="stSidebarCollapseButton"] svg,
    div[data-testid="collapsedControl"] svg,
    button[data-testid="baseButton-headerNoPadding"] svg {
        display: none !important;
    }
    button[data-testid="stSidebarCollapseButton"]::after,
    div[data-testid="collapsedControl"]::after,
    button[data-testid="baseButton-headerNoPadding"]::after {
        content: "☰";
        font-size: 1.6rem;
        line-height: 1;
        color: inherit;
    }
    div[data-testid="collapsedControl"] {
        display: flex;
        align-items: center;
        justify-content: center;
    }
    </style>
    """,
    unsafe_allow_html=True,
)

PAGE_OPTIONS = [
    "站穩均線", "三大法人", "融資融券", "均線糾結", "均線糾結爆大量",
    "下跌但法人同步買進", "主動式ETF買賣", "資金與熱度", "大戶持股比例", "損益計算", "資料庫維護",
]

selected_page = st.sidebar.radio("功能選單", PAGE_OPTIONS, key="selected_page")

# 用st.empty()包住整個分頁內容：
# Streamlit預設用「位置」去比對前後兩次 rerun 產生的元件樹，
# 不同分頁的版面結構有所差異，若下半部尚在計算(查詢DB或畫圖)，
# 前端會先用半透明方式顯示上一個分頁殘留在同位置的舊資料(圖表或篩選內容)，
# 等新內容算完才整個換掉，因此可能會看到「切換分頁時下半部短暫殘留舊畫面」的現象。
# 把整段內容放進同一個empty()容器，讓每次切換分頁時該容器被整個清空重建，
# 不再與舊分頁的元件做位置比對，藉此避免殘影。
# 此設計同時只會執行「目前選中分頁」對應的 render_xxx_tab()，
# 不會像st.tabs()一樣每次rerun都把每個分頁的邏輯全部跑一遍。
page_container = st.empty()
with page_container.container():
    if selected_page == "站穩均線":
        render_ma_signal_tab()
    elif selected_page == "三大法人":
        render_t86_tab()
    elif selected_page == "融資融券":
        render_margin_tab()
    elif selected_page == "均線糾結":
        render_ma_knot_tab()
    elif selected_page == "均線糾結爆大量":
        render_ma_knot_volume_spike_tab()
    elif selected_page == "下跌但法人同步買進":
        render_price_drop_institutional_buy_tab()
    elif selected_page == "主動式ETF買賣":
        render_active_etf_tab()
    elif selected_page == "資金與熱度":
        render_sector_heat_tab()
    elif selected_page == "大戶持股比例":
        render_big_holder_ratio_tab()
    elif selected_page == "損益計算":
        render_pnl_calculator_tab()
    elif selected_page == "資料庫維護":
        render_db_maintenance_tab()