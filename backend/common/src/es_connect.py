"""SQLite document store with the former Elasticsearch wrapper interface.

Keeping ``ElasticWrap`` as an API name makes the storage migration incremental
for callers, but this module performs no HTTP requests and imports no search
server client. Documents are JSON on disk; filtering deliberately trades CPU
and latency for a small, predictable memory footprint.
"""

import json
import re
import sqlite3
from contextlib import contextmanager
from copy import deepcopy

from common.src.env_settings import EnvironmentSettings


def _field(document, dotted):
    value = document
    for part in dotted.removesuffix(".keyword").split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
    return value


def _values(value):
    return value if isinstance(value, list) else [value]


def _matches(document, query):
    """Evaluate the subset of the Elasticsearch DSL used by TA views."""
    if not query or "match_all" in query:
        return True
    if "bool" in query:
        clause = query["bool"]
        must = clause.get("must", []) + clause.get("filter", [])
        must = must if isinstance(must, list) else [must]
        must_not = clause.get("must_not", [])
        must_not = must_not if isinstance(must_not, list) else [must_not]
        should = clause.get("should", [])
        should = should if isinstance(should, list) else [should]
        minimum = int(clause.get("minimum_should_match", 1 if should and not must else 0))
        return (
            all(_matches(document, item) for item in must)
            and not any(_matches(document, item) for item in must_not)
            and sum(_matches(document, item) for item in should) >= minimum
        )
    for operator in ("term", "match"):
        if operator in query:
            field, wanted = next(iter(query[operator].items()))
            if isinstance(wanted, dict):
                wanted = wanted.get("query")
            actual = _field(document, field)
            if operator == "term":
                return any(str(item) == str(wanted) for item in _values(actual))
            return str(wanted).casefold() in str(actual or "").casefold()
    if "terms" in query:
        field, wanted = next(iter(query["terms"].items()))
        return bool({str(v) for v in _values(_field(document, field))} & {str(v) for v in wanted})
    if "exists" in query:
        return _field(document, query["exists"]["field"]) is not None
    if "ids" in query:
        return str(document.get("_ta_id")) in map(str, query["ids"]["values"])
    if "range" in query:
        field, limits = next(iter(query["range"].items()))
        actual = _field(document, field)
        if actual is None:
            return False
        return all({"gt": actual > value, "gte": actual >= value,
                    "lt": actual < value, "lte": actual <= value}[op]
                   for op, value in limits.items() if op in {"gt", "gte", "lt", "lte"})
    if "wildcard" in query:
        field, wanted = next(iter(query["wildcard"].items()))
        wanted = wanted.get("value") if isinstance(wanted, dict) else wanted
        pattern = "^" + re.escape(wanted).replace(r"\*", ".*").replace(r"\?", ".") + "$"
        return any(re.match(pattern, str(value), re.I) for value in _values(_field(document, field)))
    if "multi_match" in query:
        spec = query["multi_match"]
        needle = str(spec.get("query", "")).casefold()
        return any(needle in str(_field(document, field.split("^")[0]) or "").casefold()
                   for field in spec.get("fields", []))
    if "nested" in query:
        nested = query["nested"]
        children = _field(document, nested["path"]) or []
        return any(_matches({nested["path"]: child}, nested["query"]) for child in children)
    if "query_string" in query or "simple_query_string" in query:
        spec = query.get("query_string", query.get("simple_query_string"))
        needle = str(spec.get("query", "")).replace("*", "").casefold()
        return needle in json.dumps(document, ensure_ascii=False).casefold()
    # Unknown clauses should not silently hide a user's library.
    return True


class ElasticWrap:
    """Compatibility facade backed entirely by an on-disk SQLite table."""

    def __init__(self, path):
        self.path = path.strip("/")

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(EnvironmentSettings.DATABASE_PATH, timeout=30)
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=NORMAL")
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute(
            """CREATE TABLE IF NOT EXISTS document_store (
                index_name TEXT NOT NULL, document_id TEXT NOT NULL,
                source TEXT NOT NULL, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY(index_name, document_id)
            ) WITHOUT ROWID"""
        )
        try:
            yield connection
            connection.commit()
        finally:
            connection.close()

    def _parts(self):
        clean = self.path.split("?", 1)[0]
        return clean.split("/") if clean else []

    def get(self, data=False, timeout=10, print_error=True):
        del timeout, print_error
        parts = self._parts()
        if parts == [""]:
            return {}, 200
        if parts and parts[-1] in {"_search", "_count"} or self.path == "_search":
            response = self._search(data or {})
            if parts and parts[-1] == "_count":
                return {"count": response["hits"]["total"]["value"]}, 200
            return response, 200
        if len(parts) >= 3 and parts[-2] == "_doc":
            with self._connection() as connection:
                row = connection.execute(
                    "SELECT source FROM document_store WHERE index_name=? AND document_id=?",
                    (parts[0], parts[-1]),
                ).fetchone()
            if not row:
                return {"found": False}, 404
            return {"_index": parts[0], "_id": parts[-1], "found": True,
                    "_source": json.loads(row[0])}, 200
        if len(parts) == 1:
            with self._connection() as connection:
                exists = connection.execute(
                    "SELECT 1 FROM document_store WHERE index_name=? LIMIT 1", (parts[0],)
                ).fetchone()
            return ({parts[0]: {"mappings": {}, "settings": {}}}, 200) if exists else ({}, 404)
        if parts and parts[-1] in {"_mapping", "_alias"}:
            return {parts[0]: {"mappings": {}, "aliases": {parts[0]: {}}}}, 200
        return {}, 200

    def put(self, data=False, refresh=False):
        del refresh
        parts = self._parts()
        if len(parts) >= 3 and parts[-2] == "_doc":
            self._upsert(parts[0], parts[-1], data or {})
            return {"result": "created", "_id": parts[-1]}, 201
        # Index creation/mappings/pipelines need no separate SQLite resource.
        return {"acknowledged": True}, 200

    def post(self, data=False, ndjson=False):
        parts = self._parts()
        if self.path == "_bulk":
            return self._bulk(data), 200
        if len(parts) >= 3 and parts[-2] == "_doc":
            self._upsert(parts[0], parts[-1], data or {})
            return {"result": "updated", "_id": parts[-1]}, 200
        if parts and parts[-1] in {"_search", "_count"}:
            return self.get(data=data)
        if len(parts) >= 3 and parts[-2] == "_update":
            current, status = ElasticWrap(f"{parts[0]}/_doc/{parts[-1]}").get()
            source = current.get("_source", {}) if status == 200 else {}
            source.update((data or {}).get("doc", {}))
            self._upsert(parts[0], parts[-1], source)
            return {"result": "updated"}, 200
        if parts and parts[-1] in {"_delete_by_query", "_update_by_query"}:
            return self._by_query(parts[0], data or {}, parts[-1]), 200
        if self.path in {"_aliases", "_pit"} or (parts and parts[-1] == "_reindex"):
            return {"acknowledged": True}, 200
        return {"acknowledged": True}, 200

    def delete(self, data=False, refresh=False):
        del data, refresh
        parts = self._parts()
        with self._connection() as connection:
            if len(parts) >= 3 and parts[-2] == "_doc":
                cursor = connection.execute(
                    "DELETE FROM document_store WHERE index_name=? AND document_id=?",
                    (parts[0], parts[-1]),
                )
            elif len(parts) == 1:
                cursor = connection.execute("DELETE FROM document_store WHERE index_name=?", (parts[0],))
            else:
                return {"acknowledged": True}, 200
        return {"result": "deleted" if cursor.rowcount else "not_found"}, 200 if cursor.rowcount else 404

    def _upsert(self, index, document_id, source):
        with self._connection() as connection:
            connection.execute(
                """INSERT INTO document_store(index_name, document_id, source)
                   VALUES(?, ?, ?) ON CONFLICT(index_name, document_id) DO UPDATE SET
                   source=excluded.source, updated_at=CURRENT_TIMESTAMP""",
                (index, str(document_id), json.dumps(source, separators=(",", ":"))),
            )

    def _search(self, request):
        parts = self._parts()
        indices = [] if not parts or parts[0] == "_search" else parts[0].split(",")
        size = max(0, int(request.get("size", 10)))
        start = max(0, int(request.get("from", 0)))
        query = request.get("query", {"match_all": {}})
        hits, total = [], 0
        sql = "SELECT index_name, document_id, source FROM document_store"
        params = []
        if indices:
            sql += " WHERE index_name IN (" + ",".join("?" for _ in indices) + ")"
            params.extend(indices)
        sql += " ORDER BY index_name, document_id"
        with self._connection() as connection:
            for index, document_id, raw in connection.execute(sql, params):
                source = json.loads(raw)
                matchable = dict(source, _ta_id=document_id)
                if not _matches(matchable, query):
                    continue
                if total >= start and len(hits) < size:
                    hits.append({"_index": index, "_id": document_id,
                                 "_source": source, "_score": 1.0,
                                 "sort": [document_id]})
                total += 1
        sort_specs = request.get("sort", [])
        for spec in reversed(sort_specs):
            if isinstance(spec, str):
                field, reverse = spec, False
            else:
                field, options = next(iter(spec.items()))
                reverse = options == "desc" or isinstance(options, dict) and options.get("order") == "desc"
            if field not in {"_doc", "_score"}:
                hits.sort(key=lambda hit: (_field(hit["_source"], field) is None,
                                           _field(hit["_source"], field)), reverse=reverse)
        response = {"took": 0, "hits": {"total": {"value": total, "relation": "eq"},
                                          "max_score": 1.0 if hits else None, "hits": hits}}
        if request.get("aggs") or request.get("aggregations"):
            response["aggregations"] = self._aggregate(
                indices, query, request.get("aggs", request.get("aggregations", {}))
            )
        return response

    def _aggregate(self, indices, query, specifications):
        """Calculate simple terms/value/count aggregations one row at a time."""
        documents = []
        sql = "SELECT source FROM document_store"
        params = []
        if indices:
            sql += " WHERE index_name IN (" + ",".join("?" for _ in indices) + ")"
            params = indices
        with self._connection() as connection:
            for (raw,) in connection.execute(sql, params):
                document = json.loads(raw)
                if _matches(document, query):
                    documents.append(document)
        output = {}
        for name, spec in specifications.items():
            if "terms" in spec:
                field = spec["terms"]["field"]
                counts = {}
                for document in documents:
                    for value in _values(_field(document, field)):
                        if value is not None:
                            counts[value] = counts.get(value, 0) + 1
                size = int(spec["terms"].get("size", 10))
                buckets = [{"key": key, "doc_count": count} for key, count in
                           sorted(counts.items(), key=lambda item: (-item[1], str(item[0])))[:size]]
                output[name] = {"doc_count_error_upper_bound": 0,
                                "sum_other_doc_count": 0, "buckets": buckets}
            elif "value_count" in spec:
                field = spec["value_count"]["field"]
                output[name] = {"value": sum(_field(doc, field) is not None for doc in documents)}
            elif "sum" in spec:
                field = spec["sum"]["field"]
                output[name] = {"value": sum(_field(doc, field) or 0 for doc in documents)}
            elif "filter" in spec:
                selected = [doc for doc in documents if _matches(doc, spec["filter"])]
                output[name] = {"doc_count": len(selected)}
        return output

    def _bulk(self, payload):
        lines = [json.loads(line) for line in str(payload or "").splitlines() if line.strip()]
        items, position = [], 0
        while position < len(lines):
            action, meta = next(iter(lines[position].items()))
            position += 1
            index, document_id = meta.get("_index"), str(meta.get("_id"))
            if action in {"index", "create"}:
                self._upsert(index, document_id, lines[position]); position += 1
            elif action == "update":
                body = lines[position]; position += 1
                current, status = ElasticWrap(f"{index}/_doc/{document_id}").get()
                source = current.get("_source", {}) if status == 200 else {}
                source.update(body.get("doc", body)); self._upsert(index, document_id, source)
            elif action == "delete":
                ElasticWrap(f"{index}/_doc/{document_id}").delete()
            items.append({action: {"_index": index, "_id": document_id, "status": 200}})
        return {"errors": False, "items": items}

    def _by_query(self, index, request, operation):
        response = ElasticWrap(f"{index}/_search").get(
            {"query": request.get("query", {"match_all": {}}), "size": 1000000}
        )[0]
        hits = response["hits"]["hits"]
        for hit in hits:
            if operation == "_delete_by_query":
                ElasticWrap(f"{index}/_doc/{hit['_id']}").delete()
            elif "script" in request:
                source = deepcopy(hit["_source"])
                # TA scripts primarily assign params to a field.
                for key, value in request["script"].get("params", {}).items():
                    source[key] = value
                self._upsert(index, hit["_id"], source)
        return {"total": len(hits), "updated": len(hits), "deleted": len(hits)}


class IndexPaginate:
    """Page through SQLite results without point-in-time server state."""

    DEFAULT_SIZE = 500

    def __init__(self, index_name, data=False, **kwargs):
        self.index_name, self.data, self.kwargs = index_name, data or {}, kwargs

    def get_results(self):
        page_size = self.kwargs.get("size", self.DEFAULT_SIZE)
        offset, results = 0, []
        while True:
            request = deepcopy(self.data)
            request.update({"size": page_size, "from": offset})
            hits = ElasticWrap(f"{self.index_name}/_search").get(request)[0]["hits"]["hits"]
            if not hits:
                break
            results.extend(hits if self.kwargs.get("keep_source") else [hit["_source"] for hit in hits])
            callback = self.kwargs.get("callback")
            if callback:
                callback(hits, self.index_name, counter=offset // page_size).run()
            offset += len(hits)
            if len(hits) < page_size:
                break
        return results
