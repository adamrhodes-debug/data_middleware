-- =================================================================
-- split_dimensions() — read the source list from source_map
--
-- Run once:  psql "$DB" -f fix_split_dimensions.sql
--
-- Before: the source names were a hardcoded tuple, so every new
-- source needed this function edited. If you forgot, src_system came
-- out NULL and the new source tag was misread as the venue — which
-- then went to Como in the VENUE generic field.
--
-- After: sources come from source_map.source_tag. Registering a
-- source is now the only step.
--
-- Brands are still a literal list, but defined once at the top
-- instead of twice inside the UPDATE.
-- =================================================================

CREATE OR REPLACE FUNCTION split_dimensions()
RETURNS TABLE (with_brand BIGINT, with_venue BIGINT) AS $sd$
DECLARE
    sources TEXT[];
    brands  TEXT[] := ARRAY['PICKL', 'BONBIRD', 'SOUTHPOUR'];
BEGIN
    -- Every registered source's tag, e.g. {WIFI, REVEL, NEWSLETTER, CONTACT}
    SELECT coalesce(array_agg(DISTINCT upper(source_tag)), ARRAY[]::TEXT[])
      INTO sources
      FROM source_map
     WHERE source_tag IS NOT NULL AND source_tag <> '';

    UPDATE master_customers m
    SET src_system = (
            SELECT t FROM unnest(m.tags) AS t
            WHERE t = ANY(sources) LIMIT 1),
        brand = (
            SELECT t FROM unnest(m.tags) AS t
            WHERE t = ANY(brands) LIMIT 1),
        country = (
            SELECT t FROM unnest(m.tags) AS t
            WHERE t IN (SELECT tag FROM country_map) LIMIT 1),
        venue = (
            -- Whatever's left over is the venue
            SELECT t FROM unnest(m.tags) AS t
            WHERE NOT (t = ANY(sources))
              AND NOT (t = ANY(brands))
              AND t NOT IN (SELECT tag FROM country_map)
            LIMIT 1);

    RETURN QUERY
    SELECT count(*) FILTER (WHERE brand IS NOT NULL),
           count(*) FILTER (WHERE venue IS NOT NULL)
    FROM master_customers;
END;
$sd$ LANGUAGE plpgsql;


\echo ''
\echo 'split_dimensions() updated. Sources it will now recognise:'
SELECT string_agg(DISTINCT upper(source_tag), ', ' ORDER BY upper(source_tag))
    AS recognised_sources
FROM source_map
WHERE source_tag IS NOT NULL AND source_tag <> '';
