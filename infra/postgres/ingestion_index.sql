-- Apply outside a transaction, using the database owner, before production scheduling.
CREATE INDEX CONCURRENTLY IF NOT EXISTS eco2mix_dataset_ingested_idx
    ON raw.eco2mix (source_dataset, ingested_at);
