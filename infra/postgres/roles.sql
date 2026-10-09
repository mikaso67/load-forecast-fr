\set ON_ERROR_STOP on
\set ECHO none
\getenv role_password LFF_AGENT_PASSWORD
\if :{?role_password}
\else
    \echo 'Set LFF_AGENT_PASSWORD in the calling environment before running this script.'
    -- ON_ERROR_STOP makes this failure return a nonzero psql exit status.
    SELECT 1 / 0;
\endif

BEGIN;
-- Password statements must not enter server statement or error logs.
SET LOCAL log_statement = 'none';
SET LOCAL log_min_error_statement = 'panic';
SET LOCAL log_min_duration_statement = -1;
SET LOCAL log_min_duration_sample = -1;
SET LOCAL log_duration = off;

DO $$
BEGIN
    IF current_database() <> 'load_forecast' THEN
        RAISE EXCEPTION 'Run roles.sql in load_forecast';
    END IF;
    IF EXISTS (SELECT FROM pg_roles WHERE rolname = 'lff_agent') THEN
        RAISE EXCEPTION 'lff_agent already exists; review its memberships and ownership before changing it';
    END IF;
END;
$$;

SELECT length(:'role_password') > 0 AS password_present \gset
\if :password_present
\else
    \echo 'LFF_AGENT_PASSWORD must not be empty.'
    ROLLBACK;
    SELECT 1 / 0;
\endif

CREATE ROLE lff_agent LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS;
SELECT format('ALTER ROLE lff_agent PASSWORD %L', :'role_password') \gexec
\unset role_password
ALTER ROLE lff_agent SET timezone = 'UTC';

-- A role-specific REVOKE cannot override privileges inherited from PUBLIC.
REVOKE ALL ON DATABASE mlflow FROM PUBLIC;
REVOKE ALL ON DATABASE mlflow FROM lff_agent;
REVOKE CREATE, TEMPORARY ON DATABASE load_forecast FROM PUBLIC;
GRANT CONNECT ON DATABASE load_forecast TO lff_agent;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;

CREATE SCHEMA IF NOT EXISTS raw;
CREATE SCHEMA IF NOT EXISTS clean;
CREATE SCHEMA IF NOT EXISTS analytics;
REVOKE ALL ON SCHEMA raw, clean, analytics FROM PUBLIC, lff_agent;
GRANT USAGE ON SCHEMA raw, clean, analytics TO lff_agent;
REVOKE ALL ON ALL TABLES IN SCHEMA raw, clean, analytics FROM PUBLIC, lff_agent;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA raw, clean, analytics FROM PUBLIC, lff_agent;
GRANT SELECT ON ALL TABLES IN SCHEMA raw, clean, analytics TO lff_agent;

-- Cover future tables created by this administrator, not just today's tables.
ALTER DEFAULT PRIVILEGES IN SCHEMA raw, clean, analytics REVOKE ALL ON TABLES FROM PUBLIC;
ALTER DEFAULT PRIVILEGES IN SCHEMA raw, clean, analytics GRANT SELECT ON TABLES TO lff_agent;
ALTER DEFAULT PRIVILEGES IN SCHEMA raw, clean, analytics REVOKE ALL ON SEQUENCES FROM PUBLIC;

CREATE SCHEMA IF NOT EXISTS dev AUTHORIZATION lff_agent;
ALTER SCHEMA dev OWNER TO lff_agent;
-- dbt must own existing relations to replace them on its next build.
DO $$
DECLARE
    relation record;
BEGIN
    FOR relation IN
        SELECT n.nspname, c.relname,
               CASE c.relkind WHEN 'v' THEN 'VIEW' WHEN 'm' THEN 'MATERIALIZED VIEW'
                    WHEN 'S' THEN 'SEQUENCE' WHEN 'f' THEN 'FOREIGN TABLE'
                    ELSE 'TABLE' END AS object_type
        FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
        WHERE n.nspname IN ('dev')
          AND c.relkind IN ('r', 'p', 'v', 'm', 'S', 'f')
        ORDER BY c.relkind = 'S', c.oid
    LOOP
        EXECUTE format('ALTER %s %I.%I OWNER TO lff_agent',
                       relation.object_type, relation.nspname, relation.relname);
    END LOOP;
END;
$$;
REVOKE ALL ON SCHEMA dev FROM PUBLIC;
COMMIT;
