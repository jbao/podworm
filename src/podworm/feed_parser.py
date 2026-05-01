"""OPML and RSS feed parsing for 小宇宙 podcasts."""

import json
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from email.utils import parsedate_to_datetime
from urllib.parse import quote_plus, parse_qs, urlparse, unquote

import feedparser
import httpx

from podworm.database import Podcast, Episode

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
}


def xiaoyuzhou_url_to_feed(url: str) -> tuple[str, str]:
    """
    Convert a Xiaoyuzhou podcast URL to RSS feed URL.

    Args:
        url: URL like https://www.xiaoyuzhoufm.com/podcast/xxx

    Returns:
        Tuple of (podcast_id, feed_url)
    """
    # Extract podcast ID from various URL formats
    patterns = [
        r"xiaoyuzhoufm\.com/podcast/([a-zA-Z0-9]+)",
        r"feed\.xyzfm\.space/([a-zA-Z0-9]+)",
    ]

    for pattern in patterns:
        if match := re.search(pattern, url):
            podcast_id = match.group(1)
            feed_url = f"https://feed.xyzfm.space/{podcast_id}"
            return podcast_id, feed_url

    raise ValueError(f"Could not extract podcast ID from URL: {url}")


def parse_opml(opml_path: Path) -> list[tuple[str, str, str | None]]:
    """
    Parse OPML file exported from 小宇宙 app.

    Args:
        opml_path: Path to the OPML file

    Returns:
        List of tuples: (podcast_id, feed_url, title)
    """
    tree = ET.parse(opml_path)
    root = tree.getroot()

    podcasts = []

    # Find all outline elements with xmlUrl attribute
    for outline in root.iter("outline"):
        xml_url = outline.get("xmlUrl")
        if xml_url:
            try:
                podcast_id, feed_url = xiaoyuzhou_url_to_feed(xml_url)
                title = outline.get("title") or outline.get("text")
                podcasts.append((podcast_id, feed_url, title))
            except ValueError:
                # Skip non-xiaoyuzhou feeds
                continue

    return podcasts


def fetch_podcast_info(feed_url: str) -> Podcast:
    """
    Fetch podcast metadata from RSS feed.

    Args:
        feed_url: RSS feed URL

    Returns:
        Podcast object with metadata
    """
    feed = feedparser.parse(feed_url)

    # Extract podcast ID from feed URL
    _, podcast_id = xiaoyuzhou_url_to_feed(feed_url)

    return Podcast(
        id=podcast_id,
        title=feed.feed.get("title", "Unknown"),
        feed_url=feed_url,
        description=feed.feed.get("description") or feed.feed.get("subtitle"),
        author=feed.feed.get("author"),
        image_url=feed.feed.get("image", {}).get("href"),
    )


def fetch_episodes(feed_url: str, podcast_id: str) -> list[Episode]:
    """
    Fetch episode list from RSS feed.

    Args:
        feed_url: RSS feed URL
        podcast_id: ID of the podcast

    Returns:
        List of Episode objects
    """
    feed = feedparser.parse(feed_url)
    episodes = []

    for entry in feed.entries:
        # Get episode ID from guid or link
        episode_id = entry.get("id") or entry.get("guid") or entry.get("link", "")
        # Clean up the ID to be a safe filename
        episode_id = re.sub(r"[^a-zA-Z0-9_-]", "_", episode_id)[-50:]

        # Get audio URL from enclosure
        audio_url = None
        for enclosure in entry.get("enclosures", []):
            if enclosure.get("type", "").startswith("audio/"):
                audio_url = enclosure.get("href") or enclosure.get("url")
                break

        # Also check links
        if not audio_url:
            for link in entry.get("links", []):
                if link.get("type", "").startswith("audio/"):
                    audio_url = link.get("href")
                    break

        if not audio_url:
            # Skip entries without audio
            continue

        # Parse published date
        published_at = None
        if pub_date := entry.get("published"):
            try:
                published_at = parsedate_to_datetime(pub_date)
            except (ValueError, TypeError):
                pass

        # Parse duration
        duration_seconds = None
        if duration := entry.get("itunes_duration"):
            duration_seconds = parse_duration(duration)

        episodes.append(
            Episode(
                id=episode_id,
                podcast_id=podcast_id,
                title=entry.get("title", "Untitled"),
                description=entry.get("summary") or entry.get("description"),
                audio_url=audio_url,
                duration_seconds=duration_seconds,
                published_at=published_at,
            )
        )

    return episodes


def parse_duration(duration_str: str) -> int | None:
    """
    Parse iTunes duration string to seconds.

    Formats: "HH:MM:SS", "MM:SS", or just seconds
    """
    if not duration_str:
        return None

    try:
        # Try as integer seconds first
        return int(duration_str)
    except ValueError:
        pass

    # Try HH:MM:SS or MM:SS format
    parts = duration_str.split(":")
    try:
        if len(parts) == 3:
            hours, minutes, seconds = map(int, parts)
            return hours * 3600 + minutes * 60 + seconds
        elif len(parts) == 2:
            minutes, seconds = map(int, parts)
            return minutes * 60 + seconds
    except ValueError:
        pass

    return None


async def fetch_feed_async(feed_url: str) -> feedparser.FeedParserDict:
    """Fetch and parse RSS feed asynchronously."""
    async with httpx.AsyncClient() as client:
        response = await client.get(feed_url, follow_redirects=True)
        response.raise_for_status()
        return feedparser.parse(response.text)


def _extract_next_data(html: str) -> dict:
    """Extract __NEXT_DATA__ JSON from Xiaoyuzhou page HTML."""
    match = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html,
    )
    if not match:
        raise ValueError("Could not find __NEXT_DATA__ in page")
    return json.loads(match.group(1))


def _episode_from_web(ep_data: dict, podcast_id: str) -> Episode:
    """Convert a web __NEXT_DATA__ episode dict to an Episode dataclass."""
    eid = ep_data["eid"]
    enclosure = ep_data.get("enclosure", {})
    media = ep_data.get("media", {})
    audio_url = (
        enclosure.get("url")
        or media.get("source", {}).get("url")
        or ""
    )

    published_at = None
    if pub_date := ep_data.get("pubDate"):
        try:
            published_at = datetime.fromisoformat(
                pub_date.replace("Z", "+00:00")
            )
        except (ValueError, TypeError):
            pass

    return Episode(
        id=eid,
        podcast_id=podcast_id,
        title=ep_data.get("title", "Untitled"),
        description=ep_data.get("description"),
        audio_url=audio_url,
        duration_seconds=ep_data.get("duration"),
        published_at=published_at,
    )


def _podcast_from_web(podcast_data: dict) -> Podcast:
    """Convert a web __NEXT_DATA__ podcast dict to a Podcast dataclass."""
    pid = podcast_data["pid"]
    return Podcast(
        id=pid,
        title=podcast_data.get("title", "Unknown"),
        feed_url=f"https://www.xiaoyuzhoufm.com/podcast/{pid}",
        description=podcast_data.get("description"),
        author=podcast_data.get("author"),
        image_url=podcast_data.get("image", {}).get("smallPicUrl"),
    )


def scrape_episode_page(episode_url: str) -> tuple[Podcast, Episode]:
    """Fetch an episode page and extract podcast + episode data from __NEXT_DATA__."""
    response = httpx.get(episode_url, headers=BROWSER_HEADERS, follow_redirects=True)
    response.raise_for_status()

    data = _extract_next_data(response.text)
    ep_data = data["props"]["pageProps"]["episode"]
    podcast_data = ep_data.get("podcast", {})

    podcast = _podcast_from_web(podcast_data)
    episode = _episode_from_web(ep_data, podcast.id)
    return podcast, episode


def scrape_podcast_page(podcast_url: str) -> tuple[Podcast, list[Episode]]:
    """Fetch a podcast page and extract podcast + episodes from __NEXT_DATA__."""
    response = httpx.get(podcast_url, headers=BROWSER_HEADERS, follow_redirects=True)
    response.raise_for_status()

    data = _extract_next_data(response.text)
    podcast_data = data["props"]["pageProps"]["podcast"]

    podcast = _podcast_from_web(podcast_data)
    episodes = [
        _episode_from_web(ep, podcast.id)
        for ep in podcast_data.get("episodes", [])
        if ep.get("enclosure", {}).get("url") or ep.get("media", {}).get("source", {}).get("url")
    ]
    return podcast, episodes


def search_xiaoyuzhou(query: str) -> list[dict]:
    """Search for Xiaoyuzhou episodes/podcasts via DuckDuckGo.

    Returns a list of dicts with 'url' and 'title' keys for matching
    xiaoyuzhoufm.com pages.
    """
    from ddgs import DDGS

    results = []
    with DDGS() as ddgs:
        for r in ddgs.text(
            f"site:xiaoyuzhoufm.com {query}",
            max_results=10,
        ):
            url = r.get("href", "")
            if "xiaoyuzhoufm.com" in url:
                results.append({"url": url, "title": r.get("title", "")})
    return results
