"""
functionality:
- import all documents of a running Elasticsearch
- to migrate an existing installation without backup and restore
"""

from typing import Callable, Iterator

import requests
from common.src.index_schema import all_indexes
from common.src.index_store import IndexStore
from common.src.sqlite_conn import write_tx


class ElasticImport:
    """copy documents from Elasticsearch into the local index"""

    SCROLL = "5m"
    TIMEOUT = 120

    def __init__(
        self,
        url: str,
        auth: tuple[str, str] | None = None,
        verify: bool = True,
        batch_size: int = 500,
        log: Callable[[str], None] = print,
    ):
        self.url = url.rstrip("/")
        self.batch_size = batch_size
        self.log = log
        self.session = requests.Session()
        self.session.auth = auth
        self.session.verify = verify
        self.store = IndexStore()

    def check_connection(self) -> str:
        """returns version of Elasticsearch, raises on connection problems"""
        response = self.session.get(self.url, timeout=self.TIMEOUT)
        if not response.ok:
            raise ValueError(
                f"Elasticsearch returned {response.status_code}: "
                + response.text[:200]
            )

        return response.json().get("version", {}).get("number", "unknown")

    def not_empty(self) -> list[str]:
        """list local indexes holding documents already"""
        return [
            index
            for index in all_indexes()
            if self.store.index_exists(index) and self.store.count(index)
        ]

    def run(self, replace: bool = False) -> dict[str, int]:
        """import all indexes, returns document count by index"""
        existing = self.not_empty()
        if existing and not replace:
            raise ValueError(
                f"local index is not empty: {', '.join(existing)}, "
                + "use --replace to overwrite"
            )

        result = {}
        for index in all_indexes():
            result[index] = self.import_index(index)
            self.log(f"{index}: imported {result[index]} documents")

        self.store.optimize()

        return result

    def import_index(self, index: str) -> int:
        """replace local index with documents of remote index"""
        count = 0
        with write_tx(self.store.conn):
            self.store.delete_index(index)
            self.store.create_index(index)
            for hits in self.scroll(index):
                for hit in hits:
                    self.store.index_doc(index, hit["_id"], hit["_source"])

                count += len(hits)
                self.log(f"{index}: {count}")

        return count

    def scroll(self, index: str) -> Iterator[list[dict]]:
        """iterate over all documents of remote index in batches"""
        response = self.session.post(
            f"{self.url}/{index}/_search",
            params={"scroll": self.SCROLL},
            json={"size": self.batch_size, "sort": ["_doc"]},
            timeout=self.TIMEOUT,
        )
        if response.status_code == 404:
            self.log(f"{index}: not found in Elasticsearch, skipping")
            return

        scroll_id = None
        try:
            while True:
                if not response.ok:
                    raise ValueError(
                        f"{index}: Elasticsearch returned "
                        + f"{response.status_code}: {response.text[:200]}"
                    )

                body = response.json()
                scroll_id = body.get("_scroll_id", scroll_id)
                hits = body["hits"]["hits"]
                if not hits:
                    break

                yield hits
                response = self.session.post(
                    f"{self.url}/_search/scroll",
                    json={"scroll": self.SCROLL, "scroll_id": scroll_id},
                    timeout=self.TIMEOUT,
                )
        finally:
            if scroll_id:
                self.session.delete(
                    f"{self.url}/_search/scroll",
                    json={"scroll_id": scroll_id},
                    timeout=self.TIMEOUT,
                )
