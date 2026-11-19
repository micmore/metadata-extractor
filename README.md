# metadata-extractor

Pulls metadata values out of structured fields (JSON / XML / `key=value` blobs living
inside a column or API response) and lands them in SQLite so you can query them with SQL.

**Stdlib only.** `requests` is needed only for the `--api` source. No pandas, no venv.

```
source (CSV or API)  ->  detect format  ->  flatten to key/value  ->  SQLite  ->  SQL
```

## Why it is shaped this way

The metadata keys aren't known up front and aren't the same on every row, so the loader
does **not** try to build a wide table. It writes one row per key into an EAV table
(`metadata`), which absorbs any shape. `pivot` then generates a wide SQL **view** from
whatever keys actually turned up. Schema discovery happens after the load, not before it.

## Quick start

```bash
cd ~/Projects/Tools/metadata-extractor

# 1. See what is actually in your columns before writing any parsing rules
python3 metaextract.py profile --csv sample_data.csv

# 2. Load the columns it flagged
python3 metaextract.py load --csv sample_data.csv \
    --field metadata --field attributes \
    --db meta.db --id-column id --strip-xml-root

# 3. Data dictionary of every key found
python3 metaextract.py keys --db meta.db

# 4. Generate the wide view
python3 metaextract.py pivot --db meta.db --min-records 3

# 5. Query it
python3 metaextract.py sql --db meta.db "SELECT * FROM v_metadata_wide LIMIT 5"
```

## GUI

```bash
python3 metagui.py            # or: python3 metagui.py /path/to/meta.db
```

Tkinter front-end over the same module — `metagui.py` contains only widgets and
threading, every bit of parsing/loading logic still comes from `metaextract.py`, so the
GUI and CLI can never drift apart.

Five tabs matching the workflow:

1. **Source & Profile** — pick a CSV (or fill in the API fields), set sample size, hit
   Profile. Full profiler report in the pane.
2. **Load** — column list with structured columns **auto-detected and preselected in
   green**, ID column auto-guessed, options, then Load. Pivot controls underneath.
3. **Keys** — sortable key census.
4. **Query** — SQL box with all 19 queries from `queries.sql` in a dropdown,
   Ctrl+Enter to run, results grid, Export CSV.
5. **Parse errors** — the rows that failed, with the original raw value.

Long loads run on a worker thread with a progress bar, so the window doesn't freeze.

## Commands

| command | what it does |
|---|---|
| `profile` | Samples rows, reports the format mix per column, lists every metadata key found and its fill rate. **Run this first.** Changes nothing. |
| `load` | Parses and loads into SQLite. Re-running replaces that source's rows (idempotent). |
| `keys` | Key census: occurrences, records, distinct values, empties. |
| `pivot` | Builds/refreshes a wide view from observed keys. Re-run after new loads. |
| `sql` | Runs ad-hoc SQL or a whole `.sql` file. `--csv-out` for export. (The `sqlite3` CLI isn't installed on mmmlinux, hence this.) |

## Postman → API

Use Postman to work out the endpoint, auth header and response shape **once**, then move
it into the script. A pipeline that needs a human to click Send isn't a pipeline.

In Postman: `Code` (`</>` icon) → **Python – Requests** gives you the URL, headers and
params to paste in below.

```bash
python3 metaextract.py profile \
    --api "https://api.example.com/v1/items" \
    --api-header "Authorization: Bearer $TOKEN" \
    --json-path data.items

python3 metaextract.py load \
    --api "https://api.example.com/v1/items" \
    --api-header "Authorization: Bearer $TOKEN" \
    --json-path data.items \
    --page-param page --pages 10 \
    --db meta.db --id-column id
```

- `--json-path` is a dotted path to the array of records (`data.items`). Omit it if the
  response is already a bare array.
- `--page-param page --pages 10` walks `?page=1..10` and stops early on an empty page.
- Keep tokens in an env var, not in shell history: `export TOKEN=...`.

Because both sources emit the same flat dicts, everything downstream is unchanged —
loading from the API writes to the same tables the CSV did, tagged with a different
`source_ref`, so you can diff the two (see query 8 in `queries.sql`).

## Schema

```sql
records (record_id PK, source_ref, natural_key, row_num, ingested_at, raw)
metadata(record_id FK, field, key, value, value_num, fmt)
v_key_census  -- per-key stats, created automatically
```

- `value` is always text; `value_num` is populated whenever the value parsed as a number,
  so `AVG()`/`>` work without casting.
- `fmt` records how that value was parsed (`json` / `xml` / `kv` / `scalar`).
- `natural_key` comes from `--id-column`, else a hash of the row.
- `--keep-raw` also stores the original row as JSON in `records.raw`.

## Key naming

| input | key |
|---|---|
| `{"vendor":{"name":"Acme"}}` | `vendor.name` |
| `{"tags":["new","sale"]}` | `tags[0]`, `tags[1]` |
| `<vendor id="92">Initech</vendor>` | `vendor@id`, `vendor` |
| `color=red;weight=1.2` | `color`, `weight` |

Two gotchas, both visible in `sample_data.csv`:

1. **XML nests under its root tag**, so `<item><sku>` becomes `item.sku` while JSON rows
   give plain `sku`, and they won't line up in the pivot. `--strip-xml-root` drops the
   wrapper.
2. **The same concept can arrive under different keys** across formats (`vendor.name` in
   JSON vs `vendor` in XML). `COALESCE` them in SQL — query 3 in `queries.sql`.

## Malformed data

A value that starts with `{`/`[`/`<` but doesn't parse is classified `invalid_json` /
`invalid_xml`. It is **not** passed to the `key=value` guesser, which would otherwise
shred it into nonsense keys. `profile` counts these, `load` skips just that field, and
the rest of the row still loads.

Failures are also written to the **`parse_errors`** table with the original value, so a
bad row is still findable a week after the load message scrolled off screen:

```sql
SELECT r.natural_key, e.field, e.error, e.raw
FROM parse_errors e JOIN records r ON r.record_id = e.record_id;
```

(GUI: tab 5.) Don't rely on the orphan-record check in query 7 for this — a row with one
bad column and one good column still produces metadata and won't show up there.

## Files

- `metaextract.py` — the tool (CLI)
- `metagui.py` — Tkinter GUI over the same module
- `queries.sql` — 19 working queries: data dictionary, filtering, aggregation, array
  handling, data-quality checks, source-vs-source diffing, export
- `sample_data.csv` — 7 rows deliberately mixing JSON, XML, `key=value`, an empty cell
  and a malformed JSON row; use it to see each behaviour
- `demo.sh` — `./demo.sh` runs all 12 steps against the sample data into `demo.db`
  (kept separate so it never disturbs the GUI's database)

## Scaling out

If this outgrows SQLite, `--db` is the only thing that changes conceptually: the `records`
/ `metadata` tables port to SQL Server on hmserverlx as-is (swap `sqlite3.connect` for
`pyodbc.connect`, `REAL` → `FLOAT`, `TEXT` → `NVARCHAR(MAX)`). Keep the EAV shape; the
pivot view is the part you'd rewrite per dialect (`PIVOT` in T-SQL).
