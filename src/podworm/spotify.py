"""Spotify integration: auth, saved episodes, RSS resolution, episode matching."""

import hashlib
import time
from dataclasses import dataclass
from datetime import datetime, date
from difflib import SequenceMatcher
from functools import lru_cache

import feedparser
import httpx
import spotipy
from spotipy.oauth2 import SpotifyOAuth

from podworm.config import (
    get_spotify_client_id,
    get_spotify_client_secret,
    get_spotify_cache_path,
    ensure_dirs,
)
from podworm.database import Database, Podcast, Episode


SPOTIFY_REDIRECT_URI = "http://127.0.0.1:8888/callback"
SPOTIFY_SCOPE = "user-library-read"


# --- Authentication ---


def _get_auth_manager() -> SpotifyOAuth:
    """Create a SpotifyOAuth manager with cached tokens."""
    ensure_dirs()
    return SpotifyOAuth(
        client_id=get_spotify_client_id(),
        client_secret=get_spotify_client_secret(),
        redirect_uri=SPOTIFY_REDIRECT_URI,
        scope=SPOTIFY_SCOPE,
        cache_path=str(get_spotify_cache_path()),
    )


def get_spotify_client() -> spotipy.Spotify:
    """Create an authenticated Spotify client using cached token."""
    return spotipy.Spotify(auth_manager=_get_auth_manager())


def spotify_login() -> spotipy.Spotify:
    """Run interactive OAuth2 flow (opens browser) and return authenticated client."""
    auth_manager = _get_auth_manager()
    # Force a fresh login by getting a new token
    auth_manager.get_access_token(as_dict=False)
    return spotipy.Spotify(auth_manager=auth_manager)


# --- Spotify episode data ---


@dataclass
class SpotifyEpisode:
    """A podcast episode from the user's Spotify saved episodes."""

    spotify_id: str
    title: str
    show_name: str
    show_spotify_id: str
    description: str | None
    duration_ms: int
    release_date: str
    added_at: datetime


def fetch_saved_episodes(
    sp: spotipy.Spotify,
    limit: int = 20,
    date_filter: date | None = None,
) -> list[SpotifyEpisode]:
    """Fetch saved episodes from Spotify, optionally filtered by added_at date.

    Results are returned newest-first. If date_filter is given, pagination
    stops early once we pass the target date.
    """
    episodes: list[SpotifyEpisode] = []
    offset = 0
    page_size = min(limit, 50)

    while len(episodes) < limit:
        results = sp.current_user_saved_episodes(limit=page_size, offset=offset)
        items = results.get("items", [])
        if not items:
            break

        for item in items:
            added_at = datetime.fromisoformat(
                item["added_at"].replace("Z", "+00:00")
            )
            ep = item["episode"]

            if date_filter:
                if added_at.date() > date_filter:
                    # Haven't reached the target date yet, keep going
                    continue
                if added_at.date() < date_filter:
                    # Past the target date, stop pagination
                    return episodes

            episodes.append(
                SpotifyEpisode(
                    spotify_id=ep["id"],
                    title=ep["name"],
                    show_name=ep["show"]["name"],
                    show_spotify_id=ep["show"]["id"],
                    description=ep.get("description"),
                    duration_ms=ep["duration_ms"],
                    release_date=ep.get("release_date", ""),
                    added_at=added_at,
                )
            )

            if len(episodes) >= limit:
                break

        offset += page_size
        if not results.get("next"):
            break

    return episodes


# --- RSS feed resolution ---


@lru_cache(maxsize=256)
def find_rss_feed_for_show(show_name: str) -> str | None:
    """Look up the RSS feed URL for a podcast via the iTunes Search API.

    Returns the feedUrl if a match with similarity >= 0.7 is found, else None.
    """
    time.sleep(1)  # Rate-limit iTunes API calls

    try:
        response = httpx.get(
            "https://itunes.apple.com/search",
            params={"term": show_name, "media": "podcast", "limit": "5"},
            timeout=10,
        )
        response.raise_for_status()
        data = response.json()
    except (httpx.HTTPError, ValueError):
        return None

    best_ratio = 0.0
    best_feed = None

    for result in data.get("results", []):
        name = result.get("collectionName", "")
        ratio = SequenceMatcher(None, show_name.lower(), name.lower()).ratio()
        feed_url = result.get("feedUrl")
        if feed_url and ratio > best_ratio:
            best_ratio = ratio
            best_feed = feed_url

    return best_feed if best_ratio >= 0.7 else None


# --- Episode matching ---


def _title_similarity(a: str, b: str) -> float:
    """Compute title similarity ratio."""
    return SequenceMatcher(None, a.lower(), b.lower()).ratio()


def _date_proximity_score(sp_date_str: str, rss_date: datetime | None) -> float:
    """Score date proximity (1.0 = same day, decays over days)."""
    if not rss_date or not sp_date_str:
        return 0.0

    try:
        # Spotify release_date can be YYYY, YYYY-MM, or YYYY-MM-DD
        parts = sp_date_str.split("-")
        if len(parts) == 3:
            sp_date = date(int(parts[0]), int(parts[1]), int(parts[2]))
        elif len(parts) == 2:
            sp_date = date(int(parts[0]), int(parts[1]), 1)
        else:
            sp_date = date(int(parts[0]), 1, 1)
    except (ValueError, IndexError):
        return 0.0

    rss_d = rss_date.date() if isinstance(rss_date, datetime) else rss_date
    diff_days = abs((sp_date - rss_d).days)

    if diff_days == 0:
        return 1.0
    elif diff_days <= 3:
        return 0.8
    elif diff_days <= 7:
        return 0.5
    elif diff_days <= 30:
        return 0.2
    return 0.0


@dataclass
class MatchResult:
    """Result of matching a Spotify episode to an RSS episode."""

    spotify_episode: SpotifyEpisode
    rss_episode: Episode | None
    podcast: Podcast | None
    score: float
    matched: bool


def match_episode_to_rss(
    sp_ep: SpotifyEpisode,
    rss_episodes: list[Episode],
) -> tuple[Episode | None, float]:
    """Match a Spotify episode to the best RSS episode.

    Uses title similarity (0.7 weight) + date proximity (0.3 weight).
    Returns (matched_episode, score). Episode is None if score < 0.5.
    """
    best_ep = None
    best_score = 0.0

    for rss_ep in rss_episodes:
        title_score = _title_similarity(sp_ep.title, rss_ep.title)
        date_score = _date_proximity_score(sp_ep.release_date, rss_ep.published_at)
        score = 0.7 * title_score + 0.3 * date_score

        if score > best_score:
            best_score = score
            best_ep = rss_ep

    if best_score >= 0.5:
        return best_ep, best_score
    return None, best_score


def _podcast_id_from_feed_url(feed_url: str) -> str:
    """Generate a podcast ID from a feed URL (SHA256 prefix to avoid conflicts)."""
    return hashlib.sha256(feed_url.encode()).hexdigest()[:16]


def resolve_spotify_episodes(
    spotify_episodes: list[SpotifyEpisode],
) -> list[MatchResult]:
    """Resolve Spotify episodes to RSS episodes.

    Groups episodes by show, looks up RSS feeds, and matches episodes.
    """
    from podworm.feed_parser import fetch_episodes as fetch_rss_episodes

    # Group by show
    shows: dict[str, list[SpotifyEpisode]] = {}
    for ep in spotify_episodes:
        shows.setdefault(ep.show_spotify_id, []).append(ep)

    results: list[MatchResult] = []

    for show_id, eps in shows.items():
        show_name = eps[0].show_name

        # Find RSS feed
        feed_url = find_rss_feed_for_show(show_name)
        if not feed_url:
            # Spotify exclusive or no matching feed
            for ep in eps:
                results.append(
                    MatchResult(
                        spotify_episode=ep,
                        rss_episode=None,
                        podcast=None,
                        score=0.0,
                        matched=False,
                    )
                )
            continue

        # Build podcast object
        podcast_id = _podcast_id_from_feed_url(feed_url)
        podcast = Podcast(
            id=podcast_id,
            title=show_name,
            feed_url=feed_url,
        )

        # Fetch RSS episodes
        try:
            rss_episodes = fetch_rss_episodes(feed_url, podcast_id)
        except Exception:
            for ep in eps:
                results.append(
                    MatchResult(
                        spotify_episode=ep,
                        rss_episode=None,
                        podcast=podcast,
                        score=0.0,
                        matched=False,
                    )
                )
            continue

        # Match each Spotify episode
        for sp_ep in eps:
            matched_ep, score = match_episode_to_rss(sp_ep, rss_episodes)
            results.append(
                MatchResult(
                    spotify_episode=sp_ep,
                    rss_episode=matched_ep,
                    podcast=podcast,
                    score=score,
                    matched=matched_ep is not None,
                )
            )

    return results


# --- Deduplication via metadata table ---


def record_spotify_mapping(db: Database, spotify_id: str, episode_id: str) -> None:
    """Record that a Spotify episode has been imported as a local episode."""
    db.set_metadata(f"spotify:{spotify_id}", episode_id)


def get_spotify_mapping(db: Database, spotify_id: str) -> str | None:
    """Check if a Spotify episode has already been imported. Returns episode_id or None."""
    return db.get_metadata(f"spotify:{spotify_id}")
