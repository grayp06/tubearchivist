"""test sqlite document store and query translation"""

# pylint: disable=redefined-outer-name

import json

import pytest
from common.src.env_settings import EnvironmentSettings
from common.src.index_client import IndexClient, IndexPaginate
from common.src.index_query import edit_distance, parse_date
from common.src.index_schema import all_indexes
from common.src.index_store import IndexStore

CHANNELS = ["UC000000000000000000000a", "UC000000000000000000000b"]
TITLES = [
    "Cooking fresh pasta at home",
    "Running a marathon in the rain",
    "Linux kernel deep dive",
    "Pasta sauce from scratch",
    "Guitar lesson for beginners",
    "Marathon training plan",
]


def build_video(idx: int) -> dict:
    """build video document"""
    channel_id = CHANNELS[idx % 2]
    return {
        "youtube_id": f"video{idx:06d}",
        "title": TITLES[idx],
        "tags": ["food", "kitchen"] if "asta" in TITLES[idx] else ["other"],
        "active": True,
        "vid_type": "shorts" if idx == 5 else "videos",
        "published": 1700000000 + idx * 86400 if idx else "2023-11-14",
        "date_downloaded": 1700000000 + idx,
        "media_size": 100 * (idx + 1),
        "player": {"watched": idx % 2 == 0, "duration": 60 + idx},
        "streams": [
            {"type": "video", "height": 1080 if idx % 2 else 720},
            {"type": "audio"},
        ],
        "channel": {
            "channel_id": channel_id,
            "channel_name": f"Channel {channel_id[-1].upper()}",
        },
        "playlist": ["PL1"] if idx < 3 else [],
    }


@pytest.fixture
def store(tmp_path, monkeypatch):
    """store with all indexes in temporary folder"""
    monkeypatch.setattr(EnvironmentSettings, "CACHE_DIR", str(tmp_path))
    index_store = IndexStore()
    for index in all_indexes():
        index_store.create_index(index)

    for idx in range(len(TITLES)):
        video = build_video(idx)
        index_store.index_doc("ta_video", video["youtube_id"], video)

    return index_store


def search_ids(store, body, index="ta_video"):
    """get ids of search"""
    response = store.search(index.split(","), body)
    return [i["_id"] for i in response["hits"]["hits"]]


def test_get_index_delete(store):
    """single document lifecycle through path interface"""
    path = "ta_video/_doc/video000000"
    response, status = IndexClient(path).get()
    assert status == 200
    assert response["_source"]["title"] == TITLES[0]

    _, status = IndexClient(path).put(build_video(0), refresh=True)
    assert status == 200
    _, status = IndexClient(f"{path}?refresh=true").delete()
    assert status == 200
    response, status = IndexClient(path).get(print_error=False)
    assert status == 404
    assert "_source" not in response
    assert IndexClient("ta_video/_count").get()[0]["count"] == 5


def test_term_and_match(store):
    """exact matches, also on nested and list fields"""
    query = {"term": {"channel.channel_id": {"value": CHANNELS[1]}}}
    assert len(search_ids(store, {"query": query})) == 3
    query = {"match": {"playlist.keyword": "PL1"}}
    assert len(search_ids(store, {"query": query})) == 3
    query = {"term": {"streams.height": {"value": "1080"}}}
    assert len(search_ids(store, {"query": query})) == 3
    query = {"match": {"player.watched": False}}
    assert len(search_ids(store, {"query": query})) == 3
    query = {"terms": {"youtube_id": ["video000001", "video000002", "x"]}}
    assert len(search_ids(store, {"query": query})) == 2


def test_bool_query(store):
    """combine must, must_not and should"""
    query = {
        "bool": {
            "must": [
                {"term": {"active": {"value": True}}},
                {"term": {"vid_type": {"value": "videos"}}},
                None,
            ],
            "must_not": [{"exists": {"field": "does_not_exist"}}],
        }
    }
    assert len(search_ids(store, {"query": query})) == 5
    query = {
        "bool": {
            "should": [
                {"prefix": {"youtube_id": {"value": "video00000"}}},
                {"prefix": {"youtube_id": {"value": "nope"}}},
            ],
            "must_not": [{"term": {"vid_type": {"value": "shorts"}}}],
        }
    }
    assert len(search_ids(store, {"query": query})) == 5
    query = {"bool": {"must_not": [{"exists": {"field": "playlist"}}]}}
    assert len(search_ids(store, {"query": query})) == 3


def test_range_dates(store):
    """range on date field with mixed epoch and iso values"""
    query = {"range": {"published": {"gte": "2023-11-15"}}}
    assert len(search_ids(store, {"query": query})) == 5
    query = {"range": {"published": {"lte": str(1700000000)}}}
    assert search_ids(store, {"query": query}) == ["video000000"]
    assert parse_date("2023-11-14") == 1699920000
    assert parse_date("1700000000") == 1700000000


def test_sort_and_pagination(store):
    """sort by field, list field and script"""
    body = {"sort": [{"published": {"order": "desc"}}], "size": 2, "from": 1}
    assert search_ids(store, body) == ["video000004", "video000003"]
    body = {"sort": [{"streams.height": {"order": "asc"}}], "size": 1}
    assert search_ids(store, body) == ["video000000"]
    scores = {"video000002": 0, "video000000": 1}
    script = {"type": "number", "script": {"params": {"scores": scores}}}
    body = {
        "query": {"match": {"playlist.keyword": "PL1"}},
        "sort": [{"_script": {**script, "order": "asc"}}],
    }
    expected = ["video000002", "video000000", "video000001"]
    assert search_ids(store, body) == expected
    response = store.search(["ta_video"], {"size": 2})
    assert response["hits"]["total"]["value"] == 6


def test_source_filter(store):
    """limit returned fields"""
    body = {"_source": ["youtube_id", "channel.channel_name"], "size": 1}
    hit = store.search(["ta_video"], body)["hits"]["hits"][0]
    expected = {
        "youtube_id": "video000000",
        "channel": {"channel_name": "Channel A"},
    }
    assert hit["_source"] == expected
    hit = store.search(["ta_video"], {"_source": False, "size": 1})
    assert "_source" not in hit["hits"]["hits"][0]
    body = {"_source": {"excludes": ["channel", "streams"]}, "size": 1}
    source = store.search(["ta_video"], body)["hits"]["hits"][0]["_source"]
    assert "channel" not in source and "title" in source


def test_full_text(store):
    """full text search with prefix, fuzziness and stemming"""

    def multi_match(query_str):
        query = {
            "multi_match": {
                "query": query_str,
                "type": "bool_prefix",
                "fuzziness": "auto",
                "operator": "and",
                "fields": [
                    "title._2gram^2",
                    "title.search_as_you_type",
                    "tags",
                ],
            }
        }
        return search_ids(store, {"query": query})

    assert set(multi_match("pasta")) == {"video000000", "video000003"}
    assert multi_match("pasta sau") == ["video000003"]
    assert set(multi_match("marathn tr")) == {"video000005"}
    assert set(multi_match("cook")) == {"video000000"}
    assert set(multi_match("kitchen")) == {"video000000", "video000003"}
    assert not multi_match("unknownword")
    assert edit_distance("marathon", "maratohn", 2) == 1


def test_multi_index_search(store):
    """search over multiple indexes"""
    channel = {"channel_id": CHANNELS[0], "channel_name": "Pasta Channel"}
    store.index_doc("ta_channel", CHANNELS[0], channel)
    query = {
        "multi_match": {
            "query": "pasta",
            "type": "bool_prefix",
            "operator": "and",
            "fields": ["channel_name.search_as_you_type", "title._2gram"],
        }
    }
    path = "ta_video,ta_channel,ta_playlist/_search"
    response, _ = IndexClient(path).get({"query": query, "size": 30})
    indexes = {i["_index"] for i in response["hits"]["hits"]}
    assert indexes == {"ta_video", "ta_channel"}
    assert response["hits"]["total"]["value"] == 3


def test_highlight_and_similar(store):
    """highlight matches and more like this"""
    for idx, line in enumerate(["we launch the rocket", "nothing here"]):
        doc = {"youtube_id": "video000000", "subtitle_line": line}
        store.index_doc("ta_subtitle", f"sub-{idx}", doc)

    body = {
        "query": {"match": {"subtitle_line": {"query": "rockets"}}},
        "highlight": {
            "fields": {
                "subtitle_line": {"pre_tags": ["<b>"], "post_tags": ["</b>"]}
            }
        },
    }
    hits = store.search(["ta_subtitle"], body)["hits"]["hits"]
    assert len(hits) == 1
    expected = ["we launch the <b>rocket</b>"]
    assert hits[0]["highlight"]["subtitle_line"] == expected

    query = {
        "more_like_this": {
            "fields": ["tags", "title"],
            "like": {"_id": "video000000"},
        }
    }
    assert search_ids(store, {"query": query, "size": 1}) == ["video000003"]


def test_aggregations(store):
    """metric and bucket aggregations"""
    body = {
        "size": 0,
        "aggs": {
            "types": {
                "terms": {"field": "vid_type"},
                "aggs": {"size": {"sum": {"field": "media_size"}}},
            },
            "watched": {"terms": {"field": "player.watched"}},
            "total": {"value_count": {"field": "youtube_id"}},
            "pending": {
                "filter": {"term": {"vid_type": "shorts"}},
                "aggs": {"count": {"value_count": {"field": "_index"}}},
            },
            "channels": {
                "multi_terms": {
                    "terms": [
                        {"field": "channel.channel_name.keyword"},
                        {"field": "channel.channel_id"},
                    ],
                    "order": {"duration": "desc"},
                },
                "aggs": {"duration": {"sum": {"field": "player.duration"}}},
            },
        },
    }
    aggs = store.search(["ta_video"], body)["aggregations"]
    assert aggs["total"]["value"] == 6
    types = {i["key"]: i for i in aggs["types"]["buckets"]}
    assert types["videos"]["doc_count"] == 5
    assert types["shorts"]["size"]["value"] == 600
    watched = {
        i["key_as_string"]: i["doc_count"] for i in aggs["watched"]["buckets"]
    }
    assert watched == {"true": 3, "false": 3}
    assert aggs["pending"] == {"doc_count": 1, "count": {"value": 1}}
    first = aggs["channels"]["buckets"][0]
    assert first["key"] == ["Channel B", CHANNELS[1]]
    assert first["duration"]["value"] == 189


def test_date_histogram(store):
    """daily histogram fills empty days"""
    body = {
        "size": 0,
        "aggs": {
            "hist": {
                "date_histogram": {
                    "field": "published",
                    "calendar_interval": "day",
                    "order": {"_key": "desc"},
                    "time_zone": "UTC",
                },
                "aggs": {"size": {"sum": {"field": "media_size"}}},
            }
        },
        "query": {"range": {"published": {"gte": 1700000000 + 86400 * 3}}},
    }
    buckets = store.search(["ta_video"], body)["aggregations"]["hist"][
        "buckets"
    ]
    assert [i["key_as_string"] for i in buckets] == [
        "2023-11-19",
        "2023-11-18",
        "2023-11-17",
    ]
    assert [i["doc_count"] for i in buckets] == [1, 1, 1]


def test_updates(store):
    """partial updates, scripts and upserts"""
    path = "ta_video/_update/video000001"
    data = {"doc": {"player": {"watched": True, "watched_date": 5}}}
    _, status = IndexClient(path).post(data)
    assert status == 200
    player = store.get_doc("ta_video", "video000001")["player"]
    assert player == {"watched": True, "duration": 61, "watched_date": 5}

    _, status = IndexClient("ta_video/_update/missing").post(data)
    assert status == 404
    data = {"script": lambda source: False, "upsert": {"task": ["url"]}}
    _, status = IndexClient("ta_config/_update/notify").post(data)
    assert status == 201
    assert store.get_doc("ta_config", "notify") == {"task": ["url"]}

    def remove_playlist(source):
        source["playlist"].remove("PL1")

    data = {
        "query": {"term": {"playlist.keyword": {"value": "PL1"}}},
        "script": remove_playlist,
    }
    response, _ = IndexClient("ta_video/_update_by_query").post(data)
    assert response["updated"] == 3
    assert not search_ids(store, {"query": data["query"]})

    data = {"query": {"term": {"vid_type": {"value": "shorts"}}}}
    response, _ = IndexClient("ta_video/_delete_by_query").post(data)
    assert response["deleted"] == 1
    assert store.count("ta_video") == 5
    # full text index follows updates and deletes
    query = {"match": {"title": {"query": "training"}}}
    assert not search_ids(store, {"query": query})


def test_bulk_and_paginate(store):
    """bulk actions and going through a complete index"""
    lines = []
    for idx in range(1200):
        doc_id = f"queue{idx:06d}"
        action = {"index": {"_index": "ta_download", "_id": doc_id}}
        doc = {"youtube_id": doc_id, "status": "pending", "timestamp": idx}
        lines.extend([json.dumps(action), json.dumps(doc)])

    lines.append(json.dumps({"delete": {"_index": "ta_download", "_id": "x"}}))
    update = {"update": {"_index": "ta_download", "_id": "queue000000"}}
    lines.extend(
        [json.dumps(update), json.dumps({"doc": {"status": "ignore"}})]
    )
    lines.append("\n")
    response, status = IndexClient("_bulk").post("\n".join(lines), ndjson=True)
    assert status == 200 and not response["errors"]

    data = {
        "query": {"term": {"status": {"value": "pending"}}},
        "sort": [{"timestamp": {"order": "desc"}}],
        "_source": ["youtube_id"],
    }
    results = IndexPaginate("ta_download", data).get_results()
    assert len(results) == 1199
    assert results[0] == {"youtube_id": "queue001199"}

    pages = []

    class Callback:
        """collect pages"""

        def __init__(self, source, index_name, counter=0):
            pages.append((len(source), index_name, counter))

        def run(self):
            """run callback"""

    result = IndexPaginate(
        "ta_download", {}, size=500, callback=Callback
    ).get_results()
    assert not result
    assert pages == [
        (500, "ta_download", 0),
        (500, "ta_download", 1),
        (200, "ta_download", 2),
    ]


def test_reset(store):
    """reset removes all documents"""
    store.reset()
    assert store.count("ta_video") == 0
    assert store.index_exists("ta_video")
    with pytest.raises(KeyError):
        store.index_doc("ta_unknown", "id", {})
