from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime

from podhub.outputs.base import RenderedOutput, RenderSourceContext, feed_url, file_url
from podhub.storage.paths import canonical_video, youtube_cover

ITUNES = "http://www.itunes.com/dtds/podcast-1.0.dtd"
PODCAST = "https://podcastindex.org/namespace/1.0"
ATOM = "http://www.w3.org/2005/Atom"
ILLEGAL_XML = re.compile(r"[\x00-\x08\x0B\x0C\x0E-\x1F\x7F\ud800-\udfff\ufffe\uffff]")
SERIAL_EPOCH = datetime(2000, 1, 1, tzinfo=UTC)


def clean(value: str) -> str:
    return ILLEGAL_XML.sub("", value)


def rss_date(value: str) -> str:
    return format_datetime(datetime.fromisoformat(value).astimezone(UTC), usegmt=True)


class PodcastRSS:
    name = "rss"
    content_type = "application/rss+xml; charset=utf-8"

    def render_source(self, ctx: RenderSourceContext) -> RenderedOutput:
        root = ET.Element("rss", {
            "version": "2.0", "xmlns:itunes": ITUNES, "xmlns:podcast": PODCAST, "xmlns:atom": ATOM,
        })
        channel = ET.SubElement(root, "channel")
        for tag, value in (
            ("title", ctx.title), ("description", f"Audio from {ctx.title}"),
            ("link", ctx.link or ctx.base_url), ("itunes:author", ctx.title), ("itunes:block", "Yes"),
        ):
            ET.SubElement(channel, tag).text = clean(value)
        ET.SubElement(channel, "atom:link", {
            "href": feed_url(ctx.base_url, ctx.feed_token), "rel": "self", "type": "application/rss+xml",
        })
        if ctx.cover:
            ET.SubElement(channel, "itunes:image", href=file_url(ctx, "covers", ctx.cover))
        serial = ctx.policy.feed_order == "playlist_serial"
        if serial:
            ET.SubElement(channel, "itunes:type").text = "serial"
            episodes = sorted(ctx.episodes, key=lambda ep: (ep.sort_key is None, ep.sort_key or 0, ep.video_id))
        else:
            episodes = sorted(ctx.episodes, key=lambda ep: (ep.published_at or "", ep.video_id), reverse=True)
        if serial and episodes:
            known = [ep.sort_key for ep in episodes if ep.sort_key is not None]
            next_episode = max(known, default=0)
        else:
            next_episode = 0
        for position, episode in enumerate(episodes[:ctx.policy.max_feed_items], 1):
            item = ET.SubElement(channel, "item")
            ET.SubElement(item, "title").text = clean(episode.title)
            ET.SubElement(item, "link").text = canonical_video(episode.video_id)
            if thumbnail := youtube_cover(episode.thumbnail_url):
                ET.SubElement(item, "itunes:image", href=thumbnail)
            if episode.description:
                ET.SubElement(item, "description").text = clean(episode.description)
            ET.SubElement(item, "guid", isPermaLink="false").text = f"yt:video:{episode.video_id}"
            if serial:
                if episode.sort_key is None:
                    next_episode += 1
                ET.SubElement(item, "itunes:episode").text = str(episode.sort_key if episode.sort_key is not None else next_episode)
                date = SERIAL_EPOCH + timedelta(days=position)
                ET.SubElement(item, "pubDate").text = format_datetime(date, usegmt=True)
            elif episode.published_at:
                ET.SubElement(item, "pubDate").text = rss_date(episode.published_at)
            ET.SubElement(item, "enclosure", {
                "url": file_url(ctx, "media", f"{episode.video_id}.m4a"),
                "length": str(episode.media_bytes), "type": "audio/mp4",
            })
            ET.SubElement(item, "itunes:duration").text = str(episode.media_duration_s)
            for artifact in episode.artifacts:
                if artifact.kind == "transcript_vtt":
                    ET.SubElement(item, "podcast:transcript", {
                        "url": file_url(ctx, "artifacts", artifact.filename), "type": artifact.mime, "rel": "captions",
                    })
        return RenderedOutput(ET.tostring(root, encoding="utf-8", xml_declaration=True), self.content_type)
