-- =================================================================
-- master_customers - exactly the fields Como wants, one row per person
--
-- Install once:   psql "$DB" -f build_master.sql
-- Refresh later:  psql "$DB" -c "SELECT * FROM refresh_master();"
--
-- Adding a new source needs NO changes to this file - just insert a
-- row into source_map. See "Adding a source later" at the bottom.
-- =================================================================


-- ── The master table: Como's format, nothing more ────────────────
-- Mirrors Como's import template:
--   Email Address, First Name, Last Name, Nationality,
--   Birthday, Tag, AllowEmail

CREATE TABLE IF NOT EXISTS master_customers (
    email           TEXT PRIMARY KEY,   -- Email Address (Como's identifier)
    first_name      TEXT,               -- First Name
    last_name       TEXT,               -- Last Name
    nationality     TEXT,               -- Nationality
    birthday        DATE,               -- Birthday (send as dd.MM.yyyy)
    tags            TEXT[],             -- kept for reference and the CSV export
                                        --   first element is always the source

    -- The same information as separate fields, because Como segments
    -- on exact field matches. A single comma-joined string forces
    -- fragile "contains" conditions; these give clean equals.
    src_system      TEXT,               -- WIFI / REVEL / ...
    brand           TEXT,               -- PICKL / BONBIRD / SOUTHPOUR
    country         TEXT,               -- UAE / JORDAN
    venue           TEXT,               -- CITY-WALK / JBR / VISTA-4
    allow_email     BOOLEAN,            -- AllowEmail (NULL = unknown)

    -- Duplicate detection (see find_duplicates() below)
    email_key       TEXT,               -- normalised email used for matching
    duplicate_of    TEXT,               -- the email we treat as this person's record
    dup_reason      TEXT,

    -- Housekeeping for the push
    sources         TEXT[],
    needs_push      BOOLEAN NOT NULL DEFAULT true,
    last_pushed_at  TIMESTAMPTZ,
    push_status     TEXT                -- 'ok' | 'failed' | 'conflict'
);

CREATE INDEX IF NOT EXISTS master_needs_push_idx
    ON master_customers (needs_push) WHERE needs_push;
CREATE INDEX IF NOT EXISTS master_email_key_idx ON master_customers (email_key);
CREATE INDEX IF NOT EXISTS master_brand_idx ON master_customers (brand);
CREATE INDEX IF NOT EXISTS master_venue_idx ON master_customers (venue);
CREATE INDEX IF NOT EXISTS master_duplicate_idx
    ON master_customers (duplicate_of) WHERE duplicate_of IS NOT NULL;


-- ── The registry: which tables feed the master, and how ──────────
-- One row per source table. Each *_expr is a snippet of SQL evaluated
-- against that table - usually just a column name, but it can be an
-- expression where the source needs converting.
--
-- Leave an expression NULL when the source doesn't have that field.

CREATE TABLE IF NOT EXISTS source_map (
    source_table      TEXT PRIMARY KEY,
    email_expr        TEXT NOT NULL,
    first_name_expr   TEXT,
    last_name_expr    TEXT,
    nationality_expr  TEXT,
    birthday_expr     TEXT,
    source_tag        TEXT NOT NULL,              -- always the FIRST tag
    extra_tags_expr   TEXT,                      -- SQL returning text[] of extra tags
    allow_email_expr  TEXT,
    where_extra       TEXT,                       -- optional filter, e.g. 'email_ok'
    priority          INT NOT NULL DEFAULT 100,   -- lower wins conflicts
    enabled           BOOLEAN NOT NULL DEFAULT true
);


-- ── Mistyped domains ─────────────────────────────────────────────
-- People fat-finger their email at the till or on a phone keyboard.
-- gmail.con, gmial.com and gamil.vom don't exist, so those addresses
-- hard-bounce - and enough bounces damage your sending reputation for
-- everyone else. Correcting them recovers real customers.
--
-- Only put unambiguous typos here. If there's any chance a domain is
-- real, leave it alone.

CREATE TABLE IF NOT EXISTS domain_corrections (
    wrong   TEXT PRIMARY KEY,
    correct TEXT NOT NULL
);

INSERT INTO domain_corrections (wrong, correct) VALUES
    -- gmail
    ('gmial.com', 'gmail.com'),   ('gmai.com', 'gmail.com'),
    ('gmail.con', 'gmail.com'),   ('gmail.co', 'gmail.com'),
    ('gmail.cm', 'gmail.com'),    ('gmail.om', 'gmail.com'),
    ('gamil.com', 'gmail.com'),   ('gamil.vom', 'gmail.com'),
    ('gmaill.com', 'gmail.com'),  ('gmail.comm', 'gmail.com'),
    ('gmali.com', 'gmail.com'),   ('gmail.co.m', 'gmail.com'),
    ('3gmail.com', 'gmail.com'),  ('gmial.co', 'gmail.com'),
    ('gmaul.com', 'gmail.com'),   ('gmeil.com', 'gmail.com'),
    ('gmail.cin', 'gmail.com'),   ('gmail.vom', 'gmail.com'),
    ('gnail.com', 'gmail.com'),   ('ymail.con', 'ymail.com'),
    -- hotmail
    ('hotmial.com', 'hotmail.com'), ('hotmail.con', 'hotmail.com'),
    ('hotmail.co', 'hotmail.com'),  ('hotmai.com', 'hotmail.com'),
    ('hotmail.vom', 'hotmail.com'), ('hotmall.com', 'hotmail.com'),
    ('homail.com', 'hotmail.com'),  ('hotmaill.com', 'hotmail.com'),
    -- yahoo
    ('yaho.com', 'yahoo.com'),      ('yahoo.con', 'yahoo.com'),
    ('yahooo.com', 'yahoo.com'),    ('yahoo.co', 'yahoo.com'),
    ('yahoo.vom', 'yahoo.com'),
    -- outlook / icloud / live
    ('outlok.com', 'outlook.com'),  ('outlook.con', 'outlook.com'),
    ('outlook.co', 'outlook.com'),  ('icloud.con', 'icloud.com'),
    ('iclould.com', 'icloud.com'),  ('icloud.co', 'icloud.com'),
    ('live.con', 'live.com')
ON CONFLICT (wrong) DO NOTHING;


CREATE OR REPLACE FUNCTION fix_domain(e TEXT)
RETURNS TEXT AS $fx$
DECLARE
    local  TEXT;
    domain TEXT;
    fixed  TEXT;
BEGIN
    IF e IS NULL OR position('@' in e) = 0 THEN
        RETURN e;
    END IF;

    local  := split_part(lower(btrim(e)), '@', 1);
    domain := split_part(lower(btrim(e)), '@', 2);

    SELECT correct INTO fixed FROM domain_corrections WHERE wrong = domain;

    RETURN local || '@' || COALESCE(fixed, domain);
END;
$fx$ LANGUAGE plpgsql STABLE;


-- ── Domains never allowed into the master table ──────────────────

CREATE TABLE IF NOT EXISTS blocked_domains (
    domain TEXT PRIMARY KEY
);

INSERT INTO blocked_domains (domain)
VALUES ('grubtech.com'), ('yolkbrands.com')
ON CONFLICT DO NOTHING;


-- ── Market string parsing ────────────────────────────────────────
-- Wi-fi markets look like  {brand}-{country}-{venue}  where country
-- is a multi-word slug, e.g:
--   pickl-united-arab-emirates-jbr        -> PICKL, UAE, JBR
--   southpour-united-arab-emirates-city-walk -> SOUTHPOUR, UAE, CITY-WALK
--   pickl-jordan-vista-4                  -> PICKL, JORDAN, VISTA-4
--
-- Country slugs are listed here so multi-word ones stay intact.
-- Add a row when you open in a new country.

CREATE TABLE IF NOT EXISTS country_map (
    slug TEXT PRIMARY KEY,   -- as it appears in the market string
    tag  TEXT NOT NULL       -- the tag to use
);

INSERT INTO country_map (slug, tag) VALUES
    ('united-arab-emirates', 'UAE'),
    ('jordan',               'JORDAN'),
    ('saudi-arabia',         'KSA'),
    ('kuwait',               'KUWAIT'),
    ('qatar',                'QATAR'),
    ('bahrain',              'BAHRAIN'),
    ('oman',                 'OMAN')
ON CONFLICT (slug) DO NOTHING;


CREATE OR REPLACE FUNCTION parse_market(m TEXT)
RETURNS TEXT[] AS $pm$
DECLARE
    clean   TEXT;
    brand   TEXT;
    rest    TEXT;
    c       RECORD;
    country TEXT;
    venue   TEXT;
BEGIN
    IF m IS NULL OR btrim(m) = '' THEN
        RETURN ARRAY[]::text[];
    END IF;

    -- Drop any query string that leaked in, e.g. '...city-walk?cmd=login'
    clean := lower(btrim(split_part(m, '?', 1)));
    clean := regexp_replace(clean, '-+$', '');

    brand := split_part(clean, '-', 1);
    rest  := substring(clean from length(brand) + 2);

    -- Longest slug first, so 'saudi-arabia' wins over any 'saudi'
    FOR c IN SELECT * FROM country_map ORDER BY length(slug) DESC LOOP
        IF rest = c.slug THEN
            country := c.tag;
            venue   := NULL;
            EXIT;
        ELSIF rest LIKE c.slug || '-%' THEN
            country := c.tag;
            venue   := substring(rest from length(c.slug) + 2);
            EXIT;
        END IF;
    END LOOP;

    -- Unrecognised country: assume it's the second segment and carry on,
    -- so a new market still produces sensible tags.
    IF country IS NULL THEN
        country := split_part(rest, '-', 1);
        venue   := substring(rest from length(split_part(rest, '-', 1)) + 2);
    END IF;

    RETURN ARRAY[upper(brand), upper(country)]
           || CASE WHEN COALESCE(venue, '') = ''
                   THEN ARRAY[]::text[]
                   ELSE ARRAY[upper(venue)]      -- hyphens kept: CITY-WALK
              END;
END;
$pm$ LANGUAGE plpgsql IMMUTABLE;


-- ── Tag helper ───────────────────────────────────────────────────
-- Uppercases, trims, drops blanks, removes duplicates, and keeps the
-- original order - so the source tag stays first.

CREATE OR REPLACE FUNCTION clean_tags(t TEXT[])
RETURNS TEXT[] AS $ct$
    SELECT array_agg(v ORDER BY ord)
    FROM (
        SELECT DISTINCT ON (upper(btrim(v))) upper(btrim(v)) AS v, ord
        FROM unnest(t) WITH ORDINALITY AS u(v, ord)
        WHERE btrim(COALESCE(v, '')) <> ''
        ORDER BY upper(btrim(v)), ord
    ) d;
$ct$ LANGUAGE sql IMMUTABLE;


-- ── Name helper ──────────────────────────────────────────────────
-- Source systems use placeholder text where a name is missing.
-- Treat those as no name rather than sending them to Como as if
-- someone were actually called "Unknown".

CREATE OR REPLACE FUNCTION clean_name(v TEXT)
RETURNS TEXT AS $cn$
    SELECT CASE
        WHEN lower(btrim(COALESCE(v, ''))) IN (
            '', '-', '.', 'n/a', 'na', 'none', 'null', 'unknown',
            'no name', 'noname', 'test', 'guest', 'customer', 'xxx', '???'
        ) THEN NULL
        ELSE btrim(v)
    END;
$cn$ LANGUAGE sql IMMUTABLE;


-- ── The refresh function ─────────────────────────────────────────
-- Walks every enabled source_map row and merges that table in.
-- Processed in priority order; the first source to supply a value for
-- a field keeps it.

CREATE OR REPLACE FUNCTION refresh_master()
RETURNS TABLE (source TEXT, rows_merged BIGINT) AS $fn$
DECLARE
    m   RECORD;
    sql TEXT;
    n   BIGINT;
BEGIN
    FOR m IN
        SELECT * FROM source_map WHERE enabled ORDER BY priority, source_table
    LOOP
        -- Skip gracefully if that table doesn't exist yet.
        IF to_regclass(m.source_table) IS NULL THEN
            source := m.source_table;
            rows_merged := -1;             -- -1 means "table not found"
            RETURN NEXT;
            CONTINUE;
        END IF;

        sql := format($f$
            INSERT INTO master_customers
                (email, first_name, last_name, nationality,
                 birthday, tags, allow_email, sources)
            SELECT DISTINCT ON (lower(trim(%1$s)))
                lower(trim(%1$s)),
                %2$s, %3$s, %4$s, %5$s,
                clean_tags(ARRAY[%6$L] || %11$s),
                %7$s,
                ARRAY[%8$L]
            FROM %9$I
            WHERE %1$s IS NOT NULL
              AND trim(%1$s) <> ''
              AND %1$s ~ '^[^@[:space:]]+@[^@[:space:]]+\.[A-Za-z]{2,}$'
              -- Local part that's only digits is a placeholder, not a person
              AND split_part(%1$s, '@', 1) !~ '^[0-9]+$'
              AND lower(split_part(%1$s, '@', 2))
                    NOT IN (SELECT domain FROM blocked_domains)
              AND (%10$s)
            ORDER BY lower(trim(%1$s))
            ON CONFLICT (email) DO UPDATE SET
                first_name  = COALESCE(master_customers.first_name,  EXCLUDED.first_name),
                last_name   = COALESCE(master_customers.last_name,   EXCLUDED.last_name),
                nationality = COALESCE(master_customers.nationality, EXCLUDED.nationality),
                birthday    = COALESCE(master_customers.birthday,    EXCLUDED.birthday),
                -- Union the tags, but the existing source tag stays first.
                tags        = clean_tags(master_customers.tags || EXCLUDED.tags),
                allow_email = COALESCE(master_customers.allow_email, EXCLUDED.allow_email),
                sources     = ARRAY(SELECT DISTINCT unnest(
                                  master_customers.sources || EXCLUDED.sources)),
                needs_push  = true
            WHERE master_customers.first_name  IS NULL AND EXCLUDED.first_name  IS NOT NULL
               OR master_customers.last_name   IS NULL AND EXCLUDED.last_name   IS NOT NULL
               OR master_customers.nationality IS NULL AND EXCLUDED.nationality IS NOT NULL
               OR master_customers.birthday    IS NULL AND EXCLUDED.birthday    IS NOT NULL
               OR NOT (EXCLUDED.tags <@ master_customers.tags)
               OR master_customers.allow_email IS NULL AND EXCLUDED.allow_email IS NOT NULL
               OR NOT (EXCLUDED.sources <@ master_customers.sources)
        $f$,
            'fix_domain(' || m.email_expr || ')',                                  -- %1
            COALESCE('clean_name(' || m.first_name_expr || ')', 'NULL'),            -- %2
            COALESCE('clean_name(' || m.last_name_expr  || ')', 'NULL'),            -- %3
            COALESCE('nullif(trim(' || m.nationality_expr || '), '''')', 'NULL'),   -- %4
            COALESCE(m.birthday_expr, 'NULL'),                                      -- %5
            m.source_tag,                                                           -- %6 first tag
            COALESCE(m.allow_email_expr, 'NULL'),                                   -- %7
            m.source_table,                                                         -- %8 label
            m.source_table,                                                         -- %9 FROM
            COALESCE(m.where_extra, 'true'),                                        -- %10
            COALESCE(m.extra_tags_expr, 'ARRAY[]::text[]')                          -- %11
        );

        EXECUTE sql;
        GET DIAGNOSTICS n = ROW_COUNT;

        source := m.source_table;
        rows_merged := n;
        RETURN NEXT;
    END LOOP;
END;
$fn$ LANGUAGE plpgsql;


-- ── Register today's sources ─────────────────────────────────────
-- priority: lower number wins when sources disagree. The wi-fi portal
-- is customer-entered, so it's trusted over Revel's staff-entered data.

INSERT INTO source_map (
    source_table, email_expr, first_name_expr, last_name_expr,
    nationality_expr, birthday_expr, source_tag, extra_tags_expr,
    allow_email_expr, where_extra, priority
) VALUES
(
    'wifi_guests',
    'email', 'fname', 'lname',
    NULL,
    -- Firestore stores dob as text, e.g. '1984-09-17'
    $$CASE WHEN dob ~ '^\d{4}-\d{2}-\d{2}'
           THEN substring(dob from 1 for 10)::date END$$,
    'WIFI',                                  -- first tag, always
    -- see parse_market() above
    $$parse_market(lastmarket)$$,
    -- The portal requires a consent checkbox before it will submit, so
    -- everyone in this table consented - it just isn't recorded per row.
    -- Once index.html saves the checkbox, point this at that column.
    $$true$$,
    NULL,
    10
),
(
    'revel_customers',
    'email', 'first_name', 'last_name',
    NULL,
    NULL,
    'REVEL',                                 -- first tag, always
    $$ARRAY['UAE', brand]$$,                 -- Revel has no country field
    -- Revel has no opt-out mechanism: giving an email is the opt-in, and
    -- email_opt_in is a default rather than a recorded decision. Don't
    -- read it as a decline.
    $$true$$,
    'email_ok',    -- only rows that passed the email checks
    20
)
ON CONFLICT (source_table) DO NOTHING;


-- ── Split tags into segmentable fields ───────────────────────────
-- Tags arrive as an ordered array, e.g. {WIFI, PICKL, UAE, CITY-WALK}.
-- Position isn't reliable once two sources merge, so each dimension is
-- identified by what it contains rather than where it sits.

CREATE OR REPLACE FUNCTION split_dimensions()
RETURNS TABLE (with_brand BIGINT, with_venue BIGINT) AS $sd$
BEGIN
    UPDATE master_customers m
    SET src_system = (
            SELECT t FROM unnest(m.tags) AS t
            WHERE t IN ('WIFI', 'REVEL') LIMIT 1),
        brand = (
            SELECT t FROM unnest(m.tags) AS t
            WHERE t IN ('PICKL', 'BONBIRD', 'SOUTHPOUR') LIMIT 1),
        country = (
            SELECT t FROM unnest(m.tags) AS t
            WHERE t IN (SELECT tag FROM country_map) LIMIT 1),
        venue = (
            -- Whatever's left over is the venue
            SELECT t FROM unnest(m.tags) AS t
            WHERE t NOT IN ('WIFI', 'REVEL', 'PICKL', 'BONBIRD', 'SOUTHPOUR')
              AND t NOT IN (SELECT tag FROM country_map)
            LIMIT 1);

    RETURN QUERY
    SELECT count(*) FILTER (WHERE brand IS NOT NULL),
           count(*) FILTER (WHERE venue IS NOT NULL)
    FROM master_customers;
END;
$sd$ LANGUAGE plpgsql;


-- ── Duplicate detection ──────────────────────────────────────────
-- master_customers is keyed on email, so identical addresses can't
-- both be present. What we're looking for is the SAME PERSON under
-- DIFFERENT addresses.
--
-- Gmail treats dots as insignificant and ignores anything after a
-- "+", so all of these reach one inbox:
--     j.o.h.n@gmail.com
--     john+shopping@gmail.com
--     john@googlemail.com
-- Pushing all three to Como creates three members for one person and
-- emails them three times.

CREATE OR REPLACE FUNCTION normalise_email(e TEXT)
RETURNS TEXT AS $ne$
DECLARE
    local  TEXT;
    domain TEXT;
BEGIN
    IF e IS NULL OR position('@' in e) = 0 THEN
        RETURN NULL;
    END IF;

    local  := lower(split_part(e, '@', 1));
    domain := lower(split_part(e, '@', 2));

    -- Anything after + is a user-chosen label, not part of the address
    local := split_part(local, '+', 1);

    -- Gmail (and googlemail) ignore dots entirely
    IF domain IN ('gmail.com', 'googlemail.com') THEN
        local  := replace(local, '.', '');
        domain := 'gmail.com';
    END IF;

    RETURN local || '@' || domain;
END;
$ne$ LANGUAGE plpgsql IMMUTABLE;


CREATE OR REPLACE FUNCTION find_duplicates()
RETURNS TABLE (duplicates BIGINT, people_affected BIGINT) AS $fd$
BEGIN
    -- Start clean so a rebuild re-evaluates everything
    UPDATE master_customers
    SET email_key = normalise_email(email),
        duplicate_of = NULL,
        dup_reason = NULL;

    -- Within each group sharing a normalised address, keep one record
    -- and point the others at it. The keeper is the one with the most
    -- complete data, then the shortest address, then alphabetical - so
    -- the choice is stable between runs.
    WITH ranked AS (
        SELECT email, email_key,
               row_number() OVER (
                   PARTITION BY email_key
                   ORDER BY (first_name IS NOT NULL)::int
                          + (last_name IS NOT NULL)::int
                          + (birthday IS NOT NULL)::int DESC,
                            length(email),
                            email
               ) AS rn,
               first_value(email) OVER (
                   PARTITION BY email_key
                   ORDER BY (first_name IS NOT NULL)::int
                          + (last_name IS NOT NULL)::int
                          + (birthday IS NOT NULL)::int DESC,
                            length(email),
                            email
               ) AS keeper
        FROM master_customers
        WHERE email_key IS NOT NULL
    )
    UPDATE master_customers m
    SET duplicate_of = r.keeper,
        dup_reason   = 'same inbox as ' || r.keeper,
        needs_push   = false
    FROM ranked r
    WHERE m.email = r.email AND r.rn > 1;

    RETURN QUERY
    SELECT count(*) FILTER (WHERE duplicate_of IS NOT NULL),
           count(DISTINCT email_key) FILTER (WHERE duplicate_of IS NOT NULL)
    FROM master_customers;
END;
$fd$ LANGUAGE plpgsql;


-- Same person under unrelated addresses - can't be resolved
-- automatically without risking merging two real people, so this only
-- reports. Review before acting on it.
CREATE OR REPLACE VIEW possible_duplicates AS
SELECT lower(btrim(first_name)) AS first_name,
       lower(btrim(last_name))  AS last_name,
       birthday,
       count(*)                 AS records,
       array_agg(email ORDER BY email) AS emails
FROM master_customers
WHERE duplicate_of IS NULL
  AND first_name IS NOT NULL
  AND last_name IS NOT NULL
GROUP BY 1, 2, 3
HAVING count(*) > 1;


-- ── Run it ───────────────────────────────────────────────────────

\echo ''
\echo '=== merging sources (-1 means table not found) ==='
SELECT * FROM refresh_master();

\echo ''
\echo '=== splitting tags into fields ==='
SELECT * FROM split_dimensions();

\echo ''
\echo '=== duplicate check ==='
SELECT * FROM find_duplicates();

\echo ''
\echo '=== master_customers ==='
SELECT
    count(*)                                    AS people,
    count(*) FILTER (WHERE allow_email)         AS consented,
    count(*) FILTER (WHERE allow_email IS NULL) AS consent_unknown,
    count(*) FILTER (WHERE allow_email = false) AS opted_out,
    count(*) FILTER (WHERE duplicate_of IS NOT NULL) AS duplicates_held_back,
    count(*) FILTER (WHERE needs_push)          AS awaiting_push
FROM master_customers;

\echo ''
\echo '=== segmentable fields ==='
SELECT brand, country, venue, count(*) AS people
FROM master_customers
GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 25;

\echo ''
\echo '=== mistyped domains corrected on the way in ==='
SELECT dc.wrong || ' -> ' || dc.correct AS correction, count(*) AS records
FROM domain_corrections dc
JOIN wifi_guests w ON lower(split_part(w.email, '@', 2)) = dc.wrong
GROUP BY 1 ORDER BY 2 DESC LIMIT 15;

\echo ''
\echo '=== same name, different address (review these yourself) ==='
SELECT * FROM possible_duplicates ORDER BY records DESC LIMIT 20;

\echo ''
\echo '=== sample tags (Como wants these as a JSON array) ==='
SELECT email, tags, to_jsonb(tags) AS como_format
FROM master_customers
LIMIT 5;


-- Keep the dashboard's read-only user working after a rebuild.
-- Only tables this role owns - dashboard_runs belongs to postgres.
DO $grant$
DECLARE t RECORD;
BEGIN
    FOR t IN SELECT tablename FROM pg_tables
             WHERE schemaname = 'public' AND tableowner = current_user
    LOOP
        EXECUTE format('GRANT SELECT ON %I TO dashboard', t.tablename);
    END LOOP;
EXCEPTION WHEN undefined_object THEN
    NULL;   -- dashboard role doesn't exist, fine
END
$grant$;


-- =================================================================
-- Adding a source later
-- =================================================================
--
-- Load the new data into its own table however you like, then tell
-- the master table about it - nothing in this file changes:
--
--   INSERT INTO source_map (
--       source_table, email_expr, first_name_expr, last_name_expr,
--       nationality_expr, birthday_expr, source_tag, extra_tags_expr,
--       allow_email_expr,
--       where_extra, priority
--   ) VALUES (
--       'my_new_source',      -- table name
--       'email_address',      -- column holding the email
--       'given_name',         -- or NULL if absent
--       'family_name',
--       'country',
--       'date_of_birth',      -- must be a DATE, or an expression casting to one
--       'MYSOURCE',           -- always the first tag
--       $$ARRAY['UAE', venue]$$,  -- extra tags (any SQL returning text[])
--       'opted_in',
--       NULL,                 -- optional extra filter
--       30                    -- priority
--   );
--
-- Then:  SELECT * FROM refresh_master();
--
-- Disable a source without deleting it:
--   UPDATE source_map SET enabled = false WHERE source_table = 'x';
--
-- Block another domain everywhere:
--   INSERT INTO blocked_domains VALUES ('example.com');
-- =================================================================