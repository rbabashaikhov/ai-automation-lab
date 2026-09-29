-- Phase 4D.2A: read-only verification of the samsung_consultant role (run as the DB owner).
-- Prints capabilities only; never prints a password or verifier.
SELECT rolname, rolsuper, rolinherit, rolcreaterole, rolcreatedb, rolcanlogin, rolreplication, rolbypassrls,
       rolconnlimit, rolconfig
FROM pg_roles WHERE rolname = 'samsung_consultant';

SELECT m.roleid::regrole AS member_of FROM pg_auth_members m WHERE m.member = 'samsung_consultant'::regrole;

SELECT t AS table_name,
       has_table_privilege('samsung_consultant', t, 'SELECT')   AS sel,
       has_table_privilege('samsung_consultant', t, 'INSERT')   AS ins,
       has_table_privilege('samsung_consultant', t, 'UPDATE')   AS upd,
       has_table_privilege('samsung_consultant', t, 'DELETE')   AS del,
       has_table_privilege('samsung_consultant', t, 'TRUNCATE') AS trunc
FROM unnest(ARRAY['products', 'product_specs', 'chunks', 'documents', 'ingestion_runs', 'ingestion_errors']) t;

SELECT has_schema_privilege('samsung_consultant', 'public', 'CREATE') AS schema_create,
       has_schema_privilege('samsung_consultant', 'public', 'USAGE')  AS schema_usage,
       has_database_privilege('samsung_consultant', current_database(), 'CREATE') AS db_create,
       has_function_privilege('samsung_consultant',
           'match_product_chunks(vector, integer, integer, numeric, numeric, text, numeric, numeric, boolean)',
           'EXECUTE') AS match_execute;
