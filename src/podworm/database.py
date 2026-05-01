"""SQLite database for storing podcast and episode metadata."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import sqlite_utils

from podworm.config import get_db_path, ensure_dirs


@dataclass
class Podcast:
    """Podcast metadata."""

    id: str
    title: str
    feed_url: str
    description: str | None = None
    author: str | None = None
    image_url: str | None = None
    added_at: datetime | None = None


@dataclass
class Episode:
    """Episode metadata."""

    id: str
    podcast_id: str
    title: str
    audio_url: str
    description: str | None = None
    duration_seconds: int | None = None
    published_at: datetime | None = None
    downloaded_at: datetime | None = None
    transcribed_at: datetime | None = None
    audio_path: str | None = None
    transcript_path: str | None = None
    digest_path: str | None = None
    digested_at: datetime | None = None


SCHEMA = """
-- Podcasts table
CREATE TABLE IF NOT EXISTS podcasts (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    feed_url TEXT NOT NULL,
    description TEXT,
    author TEXT,
    image_url TEXT,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Episodes table
CREATE TABLE IF NOT EXISTS episodes (
    id TEXT PRIMARY KEY,
    podcast_id TEXT NOT NULL REFERENCES podcasts(id),
    title TEXT NOT NULL,
    description TEXT,
    audio_url TEXT NOT NULL,
    duration_seconds INTEGER,
    published_at TIMESTAMP,
    downloaded_at TIMESTAMP,
    transcribed_at TIMESTAMP,
    audio_path TEXT,
    transcript_path TEXT
);

-- Metadata table for app state (e.g., last_auto_run)
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_episodes_podcast ON episodes(podcast_id);
CREATE INDEX IF NOT EXISTS idx_episodes_downloaded ON episodes(downloaded_at);
CREATE INDEX IF NOT EXISTS idx_episodes_transcribed ON episodes(transcribed_at);
"""


class Database:
    """Database operations for podworm."""

    def __init__(self, db_path: Path | None = None):
        """Initialize database connection."""
        ensure_dirs()
        self.db_path = db_path or get_db_path()
        self.db = sqlite_utils.Database(self.db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        """Initialize database schema."""
        for statement in SCHEMA.split(";"):
            statement = statement.strip()
            if statement:
                self.db.execute(statement)
        self._ensure_digest_columns()

    def _ensure_digest_columns(self) -> None:
        """Add digest columns if they don't exist (migration)."""
        for col in ("digest_path TEXT", "digested_at TIMESTAMP"):
            try:
                self.db.execute(f"ALTER TABLE episodes ADD COLUMN {col}")
            except Exception:
                pass

    # Podcast operations

    def add_podcast(self, podcast: Podcast) -> None:
        """Add or update a podcast."""
        self.db["podcasts"].insert(
            {
                "id": podcast.id,
                "title": podcast.title,
                "feed_url": podcast.feed_url,
                "description": podcast.description,
                "author": podcast.author,
                "image_url": podcast.image_url,
                "added_at": podcast.added_at or datetime.now().isoformat(),
            },
            replace=True,
        )

    def get_podcast(self, podcast_id: str) -> Podcast | None:
        """Get a podcast by ID."""
        try:
            row = self.db["podcasts"].get(podcast_id)
            return Podcast(
                id=row["id"],
                title=row["title"],
                feed_url=row["feed_url"],
                description=row["description"],
                author=row["author"],
                image_url=row["image_url"],
                added_at=row["added_at"],
            )
        except sqlite_utils.db.NotFoundError:
            return None

    def list_podcasts(self) -> list[Podcast]:
        """List all podcasts."""
        return [
            Podcast(
                id=row["id"],
                title=row["title"],
                feed_url=row["feed_url"],
                description=row["description"],
                author=row["author"],
                image_url=row["image_url"],
                added_at=row["added_at"],
            )
            for row in self.db["podcasts"].rows
        ]

    def delete_podcast(self, podcast_id: str) -> None:
        """Delete a podcast and its episodes."""
        self.db["episodes"].delete_where("podcast_id = ?", [podcast_id])
        self.db["podcasts"].delete_where("id = ?", [podcast_id])

    # Episode operations

    def add_episode(self, episode: Episode) -> None:
        """Add or update an episode."""
        self.db["episodes"].insert(
            {
                "id": episode.id,
                "podcast_id": episode.podcast_id,
                "title": episode.title,
                "description": episode.description,
                "audio_url": episode.audio_url,
                "duration_seconds": episode.duration_seconds,
                "published_at": (
                    episode.published_at.isoformat()
                    if episode.published_at
                    else None
                ),
                "downloaded_at": (
                    episode.downloaded_at.isoformat()
                    if episode.downloaded_at
                    else None
                ),
                "transcribed_at": (
                    episode.transcribed_at.isoformat()
                    if episode.transcribed_at
                    else None
                ),
                "audio_path": episode.audio_path,
                "transcript_path": episode.transcript_path,
                "digest_path": episode.digest_path,
                "digested_at": (
                    episode.digested_at.isoformat()
                    if episode.digested_at
                    else None
                ),
            },
            replace=True,
        )

    def get_episode(self, episode_id: str) -> Episode | None:
        """Get an episode by ID."""
        try:
            row = self.db["episodes"].get(episode_id)
            return self._row_to_episode(row)
        except sqlite_utils.db.NotFoundError:
            return None

    def list_episodes(self, podcast_id: str | None = None) -> list[Episode]:
        """List episodes, optionally filtered by podcast."""
        if podcast_id:
            rows = self.db["episodes"].rows_where(
                "podcast_id = ?", [podcast_id], order_by="-published_at"
            )
        else:
            rows = self.db["episodes"].rows_where(order_by="-published_at")
        return [self._row_to_episode(row) for row in rows]

    def list_episodes_to_download(self, limit: int | None = None) -> list[Episode]:
        """List episodes that haven't been downloaded yet."""
        query = "downloaded_at IS NULL"
        rows = self.db["episodes"].rows_where(
            query, order_by="-published_at", limit=limit
        )
        return [self._row_to_episode(row) for row in rows]

    def list_episodes_to_transcribe(self, limit: int | None = None) -> list[Episode]:
        """List episodes that have been downloaded but not transcribed."""
        query = "downloaded_at IS NOT NULL AND transcribed_at IS NULL"
        rows = self.db["episodes"].rows_where(
            query, order_by="-published_at", limit=limit
        )
        return [self._row_to_episode(row) for row in rows]

    def mark_episode_downloaded(
        self, episode_id: str, audio_path: str
    ) -> None:
        """Mark an episode as downloaded."""
        self.db["episodes"].update(
            episode_id,
            {"downloaded_at": datetime.now().isoformat(), "audio_path": audio_path},
        )

    def mark_episode_transcribed(
        self, episode_id: str, transcript_path: str
    ) -> None:
        """Mark an episode as transcribed."""
        self.db["episodes"].update(
            episode_id,
            {
                "transcribed_at": datetime.now().isoformat(),
                "transcript_path": transcript_path,
            },
        )

    def list_episodes_to_digest(self, limit: int | None = None) -> list[Episode]:
        """List episodes that have been transcribed but not digested."""
        query = "transcribed_at IS NOT NULL AND digested_at IS NULL"
        rows = self.db["episodes"].rows_where(
            query, order_by="-published_at", limit=limit
        )
        return [self._row_to_episode(row) for row in rows]

    def mark_episode_digested(
        self, episode_id: str, digest_path: str
    ) -> None:
        """Mark an episode as digested."""
        self.db["episodes"].update(
            episode_id,
            {
                "digested_at": datetime.now().isoformat(),
                "digest_path": digest_path,
            },
        )

    def list_episodes_to_clean(self, podcast_id: str | None = None) -> list[Episode]:
        """List transcribed episodes that still have audio files on disk."""
        query = "transcribed_at IS NOT NULL AND audio_path IS NOT NULL"
        params = []
        if podcast_id:
            query += " AND podcast_id = ?"
            params.append(podcast_id)
        rows = self.db["episodes"].rows_where(query, params, order_by="-published_at")
        return [self._row_to_episode(row) for row in rows]

    def clear_audio_path(self, episode_id: str) -> None:
        """Clear the audio_path for an episode (after deleting the file)."""
        self.db["episodes"].update(episode_id, {"audio_path": None})

    def _row_to_episode(self, row: dict) -> Episode:
        """Convert a database row to an Episode object."""
        return Episode(
            id=row["id"],
            podcast_id=row["podcast_id"],
            title=row["title"],
            description=row["description"],
            audio_url=row["audio_url"],
            duration_seconds=row["duration_seconds"],
            published_at=(
                datetime.fromisoformat(row["published_at"])
                if row["published_at"]
                else None
            ),
            downloaded_at=(
                datetime.fromisoformat(row["downloaded_at"])
                if row["downloaded_at"]
                else None
            ),
            transcribed_at=(
                datetime.fromisoformat(row["transcribed_at"])
                if row["transcribed_at"]
                else None
            ),
            audio_path=row["audio_path"],
            transcript_path=row["transcript_path"],
            digest_path=row.get("digest_path"),
            digested_at=(
                datetime.fromisoformat(row["digested_at"])
                if row.get("digested_at")
                else None
            ),
        )

    # Episode counts

    def count_episodes(self, podcast_id: str) -> int:
        """Count total episodes for a podcast."""
        return self.db.execute(
            "SELECT COUNT(*) FROM episodes WHERE podcast_id = ?", [podcast_id]
        ).fetchone()[0]

    def count_transcribed(self, podcast_id: str) -> int:
        """Count transcribed episodes for a podcast."""
        return self.db.execute(
            "SELECT COUNT(*) FROM episodes WHERE podcast_id = ? AND transcribed_at IS NOT NULL",
            [podcast_id],
        ).fetchone()[0]

    # Metadata operations

    def get_metadata(self, key: str) -> str | None:
        """Get a metadata value."""
        result = self.db.execute(
            "SELECT value FROM metadata WHERE key = ?", [key]
        ).fetchone()
        return result[0] if result else None

    def set_metadata(self, key: str, value: str) -> None:
        """Set a metadata value."""
        self.db["metadata"].insert({"key": key, "value": value}, replace=True)

    def get_last_auto_run(self) -> datetime | None:
        """Get the timestamp of the last auto run."""
        value = self.get_metadata("last_auto_run")
        return datetime.fromisoformat(value) if value else None

    def set_last_auto_run(self, timestamp: datetime | None = None) -> None:
        """Set the timestamp of the last auto run."""
        self.set_metadata("last_auto_run", (timestamp or datetime.now()).isoformat())
