"""
functionality:
- document store on top of sqlite
- index, get, update, delete and search json documents
- replaces the search server, all data lives in a single file
"""

import json
import sqlite3
import time
from array import array
from typing import Any, Callable, Iterator

from common.src.index_query import (
    Aggregator,
    Compiled,
    QueryCompiler,
    tokenize,
)
from common.src.index_schema import (
    TOKENIZER,
    all_indexes,
    build_drop,
    build_schema,
    json_path,
    validate_index,
)
from common.src.sqlite_conn import INDEX_DB, get_conn, write_tx

ScriptType = Callable[[dict], Any]


def _normalize(value: Any) -> str | None:
    """sql function to normalize text for substring search"""
    if value is None:
        return None

    return " ".join(tokenize(value))


def _setup_conn(conn: sqlite3.Connection) -> None:
    """register functions on new connection"""
    conn.create_function("ta_norm", 1, _normalize, deterministic=True)
    # scratch table to stem search terms like the full text index does
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS temp.ta_stem "
        + f"USING fts5(term, tokenize='{TOKENIZER}')"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS temp.ta_stem_vocab "
        + "USING fts5vocab(temp, ta_stem, row)"
    )


def deep_merge(target: dict, update: dict) -> dict:
    """merge update into target recursively"""
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            deep_merge(target[key], value)
        else:
            target[key] = value

    return target


def filter_source(source: dict, source_filter: Any) -> dict:
    """apply _source filter with excludes"""
    if not isinstance(source_filter, dict):
        return source

    for path in source_filter.get("excludes", []):
        parent = source
        *parents, last = path.split(".")
        for key in parents:
            parent = parent.get(key, {}) if isinstance(parent, dict) else {}

        if isinstance(parent, dict):
            parent.pop(last, None)

    return source


class IndexStore:
    """interact with all indexes"""

    def __init__(self):
        self.conn = get_conn(INDEX_DB, setup=_setup_conn)

    # index management

    def index_exists(self, index: str) -> bool:
        """check if index is created"""
        row = self.conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (index,),
        ).fetchone()

        return bool(row)

    def create_index(self, index: str) -> bool:
        """create index if needed, returns true if newly created"""
        existed = self.index_exists(index)
        with write_tx(self.conn):
            for statement in build_schema(index):
                self.conn.execute(statement)

        return not existed

    def delete_index(self, index: str) -> None:
        """delete index with all documents"""
        with write_tx(self.conn):
            for statement in build_drop(index):
                self.conn.execute(statement)

    def reset(self) -> None:
        """delete all documents of all indexes"""
        for index in all_indexes():
            self.delete_index(index)
            self.create_index(index)

        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _table(self, index: str) -> str | None:
        """validate index name, none if index doesn't exist"""
        try:
            validate_index(index)
        except KeyError:
            return None

        return index if self.index_exists(index) else None

    # single document

    def get_doc(self, index: str, doc_id: str) -> dict | None:
        """get single document by id"""
        if not self._table(index):
            return None

        row = self.conn.execute(
            f"SELECT json(source) FROM {index} WHERE _id = ?", (doc_id,)
        ).fetchone()
        if not row:
            return None

        return json.loads(row[0])

    def index_doc(self, index: str, doc_id: str, source: dict) -> bool:
        """add or replace document, returns true if newly created"""
        validate_index(index)
        with write_tx(self.conn):
            existed = self.conn.execute(
                f"SELECT 1 FROM {index} WHERE _id = ?", (doc_id,)
            ).fetchone()
            self._write(index, doc_id, source)

        return not existed

    def _write(self, index: str, doc_id: str, source: dict) -> None:
        """write document"""
        self.conn.execute(
            f"INSERT INTO {index} (_id, source) VALUES (?, jsonb(?)) "
            + "ON CONFLICT (_id) DO UPDATE SET source = excluded.source",
            (doc_id, json.dumps(source)),
        )

    def update_doc(self, index: str, doc_id: str, body: dict) -> str:
        """
        partial update of document, body takes:
        - doc: dict to merge into the document
        - script: callable to modify the document, return False to skip
        - upsert: document to create if it doesn't exist
        returns: updated, created, noop or not_found
        """
        validate_index(index)
        with write_tx(self.conn):
            source = self.get_doc(index, doc_id)
            if source is None:
                upsert = body.get("upsert")
                if upsert is None and body.get("doc_as_upsert"):
                    upsert = body.get("doc")

                if upsert is None:
                    return "not_found"

                self._write(index, doc_id, upsert)
                return "created"

            if not self._apply(source, body):
                return "noop"

            self._write(index, doc_id, source)

        return "updated"

    @staticmethod
    def _apply(source: dict, body: dict) -> bool:
        """apply update to source in place, returns false if unchanged"""
        before = json.dumps(source, sort_keys=True)
        if doc := body.get("doc"):
            deep_merge(source, doc)

        script = body.get("script")
        if script is not None:
            if not callable(script):
                raise ValueError("update script needs to be a callable")

            if script(source) is False:
                return False

        return before != json.dumps(source, sort_keys=True)

    def delete_doc(self, index: str, doc_id: str) -> bool:
        """delete single document, returns true if deleted"""
        if not self._table(index):
            return False

        cursor = self.conn.execute(
            f"DELETE FROM {index} WHERE _id = ?", (doc_id,)
        )

        return bool(cursor.rowcount)

    # queries

    def count(self, index: str, body: dict | None = None) -> int:
        """count documents matching query"""
        if not self._table(index):
            return 0

        where = QueryCompiler(self.conn, index).compile((body or {}).get("query"))
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM {index} WHERE {where.sql}", where.params
        ).fetchone()

        return row[0]

    def update_by_query(self, index: str, body: dict) -> int:
        """update all documents matching the query, returns updated count"""
        if not self._table(index):
            return 0

        updated = 0
        with write_tx(self.conn):
            rowids = self._match_rowids(index, body, sort=False)
            for chunk in self._chunks(rowids, 200):
                for _, doc_id, source in self._load(index, chunk):
                    if self._apply(source, body):
                        self._write(index, doc_id, source)
                        updated += 1

        return updated

    def delete_by_query(self, index: str, body: dict) -> int:
        """delete all documents matching the query, returns deleted count"""
        if not self._table(index):
            return 0

        where = QueryCompiler(self.conn, index).compile(body.get("query"))
        cursor = self.conn.execute(
            f"DELETE FROM {index} WHERE {where.sql}", where.params
        )

        return cursor.rowcount

    def search(self, indexes: list[str], body: dict | None) -> dict:
        """search one or more indexes, returns search response"""
        started = time.monotonic()
        body = body or {}
        size = int(body.get("size", 10))
        offset = int(body.get("from", 0))
        total = 0
        hits: list[dict] = []
        response: dict = {}
        for index in indexes:
            if not self._table(index):
                continue

            if len(indexes) > 1:
                page = (size + offset, 0)
            else:
                page = (size, offset)

            index_total, index_hits, aggs = self._search_index(
                index, body, page
            )
            total += index_total
            hits.extend(index_hits)
            if aggs is not None:
                response["aggregations"] = aggs

        if len(indexes) > 1:
            hits.sort(key=lambda i: i["_score"] or 0, reverse=True)
            hits = hits[offset : offset + size]

        response.update(
            {
                "took": int((time.monotonic() - started) * 1000),
                "timed_out": False,
                "hits": {
                    "total": {"value": total, "relation": "eq"},
                    "hits": hits,
                },
            }
        )

        return response

    def _search_index(
        self, index: str, body: dict, page: tuple[int, int]
    ) -> tuple[int, list[dict], dict | None]:
        """search single index"""
        compiler = QueryCompiler(self.conn, index)
        where = compiler.compile(body.get("query"))
        total = self.conn.execute(
            f"SELECT COUNT(*) FROM {index} WHERE {where.sql}", where.params
        ).fetchone()[0]

        aggs = None
        if body.get("aggs") or body.get("aggregations"):
            agg_body = body.get("aggs") or body.get("aggregations")
            aggs = Aggregator(self.conn, index).run(agg_body, where)

        hits: list[dict] = []
        if page[0] > 0 and total:
            hits = self._get_hits(index, body, compiler, where, *page)

        return total, hits, aggs

    def _get_hits(
        self, index: str, body: dict, compiler: QueryCompiler, *args
    ) -> list[dict]:
        """get hits of single index"""
        where, limit, offset = args
        with_sql, join, join_params, score_expr = compiler.score_join()
        order = compiler.compile_sort(body.get("sort"), score_expr)
        rows = self.conn.execute(
            f"{with_sql} SELECT {index}.rowid, {score_expr} "
            + f"FROM {index} {join} "
            + f"WHERE {where.sql} ORDER BY {order.sql} LIMIT ? OFFSET ?",
            join_params + where.params + order.params + [limit, offset],
        ).fetchall()
        rowids = [i[0] for i in rows]
        scores = dict(rows)
        highlights = {}
        if body.get("highlight"):
            highlights = compiler.highlight(body["highlight"], rowids)

        has_score = not body.get("sort")
        hits = []
        loaded = self._load(index, rowids, body.get("_source", True))
        for rowid, doc_id, source in loaded:
            hit = {
                "_index": index,
                "_id": doc_id,
                "_score": scores[rowid] if has_score else None,
            }
            if source is not None:
                hit["_source"] = source

            if rowid in highlights:
                field = next(iter(body["highlight"]["fields"]))
                hit["highlight"] = {field: [highlights[rowid]]}

            hits.append(hit)

        return hits

    def scan(
        self, index: str, body: dict | None, size: int = 500
    ) -> Iterator[list[dict]]:
        """iterate over all hits of a query in pages"""
        if not self._table(index):
            return

        body = body or {}
        rowids = self._match_rowids(index, body, sort=True)
        source_filter = body.get("_source", True)
        for chunk in self._chunks(rowids, size):
            page = []
            for _, doc_id, source in self._load(index, chunk, source_filter):
                hit = {"_index": index, "_id": doc_id}
                if source is not None:
                    hit["_source"] = source

                page.append(hit)

            if page:
                yield page

    def _match_rowids(self, index: str, body: dict, sort: bool) -> array:
        """get rowids of all matching documents as compact array"""
        compiler = QueryCompiler(self.conn, index)
        where = compiler.compile(body.get("query"))
        with_sql, join, join_params = "", "", []
        order = Compiled(f"{index}.rowid ASC", [])
        if sort:
            with_sql, join, join_params, score_expr = compiler.score_join()
            order = compiler.compile_sort(body.get("sort"), score_expr)

        cursor = self.conn.execute(
            f"{with_sql} SELECT {index}.rowid FROM {index} {join} "
            + f"WHERE {where.sql} ORDER BY {order.sql}",
            join_params + where.params + order.params,
        )
        rowids = array("q")
        while rows := cursor.fetchmany(5000):
            rowids.extend(i[0] for i in rows)

        return rowids

    @staticmethod
    def _chunks(rowids: array | list, size: int) -> Iterator[list[int]]:
        """split rowids into chunks"""
        for start in range(0, len(rowids), size):
            yield list(rowids[start : start + size])

    def _load(
        self, index: str, rowids: list[int], source_filter: Any = True
    ) -> list[tuple[int, str, dict | None]]:
        """load documents by rowid, keeps order of rowids"""
        if not rowids:
            return []

        includes = None
        if isinstance(source_filter, list):
            includes = source_filter
        elif isinstance(source_filter, str):
            includes = [source_filter]
        elif isinstance(source_filter, dict) and source_filter.get("includes"):
            includes = source_filter["includes"]

        if source_filter is False:
            select = "NULL"
        elif includes:
            paths = ", ".join(f"'{json_path(i)}'" for i in includes)
            # always returns a list, even for a single path
            select = f"json_extract(source, {paths}, '$.\"__none__\"')"
        else:
            select = "json(source)"

        rows = self.conn.execute(
            f"SELECT rowid, _id, {select} FROM {index} "
            + "WHERE rowid IN (SELECT value FROM json_each(?))",
            (json.dumps(rowids),),
        ).fetchall()
        by_rowid = {}
        for rowid, doc_id, raw in rows:
            source = None
            if raw is not None and includes:
                source = self._build_includes(includes, json.loads(raw))
            elif raw is not None:
                source = filter_source(json.loads(raw), source_filter)

            by_rowid[rowid] = (rowid, doc_id, source)

        return [by_rowid[i] for i in rowids if i in by_rowid]

    @staticmethod
    def _build_includes(includes: list[str], values: list) -> dict:
        """build nested source dict from extracted values"""
        source: dict = {}
        for path, value in zip(includes, values):
            if value is None:
                continue

            parent = source
            *parents, last = path.split(".")
            for key in parents:
                parent = parent.setdefault(key, {})

            parent[last] = value

        return source

    # bulk

    def bulk(self, lines: Iterator[str]) -> dict:
        """process bulk actions from ndjson lines"""
        items = []
        errors = False
        with write_tx(self.conn):
            for action, meta, payload in self._parse_bulk(lines):
                try:
                    status = self._bulk_action(action, meta, payload)
                    item = {"_id": meta.get("_id"), "status": status}
                except (KeyError, ValueError, sqlite3.Error) as err:
                    errors = True
                    item = {
                        "_id": meta.get("_id"),
                        "status": 400,
                        "error": {"reason": str(err)},
                    }

                items.append({action: item})

        return {"errors": errors, "items": items}

    @staticmethod
    def _parse_bulk(lines: Iterator[str]) -> Iterator[tuple[str, dict, dict]]:
        """parse ndjson lines to action, meta and payload"""
        lines = (i for i in lines if i.strip())
        for line in lines:
            action, meta = next(iter(json.loads(line).items()))
            payload = {}
            if action != "delete":
                payload = json.loads(next(lines))

            yield action, meta, payload

    def _bulk_action(self, action: str, meta: dict, payload: dict) -> int:
        """run single bulk action, returns status code"""
        index, doc_id = meta["_index"], meta["_id"]
        if action in ("index", "create"):
            validate_index(index)
            self._write(index, doc_id, payload)
            return 201

        if action == "update":
            result = self.update_doc(index, doc_id, payload)
            if result == "not_found":
                raise KeyError(f"document missing: {doc_id}")

            return 200

        if action == "delete":
            return 200 if self.delete_doc(index, doc_id) else 404

        raise ValueError(f"unsupported bulk action: {action}")

    # maintenance

    def snapshot_to(self, file_path: str) -> None:
        """write consistent copy of the complete index to file_path"""
        self.conn.execute("VACUUM INTO ?", (file_path,))

    def optimize(self) -> None:
        """merge full text index segments and refresh statistics"""
        for index in all_indexes():
            if not self.index_exists(f"{index}_fts"):
                continue

            fts = f"{index}_fts"
            self.conn.execute(f"INSERT INTO {fts} ({fts}) VALUES ('optimize')")

        self.conn.execute("PRAGMA optimize")
        self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
