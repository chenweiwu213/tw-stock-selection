"""
今日討論度排程腳本：抓取 CMoney 熱門貼文 feed，統計股票提及次數，寫入
stock_mention_daily。stock_mention_daily 只保留當天資料，每次執行都會先清空舊資料再寫入。
由 .github/workflows/update_stock_mention.yml 排程執行。
"""

import argparse
import re
import sys
from collections import Counter
from datetime import date, datetime
from sqlalchemy import text

from app.common.cmoney import (
    FETCH_PER_PAGE,
    MAX_PAGES,
    TAIWAN_TZ,
    fetch_bearer_token,
    fetch_recommendation_page,
)
from app.common.constants import STOCK_ID_RE
from app.common.db import get_engine
from app.common.http import get_relaxed_session

ARTICLE_LIST_KEY = "articles"


def _taiwan_today() -> date:
    return datetime.now(TAIWAN_TZ).date()


def count_stock_mentions(session, token: str, trade_date: date, collect_titles: bool = False):
    """統計台灣時間今天發文中，每個4碼股票代號被 commodityTags 標記的次數。

    collect_titles=True 時額外回傳 {stock_id: [文章標題, ...]}，供 --debug 核對用，
    只在本機列印，不會寫入DB。
    """
    counts = Counter()
    titles_by_stock = {} if collect_titles else None
    offset = 0
    pages_scanned = 0
    hit_page_cap = True

    for _ in range(MAX_PAGES):
        data = fetch_recommendation_page(session, token, offset)
        articles = data.get(ARTICLE_LIST_KEY, []) or []
        pages_scanned += 1
        if not articles:
            hit_page_cap = False
            break

        for art in articles:
            create_time_ms = art.get("createTime")
            if create_time_ms is None:
                continue
            article_date = datetime.fromtimestamp(create_time_ms / 1000, tz=TAIWAN_TZ).date()
            if article_date != trade_date:
                continue

            content = art.get("content") or {}
            title = content.get("title") or content.get("text", "")
            # 同一篇文章的commodityTags若重複標記同一支股票，只算一次，
            # 讓「提及次數」對應「不同文章數」，跟個股最新貼文抓到的篇數口徑一致。
            article_stock_ids = set()
            for tag in content.get("commodityTags") or []:
                if tag.get("type") != "Stock":
                    continue
                stock_id = str(tag.get("key", ""))
                if re.fullmatch(STOCK_ID_RE, stock_id):
                    article_stock_ids.add(stock_id)
            for stock_id in article_stock_ids:
                counts[stock_id] += 1
                if collect_titles:
                    titles_by_stock.setdefault(stock_id, []).append(title)

        has_next = data.get("hasNext")
        if has_next is None:
            # API 曾經回過缺少 hasNext 的舊格式；退回舊的「篇數不足一頁」判斷法，
            # 但這種判斷法本身就是這次抓太少的成因，印出警告方便日後追查。
            print("[警告] API回應缺少 hasNext 欄位，改用篇數判斷是否還有下一頁，結果可能不準確。")
            has_next = len(articles) >= FETCH_PER_PAGE
        if not has_next:
            hit_page_cap = False
            break
        offset = data.get("nextOffset", offset + len(articles))

    print(
        f"[scan] 共掃描 {pages_scanned} 頁(約 {offset} 篇文章)，"
        f"{'被MAX_PAGES上限切斷' if hit_page_cap else '已掃到feed自然結尾'}"
    )

    if collect_titles:
        return counts, titles_by_stock
    return counts


def upsert_stock_mentions(trade_date: date, counts: Counter) -> int:
    """清空 stock_mention_daily 既有資料後寫入本次統計結果，資料庫只保留當天資料。"""
    if not counts:
        return 0
    rows = [{"trade_date": trade_date, "stock_id": sid, "mention_count": c} for sid, c in counts.items()]
    with get_engine().begin() as conn:
        conn.execute(text("DELETE FROM stock_mention_daily"))
        conn.execute(
            text("""
                INSERT INTO stock_mention_daily (trade_date, stock_id, mention_count)
                VALUES (:trade_date, :stock_id, :mention_count)
            """),
            rows,
        )
    return len(rows)


def main():
    parser = argparse.ArgumentParser(description="抓取CMoney熱門貼文，統計今日股票討論度並寫入DB")
    parser.add_argument("--dry-run", action="store_true", help="只印出統計結果，不寫入DB")
    parser.add_argument("--debug", action="store_true", help="印出每檔股票對應的文章標題，供人工核對")
    args = parser.parse_args()

    print("正在抓取 CMoney 訪客 Bearer token...")
    token = fetch_bearer_token()
    print("Token 取得成功，開始分頁掃描熱門貼文...")

    session = get_relaxed_session()
    trade_date = _taiwan_today()

    if args.debug:
        counts, titles_by_stock = count_stock_mentions(session, token, trade_date, collect_titles=True)
    else:
        counts = count_stock_mentions(session, token, trade_date)
        titles_by_stock = None

    print(f"共統計出 {len(counts)} 檔股票的今日提及次數。")
    if not counts:
        print("警告：沒有統計出任何股票，需確認 CMoney 回應格式是否已變動。")
        sys.exit(1)

    if args.dry_run or args.debug:
        top10 = counts.most_common(10)
        print(f"[dry-run] {trade_date} 前10名：{top10}")
        if args.debug:
            print("\n[debug] 各股票對應文章標題：")
            for stock_id, cnt in top10:
                print(f"\n  {stock_id}(共{cnt}次)：")
                for t in titles_by_stock.get(stock_id, []):
                    print(f"    - {t}")
        if args.dry_run:
            return

    n = upsert_stock_mentions(trade_date, counts)
    print(f"已清空舊資料並寫入 {n} 筆 stock_mention_daily 資料(trade_date={trade_date})。")


if __name__ == "__main__":
    main()