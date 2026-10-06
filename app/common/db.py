"""
資料庫連線共用模組：讀取 DATABASE_URL(本機 .env／雲端 st.secrets)並提供 get_engine() 給所有功能模組共用連線池。
"""

import os
import streamlit as st
from dotenv import load_dotenv
from sqlalchemy import create_engine, text


# 明確指定用「專案根目錄」的 .env，而不是依賴執行 streamlit run 當下的目錄，
# 避免因為在不同資料夾下啟動而讀不到 .env。
# 這個檔案位於 app/common/db.py，專案根目錄是往上兩層。
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_ENV_PATH = os.path.join(_PROJECT_ROOT, ".env")
load_dotenv(dotenv_path=_ENV_PATH)  # 部署到 Streamlit Cloud 時 .env 不存在也沒關係，會靜默跳過，改用 st.secrets


def _get_database_url() -> str:
    """依序嘗試 st.secrets 與環境變數取得 DATABASE_URL，皆找不到則中止 App 並提示設定方式"""
    try:
        if "DATABASE_URL" in st.secrets:
            return st.secrets["DATABASE_URL"]
    except Exception:
        # 本機沒有 .streamlit/secrets.toml 時，st.secrets 會丟例外，屬正常情況
        pass

    url = os.environ.get("DATABASE_URL")
    if url:
        return url

    st.error(
        "找不到資料庫連線字串。需在專案根目錄建立 .env 檔並設定 DATABASE_URL"
        "(可參考 .env.example)，或於 Streamlit Cloud 的 App Settings → "
        "Secrets 中設定 DATABASE_URL。"
    )
    st.stop()


DATABASE_URL = _get_database_url()


@st.cache_resource
def get_engine():
    """回傳共用的 SQLAlchemy engine(cache_resource 保證整個 App 只建立一個連線池)"""
    # 所有資料表 CREATE TABLE 已移至 schema.sql，需先在 Supabase 執行過 schema.sql 再重新啟動
    return create_engine(DATABASE_URL, pool_pre_ping=True, pool_recycle=1800, pool_size=3, max_overflow=2)


def get_latest_date(table_name: str, date_column: str) -> str:
    """查詢指定表某日期欄位的最大值，作為快取版本鍵(走 index，成本極低，故意不加 st.cache_data，
    每次呼叫都直接查現況；供 xxx 天只寫入一次的表，搭配 @st.cache_data 的版本鍵參數使用，
    資料庫沒有新資料時就吃快取，一旦寫入新的一天則自動失效不需手動 clear())"""
    engine = get_engine()
    query = text(f"SELECT MAX({date_column}) FROM {table_name}")  # noqa: S608 - table_name/date_column 皆為程式內部常數，非使用者輸入
    with engine.connect() as conn:
        result = conn.execute(query).scalar()
    return str(result) if result is not None else ""