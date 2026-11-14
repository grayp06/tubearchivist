"""
functionality:
- describe the document indexes stored in sqlite
- field types are read from the index mapping
- build table, index and full text search definitions

Every index is one table holding the document as JSONB in source.
"""

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import NamedTuple

MAPPING_FILE = (
    Path(__file__).resolve().parents[2] / "appsettings" / "index_mapping.json"
)

NUMERIC_TYPES = {"long", "integer", "short", "byte", "float", "double"}
FIELD_SUFFIXES = re.compile(
    r"(\.keyword|\.search_as_you_type|\._2gram|\._3gram|\._index_prefix)$"
)

# fields holding lists, queries need to look into each item
ARRAY_FIELDS: dict[str, tuple[str, ...]] = {
    "ta_video": (
        "playlist",
        "tags",
        "category",
        "streams",
        "subtitles",
        "sponsorblock.segments",
        "channel.channel_tags",
        "channel.channel_tabs",
    ),
    "ta_channel": ("channel_tags", "channel_tabs"),
    "ta_playlist": ("playlist_entries",),
    "ta_comment": ("comment_comments",),
}

# fields available for full text search
FTS_FIELDS: dict[str, tuple[str, ...]] = {
    "ta_video": ("title", "tags", "category", "channel.channel_name"),
    "ta_channel": ("channel_name", "channel_description", "channel_tags"),
    "ta_playlist": ("playlist_name", "playlist_description"),
    "ta_subtitle": ("subtitle_line", "subtitle_channel"),
}

# expression indexes for the fields filtered and sorted on the most
INDEXED_FIELDS: dict[str, tuple[str, ...]] = {
    "ta_video": (
        "channel.channel_id",
        "published",
        "date_downloaded",
        "vid_type",
    ),
    "ta_download": ("status", "channel_id", "timestamp"),
    "ta_playlist": ("playlist_channel_id",),
    "ta_subtitle": ("youtube_id", "subtitle_channel_id"),
    "ta_comment": ("comment_channel_id",),
}

TOKENIZER = "porter unicode61 remove_diacritics 2"


class FieldRef(NamedTuple):
    """resolved field of an index"""

    path: str
    ftype: str
    array: str | None
    sub: str | None

    def value_expr(self) -> str:
        """sql expression of the value, inside of json_each for arrays"""
        if self.path == "_id":
            return "_id"

        if self.array:
            expr = "value"
            if self.sub:
                expr = f"json_extract(value, '{json_path(self.sub)}')"
        else:
            expr = f"json_extract(source, '{json_path(self.path)}')"

        if self.ftype == "date":
            expr = date_expr(expr)

        return expr

    def predicate(self, template: str) -> str:
        """build predicate from template with {x} as value placeholder"""
        condition = template.format(x=self.value_expr())
        if not self.array:
            return condition

        each = f"json_each(source, '{json_path(self.array)}')"
        return f"EXISTS (SELECT 1 FROM {each} WHERE {condition})"

    def sort_expr(self, descending: bool) -> str:
        """sql expression to sort on, lists sort by their min or max"""
        if not self.array:
            return self.value_expr()

        func = "MAX" if descending else "MIN"
        each = f"json_each(source, '{json_path(self.array)}')"
        return f"(SELECT {func}({self.value_expr()}) FROM {each})"


def json_path(path: str) -> str:
    """build sqlite json path from dotted path"""
    if not re.fullmatch(r"[\w.\-]+", path):
        raise ValueError(f"invalid field name: {path}")

    return "$." + ".".join(f'"{i}"' for i in path.split("."))


def date_expr(expr: str) -> str:
    """normalize a date value to epoch seconds, dates can be iso strings"""
    return (
        f"(CASE WHEN typeof({expr}) = 'text' THEN "
        + f"CASE WHEN {expr} GLOB '*[^0-9]*' "
        + f"THEN CAST(strftime('%s', {expr}) AS INTEGER) "
        + f"ELSE CAST({expr} AS INTEGER) END "
        + f"ELSE {expr} END)"
    )


def clean_field(field: str) -> str:
    """remove boost and sub field suffix from field name"""
    field = field.split("^")[0]
    return FIELD_SUFFIXES.sub("", field)


def _flatten(properties: dict, prefix: str = "") -> dict[str, str]:
    """flatten mapping properties to path type pairs"""
    flat = {}
    for key, value in properties.items():
        path = f"{prefix}{key}"
        flat[path] = value.get("type", "object")
        if "properties" in value:
            flat.update(_flatten(value["properties"], prefix=f"{path}."))

    return flat


@lru_cache(maxsize=1)
def get_field_types() -> dict[str, dict[str, str]]:
    """get all field types by index name"""
    with open(MAPPING_FILE, "r", encoding="utf-8") as f:
        index_config = json.load(f)["index_config"]

    return {
        f"ta_{i['index_name']}": _flatten(i.get("expected_map") or {})
        for i in index_config
    }


def all_indexes() -> list[str]:
    """list all index names"""
    return list(get_field_types())


def validate_index(index: str) -> str:
    """make sure index name is known, to use as table name"""
    if index not in get_field_types():
        raise KeyError(f"unknown index: {index}")

    return index


def resolve_field(index: str, field: str) -> FieldRef:
    """resolve field name of index"""
    path = clean_field(field)
    if path == "_id":
        return FieldRef(path="_id", ftype="keyword", array=None, sub=None)

    ftype = get_field_types()[index].get(path, "keyword")
    array, sub = None, None
    for candidate in ARRAY_FIELDS.get(index, ()):
        if path == candidate:
            array = candidate
        elif path.startswith(f"{candidate}."):
            array, sub = candidate, path.removeprefix(f"{candidate}.")

    json_path(path)

    return FieldRef(path=path, ftype=ftype, array=array, sub=sub)


def fts_table(index: str) -> str:
    """name of fts table of index"""
    return f"{index}_fts"


def fts_column(field: str) -> str:
    """column name in fts table for field"""
    return clean_field(field).replace(".", "__")


def build_schema(index: str) -> list[str]:
    """build all statements to create index"""
    validate_index(index)
    statements = [
        f"CREATE TABLE IF NOT EXISTS {index} ("
        + "rowid INTEGER PRIMARY KEY, "
        + "_id TEXT NOT NULL UNIQUE, "
        + "source BLOB NOT NULL)"
    ]
    for field in INDEXED_FIELDS.get(index, ()):
        expr = resolve_field(index, field).value_expr()
        name = f"{index}_ix_{fts_column(field)}"
        statements.append(
            f"CREATE INDEX IF NOT EXISTS {name} ON {index} ({expr})"
        )

    if index in FTS_FIELDS:
        statements.extend(_build_fts_schema(index))

    return statements


def _build_fts_schema(index: str) -> list[str]:
    """build statements for full text search, kept in sync with triggers"""
    fields = FTS_FIELDS[index]
    fts = fts_table(index)
    columns = [fts_column(i) for i in fields]
    col_list = ", ".join(columns)

    def extract(row: str) -> str:
        return ", ".join(
            f"json_extract({row}source, '{json_path(i)}')" for i in fields
        )

    view_cols = ", ".join(
        f"json_extract(source, '{json_path(field)}') AS {column}"
        for field, column in zip(fields, columns)
    )
    insert = (
        f"INSERT INTO {fts} (rowid, {col_list}) "
        + f"VALUES (new.rowid, {extract('new.')});"
    )
    delete = (
        f"INSERT INTO {fts} ({fts}, rowid, {col_list}) "
        + f"VALUES ('delete', old.rowid, {extract('old.')});"
    )

    return [
        f"CREATE VIEW IF NOT EXISTS {fts}_content AS "
        + f"SELECT rowid AS rid, {view_cols} FROM {index}",
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts} USING fts5("
        + f"{col_list}, content='{fts}_content', content_rowid='rid', "
        + f"tokenize='{TOKENIZER}')",
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {fts}_vocab "
        + f"USING fts5vocab('{fts}', 'row')",
        f"CREATE TRIGGER IF NOT EXISTS {index}_ai AFTER INSERT ON {index} "
        + f"BEGIN {insert} END",
        f"CREATE TRIGGER IF NOT EXISTS {index}_ad AFTER DELETE ON {index} "
        + f"BEGIN {delete} END",
        f"CREATE TRIGGER IF NOT EXISTS {index}_au AFTER UPDATE ON {index} "
        + f"BEGIN {delete} {insert} END",
    ]


def build_drop(index: str) -> list[str]:
    """build all statements to delete index"""
    validate_index(index)
    statements = []
    if index in FTS_FIELDS:
        fts = fts_table(index)
        statements.extend(
            [
                f"DROP TRIGGER IF EXISTS {index}_ai",
                f"DROP TRIGGER IF EXISTS {index}_ad",
                f"DROP TRIGGER IF EXISTS {index}_au",
                f"DROP TABLE IF EXISTS {fts}_vocab",
                f"DROP TABLE IF EXISTS {fts}",
                f"DROP VIEW IF EXISTS {fts}_content",
            ]
        )

    statements.append(f"DROP TABLE IF EXISTS {index}")

    return statements
