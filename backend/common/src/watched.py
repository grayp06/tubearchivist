"""
functionality:
- handle watched state for videos, channels and playlists
"""

from datetime import datetime

from common.src.index_client import IndexClient
from common.src.state_store import StateStore
from common.src.urlparser import Parser


class WatchState:
    """handle watched checkbox for videos and channels"""

    def __init__(self, youtube_id: str, is_watched: bool, user_id: int):
        self.youtube_id = youtube_id
        self.is_watched = is_watched
        self.user_id = user_id
        self.stamp = int(datetime.now().timestamp())

    def change(self):
        """change watched state of item(s)"""
        print(f"{self.youtube_id}: change watched state to {self.is_watched}")
        url_type = self._dedect_type()
        if url_type == "video":
            self.change_vid_state()
            return

        if url_type == "channel":
            self.reset_channel_progress()
        if url_type == "playlist":
            self.reset_playlist_progress()

        data = self._build_update_data(url_type)
        _, _ = IndexClient("ta_video/_update_by_query").post(data)

    def _dedect_type(self):
        """find youtube id type"""
        url_process = Parser(self.youtube_id).parse()
        url_type = url_process[0]["type"]
        return url_type

    def change_vid_state(self):
        """change watched state of video"""
        path = f"ta_video/_update/{self.youtube_id}"
        data = {"doc": {"player": {"watched": self.is_watched}}}
        if self.is_watched:
            data["doc"]["player"]["watched_date"] = self.stamp
        response, status_code = IndexClient(path).post(data=data)
        key = f"{self.user_id}:progress:{self.youtube_id}"
        StateStore().del_message(key)
        if status_code != 200:
            print(response)
            raise ValueError("failed to mark video as watched")

    def reset_channel_progress(self):
        """reset channel progress positions"""
        from channel.src.index import YoutubeChannel

        videos = YoutubeChannel(self.youtube_id).get_channel_videos()
        video_ids = [i["youtube_id"] for i in videos]
        self._reset_list(video_ids)

    def reset_playlist_progress(self):
        """reset playlist progress positions"""
        from playlist.src.index import YoutubePlaylist

        videos = YoutubePlaylist(self.youtube_id).get_playlist_videos()
        video_ids = [i["youtube_id"] for i in videos]
        self._reset_list(video_ids)

    def _reset_list(self, video_ids: list[str]):
        """reset list of video ids"""
        redis_con = StateStore()
        all_ids = redis_con.list_keys(f"{self.user_id}:progress")
        for progress_id in all_ids:
            video_id = progress_id.split(":")[-1]
            if video_id in video_ids:
                redis_con.del_message(progress_id)

    def _build_update_data(self, url_type):
        """build update by query data based on url_type"""
        term_key_map = {
            "channel": "channel.channel_id",
            "playlist": "playlist.keyword",
        }
        term_key = term_key_map.get(url_type)

        return {
            "query": {
                "bool": {
                    "must": [
                        {"term": {term_key: {"value": self.youtube_id}}},
                        {
                            "term": {
                                "player.watched": {
                                    "value": not self.is_watched
                                }
                            }
                        },
                    ],
                }
            },
            "script": self._set_watched,
        }

    def _set_watched(self, source: dict) -> None:
        """update script to set watched state of video"""
        player = source.setdefault("player", {})
        player["watched"] = self.is_watched
        player["watched_date"] = self.stamp
