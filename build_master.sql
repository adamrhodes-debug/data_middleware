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
    tags            TEXT[],             -- Tag, e.g. {UAE,BONBIRD,MAILCHIMP}
                                        --   first element is always the source
    allow_email     BOOLEAN,            -- AllowEmail (NULL = unknown)

    -- Housekeeping for the push
    sources         TEXT[],
    needs_push      BOOLEAN NOT NULL DEFAULT true,
    last_pushed_at  TIMESTAMPTZ,
    push_status     TEXT                -- 'ok' | 'failed' | 'conflict'
);

CREATE INDEX IF NOT EXISTS master_needs_push_idx
    ON master_customers (needs_push) WHERE needs_push;


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


-- ── Domains never allowed into the master table ──────────────────

CREATE TABLE IF NOT EXISTS blocked_domains (
    domain TEXT PRIMARY KEY
);

INSERT INTO blocked_domains (domain)
VALUES ('grubtech.com'), ('yolkbrands.com')
ON CONFLICT DO NOTHING;


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
            m.email_expr,                                                          -- %1
            COALESCE('nullif(trim(' || m.first_name_expr  || '), '''')', 'NULL'),   -- %2
            COALESCE('nullif(trim(' || m.last_name_expr   || '), '''')', 'NULL'),   -- %3
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
    -- lastmarket looks like 'pickl-uae-jbr' - split it on hyphens so
    -- each part becomes its own tag: PICKL, UAE, JBR
    $$string_to_array(lastmarket, '-')$$,
    NULL,          -- portal never saves its marketing checkbox
    NULL,
    10
),
(
    'revel_customers',
    'email', 'first_name', 'last_name',
    NULL,
    NULL,
    'REVEL',                                 -- first tag, always
    $$ARRAY['UAE', brand]$$,                 -- then country, then brand
    'email_opt_in',
    'email_ok',    -- only rows that passed the email checks
    20
)
ON CONFLICT (source_table) DO NOTHING;


-- ── Run it ───────────────────────────────────────────────────────

\echo ''
\echo '=== merging sources (-1 means table not found) ==='
SELECT * FROM refresh_master();

\echo ''
\echo '=== master_customers ==='
SELECT
    count(*)                                    AS people,
    count(*) FILTER (WHERE allow_email)         AS consented,
    count(*) FILTER (WHERE allow_email IS NULL) AS consent_unknown,
    count(*) FILTER (WHERE allow_email = false) AS opted_out,
    count(*) FILTER (WHERE needs_push)          AS awaiting_push
FROM master_customers;

\echo ''
\echo '=== sample tags (Como wants these as a JSON array) ==='
SELECT email, tags, to_jsonb(tags) AS como_format
FROM master_customers
LIMIT 5;


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
