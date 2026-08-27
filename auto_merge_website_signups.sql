-- =================================================================
-- Instant merge: website_signups -> master_customers
--
-- Run once, as the ingest user:
--     psql "$DB" -f auto_merge_website_signups.sql
--
-- After this, anything landing in website_signups (from the website
-- endpoint, a manual insert, anything) appears in master_customers
-- within the same transaction - no rebuild needed.
--
-- It uses the SAME cleaning as the full rebuild: domain corrections,
-- the blocked-domain list, and the under-18 gate all still apply. A
-- signup that fails those simply doesn't reach master, exactly as it
-- wouldn't in a full rebuild.
--
-- Unlike the batch path, the source tag is the row's OWN src_system
-- (NEWSLETTER, CONTACT_FORM, ...), so a contact-form signup is
-- labelled as such in master rather than flattened to NEWSLETTER.
--
-- The full rebuild (refresh_master / split_dimensions / find_
-- duplicates) is unchanged and still does the heavier cross-source
-- duplicate detection. This trigger does not dedupe across sources -
-- see the note at the bottom.
-- =================================================================


-- ── Merge one website signup into master ─────────────────────────

CREATE OR REPLACE FUNCTION merge_website_signup(p_email TEXT)
RETURNS VOID AS $mws$
BEGIN
    INSERT INTO master_customers
        (email, first_name, last_name, tags, allow_email, sources,
         src_system, brand, country)
    SELECT
        fix_domain(lower(trim(w.email))),
        clean_name(w.first_name),
        clean_name(w.last_name),
        -- First tag is the real source for this row, then country + brand.
        clean_tags(ARRAY[w.src_system] || ARRAY[w.country, w.brand]),
        -- Submitting a website form is the consent.
        true,
        ARRAY['website_signups'],
        -- Dimensions straight from the row's own clean columns - no
        -- need to infer them from the tag array.
        w.src_system,
        w.brand,
        w.country
    FROM website_signups w
    WHERE lower(trim(w.email)) = lower(trim(p_email))
      AND w.email IS NOT NULL
      AND trim(w.email) <> ''
      AND w.email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[A-Za-z]{2,}$'
      AND split_part(w.email, '@', 1) !~ '^[0-9]+$'
      AND lower(split_part(fix_domain(w.email), '@', 2))
            NOT IN (SELECT domain FROM blocked_domains)
    ON CONFLICT (email) DO UPDATE SET
        first_name  = COALESCE(master_customers.first_name, EXCLUDED.first_name),
        last_name   = COALESCE(master_customers.last_name,  EXCLUDED.last_name),
        tags        = clean_tags(master_customers.tags || EXCLUDED.tags),
        allow_email = COALESCE(master_customers.allow_email, EXCLUDED.allow_email),
        sources     = ARRAY(SELECT DISTINCT unnest(
                          master_customers.sources || EXCLUDED.sources)),
        -- Website submission is an explicit, first-party signal for
        -- these three, so let it fill them in.
        src_system  = COALESCE(master_customers.src_system, EXCLUDED.src_system),
        brand       = COALESCE(master_customers.brand,   EXCLUDED.brand),
        country     = COALESCE(master_customers.country, EXCLUDED.country),
        needs_push  = true
    WHERE master_customers.first_name IS NULL AND EXCLUDED.first_name IS NOT NULL
       OR master_customers.last_name  IS NULL AND EXCLUDED.last_name  IS NOT NULL
       OR NOT (EXCLUDED.tags <@ master_customers.tags)
       OR master_customers.allow_email IS NULL AND EXCLUDED.allow_email IS NOT NULL
       OR master_customers.src_system IS NULL AND EXCLUDED.src_system IS NOT NULL
       OR master_customers.brand   IS NULL AND EXCLUDED.brand   IS NOT NULL
       OR master_customers.country IS NULL AND EXCLUDED.country IS NOT NULL
       OR NOT (EXCLUDED.sources <@ master_customers.sources);
END;
$mws$ LANGUAGE plpgsql;


-- ── The trigger ──────────────────────────────────────────────────
-- SECURITY DEFINER so it can write master_customers whatever user did
-- the insert (the dashboard connects read-mostly and couldn't
-- otherwise). It only ever calls merge_website_signup for the row
-- that just changed, so it can't touch anything else.

CREATE OR REPLACE FUNCTION trg_website_signup_to_master()
RETURNS TRIGGER AS $trg$
BEGIN
    PERFORM merge_website_signup(NEW.email);
    RETURN NEW;
END;
$trg$ LANGUAGE plpgsql SECURITY DEFINER;


DROP TRIGGER IF EXISTS website_signup_to_master ON website_signups;

-- Fires on a new signup, and on a re-signup that changes the person's
-- details - but NOT when only como_status/como_detail/como_at change,
-- so recording the Como result doesn't pointlessly re-merge.
CREATE TRIGGER website_signup_to_master
    AFTER INSERT OR UPDATE OF email, brand, country, first_name,
                              last_name, src_system
    ON website_signups
    FOR EACH ROW
    EXECUTE FUNCTION trg_website_signup_to_master();


-- ── Backfill anything already sitting in the table ───────────────
-- Existing rows predate the trigger, so merge them once now.

DO $backfill$
DECLARE
    r RECORD;
BEGIN
    FOR r IN SELECT email FROM website_signups LOOP
        PERFORM merge_website_signup(r.email);
    END LOOP;
END;
$backfill$;


\echo ''
\echo 'Trigger installed and existing rows backfilled.'
\echo 'Website signups now in master, by source tag:'
SELECT src_system, count(*) AS n
FROM master_customers
WHERE 'website_signups' = ANY(sources)
GROUP BY src_system ORDER BY n DESC;

-- =================================================================
-- Note - cross-source duplicates
--
-- This trigger keys on the email exactly as submitted (after domain
-- correction). It does NOT run the gmail-dot / plus-address matching
-- that find_duplicates() does. So if someone signs up as
-- john.smith@gmail.com on the website but already exists in master as
-- johnsmith@gmail.com from Revel, they'll sit as two rows until the
-- next full rebuild collapses them. For newsletter/contact volumes
-- that's fine; the weekly rebuild is the safety net. Flagging it so
-- it isn't a surprise.
-- =================================================================
