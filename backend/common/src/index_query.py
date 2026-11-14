"""
functionality:
- translate the search query DSL used by the application to sqlite
- supports the subset of queries, sorts and aggregations in use
"""

import json
import re
import sqlite3
import textwrap
from datetime import datetime, timedelta, timezone
from typing import Any, NamedTuple
from zoneinfo import ZoneInfo

from common.src.index_schema import (
    FTS_FIELDS,
    NUMERIC_TYPES,
    FieldRef,
    clean_field,
    fts_column,
    fts_table,
    resolve_field,
)

MAX_EXPANSIONS = 50
MLT_STOPWORDS = {
    "and",
    "are",
    "but",
    "for",
    "from",
    "has",
    "have",
    "how",
    "not",
    "that",
    "the",
    "this",
    "was",
    "what",
    "with",
    "you",
    "your",
}
DATE_MATH = re.compile(r"^now(?:([+-]\d+)([smhdw]))?(?:/([dh]))?$")
UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}


class Scorer(NamedTuple):
    """full text query contributing to the score of a document"""

    match: str
    weights: tuple[float, ...]


class Compiled(NamedTuple):
    """compiled where clause"""

    sql: str
    params: list


def tokenize(text: Any) -> list[str]:
    """split text into lowercase search terms"""
    return re.findall(r"\w+", str(text).lower())


def quote_term(term: str) -> str:
    """quote term for fts query"""
    return '"' + term.replace('"', '""') + '"'


def parse_date(value: Any, round_up: bool = False, tz: str = "UTC") -> int:
    """parse date value to epoch seconds, supports basic date math"""
    if isinstance(value, bool):
        raise ValueError(f"invalid date value: {value}")

    if isinstance(value, (int, float)):
        return int(value)

    value = str(value).strip()
    if re.fullmatch(r"-?\d+", value):
        return int(value)

    zone = ZoneInfo(tz)
    if match := DATE_MATH.match(value):
        return _parse_date_math(match, round_up, zone)

    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=zone)

    if round_up and len(value) <= 10:
        parsed = parsed + timedelta(days=1) - timedelta(seconds=1)

    return int(parsed.timestamp())


def _parse_date_math(match: re.Match, round_up: bool, zone: ZoneInfo) -> int:
    """parse date math like now-7d/d"""
    amount, unit, rounding = match.groups()
    now = datetime.now(tz=zone)
    if amount:
        now = now + timedelta(seconds=int(amount) * UNIT_SECONDS[unit])

    if rounding == "d":
        now = now.replace(hour=0, minute=0, second=0, microsecond=0)
        if round_up:
            now = now + timedelta(days=1) - timedelta(seconds=1)
    elif rounding == "h":
        now = now.replace(minute=0, second=0, microsecond=0)
        if round_up:
            now = now + timedelta(hours=1) - timedelta(seconds=1)

    return int(now.timestamp())


def coerce_value(ftype: str, value: Any) -> Any:
    """coerce query value to the type of the field"""
    if isinstance(value, bool):
        return int(value)

    if ftype == "boolean" and isinstance(value, str):
        return int(value.lower() == "true")

    if ftype == "date":
        return parse_date(value)

    if ftype in NUMERIC_TYPES and isinstance(value, str):
        try:
            return float(value) if "." in value else int(value)
        except ValueError:
            return value

    return value


def edit_distance(first: str, second: str, limit: int) -> int:
    """optimal string alignment distance, returns limit + 1 if above limit"""
    if abs(len(first) - len(second)) > limit:
        return limit + 1

    previous2: list[int] = []
    previous = list(range(len(second) + 1))
    for i, char1 in enumerate(first, start=1):
        current = [i]
        for j, char2 in enumerate(second, start=1):
            cost = 0 if char1 == char2 else 1
            value = min(
                previous[j] + 1, current[j - 1] + 1, previous[j - 1] + cost
            )
            is_swap = (
                i > 1
                and j > 1
                and char1 == second[j - 2]
                and first[i - 2] == char2
            )
            if is_swap:
                value = min(value, previous2[j - 2] + 1)

            current.append(value)

        if min(current) > limit:
            return limit + 1

        previous2, previous = previous, current

    return previous[-1]


def fuzzy_limit(term: str, fuzziness: Any) -> int:
    """get max edit distance for term"""
    if fuzziness in (None, False, 0, "0"):
        return 0

    if str(fuzziness).lower() == "auto":
        if len(term) < 3:
            return 0

        return 1 if len(term) <= 5 else 2

    return min(int(fuzziness), 2)


class QueryCompiler:
    """compile query for a single index"""

    def __init__(self, conn: sqlite3.Connection, index: str):
        self.conn = conn
        self.index = index
        self.scorers: list[Scorer] = []

    def compile(self, query: dict | None) -> Compiled:
        """compile query dict to where clause"""
        if not query:
            return Compiled("1", [])

        if len(query) != 1:
            raise ValueError(f"expected single query key: {list(query)}")

        query_type, body = next(iter(query.items()))
        handler = getattr(self, f"_q_{query_type}", None)
        if not handler:
            raise ValueError(f"unsupported query type: {query_type}")

        return handler(body)

    # query types

    def _q_match_all(self, body: dict) -> Compiled:
        """match all documents"""
        # pylint: disable=unused-argument
        return Compiled("1", [])

    def _q_match_none(self, body: dict) -> Compiled:
        """match no documents"""
        # pylint: disable=unused-argument
        return Compiled("0", [])

    def _q_bool(self, body: dict) -> Compiled:
        """boolean combination"""
        parts: list[str] = []
        params: list = []
        musts = self._compile_list(body.get("must"))
        musts += self._compile_list(body.get("filter"))
        for clause in musts:
            parts.append(f"({clause.sql})")
            params.extend(clause.params)

        shoulds = self._compile_list(body.get("should"))
        if shoulds and not musts:
            parts.append(
                "(" + " OR ".join(f"({i.sql})" for i in shoulds) + ")"
            )
            for clause in shoulds:
                params.extend(clause.params)

        for clause in self._compile_list(body.get("must_not")):
            parts.append(f"NOT COALESCE(({clause.sql}), 0)")
            params.extend(clause.params)

        if not parts:
            return Compiled("1", [])

        return Compiled(" AND ".join(parts), params)

    def _compile_list(self, clauses: Any) -> list[Compiled]:
        """compile list of queries, skip empty"""
        if not clauses:
            return []

        if isinstance(clauses, dict):
            clauses = [clauses]

        return [self.compile(i) for i in clauses if i]

    @staticmethod
    def _unpack(body: dict, value_key: str) -> tuple[str, Any, dict]:
        """unpack field, value and options from query body"""
        field, spec = next(iter(body.items()))
        if isinstance(spec, dict):
            return field, spec.get(value_key), spec

        return field, spec, {}

    def _equals(self, ref: FieldRef, value: Any) -> Compiled:
        """build equal comparison"""
        if value is None:
            return Compiled(ref.predicate("{x} IS NULL"), [])

        value = coerce_value(ref.ftype, value)
        return Compiled(ref.predicate("{x} = ?"), [value])

    def _q_term(self, body: dict) -> Compiled:
        """exact match"""
        field, value, _ = self._unpack(body, "value")
        return self._equals(resolve_field(self.index, field), value)

    def _q_terms(self, body: dict) -> Compiled:
        """match any of the values"""
        field, values = next((k, v) for k, v in body.items() if k != "boost")
        ref = resolve_field(self.index, field)
        coerced = [coerce_value(ref.ftype, i) for i in values]
        sql = ref.predicate("{x} IN (SELECT value FROM json_each(?))")

        return Compiled(sql, [json.dumps(coerced)])

    def _q_ids(self, body: dict) -> Compiled:
        """match document ids"""
        return self._q_terms({"_id": body.get("values", [])})

    def _q_exists(self, body: dict) -> Compiled:
        """field has a value"""
        ref = resolve_field(self.index, body["field"])
        expr = f"json_extract(source, '{_path(ref)}')"
        sql = f"({expr} IS NOT NULL AND {expr} NOT IN ('[]', '{{}}'))"

        return Compiled(sql, [])

    def _q_prefix(self, body: dict) -> Compiled:
        """value starts with"""
        field, value, _ = self._unpack(body, "value")
        ref = resolve_field(self.index, field)
        sql = ref.predicate("substr({x}, 1, ?) = ?")

        return Compiled(sql, [len(str(value)), str(value)])

    def _q_range(self, body: dict) -> Compiled:
        """range comparison"""
        field, spec = next(iter(body.items()))
        ref = resolve_field(self.index, field)
        operators = {"gte": ">=", "gt": ">", "lte": "<=", "lt": "<"}
        parts, params = [], []
        for key, operator in operators.items():
            if key not in spec:
                continue

            value = spec[key]
            if ref.ftype == "date":
                value = parse_date(
                    value,
                    round_up=key in ("lte", "gt"),
                    tz=spec.get("time_zone", "UTC"),
                )
            else:
                value = coerce_value(ref.ftype, value)

            parts.append(f"{{x}} {operator} ?")
            params.append(value)

        if not parts:
            return Compiled("1", [])

        return Compiled(ref.predicate(" AND ".join(parts)), params)

    def _q_match(self, body: dict) -> Compiled:
        """match, full text on analyzed fields else exact"""
        field, value, spec = self._unpack(body, "query")
        ref = resolve_field(self.index, field)
        is_text = ref.ftype == "text" and field == ref.path
        if not is_text or isinstance(value, bool):
            return self._equals(ref, value)

        if ref.path in FTS_FIELDS.get(self.index, ()):
            return self._full_text(
                query=value,
                fields=[field],
                fuzziness=spec.get("fuzziness"),
                operator=spec.get("operator", "or"),
                prefix=False,
            )

        return self._contains(ref, str(value), word_start=False)

    def _q_match_phrase_prefix(self, body: dict) -> Compiled:
        """phrase where last word is a prefix"""
        field, value, _ = self._unpack(body, "query")
        ref = resolve_field(self.index, field)

        return self._contains(ref, str(value), word_start=True)

    @staticmethod
    def _contains(ref: FieldRef, value: str, word_start: bool) -> Compiled:
        """case insensitive substring match"""
        phrase = " ".join(tokenize(value))
        if not phrase:
            return Compiled("0", [])

        if word_start:
            sql = ref.predicate("instr(' ' || ta_norm({x}), ?) > 0")
            return Compiled(sql, [f" {phrase}"])

        return Compiled(ref.predicate("instr(ta_norm({x}), ?) > 0"), [phrase])

    def _q_multi_match(self, body: dict) -> Compiled:
        """full text over multiple fields"""
        return self._full_text(
            query=body.get("query"),
            fields=body.get("fields", []),
            fuzziness=body.get("fuzziness"),
            operator=body.get("operator", "or"),
            prefix=body.get("type") == "bool_prefix",
        )

    def _q_more_like_this(self, body: dict) -> Compiled:
        """find documents similar to an existing document"""
        doc_id = body.get("like", {}).get("_id")
        row = self.conn.execute(
            f"SELECT rowid, json(source) FROM {self.index} WHERE _id = ?",
            (doc_id,),
        ).fetchone()
        columns = self._fts_columns(body.get("fields", []))
        if not row or not columns:
            return Compiled("0", [])

        source = json.loads(row[1])
        terms: list[str] = []
        for field in body.get("fields", []):
            for term in tokenize(json.dumps(source.get(clean_field(field)))):
                is_useful = len(term) > 2 and term not in MLT_STOPWORDS
                if is_useful and term not in terms:
                    terms.append(term)

        terms = terms[: int(body.get("max_query_terms", 25))]
        if not terms:
            return Compiled("0", [])

        expression = " OR ".join(quote_term(i) for i in terms)
        clause = self._add_fts(columns, expression)

        sql = f"({clause.sql}) AND {self.index}.rowid != ?"

        return Compiled(sql, clause.params + [row[0]])

    # full text

    def _fts_columns(self, fields: list[str]) -> dict[str, float]:
        """get fts columns available in this index with boost"""
        available = [fts_column(i) for i in FTS_FIELDS.get(self.index, ())]
        columns: dict[str, float] = {}
        for field in fields:
            column = fts_column(field)
            if column not in available:
                continue

            boost = float(field.split("^")[1]) if "^" in field else 1.0
            columns[column] = max(boost, columns.get(column, 0))

        return columns

    def _full_text(
        self, query: Any, fields: list[str], fuzziness: Any, **kwargs
    ) -> Compiled:
        """build full text query"""
        terms = tokenize(query)
        columns = self._fts_columns(fields)
        if not terms or not columns:
            return Compiled("0", [])

        parts = []
        for idx, term in enumerate(terms):
            if kwargs.get("prefix") and idx == len(terms) - 1:
                parts.append(quote_term(term) + "*")
                continue

            options = [term] + self._expand(term, fuzziness)
            quoted = " OR ".join(quote_term(i) for i in options)
            parts.append(f"({quoted})" if len(options) > 1 else quoted)

        joiner = " AND " if kwargs.get("operator") == "and" else " OR "

        return self._add_fts(columns, joiner.join(parts))

    def _add_fts(self, columns: dict[str, float], expression: str) -> Compiled:
        """register fts query as scorer and build clause"""
        fts = fts_table(self.index)
        match = " OR ".join(f"{i} : ({expression})" for i in columns)
        all_columns = [fts_column(i) for i in FTS_FIELDS[self.index]]
        weights = tuple(columns.get(i, 1.0) for i in all_columns)
        self.scorers.append(Scorer(match=match, weights=weights))
        sql = (
            f"{self.index}.rowid IN "
            + f"(SELECT rowid FROM {fts} WHERE {fts} MATCH ?)"
        )

        return Compiled(sql, [match])

    def _stem(self, term: str) -> str:
        """stem term the same way the full text index does"""
        self.conn.execute(
            "INSERT INTO temp.ta_stem (term) VALUES (?)", (term,)
        )
        row = self.conn.execute(
            "SELECT term FROM temp.ta_stem_vocab LIMIT 1"
        ).fetchone()
        self.conn.execute("DELETE FROM temp.ta_stem")

        return row[0] if row else term

    def _expand(self, term: str, fuzziness: Any) -> list[str]:
        """find similar terms in the index vocabulary"""
        limit = fuzzy_limit(term, fuzziness)
        if not limit:
            return []

        similar: dict[str, int] = {}
        for variant in {term, self._stem(term)}:
            # one of the pieces needs to be unchanged in a similar term
            size = max(len(variant) // (limit + 1), 1)
            pieces = textwrap.wrap(variant, size)[: limit + 1]
            piece_sql = " OR ".join("instr(term, ?) > 0" for _ in pieces)
            rows = self.conn.execute(
                f"SELECT term, doc FROM {fts_table(self.index)}_vocab "
                + f"WHERE length(term) BETWEEN ? AND ? AND ({piece_sql})",
                [len(variant) - limit, len(variant) + limit] + pieces,
            )
            for candidate, doc in rows:
                if edit_distance(variant, candidate, limit) <= limit:
                    similar[candidate] = doc

        similar.pop(term, None)
        ranked = sorted(similar, key=lambda i: similar[i], reverse=True)

        return ranked[:MAX_EXPANSIONS]

    # scoring and sorting

    def score_join(self) -> tuple[str, str, list, str]:
        """build joins to score documents
        the matches get materialized once, to not run per document
        returns: with clause, join sql, params, score expression
        """
        fts = fts_table(self.index)
        ctes, joins, params, scores = [], [], [], []
        for idx, scorer in enumerate(self.scorers):
            weights = ", ".join(str(i) for i in scorer.weights)
            ctes.append(
                f"s{idx} AS MATERIALIZED (SELECT rowid AS rid, "
                + f"bm25({fts}, {weights}) AS rank "
                + f"FROM {fts} WHERE {fts} MATCH ?)"
            )
            joins.append(
                f"LEFT JOIN s{idx} ON s{idx}.rid = {self.index}.rowid"
            )
            params.append(scorer.match)
            scores.append(f"COALESCE(-s{idx}.rank, 0)")

        with_sql = "WITH " + ", ".join(ctes) if ctes else ""
        score_expr = " + ".join(scores) if scores else "1.0"

        return with_sql, " ".join(joins), params, score_expr

    def highlight(self, highlight: dict, rowids: list[int]) -> dict[int, str]:
        """get highlighted field for rowids, from the matching scorer"""
        field, spec = next(iter(highlight["fields"].items()))
        column = fts_column(field)
        all_columns = [fts_column(i) for i in FTS_FIELDS.get(self.index, ())]
        scorers = [i for i in self.scorers if f"{column} : " in i.match]
        if not scorers or not rowids or column not in all_columns:
            return {}

        fts = fts_table(self.index)
        pre = (spec.get("pre_tags") or ["<em>"])[0]
        post = (spec.get("post_tags") or ["</em>"])[0]
        rows = self.conn.execute(
            f"SELECT rowid, highlight({fts}, ?, ?, ?) FROM {fts} "
            + f"WHERE {fts} MATCH ? "
            + "AND rowid IN (SELECT value FROM json_each(?))",
            [
                all_columns.index(column),
                pre,
                post,
                scorers[-1].match,
                json.dumps(rowids),
            ],
        )

        return dict(rows.fetchall())

    def compile_sort(self, sort: Any, score_expr: str) -> Compiled:
        """build order by clause"""
        rowid = f"{self.index}.rowid"
        if not sort:
            if self.scorers:
                return Compiled(f"{score_expr} DESC, {rowid} ASC", [])

            return Compiled(f"{rowid} ASC", [])

        if not isinstance(sort, list):
            sort = [sort]

        parts, params = [], []
        for item in sort:
            clause = self._sort_item(item, score_expr)
            parts.append(clause.sql)
            params.extend(clause.params)

        parts.append(f"{rowid} ASC")

        return Compiled(", ".join(parts), params)

    def _sort_item(self, item: Any, score_expr: str) -> Compiled:
        """build single sort item"""
        if isinstance(item, str):
            item = {item: {"order": "desc" if item == "_score" else "asc"}}

        field, spec = next(iter(item.items()))
        if isinstance(spec, str):
            spec = {"order": spec}

        descending = spec.get("order", "asc").lower() == "desc"
        direction = "DESC" if descending else "ASC NULLS LAST"

        if field == "_script":
            scores = spec["script"].get("params", {}).get("scores", {})
            sql = (
                "COALESCE(json_extract(?, '$.\"' || _id || '\"'), 100000) "
                + direction
            )
            return Compiled(sql, [json.dumps(scores)])

        if field == "_doc":
            order = "DESC" if descending else "ASC"
            return Compiled(f"{self.index}.rowid {order}", [])

        if field == "_score":
            return Compiled(f"{score_expr} {direction}", [])

        ref = resolve_field(self.index, field)

        return Compiled(f"{ref.sort_expr(descending)} {direction}", [])


def _path(ref: FieldRef) -> str:
    """json path of field ref"""
    return "$." + ".".join(f'"{i}"' for i in ref.path.split("."))


class Aggregator:
    """run aggregations on a single index"""

    METRICS = {
        "sum": "COALESCE(SUM({x}), 0.0)",
        "avg": "AVG({x})",
        "min": "MIN({x})",
        "max": "MAX({x})",
        "value_count": "COUNT({x})",
    }

    def __init__(self, conn: sqlite3.Connection, index: str):
        self.conn = conn
        self.index = index

    def run(self, aggs: dict, where: Compiled) -> dict:
        """run all aggregations of one level"""
        result = {}
        metrics = {k: v for k, v in aggs.items() if self._is_metric(v)}
        if metrics:
            exprs = ", ".join(self._metric_expr(i) for i in metrics.values())
            row = self.conn.execute(
                f"SELECT {exprs} FROM {self.index} WHERE {where.sql}",
                where.params,
            ).fetchone()
            for name, value in zip(metrics, row):
                result[name] = {"value": value}

        for name, agg in aggs.items():
            if name in metrics:
                continue

            agg_type = next(i for i in agg if i not in ("aggs", "meta"))
            handler = getattr(self, f"_agg_{agg_type}", None)
            if not handler:
                raise ValueError(f"unsupported aggregation: {agg_type}")

            result[name] = handler(agg[agg_type], agg.get("aggs", {}), where)

        return result

    def _is_metric(self, agg: dict) -> bool:
        """check if agg is a metric aggregation"""
        return any(i in self.METRICS for i in agg)

    def _metric_expr(self, agg: dict) -> str:
        """build sql expression for metric"""
        metric_type = next(i for i in agg if i in self.METRICS)
        field = agg[metric_type]["field"]
        if field == "_index":
            return "COUNT(*)"

        ref = resolve_field(self.index, field)

        return self.METRICS[metric_type].format(x=ref.value_expr())

    def _agg_filter(self, body: dict, sub_aggs: dict, where: Compiled) -> dict:
        """filter aggregation"""
        clause = QueryCompiler(self.conn, self.index).compile(body)
        combined = Compiled(
            f"({where.sql}) AND ({clause.sql})", where.params + clause.params
        )
        row = self.conn.execute(
            f"SELECT COUNT(*) FROM {self.index} WHERE {combined.sql}",
            combined.params,
        ).fetchone()
        result = {"doc_count": row[0]}
        result.update(self.run(sub_aggs, combined))

        return result

    def _agg_terms(self, body: dict, sub_aggs: dict, where: Compiled) -> dict:
        """terms aggregation"""
        return self._buckets([body], body, sub_aggs, where, multi=False)

    def _agg_multi_terms(
        self, body: dict, sub_aggs: dict, where: Compiled
    ) -> dict:
        """multi terms aggregation"""
        return self._buckets(body["terms"], body, sub_aggs, where, multi=True)

    def _buckets(
        self, terms: list, body: dict, sub_aggs: dict, where: Compiled, **kw
    ) -> dict:
        """build buckets grouped by terms"""
        refs = [resolve_field(self.index, i["field"]) for i in terms]
        keys = [i.value_expr() for i in refs]
        metrics = {k: v for k, v in sub_aggs.items() if self._is_metric(v)}
        select = [f"{expr} AS k{idx}" for idx, expr in enumerate(keys)]
        select.append("COUNT(*) AS doc_count__")
        for idx, agg in enumerate(metrics.values()):
            select.append(f"{self._metric_expr(agg)} AS m{idx}")

        not_null = " AND ".join(f"{i} IS NOT NULL" for i in keys)
        group = ", ".join(f"k{idx}" for idx in range(len(keys)))
        order = self._bucket_order(body.get("order"), list(metrics))
        rows = self.conn.execute(
            f"SELECT {', '.join(select)} FROM {self.index} "
            + f"WHERE ({where.sql}) AND {not_null} "
            + f"GROUP BY {group} ORDER BY {order}, {group} LIMIT ?",
            where.params + [int(body.get("size", 10))],
        ).fetchall()

        buckets = []
        for row in rows:
            key_values = row[: len(keys)]
            bucket = self._bucket_keys(refs, key_values, multi=kw["multi"])
            bucket["doc_count"] = row[len(keys)]
            first_metric = len(keys) + 1
            for name, value in zip(metrics, row[first_metric:]):
                bucket[name] = {"value": value}

            other = {k: v for k, v in sub_aggs.items() if k not in metrics}
            if other:
                condition = " AND ".join(f"{i} = ?" for i in keys)
                combined = Compiled(
                    f"({where.sql}) AND {condition}",
                    where.params + list(key_values),
                )
                bucket.update(self.run(other, combined))

            buckets.append(bucket)

        total = self.conn.execute(
            f"SELECT COUNT(*) FROM {self.index} "
            + f"WHERE ({where.sql}) AND {not_null}",
            where.params,
        ).fetchone()[0]
        listed = sum(i[len(keys)] for i in rows)

        return {
            "doc_count_error_upper_bound": 0,
            "sum_other_doc_count": total - listed,
            "buckets": buckets,
        }

    @staticmethod
    def _bucket_order(order: dict | None, metrics: list[str]) -> str:
        """build order by of buckets"""
        if not order:
            return "doc_count__ DESC"

        key, direction = next(iter(order.items()))
        direction = "DESC" if direction.lower() == "desc" else "ASC"
        if key == "_count":
            return f"doc_count__ {direction}"

        if key == "_key":
            return f"k0 {direction}"

        if key in metrics:
            return f"m{metrics.index(key)} {direction}"

        raise ValueError(f"unsupported bucket order: {key}")

    @staticmethod
    def _bucket_keys(refs: list[FieldRef], values: tuple, multi: bool) -> dict:
        """build key and key_as_string of bucket"""
        as_strings = []
        for ref, value in zip(refs, values):
            if ref.ftype == "boolean":
                as_strings.append("true" if value else "false")
            else:
                as_strings.append(str(value))

        if multi:
            return {"key": list(values), "key_as_string": "|".join(as_strings)}

        bucket = {"key": values[0]}
        if refs[0].ftype == "boolean":
            bucket["key_as_string"] = as_strings[0]

        return bucket

    def _agg_date_histogram(
        self, body: dict, sub_aggs: dict, where: Compiled
    ) -> dict:
        """date histogram with daily buckets"""
        interval = body.get("calendar_interval") or body.get("fixed_interval")
        if interval not in ("day", "1d"):
            raise ValueError(f"unsupported histogram interval: {interval}")

        ref = resolve_field(self.index, body["field"])
        zone = ZoneInfo(body.get("time_zone", "UTC"))
        metrics = {k: v for k, v in sub_aggs.items() if self._is_metric(v)}
        fields = [self._metric_field(i).value_expr() for i in metrics.values()]
        select = ", ".join([ref.value_expr()] + fields)
        rows = self.conn.execute(
            f"SELECT {select} FROM {self.index} "
            + f"WHERE ({where.sql}) AND {ref.value_expr()} IS NOT NULL",
            where.params,
        )
        days: dict = {}
        for row in rows:
            day = datetime.fromtimestamp(row[0], tz=zone).date()
            bucket = days.setdefault(day, {"doc_count": 0, "values": []})
            bucket["doc_count"] += 1
            bucket["values"].append(row[1:])

        buckets = []
        for day in self._fill_days(days):
            bucket = days.get(day, {"doc_count": 0, "values": []})
            start = datetime(day.year, day.month, day.day, tzinfo=zone)
            item = {
                "key_as_string": day.isoformat(),
                "key": int(start.astimezone(timezone.utc).timestamp() * 1000),
                "doc_count": bucket["doc_count"],
            }
            for idx, (name, agg) in enumerate(metrics.items()):
                values = [i[idx] for i in bucket["values"]]
                item[name] = {"value": self._py_metric(agg, values)}

            buckets.append(item)

        order = body.get("order", {"_key": "asc"})
        if order.get("_key", "asc") == "desc":
            buckets.reverse()

        return {"buckets": buckets}

    @staticmethod
    def _fill_days(days: dict) -> list:
        """list all days between first and last day"""
        if not days:
            return []

        first, last = min(days), max(days)
        total = (last - first).days + 1

        return [first + timedelta(days=i) for i in range(total)]

    def _metric_field(self, agg: dict) -> FieldRef:
        """get field ref of metric agg"""
        metric_type = next(i for i in agg if i in self.METRICS)
        field = agg[metric_type]["field"]
        if field == "_index":
            field = "_id"

        return resolve_field(self.index, field)

    def _py_metric(self, agg: dict, values: list) -> Any:
        """calculate metric in python"""
        metric_type = next(i for i in agg if i in self.METRICS)
        values = [i for i in values if i is not None]
        if metric_type == "value_count":
            return len(values)

        if metric_type == "sum":
            return float(sum(values))

        if not values:
            return None

        if metric_type == "avg":
            return sum(values) / len(values)

        return min(values) if metric_type == "min" else max(values)
