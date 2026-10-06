-- ============================================================
-- 股票分析 App - 資料庫 Schema
-- ============================================================
-- 用途：在新的 Supabase 專案上，一次建立所有本 App
--       需要的資料表。
--
-- 使用方式：
--   1. 到 Supabase 專案的 SQL Editor
--   2. 貼上本檔案全部內容並執行（可重複執行，皆為 IF NOT EXISTS）
--   3. 確認 10 張表都建立成功後，才能啟動 analysis_app.py
--
-- 注意：app/pages/ 底下各功能模組已不再包含建表邏輯，
--       如果之後要新增欄位或新表，直接修改這個檔案，
--       並手動到 Supabase 執行 ALTER TABLE / CREATE TABLE。
-- ============================================================


-- ------------------------------------------------------------
-- 1. 融資融券 - 上市 (margin_trading_daily)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS margin_trading_daily (
    id BIGSERIAL PRIMARY KEY,
    trade_date DATE NOT NULL,
    stock_id VARCHAR(10) NOT NULL,
    stock_name VARCHAR(50),
    margin_buy INT,
    margin_sell INT,
    short_buy INT,
    short_sell INT,
    UNIQUE(trade_date, stock_id)
);

-- ------------------------------------------------------------
-- 2. 融資融券 - 上櫃 (margin_trading_otc_daily)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS margin_trading_otc_daily (
    id BIGSERIAL PRIMARY KEY,
    trade_date DATE NOT NULL,
    stock_id VARCHAR(10) NOT NULL,
    stock_name VARCHAR(50),
    margin_buy INT,
    margin_sell INT,
    margin_cash_redemption INT,
    short_buy INT,
    short_sell INT,
    short_redemption INT,
    UNIQUE(trade_date, stock_id)
);

-- ------------------------------------------------------------
-- 3. 三大法人 (three_institutional_daily)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS three_institutional_daily (
    trade_date DATE NOT NULL,
    stock_id TEXT NOT NULL,
    stock_name TEXT,
    foreign_net BIGINT,
    trust_net BIGINT,
    dealer_net BIGINT,
    institutional_net_total BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (trade_date, stock_id)
);
-- 歷史遷移備註：舊版曾有 market 欄位，已移除。
-- 新專案從這份 schema 建表時不會產生該欄位，此語句僅為保險，可安全重複執行。
ALTER TABLE three_institutional_daily DROP COLUMN IF EXISTS market;

-- ------------------------------------------------------------
-- 4. 站穩均線 / 均線糾結 / 均線糾結爆大量 / 下跌但法人同步買進
--    共用的股價資料 (stock_price_daily)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS stock_price_daily (
    trade_date DATE NOT NULL,
    stock_id VARCHAR(10) NOT NULL,
    stock_name VARCHAR(50),
    volume_shares BIGINT,
    open_price NUMERIC(14,2),
    high_price NUMERIC(14,2),
    low_price NUMERIC(14,2),
    close_price NUMERIC(14,2),
    PRIMARY KEY (trade_date, stock_id)
);

-- ------------------------------------------------------------
-- 5. 處置股（共用於所有 K 線圖分頁的處置區間標示）
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS disposition_stocks (
    market VARCHAR(10) NOT NULL,
    stock_id VARCHAR(10) NOT NULL,
    stock_name VARCHAR(50),
    start_date DATE NOT NULL,
    end_date DATE NOT NULL,
    PRIMARY KEY (market, stock_id, start_date)
);

-- ------------------------------------------------------------
-- 6. 主動式ETF買賣 - ETF 主檔 (etf_master)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS etf_master (
    etf_code    VARCHAR(10) PRIMARY KEY,
    etf_name    VARCHAR(50),
    is_active   BOOLEAN DEFAULT TRUE
);

-- ------------------------------------------------------------
-- 7. 主動式ETF買賣 - 每日持股資料 (etf_holdings_daily)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS etf_holdings_daily (
    etf_code     VARCHAR(10) NOT NULL,
    trade_date   DATE NOT NULL,
    stock_id     VARCHAR(4) NOT NULL,
    shares_held  BIGINT,
    weight_pct   NUMERIC(6,2),
    PRIMARY KEY (etf_code, trade_date, stock_id)
);

-- ------------------------------------------------------------
-- 8. 類股熱度儀表板 - 類股分類主檔 (sector_category_master)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sector_category_master (
    category_label VARCHAR(50) NOT NULL,
    stock_id       VARCHAR(10) NOT NULL,
    stock_name     VARCHAR(50),
    updated_at     TIMESTAMP DEFAULT NOW(),
    PRIMARY KEY (category_label, stock_id)
);

-- ------------------------------------------------------------
-- 9. 大戶持股比例 (shareholding_distribution_weekly)
-- ------------------------------------------------------------
CREATE TABLE IF NOT EXISTS shareholding_distribution_weekly (
    trade_date     DATE NOT NULL,
    stock_id       VARCHAR(4) NOT NULL,
    holding_level  SMALLINT NOT NULL,
    holders_count  INTEGER,
    shares         BIGINT,
    pct_of_custody NUMERIC(7,3),
    PRIMARY KEY (trade_date, stock_id, holding_level)
);

-- ------------------------------------------------------------
-- 10. 資金與熱度 - 今日討論度 (stock_mention_daily)
-- ------------------------------------------------------------
-- 只存彙總後的每日提及次數，不存貼文原文。
-- 來源：CMoney熱門貼文feed，由 update_stock_mention.py 排程寫入。
CREATE TABLE IF NOT EXISTS stock_mention_daily (
    trade_date     DATE NOT NULL,
    stock_id       VARCHAR(4) NOT NULL,
    mention_count  INTEGER NOT NULL,
    PRIMARY KEY (trade_date, stock_id)
);

-- ------------------------------------------------------------
-- 11. Row Level Security（安全設定，可重複執行）
-- ------------------------------------------------------------
-- 在 SQL Editor 建立的資料表預設「沒有啟用」RLS，代表任何拿到專案 anon key 的人，
-- 都能透過 Supabase 自動產生的 REST API 讀寫這些表。
-- 本 App 只透過 Session Pooler 以 postgres 角色（資料表擁有者）直連資料庫，
-- 擁有者預設會略過 RLS，所以「啟用 RLS 且不建立任何 policy」等於封鎖 REST API 存取，
-- 同時不影響 App 與排程腳本的讀寫。
-- 已經建好的 Supabase 專案：把這一段單獨貼到 SQL Editor 執行一次，
-- 執行後重新整理 App，確認各分頁仍能正常顯示資料。
ALTER TABLE margin_trading_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE margin_trading_otc_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE three_institutional_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_price_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE disposition_stocks ENABLE ROW LEVEL SECURITY;
ALTER TABLE etf_master ENABLE ROW LEVEL SECURITY;
ALTER TABLE etf_holdings_daily ENABLE ROW LEVEL SECURITY;
ALTER TABLE sector_category_master ENABLE ROW LEVEL SECURITY;
ALTER TABLE shareholding_distribution_weekly ENABLE ROW LEVEL SECURITY;
ALTER TABLE stock_mention_daily ENABLE ROW LEVEL SECURITY;