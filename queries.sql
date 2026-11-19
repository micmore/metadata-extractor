-- Working SQL against the metadata database built by metaextract.py
-- Run with:  sqlite3 meta.db < queries.sql
-- Tables:    records(record_id, source_ref, natural_key, row_num, ingested_at, raw)
--            metadata(record_id, field, key, value, value_num, fmt)

-- ---------------------------------------------------------------------------
-- 1. What keys exist, and how complete are they?
--    Run this first on any new dataset. It is your data dictionary.
-- ---------------------------------------------------------------------------
SELECT field, key, records, distinct_values, empties
FROM v_key_census
ORDER BY records DESC, key;

-- Coverage: which keys are only present on some records?
SELECT c.field, c.key, c.records,
       (SELECT COUNT(*) FROM records) AS total_records,
       ROUND(100.0 * c.records / (SELECT COUNT(*) FROM records), 1) AS pct_present
FROM v_key_census c
WHERE c.records < (SELECT COUNT(*) FROM records)
ORDER BY pct_present;

-- ---------------------------------------------------------------------------
-- 2. Pull one metadata value per record (the everyday query)
-- ---------------------------------------------------------------------------
SELECT r.natural_key, m.value AS sku
FROM records r
JOIN metadata m ON m.record_id = r.record_id AND m.key = 'sku';

-- Several keys at once, without the generated pivot view
SELECT r.natural_key,
       MAX(CASE WHEN m.key = 'sku'    THEN m.value END)     AS sku,
       MAX(CASE WHEN m.key = 'region' THEN m.value END)     AS region,
       MAX(CASE WHEN m.key = 'price'  THEN m.value_num END) AS price
FROM records r
JOIN metadata m ON m.record_id = r.record_id
GROUP BY r.record_id, r.natural_key;

-- ---------------------------------------------------------------------------
-- 3. Reconciling keys that mean the same thing
--    JSON rows may say vendor.name while XML rows say vendor -- COALESCE them.
-- ---------------------------------------------------------------------------
SELECT r.natural_key,
       COALESCE(
           MAX(CASE WHEN m.key = 'vendor.name' THEN m.value END),
           MAX(CASE WHEN m.key = 'vendor'      THEN m.value END)
       ) AS vendor_name
FROM records r
JOIN metadata m ON m.record_id = r.record_id
GROUP BY r.record_id, r.natural_key;

-- ---------------------------------------------------------------------------
-- 4. Filtering ON a metadata value (this is why it is in SQL and not a dict)
-- ---------------------------------------------------------------------------
-- records whose region is us-east
SELECT r.natural_key
FROM records r
JOIN metadata m ON m.record_id = r.record_id
WHERE m.key = 'region' AND m.value = 'us-east';

-- AND across two different keys: needs one join per condition
SELECT r.natural_key
FROM records r
JOIN metadata reg ON reg.record_id = r.record_id AND reg.key = 'region'
JOIN metadata pr  ON pr.record_id  = r.record_id AND pr.key  = 'price'
WHERE reg.value = 'us-east' AND pr.value_num > 15;

-- ---------------------------------------------------------------------------
-- 5. Aggregation -- value_num is populated whenever the value parsed as a number
-- ---------------------------------------------------------------------------
SELECT reg.value                     AS region,
       COUNT(*)                      AS n,
       ROUND(AVG(pr.value_num), 2)   AS avg_price,
       ROUND(SUM(pr.value_num), 2)   AS total_price
FROM metadata reg
JOIN metadata pr ON pr.record_id = reg.record_id AND pr.key = 'price'
WHERE reg.key = 'region'
GROUP BY reg.value
ORDER BY total_price DESC;

-- Value distribution for any key -- good for spotting typos and stray casing
SELECT value, COUNT(*) AS n
FROM metadata
WHERE key = 'region'
GROUP BY value
ORDER BY n DESC;

-- ---------------------------------------------------------------------------
-- 6. Array members. JSON arrays flatten to tags[0], tags[1], ...
-- ---------------------------------------------------------------------------
SELECT r.natural_key, m.key, m.value
FROM records r
JOIN metadata m ON m.record_id = r.record_id
WHERE m.key LIKE 'tags[%'
ORDER BY r.natural_key, m.key;

-- Which records carry a given tag, regardless of its position?
SELECT DISTINCT r.natural_key
FROM records r
JOIN metadata m ON m.record_id = r.record_id
WHERE m.key LIKE 'tags[%' AND m.value = 'new';

-- Tag frequency
SELECT value AS tag, COUNT(DISTINCT record_id) AS records
FROM metadata
WHERE key LIKE 'tags[%' AND value IS NOT NULL
GROUP BY value
ORDER BY records DESC;

-- ---------------------------------------------------------------------------
-- 7. Data-quality checks -- run these before trusting any of the above
-- ---------------------------------------------------------------------------
-- records that produced no metadata at all (empty or unparseable source field)
SELECT r.record_id, r.natural_key, r.row_num
FROM records r
LEFT JOIN metadata m ON m.record_id = r.record_id
WHERE m.record_id IS NULL;

-- duplicate natural keys -- your id column is not unique if this returns rows
SELECT natural_key, COUNT(*) AS n
FROM records
GROUP BY natural_key
HAVING COUNT(*) > 1;

-- keys whose values are not consistently numeric (mixed types hiding in a field)
SELECT key,
       SUM(CASE WHEN value_num IS NOT NULL THEN 1 ELSE 0 END) AS numeric_vals,
       SUM(CASE WHEN value_num IS NULL     THEN 1 ELSE 0 END) AS text_vals
FROM metadata
WHERE value IS NOT NULL AND value <> ''
GROUP BY key
HAVING numeric_vals > 0 AND text_vals > 0;

-- how each source field was parsed, by format
SELECT field, fmt, COUNT(DISTINCT record_id) AS records
FROM metadata
GROUP BY field, fmt
ORDER BY field, records DESC;

-- ---------------------------------------------------------------------------
-- 8. Comparing two loads (e.g. yesterday's CSV vs today's API pull)
-- ---------------------------------------------------------------------------
SELECT source_ref, COUNT(*) AS records, MAX(ingested_at) AS last_run
FROM records
GROUP BY source_ref;

-- values that changed for the same natural_key across two sources
SELECT a.natural_key, ma.key, ma.value AS old_value, mb.value AS new_value
FROM records a
JOIN records b   ON b.natural_key = a.natural_key AND b.source_ref <> a.source_ref
JOIN metadata ma ON ma.record_id = a.record_id
JOIN metadata mb ON mb.record_id = b.record_id AND mb.key = ma.key
WHERE IFNULL(ma.value, '') <> IFNULL(mb.value, '');

-- ---------------------------------------------------------------------------
-- 9. Export a flat result for Excel / handoff
--    In the sqlite3 shell:  .headers on   .mode csv   .once out.csv
-- ---------------------------------------------------------------------------
SELECT * FROM v_metadata_wide;
