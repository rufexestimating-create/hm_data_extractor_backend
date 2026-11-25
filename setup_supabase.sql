-- Run this in your Supabase SQL Editor (Dashboard > SQL Editor > New Query)
-- Creates the table for storing extracted business data

CREATE TABLE IF NOT EXISTS extracted_data (
    id              bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    created_at      timestamptz DEFAULT now(),
    batch_id        text NOT NULL,
    source_url      text,
    mode            text,
    name            text,
    website         text,
    contact_page    text,
    emails          text,
    phone           text,
    address         text,
    state           text,
    license_number  text,
    facebook        text,
    instagram       text,
    twitter         text,
    linkedin        text,
    youtube         text,
    page_url        text,
    website_source  text
);

CREATE INDEX IF NOT EXISTS idx_extracted_data_batch ON extracted_data (batch_id);
CREATE INDEX IF NOT EXISTS idx_extracted_data_created ON extracted_data (created_at DESC);

ALTER TABLE extracted_data ENABLE ROW LEVEL SECURITY;

CREATE POLICY "Allow all with service key"
    ON extracted_data
    FOR ALL
    USING (true)
    WITH CHECK (true);
