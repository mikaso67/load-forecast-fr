\set ON_ERROR_STOP on
\set ECHO none
BEGIN;
DO $$
BEGIN
    IF current_database() <> 'load_forecast' THEN
        RAISE EXCEPTION 'Run labels_access.sql in load_forecast';
    END IF;
END;
$$;
CREATE SCHEMA IF NOT EXISTS labels;
REVOKE ALL ON SCHEMA labels FROM PUBLIC, lff_agent;
GRANT USAGE ON SCHEMA labels TO lff_agent;
REVOKE ALL ON ALL TABLES IN SCHEMA labels FROM PUBLIC, lff_agent;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA labels FROM PUBLIC, lff_agent;
GRANT SELECT ON ALL TABLES IN SCHEMA labels TO lff_agent;
-- Run as the same administrator that creates tables during the collector load.
ALTER DEFAULT PRIVILEGES IN SCHEMA labels REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA labels GRANT SELECT ON TABLES TO lff_agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA labels REVOKE ALL ON SEQUENCES FROM PUBLIC;
COMMIT;
