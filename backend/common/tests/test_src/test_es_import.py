"""test import from Elasticsearch"""

# pylint: disable=redefined-outer-name

import pytest
from appsettings.src.es_import import ElasticImport
from common.src.env_settings import EnvironmentSettings
from common.src.index_schema import all_indexes
from common.src.index_store import IndexStore
from common.src.sqlite_conn import INDEX_DB, close_conn

VIDEOS = [
    {"_id": f"video{i}", "_source": {"youtube_id": f"video{i}", "title": "x"}}
    for i in range(7)
]


class FakeResponse:
    """response of fake Elasticsearch"""

    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code
        self.ok = status_code < 400
        self.text = str(body)

    def json(self):
        """get body"""
        return self.body


class FakeSession:
    """Elasticsearch with ta_video only, scrolls in batches"""

    def __init__(self):
        self.position = 0
        self.size = 0
        self.cleared = []
        self.auth = None
        self.verify = True

    def _page(self):
        end = self.position + self.size
        hits = VIDEOS[slice(self.position, end)]
        self.position = end
        return FakeResponse({"_scroll_id": "abc", "hits": {"hits": hits}})

    def get(self, url, **kwargs):
        """cluster info"""
        # pylint: disable=unused-argument
        return FakeResponse({"version": {"number": "8.19.0"}})

    def post(self, url, json=None, **kwargs):
        """search and scroll"""
        # pylint: disable=unused-argument
        if url.endswith("/_search/scroll"):
            assert json["scroll_id"] == "abc"
            return self._page()

        if not url.endswith("/ta_video/_search"):
            return FakeResponse({"error": "index_not_found"}, 404)

        self.position = 0
        self.size = json["size"]

        return self._page()

    def delete(self, url, json=None, **kwargs):
        """clear scroll"""
        # pylint: disable=unused-argument
        self.cleared.append(json["scroll_id"])
        return FakeResponse({})


@pytest.fixture
def handler(tmp_path, monkeypatch):
    """import handler with fake session"""
    monkeypatch.setattr(EnvironmentSettings, "CACHE_DIR", str(tmp_path))
    store = IndexStore()
    for index in all_indexes():
        store.create_index(index)

    elastic_import = ElasticImport(
        "http://es:9200/", batch_size=3, log=lambda _: None
    )
    elastic_import.session = FakeSession()
    yield elastic_import
    close_conn(INDEX_DB)


def test_import(handler):
    """all documents are imported and searchable"""
    assert handler.check_connection() == "8.19.0"
    result = handler.run()
    assert result["ta_video"] == 7
    assert sum(result.values()) == 7
    assert handler.session.cleared == ["abc"]

    store = IndexStore()
    assert store.count("ta_video") == 7
    assert store.get_doc("ta_video", "video3")["title"] == "x"
    response = store.search(["ta_video"], {"query": {"match": {"title": "x"}}})
    assert response["hits"]["total"]["value"] == 7


def test_import_refuses_to_overwrite(handler):
    """existing documents need replace"""
    store = IndexStore()
    store.index_doc("ta_video", "local", {"youtube_id": "local"})
    with pytest.raises(ValueError):
        handler.run()

    assert store.get_doc("ta_video", "local")
    handler.run(replace=True)
    assert store.get_doc("ta_video", "local") is None
    assert store.count("ta_video") == 7
