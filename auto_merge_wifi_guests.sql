-- =================================================================
-- Instant merge: wifi_guests -> master_customers  (new guests only)
--
-- Run once, as the ingest user:
--     psql "$DB" -f auto_merge_wifi_guests.sql
--
-- After this, a genuinely NEW wi-fi guest appears in master the moment
-- the sync inserts them. The trigger fires on INSERT only, so the
-- re-sync of guests already in the table does nothing - which is what
-- keeps the thousands already pushed to Como from being touched again.
--
-- Brand / country / venue come from parse_market(lastmarket), which
-- returns them positionally as [brand, country, venue]. Source is
-- always WIFI. Same cleaning as the full rebuild: valid email, not a
-- numeric-only placeholder, not a blocked domain, and no under-18s.
--
-- There is deliberately NO backfill here: existing guests are already
-- in master and already pushed. This only handles arrivals from now on.
-- =================================================================


-- ── Merge one wi-fi guest into master ────────────────────────────
-- Keyed on doc_id (the Firestore id) because a guest row is uniquely
-- that document; email may be blank or shared, doc_id never is.

CREATE OR REPLACE FUNCTION merge_wifi_guest(p_doc_id TEXT)
RETURNS VOID AS $mwg$
DECLARE
    market TEXT[];
    g      RECORD;
    bday   DATE;
BEGIN
    SELECT * INTO g FROM wifi_guests WHERE doc_id = p_doc_id;
    IF NOT FOUND THEN
        RETURN;
    END IF;

    -- parse_market returns [BRAND, COUNTRY] and optionally [, VENUE],
    -- always in that order. Empty array if lastmarket is blank/garbage.
    market := parse_market(g.lastmarket);

    -- Firestore stores dob as text like '1984-09-17'.
    bday := CASE WHEN g.dob ~ '^\d{4}-\d{2}-\d{2}'
                 THEN substring(g.dob from 1 for 10)::date END;

    INSERT INTO master_customers
        (email, first_name, last_name, birthday, tags, allow_email, sources,
         src_system, brand, country, venue)
    SELECT
        fix_domain(lower(trim(g.email))),
        clean_name(g.fname),
        clean_name(g.lname),
        clean_birthday(bday),
        clean_tags(ARRAY['WIFI'] || market),
        -- The portal requires a consent checkbox before it submits, so
        -- everyone here consented; it just isn't recorded per row.
        true,
        ARRAY['wifi_guests'],
        'WIFI',
        market[1],          -- brand
        market[2],          -- country
        market[3]           -- venue (NULL if the market had none)
    WHERE g.email IS NOT NULL
      AND trim(g.email) <> ''
      AND g.email ~ '^[^@[:space:]]+@[^@[:space:]]+\.[A-Za-z]{2,}$'
      AND split_part(g.email, '@', 1) !~ '^[0-9]+$'
      AND lower(split_part(fix_domain(g.email), '@', 2))
            NOT IN (SELECT domain FROM blocked_domains)
      -- Under-18s never enter master. NULL dob is allowed through.
      AND (bday IS NULL OR is_adult(bday))
    ON CONFLICT (email) DO UPDATE SET
        first_name  = COALESCE(master_customers.first_name,  EXCLUDED.first_name),
        last_name   = COALESCE(master_customers.last_name,   EXCLUDED.last_name),
        birthday    = COALESCE(master_customers.birthday,    EXCLUDED.birthday),
        tags        = clean_tags(master_customers.tags || EXCLUDED.tags),
        allow_email = COALESCE(master_customers.allow_email, EXCLUDED.allow_email),
        sources     = ARRAY(SELECT DISTINCT unnest(
                          master_customers.sources || EXCLUDED.sources)),
        src_system  = COALESCE(master_customers.src_system, EXCLUDED.src_system),
        brand       = COALESCE(master_customers.brand,   EXCLUDED.brand),
        country     = COALESCE(master_customers.country, EXCLUDED.country),
        venue       = COALESCE(master_customers.venue,   EXCLUDED.venue),
        needs_push  = true
    WHERE master_customers.first_name  IS NULL AND EXCLUDED.first_name  IS NOT NULL
       OR master_customers.last_name   IS NULL AND EXCLUDED.last_name   IS NOT NULL
       OR master_customers.birthday    IS NULL AND EXCLUDED.birthday    IS NOT NULL
       OR NOT (EXCLUDED.tags <@ master_customers.tags)
       OR master_customers.allow_email IS NULL AND EXCLUDED.allow_email IS NOT NULL
       OR master_customers.src_system  IS NULL AND EXCLUDED.src_system  IS NOT NULL
       OR master_customers.brand   IS NULL AND EXCLUDED.brand   IS NOT NULL
       OR master_customers.country IS NULL AND EXCLUDED.country IS NOT NULL
       OR master_customers.venue   IS NULL AND EXCLUDED.venue   IS NOT NULL
       OR NOT (EXCLUDED.sources <@ master_customers.sources);
END;
$mwg$ LANGUAGE plpgsql;


-- ── The trigger ──────────────────────────────────────────────────
-- INSERT only: fires for a brand-new doc_id, never for the re-sync of
-- an existing guest. SECURITY DEFINER so it can write master whatever
-- user ran the sync.

CREATE OR REPLACE FUNCTION trg_wifi_guest_to_master()
RETURNS TRIGGER AS $trg$
BEGIN
    PERFORM merge_wifi_guest(NEW.doc_id);
    RETURN NEW;
END;
$trg$ LANGUAGE plpgsql SECURITY DEFINER;


DROP TRIGGER IF EXISTS wifi_guest_to_master ON wifi_guests;

CREATE TRIGGER wifi_guest_to_master
    AFTER INSERT ON wifi_guests
    FOR EACH ROW
    EXECUTE FUNCTION trg_wifi_guest_to_master();


\echo ''
\echo 'Trigger installed. Existing wifi_guests were NOT touched.'
\echo 'From the next sync, new guests will merge into master on arrival.'
\echo ''
\echo 'wifi rows currently in master (unchanged by this):'
SELECT count(*) AS wifi_in_master
FROM master_customers WHERE 'wifi_guests' = ANY(sources);
