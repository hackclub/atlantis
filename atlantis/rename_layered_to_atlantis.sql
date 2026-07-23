-- One-time migration: rename Django app "layered_site" -> "atlantis_site"
-- (the project package "layered" -> "atlantis" is code-only and needs no DB change).
--
-- Run this ONCE against the EXISTING database BEFORE deploying the renamed code and
-- BEFORE running `python manage.py migrate` on it. On a fresh/empty database, skip
-- this entirely -- the normal migrations create the atlantis_site_* tables directly.
--
-- Why a raw SQL script and not a Django migration:
--   Django tracks applied migrations in django_migrations keyed by app label. Renaming
--   the app changes that label, so the migration executor would treat the whole history
--   as unapplied and try to re-CREATE the tables (orphaning the real data). A migration
--   cannot rewrite its own history rows before the executor reads them, so this bootstrap
--   must run outside Django. After it runs, `migrate` is a clean no-op.

BEGIN;

-- 1. Rename every table from the old app's default prefix to the new one.
--    (Covers model tables and the project_followers M2M through table.)
DO $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT tablename
        FROM pg_tables
        WHERE schemaname = current_schema()
          AND tablename LIKE 'layered\_site\_%'
    LOOP
        EXECUTE format(
            'ALTER TABLE %I RENAME TO %I',
            r.tablename,
            regexp_replace(r.tablename, '^layered_site_', 'atlantis_site_')
        );
    END LOOP;
END $$;

-- 2. Point Django's migration history at the new app label.
UPDATE django_migrations   SET app       = 'atlantis_site' WHERE app       = 'layered_site';

-- 3. Point content types (and therefore all permissions) at the new app label.
UPDATE django_content_type SET app_label = 'atlantis_site' WHERE app_label = 'layered_site';

COMMIT;
