from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Iterable

from podhub.outputs.base import RenderedOutput, feed_url
from podhub.outputs.rss import clean


def render_opml(base: str, sources: Iterable[tuple[str, str, str]]) -> RenderedOutput:
    root = ET.Element("opml", version="2.0")
    ET.SubElement(ET.SubElement(root, "head"), "title").text = "Pod Hub subscriptions"
    body = ET.SubElement(root, "body")
    for title, token, url in sources:
        ET.SubElement(body, "outline", type="rss", text=clean(title), title=clean(title),
                      xmlUrl=feed_url(base, token), htmlUrl=clean(url or base))
    return RenderedOutput(ET.tostring(root, encoding="utf-8", xml_declaration=True), "text/x-opml; charset=utf-8")
