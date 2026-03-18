"""db_utils.py - Database table operations, CRUD, schema management, and exports."""
from __future__ import annotations
import json
import re
import io
import csv
import zipfile
import io
from typing import Any, Dict, Optional, List
from urllib.parse import urlparse, parse_qs

import logging
from logger import Logger
logger = Logger(name="db_utils.py", level=logging.DEBUG)

class DBUtils:
    """Utilities for database management operations."""

    def __init__(self, shared_data):
        self.logger = logger
        self.shared_data = shared_data

    def _db_safe_ident(self, name: str) -> str:
        """Validate and sanitize SQL identifiers."""
        if not isinstance(name, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError("Invalid identifier")
        return name

    def _db_table_info(self, table: str):
        """Get table info (primary key and columns)."""
        table = self._db_safe_ident(table)
        rows = self.shared_data.db.query(f"PRAGMA table_info({table});")
        if not rows:
            raise ValueError("Table not found")
        cols = [r["name"] for r in rows]
        pk = next((r["name"] for r in rows if int(r["pk"] or 0) == 1), None)
        if not pk:
            pk = "id" if "id" in cols else cols[0]
        return pk, cols

    def _db_list_tables(self):
        """List all tables with row counts and primary keys."""
        rows = self.shared_data.db.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"
        )
        out = []
        for r in rows:
            name = r["name"]
            try:
                pk, _ = self._db_table_info(name)
            except Exception:
                pk = None
            cnt = self.shared_data.db.query_one(f"SELECT COUNT(*) c FROM {self._db_safe_ident(name)};")["c"]
            out.append({"name": name, "count": cnt, "pk": pk})
        return out

    def _db_list_views(self):
        """List all views with row counts."""
        rows = self.shared_data.db.query(
            "SELECT name, sql FROM sqlite_master WHERE type='view' ORDER BY name;"
        )
        out = []
        for r in rows:
            name = r["name"]
            try:
                cnt = self.shared_data.db.query_one(f"SELECT COUNT(*) c FROM {self._db_safe_ident(name)};")["c"]
            except Exception:
                cnt = None
            out.append({"name": name, "count": cnt})
        return out

    def _db_build_where(self, table: str, cols: list[str], q: str):
        """Build WHERE clause from query string."""
        if not q:
            return "", []
        
        parts = [p.strip() for p in q.split(",") if p.strip()]
        where_clauses = []
        params = []
        text_cols = set()
        
        # Determine text columns
        colinfo = {r["name"]: (r["type"] or "").upper()
                   for r in self.shared_data.db.query(f"PRAGMA table_info({self._db_safe_ident(table)});")}
        for c, t in colinfo.items():
            if "CHAR" in t or "TEXT" in t or t == "":
                text_cols.add(c)

        relop = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)\s*(=|>=|<=|>|<|:)\s*(.+)$")
        for p in parts:
            m = relop.match(p)
            if m:
                col, op, val = m.groups()
                if col not in cols:
                    continue
                if op == ":":
                    where_clauses.append(f"{self._db_safe_ident(col)} LIKE ?")
                    params.append(f"%{val}%")
                elif op in ("=", ">=", "<=", ">", "<"):
                    where_clauses.append(f"{self._db_safe_ident(col)} {op} ?")
                    params.append(val)
            else:
                # Free text search
                ors = []
                for c in text_cols:
                    ors.append(f"{self._db_safe_ident(c)} LIKE ?")
                    params.append(f"%{p}%")
                if ors:
                    where_clauses.append("(" + " OR ".join(ors) + ")")
        
        if not where_clauses:
            return "", []
        return "WHERE " + " AND ".join(where_clauses), params

    def db_catalog_endpoint(self, handler):
        """Get database catalog (tables and views)."""
        try:
            data = {"tables": self._db_list_tables(), "views": self._db_list_views()}
            self._write_json(handler, data)
        except Exception as e:
            logger.error(f"Error fetching database catalog: {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 500)

    def db_schema_endpoint(self, handler, name: str):
        """Get schema for a table or view."""
        try:
            name = self._db_safe_ident(name)
            row = self.shared_data.db.query_one(
                "SELECT type, name, sql FROM sqlite_master WHERE (type='table' OR type='view') AND name=?;", (name,)
            )
            cols = self.shared_data.db.query(f"PRAGMA table_info({name});")
            self._write_json(handler, {"meta": row, "columns": cols})
        except ValueError:
            self._write_json(handler, {"status": "error", "message": "Invalid table or view name"}, 400)
        except Exception as e:
            logger.error(f"Error fetching schema for '{name}': {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 500)

    def db_get_table_endpoint(self, handler, table_name: str):
        """Get table data with pagination and filtering."""
        try:
            qd = parse_qs(urlparse(handler.path).query)
            limit = int(qd.get("limit", ["50"])[0])
            offset = int(qd.get("offset", ["0"])[0])
            sort = (qd.get("sort", [""])[0] or "").strip()
            q = (qd.get("q", [""])[0] or "").strip()

            pk, cols = self._db_table_info(table_name)

            # WHERE
            where_sql, params = self._db_build_where(table_name, cols, q)

            # ORDER BY
            order_sql = ""
            if sort:
                if ":" in sort:
                    col, direction = sort.split(":", 1)
                    col = col.strip()
                    direction = direction.strip().lower()
                else:
                    col, direction = sort, "asc"
                if col in cols and direction in ("asc", "desc"):
                    order_sql = f"ORDER BY {self._db_safe_ident(col)} {direction.upper()}"

            # Total
            total = self.shared_data.db.query_one(
                f"SELECT COUNT(*) c FROM {self._db_safe_ident(table_name)} {where_sql};", tuple(params)
            )["c"]

            # Rows
            rows = self.shared_data.db.query(
                f"SELECT * FROM {self._db_safe_ident(table_name)} {where_sql} {order_sql} LIMIT ? OFFSET ?;",
                tuple(params) + (int(limit), int(offset))
            )

            self._write_json(handler, {
                "columns": cols,
                "rows": rows,
                "pk": pk,
                "total": total
            })
        except ValueError:
            self._write_json(handler, {"status": "error", "message": "Invalid table or query parameters"}, 400)
        except Exception as e:
            logger.error(f"Error fetching table data: {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 500)

    def db_update_cells_endpoint(self, handler, payload: dict):
        """Update table cells."""
        try:
            table = payload["table"]
            pk = payload.get("pk") or self._db_table_info(table)[0]
            _, cols = self._db_table_info(table)

            with self.shared_data.db.transaction():
                for row in payload.get("rows", []):
                    pk_val = row["pk"]
                    changes = row.get("changes", {}) or {}
                    sets = []
                    params = []
                    for c, v in changes.items():
                        if c not in cols or c == pk:
                            continue
                        sets.append(f"{self._db_safe_ident(c)} = ?")
                        params.append(v)
                    if not sets:
                        continue
                    params.append(pk_val)
                    self.shared_data.db.execute(
                        f"UPDATE {self._db_safe_ident(table)} SET {', '.join(sets)} WHERE {self._db_safe_ident(pk)} = ?;",
                        tuple(params)
                    )

            self._write_json(handler, {"status": "ok"})
        except Exception as e:
            logger.error(f"Error updating cells: {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 400)

    def db_delete_rows_endpoint(self, handler, payload: dict):
        """Delete table rows."""
        try:
            table = payload["table"]
            pk = payload.get("pk") or self._db_table_info(table)[0]
            pks = payload.get("pks", []) or []
            if not pks:
                raise ValueError("No primary keys provided")
            qmarks = ",".join("?" for _ in pks)
            self.shared_data.db.execute(
                f"DELETE FROM {self._db_safe_ident(table)} WHERE {self._db_safe_ident(pk)} IN ({qmarks});",
                tuple(pks)
            )
            self._write_json(handler, {"status": "ok", "deleted": len(pks)})
        except ValueError as e:
            self._write_json(handler, {"status": "error", "message": str(e)}, 400)
        except Exception as e:
            logger.error(f"Error deleting rows: {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 400)

    def db_insert_row_endpoint(self, handler, payload: dict):
        """Insert a new row."""
        try:
            table = payload["table"]
            pk, cols = self._db_table_info(table)
            values = payload.get("values", {}) or {}

            insert_cols = []
            insert_vals = []
            qmarks = []
            for c in cols:
                if c == pk:
                    continue
                if c in values:
                    insert_cols.append(self._db_safe_ident(c))
                    insert_vals.append(values[c])
                    qmarks.append("?")
            
            if not insert_cols:
                self.shared_data.db.execute(f"INSERT INTO {self._db_safe_ident(table)} DEFAULT VALUES;")
            else:
                self.shared_data.db.execute(
                    f"INSERT INTO {self._db_safe_ident(table)} ({', '.join(insert_cols)}) VALUES ({', '.join(qmarks)});",
                    tuple(insert_vals)
                )

            row = self.shared_data.db.query_one("SELECT last_insert_rowid() AS lid;")
            new_pk = row["lid"]
            self._write_json(handler, {"status": "ok", "pk": new_pk})
        except Exception as e:
            logger.error(f"Error inserting row: {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 400)

    def db_export_table_endpoint(self, handler, table_name: str):
        """Export table as CSV or JSON."""
        try:
            fmt = (parse_qs(urlparse(handler.path).query).get("format", ["csv"])[0] or "csv").lower()
            pk, cols = self._db_table_info(table_name)
            rows = self.shared_data.db.query(f"SELECT * FROM {self._db_safe_ident(table_name)};")
            
            if fmt == "json":
                payload = json.dumps(rows, ensure_ascii=False, indent=2)
                handler.send_response(200)
                handler.send_header("Content-Type", "application/json; charset=utf-8")
                handler.end_headers()
                handler.wfile.write(payload.encode("utf-8"))
                return

            # CSV
            buf = io.StringIO()
            w = csv.DictWriter(buf, fieldnames=cols, extrasaction='ignore')
            w.writeheader()
            for r in rows:
                w.writerow({c: r.get(c) for c in cols})
            handler.send_response(200)
            handler.send_header("Content-Type", "text/csv; charset=utf-8")
            handler.end_headers()
            handler.wfile.write(buf.getvalue().encode("utf-8"))
        except Exception as e:
            logger.error(f"Error exporting table: {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 400)

    def db_vacuum_endpoint(self, handler):
        """Vacuum and optimize database."""
        try:
            self.shared_data.db.vacuum()
            self.shared_data.db.optimize()
            self._write_json(handler, {"status": "ok"})
        except Exception as e:
            logger.error(f"Error during database vacuum: {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 500)

    def db_drop_table_endpoint(self, handler, table_name: str):
        """Drop a table."""
        try:
            table = self._db_safe_ident(table_name)
            self.shared_data.db.execute(f"DROP TABLE IF EXISTS {table};")
            self._write_json(handler, {"status": "ok"})
        except Exception as e:
            logger.error(f"Error dropping table: {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 400)

    def db_truncate_table_endpoint(self, handler, table_name: str):
        """Truncate a table."""
        try:
            table = self._db_safe_ident(table_name)
            self.shared_data.db.execute(f"DELETE FROM {table};")
            try:
                self.shared_data.db.execute("DELETE FROM sqlite_sequence WHERE name=?;", (table,))
            except Exception:
                pass
            self._write_json(handler, {"status": "ok"})
        except Exception as e:
            logger.error(f"Error truncating table: {e}")
            self._write_json(handler, {"status": "error", "message": "Internal server error"}, 400)


    def db_create_table_endpoint(self, handler, payload: dict):
        """
        payload = {
          "name":"my_table",
          "if_not_exists": true,
          "columns":[{"name":"id","type":"INTEGER","pk":true,"not_null":true,"default":"AUTOINCREMENT"}, ...]
        }
        """
        try:
            name = self._db_safe_ident(payload["name"])
            cols = payload.get("columns") or []
            if not cols:
                raise ValueError("columns required")
            parts = []
            pk_inline = None
            for c in cols:
                cname = self._db_safe_ident(c["name"])
                ctype = (c.get("type") or "").strip()
                seg = f"{cname} {ctype}".strip()
                if c.get("not_null"): seg += " NOT NULL"
                if "default" in c and c["default"] is not None:
                    seg += " DEFAULT " + str(c["default"])
                if c.get("pk"):
                    pk_inline = cname
                    # AUTOINCREMENT only valid on INTEGER PRIMARY KEY in SQLite
                    if ctype.upper().startswith("INTEGER"):
                        seg += " PRIMARY KEY AUTOINCREMENT"
                    else:
                        seg += " PRIMARY KEY"
                parts.append(seg)
            if pk_inline is None:
                # no explicit PK, implicit rowid or none
                pass
            ine = "IF NOT EXISTS " if payload.get("if_not_exists") else ""
            sql = f"CREATE TABLE {ine}{name} ({', '.join(parts)});"
            self.shared_data.db.execute(sql)
            handler.send_response(200); handler.send_header("Content-Type","application/json"); handler.end_headers()
            handler.wfile.write(json.dumps({"status":"ok"}).encode("utf-8"))
        except Exception as e:
            logger.error(f"Error creating table: {e}")
            handler.send_response(400); handler.send_header("Content-Type","application/json"); handler.end_headers()
            handler.wfile.write(json.dumps({"status":"error","message":"Internal server error"}).encode("utf-8"))

    def db_rename_table_endpoint(self, handler, payload: dict):
        try:
            old = self._db_safe_ident(payload["from"])
            new = self._db_safe_ident(payload["to"])
            self.shared_data.db.execute(f"ALTER TABLE {old} RENAME TO {new};")
            handler.send_response(200); handler.send_header("Content-Type","application/json"); handler.end_headers()
            handler.wfile.write(json.dumps({"status":"ok"}).encode("utf-8"))
        except Exception as e:
            logger.error(f"Error renaming table: {e}")
            handler.send_response(400); handler.send_header("Content-Type","application/json"); handler.end_headers()
            handler.wfile.write(json.dumps({"status":"error","message":"Internal server error"}).encode("utf-8"))

    def db_add_column_endpoint(self, handler, payload: dict):
        """
        payload = {table, column: {name,type,not_null?,default?}}
        """
        try:
            table = self._db_safe_ident(payload["table"])
            c = payload["column"]
            cname = self._db_safe_ident(c["name"])
            ctype = (c.get("type") or "").strip()
            seg = f"{cname} {ctype}".strip()
            if c.get("not_null"): seg += " NOT NULL"
            if "default" in c and c["default"] is not None:
                seg += " DEFAULT " + str(c["default"])
            self.shared_data.db.execute(f"ALTER TABLE {table} ADD COLUMN {seg};")
            handler.send_response(200); handler.send_header("Content-Type","application/json"); handler.end_headers()
            handler.wfile.write(json.dumps({"status":"ok"}).encode("utf-8"))
        except Exception as e:
            logger.error(f"Error adding column: {e}")
            handler.send_response(400); handler.send_header("Content-Type","application/json"); handler.end_headers()
            handler.wfile.write(json.dumps({"status":"error","message":"Internal server error"}).encode("utf-8"))


    # --- drop/truncate (view/table) ---
    def db_drop_view_endpoint(self, handler, view_name: str):
        try:
            view = self._db_safe_ident(view_name)
            self.shared_data.db.execute(f"DROP VIEW IF EXISTS {view};")
            handler.send_response(200); handler.send_header("Content-Type","application/json"); handler.end_headers()
            handler.wfile.write(json.dumps({"status":"ok"}).encode("utf-8"))
        except Exception as e:
            logger.error(f"Error dropping view: {e}")
            handler.send_response(400); handler.send_header("Content-Type","application/json"); handler.end_headers()
            handler.wfile.write(json.dumps({"status":"error","message":"Internal server error"}).encode("utf-8"))

    # --- export all (zip CSV/JSON) ---
    def db_export_all_endpoint(self, handler):
        try:
            fmt = (parse_qs(urlparse(handler.path).query).get("format", ["csv"])[0] or "csv").lower()
            mem = io.BytesIO()
            with zipfile.ZipFile(mem, mode="w", compression=zipfile.ZIP_DEFLATED) as z:
                # tables
                for t in self._db_list_tables():
                    name = t["name"]
                    rows = self.shared_data.db.query(f"SELECT * FROM {self._db_safe_ident(name)};")
                    if fmt == "json":
                        z.writestr(f"tables/{name}.json", json.dumps(rows, ensure_ascii=False, indent=2))
                    else:
                        cols = [c["name"] for c in self.shared_data.db.query(f"PRAGMA table_info({self._db_safe_ident(name)});")]
                        sio = io.StringIO()
                        w = csv.DictWriter(sio, fieldnames=cols, extrasaction='ignore')
                        w.writeheader()
                        for r in rows: w.writerow({c: r.get(c) for c in cols})
                        z.writestr(f"tables/{name}.csv", sio.getvalue())
                # views (read-only)
                for v in self._db_list_views():
                    name = v["name"]
                    try:
                        rows = self.shared_data.db.query(f"SELECT * FROM {self._db_safe_ident(name)};")
                    except Exception:
                        rows = []
                    if fmt == "json":
                        z.writestr(f"views/{name}.json", json.dumps(rows, ensure_ascii=False, indent=2))
                    else:
                        if rows:
                            cols = list(rows[0].keys())
                        else:
                            cols = []
                        sio = io.StringIO()
                        w = csv.DictWriter(sio, fieldnames=cols, extrasaction='ignore')
                        if cols: w.writeheader()
                        for r in rows: w.writerow({c: r.get(c) for c in cols})
                        z.writestr(f"views/{name}.csv", sio.getvalue())
            payload = mem.getvalue()
            handler.send_response(200)
            handler.send_header("Content-Type","application/zip")
            handler.send_header("Content-Disposition","attachment; filename=database_export.zip")
            handler.end_headers()
            handler.wfile.write(payload)
        except Exception as e:
            logger.error(f"Error exporting database: {e}")
            handler.send_response(500); handler.send_header("Content-Type","application/json"); handler.end_headers()
            handler.wfile.write(json.dumps({"status":"error","message":"Internal server error"}).encode("utf-8"))

    def db_list_tables_endpoint(self, handler):
        try:
            data = self._db_list_tables()
            handler.send_response(200)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps(data).encode("utf-8"))
        except Exception as e:
            logger.error(f"/api/db/tables error: {e}")
            handler.send_response(500)
            handler.send_header("Content-Type", "application/json")
            handler.end_headers()
            handler.wfile.write(json.dumps({"status":"error","message":"Internal server error"}).encode("utf-8"))



    def _write_json(self, handler, obj: dict, code: int = 200):
        """Write JSON response."""
        handler.send_response(code)
        handler.send_header('Content-Type', 'application/json')
        handler.end_headers()
        handler.wfile.write(json.dumps(obj).encode('utf-8'))
