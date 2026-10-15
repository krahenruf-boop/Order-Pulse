"""Presentation-only wrapper; reuse the audited HTTP/signature/retry transport."""
import json
import re
from .feishu import FeishuClient


class PulseFeishuClient(FeishuClient):
    def _post(self, body):
        payload=json.loads(body)
        text=payload['content']['text']
        # Keep the event ID in our independent database, not in the group message.
        payload['content']['text']=re.sub(r'\n\n事件编号：[0-9a-f]{32}$','',text)
        return super()._post(json.dumps(payload,ensure_ascii=False,separators=(',',':')).encode())
