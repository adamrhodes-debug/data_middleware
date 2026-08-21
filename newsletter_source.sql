-- =================================================================
-- Newsletter signups from the websites
--
-- Run once:  psql "$DB" -f newsletter_source.sql
--
-- Rows land here the moment someone submits a signup form. The
-- endpoint also pushes them straight to Como, but this table is the
-- record of truth - if Como is unreachable, the weekly sync catches
-- up from here.
-- =================================================================

CREATE TABLE IF NOT EXISTS newsletter_signups (
    email        TEXT PRIMARY KEY,
    brand        TEXT,                 -- PICKL / BONBIRD / SOUTHPOUR
    country      TEXT,                 -- UAE
    src_system   TEXT NOT NULL DEFAULT 'NEWSLETTER',
    site         TEXT,                 -- which website it came from
    page         TEXT,                 -- which page/block, if sent
    first_name   TEXT,
    last_name    TEXT,

    -- Did the immediate push to Como work?
    como_status  TEXT,                 -- 'ok' | 'exists' | 'failed' | NULL
    como_detail  TEXT,
    como_at      TIMESTAMPTZ,

    signed_up_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS newsletter_brand_idx ON newsletter_signups (brand);
CREATE INDEX IF NOT EXISTS newsletter_como_idx  ON newsletter_signups (como_status);


-- Feed it into the master table like any other source.
-- Tags come out as {NEWSLETTER, <brand>, <country>}, which the
-- dimension split then turns into src_system / brand / country.
INSERT INTO source_map (
    source_table, email_expr, first_name_expr, last_name_expr,
    nationality_expr, birthday_expr, source_tag, extra_tags_expr,
    allow_email_expr, where_extra, priority
) VALUES (
    'newsletter_signups',
    'email', 'first_name', 'last_name',
    NULL,
    NULL,                                    -- no birthday collected
    'NEWSLETTER',
    $$ARRAY[country, brand]$$,
    -- Signing up to a newsletter is the consent.
    $$true$$,
    NULL,
    5                                        -- most explicit source, wins conflicts
)
ON CONFLICT (source_table) DO UPDATE SET
    email_expr       = EXCLUDED.email_expr,
    first_name_expr  = EXCLUDED.first_name_expr,
    last_name_expr   = EXCLUDED.last_name_expr,
    source_tag       = EXCLUDED.source_tag,
    extra_tags_expr  = EXCLUDED.extra_tags_expr,
    allow_email_expr = EXCLUDED.allow_email_expr,
    priority         = EXCLUDED.priority;

GRANT SELECT, INSERT, UPDATE ON newsletter_signups TO dashboard;

\echo ''
\echo 'newsletter_signups ready and registered in source_map.'
\echo 'Rebuild the master table to pick up existing rows:'
\echo '  psql "$DB" -c "SELECT * FROM refresh_master();"'
