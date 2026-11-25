-- One row per stock and confirmed session: the card shown (AI or local per level), the purely local
-- levels beside it, and the 10-session hit results filled in later by scripts/score_price_cards.py.
CREATE TABLE IF NOT EXISTS price_card_log (
    stock_code VARCHAR(12) NOT NULL,
    as_of DATE NOT NULL,
    current_price NUMERIC NOT NULL,
    low NUMERIC NOT NULL,
    high NUMERIC NOT NULL,
    ceiling NUMERIC NOT NULL,
    local_low NUMERIC NOT NULL,
    local_high NUMERIC NOT NULL,
    local_ceiling NUMERIC NOT NULL,
    atr14 NUMERIC,
    low_source TEXT NOT NULL,
    high_source TEXT NOT NULL,
    ceiling_source TEXT NOT NULL,
    model TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    scored_at TIMESTAMPTZ,
    low_held BOOLEAN,
    high_reached BOOLEAN,
    ceiling_held BOOLEAN,
    local_low_held BOOLEAN,
    local_high_reached BOOLEAN,
    local_ceiling_held BOOLEAN,
    PRIMARY KEY (stock_code, as_of)
);
