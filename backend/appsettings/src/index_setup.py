"""
functionality:
- setup index tables at first start
- reset all indexes to blank
"""

from common.src.index_schema import all_indexes
from common.src.index_store import IndexStore


class IndexSetup:
    """interact with all indexes"""

    def __init__(self):
        self.store = IndexStore()

    def setup(self):
        """create all missing indexes, run at startup"""
        for index in all_indexes():
            created = self.store.create_index(index)
            if created:
                print(f"[{index}] created new index.")
            else:
                print(f"[{index}] index status is as expected.")

    def reset(self):
        """reset all indexes to blank"""
        print("reset all indexes to blank")
        self.store.reset()
