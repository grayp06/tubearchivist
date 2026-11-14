"""stand-in for yt_dlp.utils"""


class DownloadError(Exception):
    """download failed"""


class ExtractorError(Exception):
    """extraction failed"""


def orderedSet_from_options(options, alias_dict, use_regex=False):
    """keep order, drop duplicates"""
    # pylint: disable=invalid-name,unused-argument
    return list(dict.fromkeys(options))


class ISO639Utils:
    """language codes"""

    @staticmethod
    def long2short(code):
        """three letter to two letter code"""
        return code[:2]
