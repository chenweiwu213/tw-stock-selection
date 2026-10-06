"""
跨功能共用常數
"""

import datetime

# 台灣時區(UTC+8，固定偏移8hr)。
# Streamlit Cloud／GitHub Actions 的伺服器時區是 UTC，直接用 datetime.date.today()
# 台灣時間 00:00～08:00 會拿到前一天的日期，因此所有「今天」的判斷一律改用 today_tw()。
TAIWAN_TZ = datetime.timezone(datetime.timedelta(hours=8))


def today_tw() -> datetime.date:
    """回傳台灣時間的今天日期(不受伺服器時區影響)"""
    return datetime.datetime.now(TAIWAN_TZ).date()


# 4 位數股票代號規則：用來篩掉權證、牛熊證、TDR、公司債、興櫃代號等。
# 三大法人、股價、處置股、ETF 持股、類股分類、大戶持股等所有資料源匯入時都套用同一套規則。
STOCK_ID_RE = r"^\d{4}$"

# 只篩選這些前兩碼開頭的 4 碼證券代號(電子股，可視情況新增修改刪除)。
# 用於 add_ma_columns(restrict_prefixes=True) 及三大法人分頁的個股下拉選單。
STOCK_PRICE_ALLOWED_PREFIXES = {
    "23", "24",
    "30", "31", "32", "33", "34", "35", "36",
    "52", "53", "54", "55",
    "61", "62", "63", "64", "65", "66", "67", "68", "69",
    "76", "77",
    "80", "81", "82", "83", "84",
}