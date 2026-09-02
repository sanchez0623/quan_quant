# -*- coding: utf-8 -*-
"""飞书机器人推送（LIVE_SIGNAL_SYSTEM §6/§7）。

webhook 从 .env 的 FEISHU_WEBHOOK_URL 读取（config.py）；
未配置时静默跳过（返回 False），推送失败不阻断盘前/盘中主流程。
"""
import json
import urllib.request

from .. import config


def send_text(text: str) -> bool:
    """推送纯文本到飞书群。返回是否成功；未配置 webhook 返回 False。"""
    url = config.FEISHU_WEBHOOK_URL
    if not url:
        return False
    body = {"msg_type": "text", "content": {"text": text}}
    req = urllib.request.Request(
        url,
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"})
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        return resp.get("code") == 0 or resp.get("StatusCode") == 0
    except Exception:
        return False


def configured() -> bool:
    """webhook 是否已配置"""
    return bool(config.FEISHU_WEBHOOK_URL)
