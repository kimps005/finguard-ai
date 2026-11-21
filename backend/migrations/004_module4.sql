BEGIN;

CREATE TABLE IF NOT EXISTS dart_chunks (
    corp_code VARCHAR(10) NOT NULL,
    rcept_no VARCHAR(14) NOT NULL CHECK (rcept_no ~ '^[0-9]{14}$'),
    chunk_no INTEGER NOT NULL CHECK (chunk_no >= 0),
    report_nm TEXT NOT NULL,
    rcept_dt DATE NOT NULL,
    chunk_text TEXT NOT NULL,
    embedding vector(1024) NOT NULL,
    search_tsv TSVECTOR NOT NULL,
    PRIMARY KEY (rcept_no, chunk_no)
);
CREATE INDEX IF NOT EXISTS idx_dart_chunks_corp ON dart_chunks (corp_code);
CREATE INDEX IF NOT EXISTS idx_dart_chunks_search ON dart_chunks USING GIN (search_tsv);

COMMIT;
