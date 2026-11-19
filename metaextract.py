#!/usr/bin/env python3
"""Extract metadata out of structured fields (CSV now, API later) into SQLite.

Pipeline:  source -> detect format -> flatten to key/value -> load to SQLite -> query with SQL

Stdlib only, except the `api` source which needs `requests`.

  python3 metaextract.py profile --csv data.csv
  python3 metaextract.py load    --csv data.csv --field metadata --db meta.db --id-column id
  python3 metaextract.py load    --api https://host/v1/items --json-path data.items --db meta.db
  python3 metaextract.py keys    --db meta.db
  python3 metaextract.py pivot   --db meta.db --view v_meta_wide
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from datetime import datetime, timezone

csv.field_size_limit(10 * 1024 * 1024)

PAIR_SEPS = [";", "|", "\n", ","]
KV_SEPS = ["=", ":"]


# --------------------------------------------------------------------------
# format detection
# --------------------------------------------------------------------------

def detect_format(value):
    """Classify a single raw value. Returns (fmt, detail).

    fmt is one of: empty, json, xml, kv, scalar, invalid_json, invalid_xml
    detail carries the separators for kv so the parser doesn't re-guess.

    A value that clearly *means* to be JSON or XML but does not parse is reported
    as invalid rather than falling through to the key=value guesser -- otherwise
    malformed rows get silently shredded into nonsense keys.
    """
    if value is None:
        return "empty", None
    s = str(value).strip()
    if not s:
        return "empty", None

    if s[0] in "{[":
        try:
            json.loads(s)
            return "json", None
        except (ValueError, TypeError) as exc:
            return "invalid_json", str(exc)

    if s.startswith("<"):
        try:
            ET.fromstring(s)
            return "xml", None
        except ET.ParseError as exc:
            return "invalid_xml", str(exc)

    seps = _sniff_kv_seps(s)
    if seps:
        return "kv", seps

    return "scalar", None


def _sniff_kv_seps(s):
    """Find the (pair_sep, kv_sep) that best explains a key=value string."""
    best = None
    best_score = 0.0
    for kv in KV_SEPS:
        if kv not in s:
            continue
        for pair in PAIR_SEPS:
            parts = [p.strip() for p in s.split(pair) if p.strip()]
            if not parts:
                continue
            # every part must look like key<kv>value, with a non-empty key
            good = sum(1 for p in parts if kv in p and p.split(kv, 1)[0].strip())
            score = good / len(parts)
            # prefer explaining more parts; a single pair is a weak signal
            if score == 1.0 and len(parts) >= 1:
                score += len(parts) * 0.01
            if score > best_score:
                best_score, best = score, (pair, kv)
    if best and best_score >= 1.0:
        return best
    return None


# --------------------------------------------------------------------------
# flatteners -> {dotted.path: scalar}
# --------------------------------------------------------------------------

def flatten_json(obj, prefix=""):
    out = {}
    if isinstance(obj, dict):
        if not obj:
            out[prefix or "_root"] = None
        for k, v in obj.items():
            out.update(flatten_json(v, f"{prefix}.{k}" if prefix else str(k)))
    elif isinstance(obj, list):
        if not obj:
            out[prefix or "_root"] = None
        for i, v in enumerate(obj):
            out.update(flatten_json(v, f"{prefix}[{i}]"))
    else:
        out[prefix or "_root"] = obj
    return out


def _strip_ns(tag):
    return tag.split("}", 1)[1] if "}" in tag else tag


def flatten_xml(elem, path):
    out = {}
    for ak, av in elem.attrib.items():
        out[f"{path}@{_strip_ns(ak)}"] = av
    text = (elem.text or "").strip()
    children = list(elem)
    if text:
        out[path if not children else f"{path}#text"] = text
    counts = Counter(_strip_ns(c.tag) for c in children)
    seen = defaultdict(int)
    for c in children:
        tag = _strip_ns(c.tag)
        if counts[tag] > 1:
            child_path = f"{path}.{tag}[{seen[tag]}]"
            seen[tag] += 1
        else:
            child_path = f"{path}.{tag}"
        out.update(flatten_xml(c, child_path))
    return out


def flatten_kv(s, seps):
    pair, kv = seps
    out = {}
    for part in s.split(pair):
        part = part.strip()
        if not part or kv not in part:
            continue
        k, v = part.split(kv, 1)
        k = k.strip()
        if k:
            out[k] = v.strip()
    return out


def extract(value, fmt=None, detail=None, strip_xml_root=False):
    """Raw field value -> flat {key: value}. Detects format if not given."""
    if fmt is None:
        fmt, detail = detect_format(value)
    s = str(value).strip() if value is not None else ""

    if fmt == "empty":
        return {}, fmt
    if fmt.startswith("invalid_"):
        raise ValueError(f"{fmt}: {detail}")
    if fmt == "json":
        return flatten_json(json.loads(s)), fmt
    if fmt == "xml":
        root = ET.fromstring(s)
        flat = flatten_xml(root, _strip_ns(root.tag))
        if strip_xml_root:
            # <item><sku>..</sku></item> -> "sku" instead of "item.sku", so XML
            # rows line up with JSON rows that have no wrapper element.
            prefix = _strip_ns(root.tag)
            flat = {(k[len(prefix) + 1:] if k.startswith(prefix + ".") else k): v
                    for k, v in flat.items()}
        return flat, fmt
    if fmt == "kv":
        return flatten_kv(s, detail or _sniff_kv_seps(s) or (";", "=")), fmt
    return {"_value": s}, fmt


# --------------------------------------------------------------------------
# sources -> iterator of flat dict rows
# --------------------------------------------------------------------------

def csv_source(path):
    with open(path, newline="", encoding="utf-8-sig") as fh:
        head = fh.read(64 * 1024)
        fh.seek(0)
        try:
            dialect = csv.Sniffer().sniff(head, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        for row in csv.DictReader(fh, dialect=dialect):
            yield row


def api_source(url, headers=None, params=None, json_path=None,
               page_param=None, pages=1, start_page=1):
    try:
        import requests
    except ImportError:
        sys.exit("The api source needs requests:  pip3 install requests")

    params = dict(params or {})
    for n in range(pages):
        if page_param:
            params[page_param] = start_page + n
        resp = requests.get(url, headers=headers or {}, params=params, timeout=30)
        resp.raise_for_status()
        payload = resp.json()
        records = _dig(payload, json_path)
        if isinstance(records, dict):
            records = [records]
        if not records:
            break
        for rec in records:
            yield rec


def _dig(payload, path):
    """Walk a dotted path into a JSON payload. Empty path -> payload itself."""
    if not path:
        return payload if isinstance(payload, list) else [payload]
    node = payload
    for part in path.split("."):
        if isinstance(node, list):
            node = node[int(part)]
        else:
            node = node.get(part)
        if node is None:
            return []
    return node


# --------------------------------------------------------------------------
# sqlite
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS records (
    record_id   INTEGER PRIMARY KEY,
    source_ref  TEXT NOT NULL,
    natural_key TEXT,
    row_num     INTEGER,
    ingested_at TEXT NOT NULL,
    raw         TEXT
);
CREATE TABLE IF NOT EXISTS metadata (
    record_id  INTEGER NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    key        TEXT NOT NULL,
    value      TEXT,
    value_num  REAL,
    fmt        TEXT
);
CREATE TABLE IF NOT EXISTS parse_errors (
    record_id  INTEGER NOT NULL REFERENCES records(record_id) ON DELETE CASCADE,
    field      TEXT NOT NULL,
    fmt        TEXT,
    error      TEXT,
    raw        TEXT
);
CREATE INDEX IF NOT EXISTS ix_errors_record   ON parse_errors(record_id);
CREATE INDEX IF NOT EXISTS ix_records_source  ON records(source_ref);
CREATE INDEX IF NOT EXISTS ix_records_natkey  ON records(natural_key);
CREATE INDEX IF NOT EXISTS ix_meta_record     ON metadata(record_id);
CREATE INDEX IF NOT EXISTS ix_meta_key        ON metadata(key);
CREATE INDEX IF NOT EXISTS ix_meta_key_value  ON metadata(key, value);

CREATE VIEW IF NOT EXISTS v_key_census AS
SELECT field, key, fmt,
       COUNT(*)                                   AS occurrences,
       COUNT(DISTINCT record_id)                  AS records,
       COUNT(DISTINCT value)                      AS distinct_values,
       SUM(CASE WHEN value IS NULL OR value = '' THEN 1 ELSE 0 END) AS empties,
       MIN(value) AS min_value, MAX(value) AS max_value
FROM metadata
GROUP BY field, key, fmt;
"""


def connect(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


def _num(v):
    if isinstance(v, bool):
        return 1.0 if v else 0.0
    if isinstance(v, (int, float)):
        return float(v)
    try:
        return float(str(v).strip())
    except (TypeError, ValueError):
        return None


def _text(v):
    if v is None:
        return None
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (dict, list)):
        return json.dumps(v, ensure_ascii=False)
    return str(v)


# --------------------------------------------------------------------------
# commands
# --------------------------------------------------------------------------

def rows_from_args(args):
    if args.csv:
        return csv_source(args.csv), os.path.abspath(args.csv)
    headers = dict(h.split(":", 1) for h in (args.api_header or []))
    headers = {k.strip(): v.strip() for k, v in headers.items()}
    params = dict(p.split("=", 1) for p in (args.api_param or []))
    return (
        api_source(args.api, headers, params, args.json_path,
                   args.page_param, args.pages, args.start_page),
        args.api,
    )


def cmd_profile(args):
    rows, ref = rows_from_args(args)
    sample = []
    for i, row in enumerate(rows):
        if i >= args.sample:
            break
        sample.append(row)
    if not sample:
        sys.exit("No rows found in " + ref)

    columns = args.field or list(sample[0].keys())
    print(f"source: {ref}\nsampled: {len(sample)} rows\n")
    header = f"{'column':<28} {'rows':>6}  format mix"
    print(header)
    print("-" * max(len(header), 72))

    structured = []
    for col in columns:
        fmts = Counter()
        example = None
        keys = Counter()
        for row in sample:
            raw = row.get(col)
            fmt, detail = detect_format(raw)
            fmts[fmt] += 1
            if fmt in ("json", "xml", "kv"):
                if example is None:
                    example = (fmt, detail, str(raw).strip())
                try:
                    flat, _ = extract(raw, fmt, detail)
                    keys.update(flat.keys())
                except Exception:  # noqa: BLE001 - profiling must not die on one bad row
                    fmts["parse_error"] += 1
        mix = ", ".join(f"{f} {n}" for f, n in fmts.most_common())
        print(f"{col:<28} {len(sample):>6}  {mix}")
        if example or any(f.startswith("invalid_") for f in fmts):
            structured.append((col, example, keys, fmts))

    if not structured:
        print("\nNo JSON / XML / key=value columns detected. "
              "These look like plain scalar columns.")
        return

    print("\n" + "=" * 72)
    for col, example, keys, fmts in structured:
        if example:
            fmt, detail, raw = example
            print(f"\ncolumn: {col}   detected: {fmt}"
                  + (f"   separators: pair={detail[0]!r} kv={detail[1]!r}" if detail else ""))
            print(f"  sample raw:  {raw[:160]}{'...' if len(raw) > 160 else ''}")
        else:
            print(f"\ncolumn: {col}   no parseable values in sample")
        print(f"  {len(keys)} distinct metadata keys found:")
        for k, n in keys.most_common(args.top_keys):
            pct = 100.0 * n / len(sample)
            print(f"    {k:<44} {n:>5} rows ({pct:.0f}%)")
        if len(keys) > args.top_keys:
            print(f"    ... and {len(keys) - args.top_keys} more")
        bad = {f: n for f, n in fmts.items() if f.startswith("invalid_") or f == "parse_error"}
        for f, n in bad.items():
            print(f"  !! {n} rows are {f} -- they will be skipped and reported by `load`")

    names = " ".join(f"--field {c}" for c, *_ in structured)
    print(f"\nNext:  python3 {os.path.basename(__file__)} load "
          f"--csv {args.csv or '<file>'} {names} --db meta.db")


def cmd_load(args):
    rows, ref = rows_from_args(args)
    conn = connect(args.db)
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.mode == "replace":
        cur = conn.execute(
            "DELETE FROM metadata WHERE record_id IN "
            "(SELECT record_id FROM records WHERE source_ref = ?)", (ref,))
        conn.execute(
            "DELETE FROM parse_errors WHERE record_id IN "
            "(SELECT record_id FROM records WHERE source_ref = ?)", (ref,))
        conn.execute("DELETE FROM records WHERE source_ref = ?", (ref,))
        if cur.rowcount > 0:
            print(f"replaced {cur.rowcount} existing metadata rows for this source")

    n_rows = n_pairs = n_errors = 0
    fmt_seen = Counter()
    errors = []

    for i, row in enumerate(rows, start=1):
        fields = args.field or _auto_fields(row)
        natural = None
        if args.id_column:
            natural = _text(row.get(args.id_column))
        if natural is None:
            natural = hashlib.sha1(
                json.dumps(row, sort_keys=True, default=str).encode()
            ).hexdigest()[:16]

        cur = conn.execute(
            "INSERT INTO records (source_ref, natural_key, row_num, ingested_at, raw) "
            "VALUES (?,?,?,?,?)",
            (ref, natural, i, now,
             json.dumps(row, ensure_ascii=False, default=str) if args.keep_raw else None),
        )
        rid = cur.lastrowid
        n_rows += 1

        for field in fields:
            raw = row.get(field)
            try:
                flat, fmt = extract(raw, strip_xml_root=args.strip_xml_root)
            except Exception as exc:  # noqa: BLE001 - one bad row must not kill the load
                n_errors += 1
                if len(errors) < 10:
                    errors.append(f"row {i} field {field}: {exc}")
                # persist it: a message on stdout is unfindable a week later
                conn.execute(
                    "INSERT INTO parse_errors (record_id, field, fmt, error, raw) "
                    "VALUES (?,?,?,?,?)",
                    (rid, field, detect_format(raw)[0], str(exc), _text(raw)))
                continue
            fmt_seen[fmt] += 1
            if not flat:
                continue
            conn.executemany(
                "INSERT INTO metadata (record_id, field, key, value, value_num, fmt) "
                "VALUES (?,?,?,?,?,?)",
                [(rid, field, k, _text(v), _num(v), fmt) for k, v in flat.items()],
            )
            n_pairs += len(flat)

        if i % 5000 == 0:
            conn.commit()
            print(f"  ...{i} rows")

    conn.commit()
    print(f"loaded {n_rows} records / {n_pairs} metadata pairs into {args.db}")
    print("formats: " + (", ".join(f"{f}={n}" for f, n in fmt_seen.most_common()) or "none"))
    if n_errors:
        print(f"{n_errors} parse errors (first {len(errors)}):")
        for e in errors:
            print("  " + e)
    conn.close()


def _auto_fields(row):
    """Columns in this row that hold structured metadata.

    invalid_* is included on purpose so malformed rows surface as load errors
    instead of vanishing.
    """
    keep = ("json", "xml", "kv", "invalid_json", "invalid_xml")
    return [k for k, v in row.items() if detect_format(v)[0] in keep]


def cmd_keys(args):
    conn = connect(args.db)
    # The census groups by fmt as well as key, so the same key can legitimately
    # appear twice (e.g. sku arriving as json on some rows and xml on others).
    # Show fmt so that reads as information rather than a duplicate row.
    sql = ("SELECT field, key, fmt, occurrences, records, distinct_values, empties "
           "FROM v_key_census ORDER BY key, records DESC")
    rows = conn.execute(sql).fetchall()
    if not rows:
        sys.exit("No metadata loaded yet. Run `load` first.")
    print(f"{'field':<14} {'key':<38} {'fmt':<7} {'recs':>6} {'distinct':>9} {'empty':>6}")
    print("-" * 86)
    for field, key, fmt, occ, recs, distinct, empties in rows:
        print(f"{field:<14} {key:<38} {fmt:<7} {recs:>6} {distinct:>9} {empties:>6}")

    split = conn.execute(
        "SELECT key, COUNT(*) FROM v_key_census GROUP BY field, key HAVING COUNT(*) > 1"
    ).fetchall()
    if split:
        print("\nkeys arriving in more than one format (same column, mixed rows):")
        for key, n in split:
            print(f"  {key} ({n} formats)")
    conn.close()


def _safe_ident(name):
    return '"' + name.replace('"', '""') + '"'


def cmd_pivot(args):
    """Build a wide view from whatever keys actually exist."""
    conn = connect(args.db)
    params = [args.min_records]
    where = "HAVING COUNT(DISTINCT record_id) >= ?"
    field_clause = ""
    if args.field:
        field_clause = "WHERE field IN (%s)" % ",".join("?" * len(args.field))
        params = list(args.field) + params
    keys = [r[0] for r in conn.execute(
        f"SELECT key FROM metadata {field_clause} GROUP BY key {where} "
        f"ORDER BY COUNT(DISTINCT record_id) DESC LIMIT ?",
        params + [args.max_columns]).fetchall()]

    if not keys:
        sys.exit("No keys matched. Try lowering --min-records or check --field.")

    # Different keys can sanitize to the same column name (vendor.id from JSON and
    # vendor@id from XML both give vendor_id). Disambiguate rather than letting
    # SQLite emit two columns with the same name.
    used, named = {"record_id", "natural_key", "source_ref"}, []
    for k in keys:
        base = _colname(k)
        name, n = base, 2
        while name in used:
            name, n = f"{base}_{n}", n + 1
        used.add(name)
        named.append((k, name))

    cols = ",\n       ".join(
        f"MAX(CASE WHEN m.key = {_lit(k)} THEN m.value END) AS {_safe_ident(name)}"
        for k, name in named)
    filt = ""
    if args.field:
        filt = "WHERE m.field IN (%s)" % ",".join(_lit(f) for f in args.field)

    sql = (f"CREATE VIEW {_safe_ident(args.view)} AS\n"
           f"SELECT r.record_id,\n       r.natural_key,\n       r.source_ref,\n"
           f"       {cols}\n"
           f"FROM records r JOIN metadata m ON m.record_id = r.record_id\n"
           f"{filt}\n"
           f"GROUP BY r.record_id, r.natural_key, r.source_ref")

    conn.execute(f"DROP VIEW IF EXISTS {_safe_ident(args.view)}")
    conn.execute(sql)
    conn.commit()
    print(f"created view {args.view} with {len(named)} metadata columns:")
    for k, name in named:
        print(f"  {k}  ->  {name}")
    print(f"\nTry:  python3 {os.path.basename(__file__)} sql --db {args.db} "
          f"\"SELECT * FROM {args.view} LIMIT 5\"")
    if args.sql_out:
        with open(args.sql_out, "w", encoding="utf-8") as fh:
            fh.write(sql + ";\n")
        print(f"view DDL written to {args.sql_out}")
    conn.close()


def cmd_sql(args):
    """Run ad-hoc SQL. Handy because the sqlite3 CLI is not always installed."""
    conn = connect(args.db)
    conn.row_factory = sqlite3.Row
    statements = [args.query] if args.query else _split_sql(
        open(args.file, encoding="utf-8").read())

    for stmt in statements:
        try:
            rows = conn.execute(stmt).fetchall()
        except sqlite3.Error as exc:
            print(f"!! {exc}\n   {stmt.splitlines()[0][:70]}", file=sys.stderr)
            continue
        if len(statements) > 1:
            print(f"\n-- {stmt.splitlines()[0][:70]}")
        if not rows:
            print("(no rows)")
            continue
        cols = rows[0].keys()
        if args.csv_out:
            w = csv.writer(sys.stdout)
            w.writerow(cols)
            w.writerows([list(r) for r in rows])
        else:
            widths = [max(len(str(c)),
                          max((len(str(r[c])) for r in rows[:args.limit]), default=0))
                      for c in cols]
            widths = [min(w, 40) for w in widths]
            print(" | ".join(f"{c:<{w}}" for c, w in zip(cols, widths)))
            print("-+-".join("-" * w for w in widths))
            for r in rows[:args.limit]:
                print(" | ".join(
                    f"{('' if r[c] is None else str(r[c]))[:w]:<{w}}"
                    for c, w in zip(cols, widths)))
            if len(rows) > args.limit:
                print(f"... {len(rows) - args.limit} more rows")
    conn.commit()
    conn.close()


def _split_sql(text):
    body = "\n".join(l for l in text.splitlines() if not l.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def _lit(s):
    return "'" + str(s).replace("'", "''") + "'"


def _colname(key):
    """Dotted metadata path -> a column name that is pleasant to type in SQL."""
    name = re.sub(r"[^0-9a-zA-Z]+", "_", key).strip("_").lower()
    return name or "col"


# --------------------------------------------------------------------------

def main():
    p = argparse.ArgumentParser(
        description="Extract metadata from structured fields into SQLite.",
        formatter_class=argparse.RawDescriptionHelpFormatter, epilog=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_source(sp):
        g = sp.add_argument_group("source (pick one)")
        g.add_argument("--csv", help="path to a CSV file")
        g.add_argument("--api", help="URL to GET (needs requests)")
        g.add_argument("--api-header", action="append", metavar="'K: V'",
                       help="repeatable request header")
        g.add_argument("--api-param", action="append", metavar="k=v",
                       help="repeatable query param")
        g.add_argument("--json-path", help="dotted path to the record list, e.g. data.items")
        g.add_argument("--page-param", help="query param to increment for paging, e.g. page")
        g.add_argument("--pages", type=int, default=1, help="how many pages to pull")
        g.add_argument("--start-page", type=int, default=1)

    sp = sub.add_parser("profile", help="inspect columns and report detected formats + keys")
    add_source(sp)
    sp.add_argument("--field", action="append", help="limit to these columns (repeatable)")
    sp.add_argument("--sample", type=int, default=500, help="rows to sample (default 500)")
    sp.add_argument("--top-keys", type=int, default=40)
    sp.set_defaults(func=cmd_profile)

    sp = sub.add_parser("load", help="parse and load into SQLite")
    add_source(sp)
    sp.add_argument("--db", required=True, help="SQLite file (created if absent)")
    sp.add_argument("--field", action="append",
                    help="column(s) holding metadata; omit to auto-detect per row")
    sp.add_argument("--id-column", help="column to use as the natural key")
    sp.add_argument("--mode", choices=["replace", "append"], default="replace",
                    help="replace (default) wipes prior rows from the same source")
    sp.add_argument("--keep-raw", action="store_true",
                    help="also store the full original row as JSON")
    sp.add_argument("--strip-xml-root", action="store_true",
                    help="drop the XML root tag from keys (item.sku -> sku) so XML "
                         "rows align with JSON rows")
    sp.set_defaults(func=cmd_load)

    sp = sub.add_parser("keys", help="census of every metadata key found")
    sp.add_argument("--db", required=True)
    sp.set_defaults(func=cmd_keys)

    sp = sub.add_parser("pivot", help="build a wide SQL view from the observed keys")
    sp.add_argument("--db", required=True)
    sp.add_argument("--view", default="v_metadata_wide")
    sp.add_argument("--field", action="append", help="limit to these source columns")
    sp.add_argument("--min-records", type=int, default=1,
                    help="only include keys present in >= N records")
    sp.add_argument("--max-columns", type=int, default=200)
    sp.add_argument("--sql-out", help="also write the view DDL to a .sql file")
    sp.set_defaults(func=cmd_pivot)

    sp = sub.add_parser("sql", help="run ad-hoc SQL against the database")
    sp.add_argument("--db", required=True)
    sp.add_argument("query", nargs="?", help="SQL to run")
    sp.add_argument("--file", help="run every statement in a .sql file instead")
    sp.add_argument("--limit", type=int, default=50, help="rows to print (default 50)")
    sp.add_argument("--csv-out", action="store_true", help="emit CSV instead of a table")
    sp.set_defaults(func=cmd_sql)

    args = p.parse_args()
    if args.cmd in ("profile", "load") and not (args.csv or args.api):
        p.error("give --csv PATH or --api URL")
    if args.cmd == "sql" and not (args.query or args.file):
        p.error("give a SQL string or --file PATH")
    args.func(args)


if __name__ == "__main__":
    main()
