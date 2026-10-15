"""Normalize the verified UI-DOM item contract without inventing video URLs.

Only the currently observed DOM/persisted-item schema is supported.  An absent
link or exhausted access quota is a parse/access failure, not proof that an
item is not a video.  NOT_VIDEO_ITEM remains reserved until an explicit source
marker has actually been verified.
"""
import re
from urllib.parse import urlsplit


class KalodataItemNormalizer:
    SCHEMA = "kalodata-ui-dom-v1"
    STATUSES = {"VALID_VIDEO", "NOT_VIDEO_ITEM", "PARSE_FAILED"}

    @classmethod
    def normalize(cls, item, *, page_size, schema_version=SCHEMA):
        page, index = item.get("source_page"), item.get("source_index")
        position_valid = all(type(v) is int and v > 0 for v in (page, index, page_size))
        result = {
            "rank": (page - 1) * page_size + index if position_valid else None,
            "page": page if type(page) is int and page > 0 else None,
            "video_url": None,
            "source_url": item.get("kalodata_video_url"),
            "status": "PARSE_FAILED",
            "reason_code": item.get("error_code") or "LINK_NOT_FOUND",
            "schema_version": cls.SCHEMA,
        }
        if schema_version != cls.SCHEMA or not position_valid:
            result["reason_code"] = "UNSUPPORTED_ITEM_SCHEMA"
            return result
        url, video_id = item.get("tiktok_url"), item.get("video_id")
        if item.get("status") != "resolved" or not isinstance(url, str) or not isinstance(video_id, str):
            return result
        try:
            parsed = urlsplit(url)
            match = re.fullmatch(r"/@[A-Za-z0-9_.-]{1,64}/video/([0-9]{1,64})/?", parsed.path)
            valid = (parsed.scheme == "https" and parsed.netloc == "www.tiktok.com"
                     and not parsed.query and not parsed.fragment and match and match[1] == video_id)
        except ValueError:
            valid = False
        if valid:
            result.update(video_url=url, status="VALID_VIDEO", reason_code=None)
        else:
            result["reason_code"] = "LINK_ID_MISMATCH"
        return result
