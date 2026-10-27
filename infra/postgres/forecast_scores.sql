\set ON_ERROR_STOP on
BEGIN;
-- Apply manually as the owner after forecast.sql. No runtime DDL is required.
DO $$
BEGIN
    IF current_database() <> 'load_forecast' THEN
        RAISE EXCEPTION 'Run forecast_scores.sql in load_forecast';
    END IF;
END;
$$;
-- BEGIN TABLES
ALTER TABLE forecast.scores ADD COLUMN score_id TEXT;
CREATE UNIQUE INDEX scores_identity ON forecast.scores (score_id);
CREATE TABLE forecast.monitoring (
    monitor_id TEXT PRIMARY KEY,
    delivery_day DATE NOT NULL,
    variant TEXT NOT NULL CHECK (variant IN ('A', 'B')),
    series TEXT NOT NULL CHECK (series IN ('served', 'model')),
    actual_vintage TEXT NOT NULL CHECK (actual_vintage IN
                                      ('realtime', 'consolidated', 'definitive')),
    computed_at TIMESTAMPTZ NOT NULL,
    state TEXT NOT NULL
);
-- END TABLES
GRANT SELECT, INSERT ON forecast.monitoring TO lff_forecast_writer;
GRANT SELECT ON forecast.monitoring TO lff_forecast_reader;
COMMIT;
