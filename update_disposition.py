"""
處置股清單排程腳本：抓取證交所與櫃買中心處置股公告，upsert 進 disposition_stocks。
由 .github/workflows/update_disposition.yml 排程執行。
"""

import argparse
import sys

from app.common.disposition import fetch_disposition_tpex, fetch_disposition_twse, upsert_disposition

SOURCES = [
    ("上市(TWSE)", fetch_disposition_twse),
    ("上櫃(TPEx)", fetch_disposition_tpex),
]


def main():
    parser = argparse.ArgumentParser(description="抓取上市與上櫃處置股清單並寫入DB")
    parser.add_argument("--dry-run", action="store_true", help="只印出抓到的資料，不寫入DB")
    args = parser.parse_args()

    total = 0
    failed = []
    for label, fetch_fn in SOURCES:
        try:
            df = fetch_fn()
        except Exception as e:
            # 上市上櫃互不影響：一邊失敗仍繼續處理另一邊，最後再以非0狀態碼結束，讓排程顯示失敗
            print(f"[錯誤] {label} 抓取失敗：{e}")
            failed.append(label)
            continue

        if not df.empty:
            # 同一批資料若出現相同 (market, stock_id, start_date)，upsert 會報錯
            df = df.drop_duplicates(subset=["market", "stock_id", "start_date"], keep="last")
        print(f"{label}：抓到 {len(df)} 筆")

        if args.dry_run:
            if not df.empty:
                print(df.to_string(index=False))
            continue

        total += upsert_disposition(df)

    if not args.dry_run:
        print(f"完成，共新增/更新 {total} 筆處置股紀錄。")

    if failed:
        print(f"[警告] 以下來源抓取失敗：{'、'.join(failed)}")
        sys.exit(1)


if __name__ == "__main__":
    main()