"""Push notifications through ntfy (https://ntfy.sh): free, no account, iOS + Android apps."""

from __future__ import annotations

import httpx


def send(topic: str, *, title: str, message: str, url: str = "", image_url: str = "",
         server: str = "https://ntfy.sh") -> None:
    if not topic:
        return
    payload = {"topic": topic, "title": title, "message": message, "tags": ["shopping_bags"],
               "priority": 4}
    if url:
        payload["click"] = url
        payload["actions"] = [{"action": "view", "label": "Open listing", "url": url}]
    if image_url:
        payload["attach"] = image_url
    httpx.post(server, json=payload, timeout=15).raise_for_status()
