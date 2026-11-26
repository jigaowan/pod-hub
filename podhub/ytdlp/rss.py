from __future__ import annotations

import xml.etree.ElementTree as ET
from dataclasses import dataclass
from typing import Any

import httpx


ATOM = "{http://www.w3.org/2005/Atom}"
YT = "{http://www.youtube.com/xml/schemas/2015}"
MEDIA = "{http://search.yahoo.com/mrss/}"


@dataclass(frozen=True)
class AtomResult:
    entries: list[dict[str, Any]]
    etag: str | None
    last_modified: str | None
    not_modified: bool = False


async def fetch_atom(
    client: httpx.AsyncClient, channel: str, *, long_form: bool,
    etag: str | None = None, last_modified: str | None = None,
) -> AtomResult:
    headers = {}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified
    params = {"playlist_id": f"UULF{channel[2:]}"} if long_form else {"channel_id": channel}
    response = await client.get("https://www.youtube.com/feeds/videos.xml", params=params, headers=headers)
    if response.status_code == 304:
        return AtomResult([], response.headers.get("etag", etag), response.headers.get("last-modified", last_modified), True)
    response.raise_for_status()
    root = ET.fromstring(response.content)
    if root.tag != f"{ATOM}feed":
        raise ET.ParseError("expected a YouTube Atom feed")
    entries = []
    for entry in root.findall(f"{ATOM}entry"):
        image = entry.find(f"{MEDIA}group/{MEDIA}thumbnail")
        entries.append({
            "id": entry.findtext(f"{YT}videoId"),
            "title": entry.findtext(f"{ATOM}title"),
            "timestamp": entry.findtext(f"{ATOM}published"),
            "channel_id": entry.findtext(f"{YT}channelId"),
            "thumbnail": image.get("url") if image is not None else None,
            "description": entry.findtext(f"{MEDIA}group/{MEDIA}description"),
        })
    return AtomResult(entries, response.headers.get("etag"), response.headers.get("last-modified"))
