\set ON_ERROR_STOP on
BEGIN;
-- Apply manually as the database administrator. Runtime roles never own tables.
DO $$
BEGIN
    IF current_database() <> 'load_forecast' THEN
        RAISE EXCEPTION 'Run forecast.sql in load_forecast';
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'lff_forecast_writer') THEN
        CREATE ROLE lff_forecast_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'lff_forecast_reader') THEN
        CREATE ROLE lff_forecast_reader NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END;
$$;
CREATE SCHEMA forecast;
SET LOCAL search_path = forecast, pg_catalog;

-- BEGIN TABLES (also exercised by the local SQLite tests).
CREATE TABLE forecast.run_starts (
    run_id TEXT PRIMARY KEY,
    delivery_day DATE NOT NULL,
    variant TEXT NOT NULL CHECK (variant IN ('A', 'B')),
    freeze_time TIMESTAMPTZ NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('primary', 'fallback', 'replay')),
    started_at TIMESTAMPTZ NOT NULL,
    code_commit TEXT NOT NULL,
    UNIQUE (run_id, delivery_day, variant),
    UNIQUE (run_id, variant)
);
CREATE TABLE forecast.run_results (
    run_id TEXT PRIMARY KEY REFERENCES run_starts(run_id),
    completed_at TIMESTAMPTZ NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('complete', 'failed')),
    refit_day DATE,
    mlflow_run_id TEXT,
    kalman_quarter TEXT,
    weather_status TEXT,
    last_consumption_slot TIMESTAMPTZ,
    max_ingested_at TIMESTAMPTZ,
    feature_hash TEXT,
    flags TEXT NOT NULL
);
CREATE TABLE forecast.run_weather (
    run_id TEXT NOT NULL REFERENCES run_results(run_id),
    selection_hash TEXT NOT NULL,
    run_init_time TIMESTAMPTZ NOT NULL,
    file_last_modified TIMESTAMPTZ NOT NULL,
    index_last_modified TIMESTAMPTZ NOT NULL,
    end_step INTEGER NOT NULL,
    variable TEXT NOT NULL,
    point TEXT NOT NULL,
    message_hash TEXT NOT NULL,
    PRIMARY KEY (run_id, end_step, variable, point)
);
CREATE TABLE forecast."values" (
    run_id TEXT NOT NULL REFERENCES run_results(run_id),
    variant TEXT NOT NULL CHECK (variant IN ('A', 'B')),
    value_time TIMESTAMPTZ NOT NULL,
    local_slot INTEGER NOT NULL CHECK (local_slot BETWEEN 0 AND 47),
    point DOUBLE PRECISION NOT NULL,
    lower DOUBLE PRECISION,
    upper DOUBLE PRECISION,
    n_pool INTEGER CHECK (n_pool >= 0),
    CHECK ((lower IS NULL AND upper IS NULL) OR
           (lower IS NOT NULL AND upper IS NOT NULL AND lower <= upper)),
    PRIMARY KEY (run_id, value_time),
    FOREIGN KEY (run_id, variant) REFERENCES run_starts(run_id, variant)
);
CREATE TABLE forecast.experts (
    run_id TEXT NOT NULL REFERENCES run_results(run_id),
    value_time TIMESTAMPTZ NOT NULL,
    expert TEXT NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    backup TEXT,
    PRIMARY KEY (run_id, value_time, expert)
);
CREATE TABLE forecast.states (
    run_id TEXT NOT NULL REFERENCES run_results(run_id),
    component TEXT NOT NULL,
    state_key TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (run_id, component, state_key)
);
CREATE TABLE forecast.fallbacks (
    fallback_id TEXT PRIMARY KEY,
    delivery_day DATE NOT NULL,
    prepared_at TIMESTAMPTZ NOT NULL,
    models TEXT NOT NULL,
    completeness TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    payload TEXT NOT NULL
);
CREATE TABLE forecast.choices (
    choice_hash TEXT PRIMARY KEY,
    delivery_day DATE NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('primary', 'fallback', 'none')),
    run_id TEXT REFERENCES run_results(run_id),
    fallback_id TEXT REFERENCES fallbacks(fallback_id),
    content_hash TEXT,
    values_path TEXT,
    chosen_at TIMESTAMPTZ NOT NULL,
    CHECK ((kind = 'primary' AND run_id IS NOT NULL AND fallback_id IS NULL
            AND content_hash IS NOT NULL AND values_path IS NOT NULL) OR
           (kind = 'fallback' AND run_id IS NULL AND fallback_id IS NOT NULL
            AND content_hash IS NOT NULL AND values_path IS NOT NULL) OR
           (kind = 'none' AND run_id IS NULL AND fallback_id IS NULL
            AND content_hash IS NULL AND values_path IS NULL)),
    UNIQUE (choice_hash, delivery_day)
);
CREATE TABLE forecast.deliveries (
    delivery_day DATE PRIMARY KEY,
    choice_hash TEXT NOT NULL,
    FOREIGN KEY (choice_hash, delivery_day) REFERENCES choices(choice_hash, delivery_day)
);
CREATE TABLE forecast.delivery_incidents (
    delivery_day DATE NOT NULL,
    mirror_hash TEXT NOT NULL REFERENCES choices(choice_hash),
    file_hash TEXT NOT NULL REFERENCES choices(choice_hash),
    detected_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (delivery_day, mirror_hash, file_hash),
    CHECK (mirror_hash <> file_hash)
);
CREATE TABLE forecast.state_runs (
    delivery_day DATE NOT NULL,
    variant TEXT NOT NULL CHECK (variant IN ('A', 'B')),
    run_id TEXT NOT NULL UNIQUE REFERENCES run_results(run_id),
    PRIMARY KEY (delivery_day, variant),
    FOREIGN KEY (run_id, delivery_day, variant)
        REFERENCES run_starts(run_id, delivery_day, variant)
);
CREATE TABLE forecast.scores (
    delivery_day DATE NOT NULL,
    choice_hash TEXT NOT NULL REFERENCES choices(choice_hash),
    actual_vintage TEXT NOT NULL CHECK (actual_vintage IN
                                      ('realtime', 'consolidated', 'definitive')),
    actual_max_ingested_at TIMESTAMPTZ NOT NULL,
    scored_at TIMESTAMPTZ NOT NULL,
    metrics TEXT NOT NULL,
    PRIMARY KEY (delivery_day, actual_vintage, actual_max_ingested_at, scored_at),
    FOREIGN KEY (choice_hash, delivery_day) REFERENCES choices(choice_hash, delivery_day)
);
-- END TABLES

REVOKE ALL ON SCHEMA forecast FROM PUBLIC, lff_forecast_writer, lff_forecast_reader;
GRANT USAGE ON SCHEMA forecast TO lff_forecast_writer, lff_forecast_reader;
REVOKE ALL ON ALL TABLES IN SCHEMA forecast
    FROM PUBLIC, lff_forecast_writer, lff_forecast_reader;
GRANT SELECT, INSERT ON ALL TABLES IN SCHEMA forecast TO lff_forecast_writer;
GRANT SELECT ON ALL TABLES IN SCHEMA forecast TO lff_forecast_reader;
ALTER DEFAULT PRIVILEGES IN SCHEMA forecast REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA forecast
    GRANT SELECT, INSERT ON TABLES TO lff_forecast_writer;
ALTER DEFAULT PRIVILEGES IN SCHEMA forecast
    GRANT SELECT ON TABLES TO lff_forecast_reader;
COMMIT;
