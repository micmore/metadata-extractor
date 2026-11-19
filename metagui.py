#!/usr/bin/env python3
"""Tkinter front-end for metaextract.py.

Runs the same code the CLI does -- this file only handles widgets and threading,
all parsing/loading logic lives in metaextract.py.

    python3 metagui.py [meta.db]
"""

from __future__ import annotations

import argparse
import csv
import io
import os
import queue
import sqlite3
import sys
import threading
import traceback
from contextlib import redirect_stdout, redirect_stderr

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import metaextract as mx

HERE = os.path.dirname(os.path.abspath(__file__))
PAD = {"padx": 6, "pady": 4}


class App(tk.Tk):
    def __init__(self, db_path=None):
        super().__init__()
        self.title("Metadata Extractor")
        self.geometry("1080x720")
        self.minsize(880, 560)

        self.q = queue.Queue()
        self.busy = False

        self.db_var = tk.StringVar(value=db_path or os.path.join(HERE, "meta.db"))
        self.csv_var = tk.StringVar()
        self.api_var = tk.StringVar()
        self.header_var = tk.StringVar()
        self.jsonpath_var = tk.StringVar()
        self.pageparam_var = tk.StringVar()
        self.pages_var = tk.IntVar(value=1)
        self.sample_var = tk.IntVar(value=500)
        self.idcol_var = tk.StringVar()
        self.mode_var = tk.StringVar(value="replace")
        self.strip_var = tk.BooleanVar(value=True)
        self.raw_var = tk.BooleanVar(value=True)
        self.minrec_var = tk.IntVar(value=1)
        self.view_var = tk.StringVar(value="v_metadata_wide")
        self.status_var = tk.StringVar(value="ready")

        self._build_topbar()
        self._build_tabs()
        self._build_status()
        self.after(100, self._drain)
        self.after(200, self._autoload)

    def _autoload(self):
        """If the DB already holds data, show it instead of five empty tabs."""
        path = self.db_var.get().strip()
        if not os.path.exists(path):
            return
        try:
            conn = sqlite3.connect(path)
            n = conn.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            srcs = [r[0] for r in conn.execute(
                "SELECT DISTINCT source_ref FROM records").fetchall()]
            views = [r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='view' "
                "AND name LIKE 'v_%wide%'").fetchall()]
            conn.close()
        except sqlite3.Error:
            return
        if not n:
            return

        # pre-fill the source box from what was loaded last, so Profile/Load work
        for s in srcs:
            if s.lower().endswith(".csv") and os.path.exists(s):
                self.csv_var.set(s)
                self.do_detect_columns(quiet=True)
                break

        if views:
            self.view_var.set(views[0])
            self.sql_text.delete("1.0", "end")
            self.sql_text.insert("1.0", f"SELECT * FROM {views[0]} LIMIT 50")
        self.do_keys()
        self.do_errors()
        self.do_query()
        self.nb.select(3)
        self.status_var.set(f"{os.path.basename(path)}: {n} records already loaded")

    # ---------------------------------------------------------------- layout
    def _build_topbar(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", **PAD)
        ttk.Label(bar, text="SQLite DB:").pack(side="left")
        ttk.Entry(bar, textvariable=self.db_var).pack(side="left", fill="x",
                                                      expand=True, padx=6)
        ttk.Button(bar, text="Browse…", command=self._pick_db).pack(side="left")
        ttk.Button(bar, text="Open folder", command=self._open_folder).pack(side="left",
                                                                           padx=(6, 0))

    def _build_tabs(self):
        nb = ttk.Notebook(self)
        nb.pack(fill="both", expand=True, **PAD)
        self.nb = nb
        self._tab_source(nb)
        self._tab_load(nb)
        self._tab_keys(nb)
        self._tab_query(nb)
        self._tab_errors(nb)

    def _build_status(self):
        bar = ttk.Frame(self)
        bar.pack(fill="x", side="bottom")
        self.progress = ttk.Progressbar(bar, mode="indeterminate", length=120)
        self.progress.pack(side="right", padx=6, pady=3)
        ttk.Label(bar, textvariable=self.status_var, anchor="w").pack(
            side="left", fill="x", expand=True, padx=8)

    # ------------------------------------------------------------ tab: source
    def _tab_source(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="1. Source & Profile")

        cf = ttk.LabelFrame(f, text="CSV file")
        cf.pack(fill="x", **PAD)
        row = ttk.Frame(cf)
        row.pack(fill="x", **PAD)
        ttk.Entry(row, textvariable=self.csv_var).pack(side="left", fill="x",
                                                       expand=True)
        ttk.Button(row, text="Browse…", command=self._pick_csv).pack(side="left",
                                                                     padx=6)

        af = ttk.LabelFrame(f, text="or API (leave CSV empty to use this)")
        af.pack(fill="x", **PAD)
        grid = ttk.Frame(af)
        grid.pack(fill="x", **PAD)
        fields = [("URL", self.api_var, 0), ("Header (K: V)", self.header_var, 1),
                  ("JSON path to records", self.jsonpath_var, 2),
                  ("Page param", self.pageparam_var, 3)]
        for label, var, r in fields:
            ttk.Label(grid, text=label + ":", width=20, anchor="e").grid(
                row=r, column=0, sticky="e", padx=4, pady=2)
            ttk.Entry(grid, textvariable=var).grid(row=r, column=1, sticky="ew",
                                                   padx=4, pady=2)
        ttk.Label(grid, text="Pages:", width=20, anchor="e").grid(
            row=4, column=0, sticky="e", padx=4, pady=2)
        ttk.Spinbox(grid, from_=1, to=10000, textvariable=self.pages_var,
                    width=8).grid(row=4, column=1, sticky="w", padx=4, pady=2)
        grid.columnconfigure(1, weight=1)

        act = ttk.Frame(f)
        act.pack(fill="x", **PAD)
        ttk.Label(act, text="Sample rows:").pack(side="left")
        ttk.Spinbox(act, from_=10, to=100000, textvariable=self.sample_var,
                    width=8).pack(side="left", padx=6)
        ttk.Button(act, text="Profile  ▶", command=self.do_profile).pack(side="left")
        ttk.Label(act, text="  (inspect formats and keys before loading)",
                  foreground="#666").pack(side="left")

        self.profile_out = _mono_text(f)

    # -------------------------------------------------------------- tab: load
    def _tab_load(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="2. Load")

        top = ttk.Frame(f)
        top.pack(fill="both", **PAD)

        lf = ttk.LabelFrame(top, text="Columns to extract (none selected = auto-detect)")
        lf.pack(side="left", fill="both", expand=True)
        self.fields_list = tk.Listbox(lf, selectmode="extended", height=9,
                                      exportselection=False)
        self.fields_list.pack(fill="both", expand=True, padx=6, pady=6)
        ttk.Button(lf, text="Detect columns", command=self.do_detect_columns).pack(
            pady=(0, 6))

        of = ttk.LabelFrame(top, text="Options")
        of.pack(side="left", fill="y", padx=(8, 0))
        ttk.Label(of, text="ID column:").pack(anchor="w", padx=6, pady=(6, 0))
        self.idcol_combo = ttk.Combobox(of, textvariable=self.idcol_var, width=22)
        self.idcol_combo.pack(anchor="w", padx=6)
        ttk.Checkbutton(of, text="Strip XML root tag", variable=self.strip_var).pack(
            anchor="w", padx=6, pady=(8, 0))
        ttk.Checkbutton(of, text="Keep raw row JSON", variable=self.raw_var).pack(
            anchor="w", padx=6)
        ttk.Label(of, text="On re-run:").pack(anchor="w", padx=6, pady=(8, 0))
        ttk.Radiobutton(of, text="replace this source", variable=self.mode_var,
                        value="replace").pack(anchor="w", padx=6)
        ttk.Radiobutton(of, text="append", variable=self.mode_var,
                        value="append").pack(anchor="w", padx=6)
        ttk.Button(of, text="Load  ▶", command=self.do_load).pack(padx=6, pady=10,
                                                                  fill="x")

        pf = ttk.LabelFrame(f, text="Pivot -> wide view")
        pf.pack(fill="x", **PAD)
        row = ttk.Frame(pf)
        row.pack(fill="x", **PAD)
        ttk.Label(row, text="View name:").pack(side="left")
        ttk.Entry(row, textvariable=self.view_var, width=24).pack(side="left", padx=6)
        ttk.Label(row, text="Min records per key:").pack(side="left", padx=(12, 0))
        ttk.Spinbox(row, from_=1, to=100000, textvariable=self.minrec_var,
                    width=6).pack(side="left", padx=6)
        ttk.Button(row, text="Build view", command=self.do_pivot).pack(side="left")

        self.load_out = _mono_text(f)

    # -------------------------------------------------------------- tab: keys
    def _tab_keys(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="3. Keys")
        ttk.Button(f, text="Refresh", command=self.do_keys).pack(anchor="w", **PAD)
        self.keys_tree = _tree(f, ("field", "key", "fmt", "records",
                                   "distinct_values", "empties"))

    # ------------------------------------------------------------- tab: query
    def _tab_query(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="4. Query")

        top = ttk.Frame(f)
        top.pack(fill="x", **PAD)
        ttk.Label(top, text="Saved query:").pack(side="left")
        self.canned = ttk.Combobox(top, width=62, state="readonly")
        self.canned.pack(side="left", padx=6)
        self.canned.bind("<<ComboboxSelected>>", self._use_canned)
        ttk.Button(top, text="Run  ▶  (Ctrl+Enter)", command=self.do_query).pack(
            side="left", padx=6)
        ttk.Button(top, text="Export CSV…", command=self.do_export).pack(side="left")

        self.sql_text = tk.Text(f, height=7, wrap="none", undo=True,
                                font=("monospace", 10))
        self.sql_text.pack(fill="x", **PAD)
        self.sql_text.insert("1.0", "SELECT * FROM v_metadata_wide LIMIT 50")
        self.sql_text.bind("<Control-Return>", lambda e: (self.do_query(), "break"))

        self.query_tree = _tree(f, ("result",))
        self._load_canned()

    # ------------------------------------------------------------ tab: errors
    def _tab_errors(self, nb):
        f = ttk.Frame(nb)
        nb.add(f, text="5. Parse errors")
        ttk.Button(f, text="Refresh", command=self.do_errors).pack(anchor="w", **PAD)
        ttk.Label(f, text="Rows whose metadata field could not be parsed. "
                          "The rest of each row still loaded.",
                  foreground="#666").pack(anchor="w", padx=6)
        self.err_tree = _tree(f, ("natural_key", "field", "fmt", "error", "raw"))

    # ------------------------------------------------------------- helpers
    def _pick_db(self):
        p = filedialog.asksaveasfilename(
            title="SQLite database", initialdir=HERE, initialfile="meta.db",
            defaultextension=".db", confirmoverwrite=False,
            filetypes=[("SQLite", "*.db *.sqlite *.sqlite3"), ("All", "*.*")])
        if p:
            self.db_var.set(p)

    def _pick_csv(self):
        p = filedialog.askopenfilename(
            title="CSV file", initialdir=HERE,
            filetypes=[("CSV", "*.csv *.tsv *.txt"), ("All", "*.*")])
        if p:
            self.csv_var.set(p)
            self.do_detect_columns()

    def _open_folder(self):
        import subprocess
        d = os.path.dirname(self.db_var.get()) or HERE
        try:
            subprocess.Popen(["xdg-open", d])
        except OSError as exc:
            messagebox.showerror("Cannot open", str(exc))

    def _args(self, **extra):
        """Build the same Namespace the CLI's argparse would produce."""
        headers = [h for h in [self.header_var.get().strip()] if h]
        ns = argparse.Namespace(
            csv=self.csv_var.get().strip() or None,
            api=self.api_var.get().strip() or None,
            api_header=headers, api_param=[],
            json_path=self.jsonpath_var.get().strip() or None,
            page_param=self.pageparam_var.get().strip() or None,
            pages=max(1, self.pages_var.get()), start_page=1,
            db=self.db_var.get().strip(),
        )
        for k, v in extra.items():
            setattr(ns, k, v)
        return ns

    def _selected_fields(self):
        return [self.fields_list.get(i) for i in self.fields_list.curselection()]

    def _run(self, label, fn, done=None):
        """Run fn in a worker thread; stream its stdout into the UI."""
        if self.busy:
            messagebox.showinfo("Busy", "Another job is still running.")
            return
        self.busy = True
        self.status_var.set(label + "…")
        self.progress.start(12)

        def worker():
            buf = io.StringIO()
            try:
                with redirect_stdout(buf), redirect_stderr(buf):
                    result = fn()
                self.q.put(("ok", label, buf.getvalue(), result, done))
            except Exception:
                self.q.put(("err", label, buf.getvalue() + "\n" + traceback.format_exc(),
                            None, None))

        threading.Thread(target=worker, daemon=True).start()

    def _drain(self):
        try:
            while True:
                kind, label, text, result, done = self.q.get_nowait()
                self.busy = False
                self.progress.stop()
                if kind == "err":
                    self.status_var.set(label + " failed")
                    _set_text(self._active_out(), text)
                    messagebox.showerror(label + " failed", text.strip()[-1500:])
                else:
                    self.status_var.set(label + " finished")
                    if text.strip():
                        _set_text(self._active_out(), text)
                    if done:
                        done(result)
        except queue.Empty:
            pass
        self.after(120, self._drain)

    def _active_out(self):
        return self.load_out if self.nb.index("current") == 1 else self.profile_out

    # -------------------------------------------------------------- actions
    def do_profile(self):
        if not (self.csv_var.get().strip() or self.api_var.get().strip()):
            messagebox.showwarning("No source", "Pick a CSV file or enter an API URL.")
            return
        args = self._args(field=None, sample=self.sample_var.get(), top_keys=40)
        self.nb.select(0)
        self._run("Profile", lambda: mx.cmd_profile(args),
                  done=lambda _: self.do_detect_columns(quiet=True))

    def do_detect_columns(self, quiet=False):
        """Fill the column list from the source header."""
        try:
            if self.csv_var.get().strip():
                rows = mx.csv_source(self.csv_var.get().strip())
            elif self.api_var.get().strip():
                args = self._args()
                rows = mx.api_source(args.api, _hdr(args.api_header), {},
                                     args.json_path, None, 1, 1)
            else:
                return
            first = next(iter(rows), None)
        except Exception as exc:  # noqa: BLE001
            if not quiet:
                messagebox.showerror("Cannot read source", str(exc))
            return
        if not first:
            return

        cols = list(first.keys())
        structured = set(mx._auto_fields(first))
        self.fields_list.delete(0, "end")
        for c in cols:
            self.fields_list.insert("end", c)
        for i, c in enumerate(cols):
            if c in structured:
                self.fields_list.selection_set(i)
                self.fields_list.itemconfig(i, foreground="#0a7")
        self.idcol_combo["values"] = [""] + cols
        if not self.idcol_var.get():
            for guess in ("id", "ID", "Id", "record_id", "uuid", "key"):
                if guess in cols:
                    self.idcol_var.set(guess)
                    break
        self.status_var.set(f"{len(cols)} columns, "
                            f"{len(structured)} look structured (preselected)")

    def do_load(self):
        fields = self._selected_fields() or None
        args = self._args(field=fields, id_column=self.idcol_var.get().strip() or None,
                          mode=self.mode_var.get(), keep_raw=self.raw_var.get(),
                          strip_xml_root=self.strip_var.get())
        self.nb.select(1)
        self._run("Load", lambda: mx.cmd_load(args), done=lambda _: self.do_keys())

    def do_pivot(self):
        args = self._args(view=self.view_var.get().strip() or "v_metadata_wide",
                          field=None, min_records=self.minrec_var.get(),
                          max_columns=200, sql_out=None)
        self.nb.select(1)
        self._run("Pivot", lambda: mx.cmd_pivot(args))

    def do_keys(self):
        rows, cols = self._fetch(
            "SELECT field, key, fmt, records, distinct_values, empties "
            "FROM v_key_census ORDER BY key, records DESC")
        if rows is not None:
            _fill(self.keys_tree, cols, rows)
            self.status_var.set(f"{len(rows)} keys")

    def do_errors(self):
        rows, cols = self._fetch(
            "SELECT r.natural_key, e.field, e.fmt, e.error, substr(e.raw,1,120) AS raw "
            "FROM parse_errors e JOIN records r ON r.record_id = e.record_id "
            "ORDER BY r.row_num")
        if rows is not None:
            _fill(self.err_tree, cols, rows)
            self.status_var.set(f"{len(rows)} parse errors"
                                if rows else "no parse errors")

    def do_query(self):
        sql = self.sql_text.get("1.0", "end").strip().rstrip(";")
        if not sql:
            return
        rows, cols = self._fetch(sql)
        if rows is not None:
            _fill(self.query_tree, cols, rows)
            self.status_var.set(f"{len(rows)} rows")

    def _fetch(self, sql):
        try:
            conn = mx.connect(self.db_var.get().strip())
            conn.row_factory = sqlite3.Row
            cur = conn.execute(sql)
            rows = cur.fetchall()
            cols = ([d[0] for d in cur.description] if cur.description
                    else ["(no columns)"])
            conn.commit()
            conn.close()
            return [tuple(r) for r in rows], cols
        except sqlite3.Error as exc:
            messagebox.showerror("SQL error", str(exc))
            return None, None
        except Exception as exc:  # noqa: BLE001
            messagebox.showerror("Error", str(exc))
            return None, None

    def do_export(self):
        cols = self.query_tree["columns"]
        items = self.query_tree.get_children()
        if not items:
            messagebox.showinfo("Nothing to export", "Run a query first.")
            return
        p = filedialog.asksaveasfilename(
            title="Export CSV", defaultextension=".csv", initialdir=HERE,
            initialfile="export.csv", filetypes=[("CSV", "*.csv")])
        if not p:
            return
        with open(p, "w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow(cols)
            for i in items:
                w.writerow(self.query_tree.item(i, "values"))
        self.status_var.set(f"exported {len(items)} rows -> {p}")

    def _load_canned(self):
        path = os.path.join(HERE, "queries.sql")
        self._canned_map = {}
        labels = []
        if os.path.exists(path):
            text = open(path, encoding="utf-8").read()
            title = None
            for block in text.split("\n\n"):
                for line in block.splitlines():
                    s = line.strip()
                    if s.startswith("--") and not s.startswith("-- ---") and len(s) > 4:
                        title = s.lstrip("- ").strip()
                        break
                stmt = "\n".join(l for l in block.splitlines()
                                 if not l.strip().startswith("--")).strip()
                if stmt and title:
                    label = title[:70]
                    n = 2
                    while label in self._canned_map:
                        label, n = f"{title[:66]} ({n})", n + 1
                    self._canned_map[label] = stmt.rstrip(";")
                    labels.append(label)
        self.canned["values"] = labels

    def _use_canned(self, _evt=None):
        stmt = self._canned_map.get(self.canned.get())
        if stmt:
            self.sql_text.delete("1.0", "end")
            self.sql_text.insert("1.0", stmt)


# ------------------------------------------------------------------ widgets

def _mono_text(parent):
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True, **PAD)
    txt = tk.Text(frame, wrap="none", font=("monospace", 9))
    ys = ttk.Scrollbar(frame, orient="vertical", command=txt.yview)
    xs = ttk.Scrollbar(frame, orient="horizontal", command=txt.xview)
    txt.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    txt.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    xs.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    return txt


def _tree(parent, columns):
    frame = ttk.Frame(parent)
    frame.pack(fill="both", expand=True, **PAD)
    tv = ttk.Treeview(frame, columns=columns, show="headings", selectmode="extended")
    ys = ttk.Scrollbar(frame, orient="vertical", command=tv.yview)
    xs = ttk.Scrollbar(frame, orient="horizontal", command=tv.xview)
    tv.configure(yscrollcommand=ys.set, xscrollcommand=xs.set)
    tv.grid(row=0, column=0, sticky="nsew")
    ys.grid(row=0, column=1, sticky="ns")
    xs.grid(row=1, column=0, sticky="ew")
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    for c in columns:
        tv.heading(c, text=c)
        tv.column(c, width=140, stretch=True)
    return tv


def _fill(tv, columns, rows):
    tv.delete(*tv.get_children())
    tv["columns"] = columns
    for c in columns:
        tv.heading(c, text=c)
        width = max(90, min(340, 9 * max([len(str(c))] +
                                         [len(str(r[columns.index(c)]))
                                          for r in rows[:60]] or [10])))
        tv.column(c, width=width, stretch=True)
    for r in rows:
        tv.insert("", "end", values=["" if v is None else v for v in r])


def _set_text(widget, text):
    widget.delete("1.0", "end")
    widget.insert("1.0", text)
    widget.see("end")


def _hdr(headers):
    out = {}
    for h in headers or []:
        if ":" in h:
            k, v = h.split(":", 1)
            out[k.strip()] = v.strip()
    return out


def main():
    db = sys.argv[1] if len(sys.argv) > 1 else None
    App(db).mainloop()


if __name__ == "__main__":
    main()
