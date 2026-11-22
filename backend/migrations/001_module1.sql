BEGIN;

CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE stock_master (
    stock_code VARCHAR(10) PRIMARY KEY,
    corp_code VARCHAR(10),
    isin_cd VARCHAR(12) NOT NULL,
    crno VARCHAR(13),
    corp_name VARCHAR(100) NOT NULL,
    market VARCHAR(32),
    asset_type VARCHAR(10) NOT NULL DEFAULT 'STOCK',
    induty_code VARCHAR(10),
    listed_shares BIGINT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT ck_stock_master_asset_type CHECK (asset_type IN ('STOCK', 'ETF', 'ETN', 'REIT')),
    CONSTRAINT ck_stock_master_etp_corp_code CHECK (asset_type NOT IN ('ETF', 'ETN') OR corp_code IS NULL),
    CONSTRAINT ck_stock_master_listed_shares CHECK (listed_shares > 0)
);

CREATE INDEX idx_stock_master_corp ON stock_master (corp_code) WHERE corp_code IS NOT NULL;
CREATE INDEX idx_stock_master_isin ON stock_master (isin_cd);
CREATE INDEX idx_stock_master_crno ON stock_master (crno);

CREATE TABLE overhang_events (
    id SERIAL PRIMARY KEY,
    corp_code VARCHAR(10) NOT NULL,
    bond_type_issue_no VARCHAR(50),
    board_resolution_date DATE NOT NULL,
    event_type VARCHAR(20) NOT NULL,
    convertible_shares BIGINT NOT NULL,
    conversion_price NUMERIC(18, 2),
    exercise_start_date DATE,
    exercise_end_date DATE,
    rcept_no VARCHAR(14) NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT uq_overhang_event UNIQUE NULLS NOT DISTINCT (corp_code, bond_type_issue_no, board_resolution_date)
);

COMMIT;
