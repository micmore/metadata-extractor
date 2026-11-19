#!/usr/bin/env bash
# End-to-end demo of metaextract.py against sample_data.csv.
# Uses demo.db so it never disturbs whatever you have open in the GUI.
#
#   ./demo.sh
set -euo pipefail
cd "$(dirname "$0")"

DB=demo.db
PY=python3
rm -f "$DB"

step() { printf '\n\033[1;36m═══ %s ═══\033[0m\n' "$*"; }
note() { printf '\033[0;33m→ %s\033[0m\n' "$*"; }

step "0. The raw data"
note "7 rows, deliberately messy: JSON, XML, key=value, an empty cell, malformed JSON"
# a real CSV parser, not `column -s,` -- the metadata field contains commas
$PY - <<'EOF'
import csv, textwrap
with open("sample_data.csv", newline="", encoding="utf-8-sig") as fh:
    for row in csv.DictReader(fh):
        print(f"\033[1m{row['id']}  {row['name']:<12}\033[0m {row['notes']}")
        meta = row["metadata"] or "(empty)"
        print("    metadata:   " + textwrap.shorten(meta, 118, placeholder=" …"))
        print("    attributes: " + row["attributes"])
EOF

step "1. PROFILE — what is actually in these columns?"
note "Changes nothing. Run this before writing a single parsing rule."
$PY metaextract.py profile --csv sample_data.csv

step "2. LOAD — parse and store"
note "--strip-xml-root aligns the XML row's keys with the JSON rows'"
$PY metaextract.py load --csv sample_data.csv \
    --field metadata --field attributes \
    --db "$DB" --id-column id --keep-raw --strip-xml-root

step "3. IDEMPOTENCY — run the exact same load again"
note "Should still be 7 records, not 14"
$PY metaextract.py load --csv sample_data.csv \
    --field metadata --field attributes \
    --db "$DB" --id-column id --keep-raw --strip-xml-root
$PY metaextract.py sql --db "$DB" \
    "SELECT COUNT(*) AS records, (SELECT COUNT(*) FROM metadata) AS metadata_pairs FROM records"

step "4. KEYS — the data dictionary, discovered not declared"
$PY metaextract.py keys --db "$DB"

step "5. PIVOT — build a wide view from the keys that actually turned up"
$PY metaextract.py pivot --db "$DB" --min-records 3

step "6. QUERY — the wide view"
$PY metaextract.py sql --db "$DB" \
    "SELECT natural_key, sku, region, price, vendor_name, color, weight, bin
     FROM v_metadata_wide ORDER BY natural_key"

step "7. AGGREGATION — value_num means no casting in SQL"
$PY metaextract.py sql --db "$DB" \
    "SELECT reg.value AS region, COUNT(*) AS n,
            ROUND(AVG(pr.value_num),2) AS avg_price,
            ROUND(SUM(pr.value_num),2) AS total
     FROM metadata reg
     JOIN metadata pr ON pr.record_id = reg.record_id AND pr.key = 'price'
     WHERE reg.key = 'region' GROUP BY reg.value ORDER BY total DESC"

step "8. JSON ARRAYS — tags[0], tags[1] ... queried positionally-agnostic"
$PY metaextract.py sql --db "$DB" \
    "SELECT value AS tag, COUNT(DISTINCT record_id) AS records
     FROM metadata WHERE key LIKE 'tags[%' AND value IS NOT NULL
     GROUP BY value ORDER BY records DESC"

step "9. MIXED FORMATS — same concept, different key per format"
note "JSON rows say vendor.name; the XML row says vendor. COALESCE reconciles them."
$PY metaextract.py sql --db "$DB" \
    "SELECT r.natural_key,
            COALESCE(MAX(CASE WHEN m.key='vendor.name' THEN m.value END),
                     MAX(CASE WHEN m.key='vendor'      THEN m.value END)) AS vendor
     FROM records r JOIN metadata m ON m.record_id = r.record_id
     GROUP BY r.record_id, r.natural_key ORDER BY r.natural_key"

step "10. BAD DATA — findable in SQL, not just in a log line"
$PY metaextract.py sql --db "$DB" \
    "SELECT r.natural_key, e.field, e.fmt, e.error, e.raw
     FROM parse_errors e JOIN records r ON r.record_id = e.record_id"

step "11. DATA QUALITY — keys with inconsistent types"
$PY metaextract.py sql --db "$DB" \
    "SELECT key,
            SUM(CASE WHEN value_num IS NOT NULL THEN 1 ELSE 0 END) AS numeric_vals,
            SUM(CASE WHEN value_num IS NULL     THEN 1 ELSE 0 END) AS text_vals
     FROM metadata WHERE value IS NOT NULL AND value <> ''
     GROUP BY key HAVING numeric_vals > 0 AND text_vals > 0"

step "12. EXPORT"
$PY metaextract.py sql --db "$DB" --csv-out \
    "SELECT * FROM v_metadata_wide ORDER BY natural_key" > demo_export.csv
note "wrote demo_export.csv ($(wc -l < demo_export.csv) lines incl. header)"

printf '\n\033[1;32mDemo complete.\033[0m  Database: %s   GUI: python3 metagui.py %s\n' "$DB" "$DB"
