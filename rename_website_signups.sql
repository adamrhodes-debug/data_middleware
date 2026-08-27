-- =================================================================
-- Rename newsletter_signups -> website_signups
--
-- Run once, as the ingest user:  psql "$DB" -f rename_website_signups.sql
--
-- Everything website-originated (newsletter, contact form, and
-- whatever comes next) lands in this one table. The rename keeps all
-- data, indexes and grants - only the name changes.
--
-- All of it is in one transaction: if any step fails, nothing changes.
-- =================================================================

BEGIN;

-- The table itself. Data, primary key and column defaults come with it.
ALTER TABLE newsletter_signups RENAME TO website_signups;

-- Indexes keep working after a table rename, but their names would
-- still say "newsletter" - tidy them so nothing is misleading later.
ALTER INDEX IF EXISTS newsletter_brand_idx RENAME TO website_brand_idx;
ALTER INDEX IF EXISTS newsletter_como_idx  RENAME TO website_como_idx;

-- The merge reads this registry to know which tables feed master.
-- Point it at the new name, or refresh_master() stops seeing the table.
UPDATE source_map
   SET source_table = 'website_signups'
 WHERE source_table = 'newsletter_signups';

-- Grants follow the table through a rename, so this is belt-and-braces.
GRANT SELECT, INSERT, UPDATE ON website_signups TO dashboard;

COMMIT;

\echo ''
\echo 'Renamed. Checks:'
\echo '  table:'
SELECT to_regclass('website_signups') AS website_signups,
       to_regclass('newsletter_signups') AS old_name_should_be_null;
\echo '  source_map (should show website_signups):'
SELECT source_table, source_tag, priority
FROM source_map WHERE source_table = 'website_signups';
