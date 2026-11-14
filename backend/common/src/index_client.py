"""
functionality:
- route index calls to the sqlite document store
- reusable pagination to go through a complete index

IndexClient keeps the path based interface of the search server
the application was built on, the calls never leave the process.
"""

import re
from typing import Any
from urllib.parse import parse_qs

from common.src.index_store import IndexStore

DOC_ACTIONS = ("_doc", "_create", "_update")


class IndexClient:
    """makes all calls to the document store
    returns response dict and status code tuple
    """

    def __init__(self, path: str):
        self.url: str = path
        path, _, query = path.partition("?")
        self.params: dict[str, str] = {
            k: v[0] for k, v in parse_qs(query).items()
        }
        self.parts: list[str] = [i for i in path.split("/") if i]
        self.store = IndexStore()

    @property
    def index(self) -> str:
        """index name of path"""
        return re.sub(r"_v\d+$", "", self.parts[0])

    @property
    def indexes(self) -> list[str]:
        """all index names of path"""
        return [re.sub(r"_v\d+$", "", i) for i in self.parts[0].split(",")]

    @property
    def action(self) -> str | None:
        """action of path"""
        if not self.parts:
            return "_info"

        if self.parts[0].startswith("_"):
            return self.parts[0]

        if len(self.parts) == 1:
            return "_index"

        return self.parts[1]

    def _unsupported(self, method: str) -> tuple[dict, int]:
        """path is not implemented"""
        message = f"unsupported index call: {method} {self.url}"
        print(message)
        return {"error": message}, 400

    def get(
        self,
        data: bool | dict = False,
        timeout: int = 10,
        print_error: bool = True,
    ) -> tuple[dict, int]:
        """get data from index"""
        # pylint: disable=unused-argument
        body = data if isinstance(data, dict) else None
        if self.action == "_info":
            return {"name": "sqlite", "version": {"number": "sqlite"}}, 200

        if self.action == "_index":
            if self.store.index_exists(self.index):
                return {self.index: {}}, 200

            return self._not_found(print_error)

        if self.action == "_doc":
            return self._get_doc(print_error)

        if self.action == "_search":
            return self.store.search(self.indexes, body), 200

        if self.action == "_count":
            return {"count": self.store.count(self.index, body)}, 200

        return self._unsupported("GET")

    def _get_doc(self, print_error: bool) -> tuple[dict, int]:
        """get single document"""
        doc_id = self.parts[2]
        source = self.store.get_doc(self.index, doc_id)
        if source is None:
            return self._not_found(print_error)

        response = {
            "_index": self.index,
            "_id": doc_id,
            "found": True,
            "_source": source,
        }

        return response, 200

    def _not_found(self, print_error: bool = True) -> tuple[dict, int]:
        """build not found response"""
        response: dict[str, Any] = {"found": False}
        if len(self.parts) > 2:
            response.update({"_index": self.index, "_id": self.parts[2]})

        if print_error:
            print(f"not found: {self.url}")

        return response, 404

    def post(
        self, data: bool | dict | str = False, ndjson: bool = False
    ) -> tuple[dict, int]:
        """post data to index"""
        if self.action == "_bulk":
            if not ndjson or not isinstance(data, str):
                return self._unsupported("POST")

            return self.store.bulk(iter(data.splitlines())), 200

        if self.action == "_refresh":
            return {}, 200

        body: dict = data if isinstance(data, dict) else {}
        if self.action == "_search":
            return self.store.search(self.indexes, body), 200

        if self.action == "_count":
            return {"count": self.store.count(self.index, body)}, 200

        if self.action in ("_doc", "_create"):
            return self._index_doc(body)

        if self.action == "_update":
            return self._update_doc(body)

        if self.action == "_update_by_query":
            updated = self.store.update_by_query(self.index, body)
            return {"updated": updated, "failures": []}, 200

        if self.action == "_delete_by_query":
            deleted = self.store.delete_by_query(self.index, body)
            return {"deleted": deleted, "failures": []}, 200

        return self._unsupported("POST")

    def _index_doc(self, body: dict) -> tuple[dict, int]:
        """index single document"""
        doc_id = self.parts[2]
        created = self.store.index_doc(self.index, doc_id, body)
        response = {
            "_index": self.index,
            "_id": doc_id,
            "result": "created" if created else "updated",
        }

        return response, 201 if created else 200

    def _update_doc(self, body: dict) -> tuple[dict, int]:
        """partial update single document"""
        doc_id = self.parts[2]
        result = self.store.update_doc(self.index, doc_id, body)
        response = {"_index": self.index, "_id": doc_id, "result": result}
        if result == "not_found":
            print(f"document missing: {self.url}")
            return response, 404

        return response, 201 if result == "created" else 200

    def put(
        self,
        data: bool | dict = False,
        refresh: bool = False,
    ) -> tuple[dict, Any]:
        """put data to index"""
        # pylint: disable=unused-argument
        if self.action == "_index":
            self.store.create_index(self.index)
            return {"acknowledged": True, "index": self.index}, 200

        if self.action in ("_doc", "_create") and isinstance(data, dict):
            return self._index_doc(data)

        print(data)
        raise ValueError(f"failed to add item to index: PUT {self.url}")

    def delete(
        self,
        data: bool | dict = False,
        refresh: bool = False,
    ) -> tuple[dict, Any]:
        """delete document from index"""
        # pylint: disable=unused-argument
        if self.action == "_index":
            self.store.delete_index(self.index)
            return {"acknowledged": True}, 200

        if self.action == "_doc":
            deleted = self.store.delete_doc(self.index, self.parts[2])
            if not deleted:
                return self._not_found()

            response = {
                "_index": self.index,
                "_id": self.parts[2],
                "result": "deleted",
            }
            return response, 200

        return self._unsupported("DELETE")


class IndexPaginate:
    """go through all results of a query
    kwargs:
    - size: int, overwrite DEFAULT_SIZE
    - keep_source: bool, keep _source key from results
    - callback: obj, Class implementing run method callback for every loop
    - task: task object to send notification
    - total: int, total items in index for progress message

    Results are only collected and returned if there is no callback,
    to keep memory usage flat while processing a complete index.
    """

    DEFAULT_SIZE = 500

    def __init__(self, index_name, data, **kwargs):
        self.index_name = index_name
        self.data = data
        self.kwargs = kwargs

    def get_results(self):
        """get all results, add task and total for notifications"""
        size = self.kwargs.get("size") or self.DEFAULT_SIZE
        callback = self.kwargs.get("callback")
        store = IndexStore()
        all_results = []
        processed = 0
        counter = 0
        for index in self.index_name.split(","):
            for all_hits in store.scan(index, self.data, size=size):
                processed += len(all_hits)
                if callback:
                    callback(all_hits, self.index_name, counter=counter).run()
                elif self.kwargs.get("keep_source"):
                    all_results.extend(all_hits)
                else:
                    all_results.extend(i["_source"] for i in all_hits)

                if self.kwargs.get("task"):
                    print(f"{self.index_name}: processing page {counter}")
                    self._notify(processed)

                counter += 1

        return all_results

    def _notify(self, processed):
        """send notification on task"""
        total = self.kwargs.get("total")
        progress = processed / total if total else 0
        index_clean = self.index_name.lstrip("ta_").title()
        message = [f"Processing {index_clean}s {processed}/{total}"]
        self.kwargs.get("task").send_progress(message, progress=progress)
