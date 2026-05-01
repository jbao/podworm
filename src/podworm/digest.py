"""Digest file generation — saves transcripts with metadata for Claude Code to summarize."""

from pathlib import Path

from rich.progress import Progress, SpinnerColumn, TextColumn

from podworm.config import get_transcripts_dir
from podworm.database import Episode, Podcast


def save_digest(
    episode: Episode,
    podcast: Podcast,
    digest_text: str,
    output_dir: Path | None = None,
) -> Path:
    """Save digest as a markdown file."""
    output_dir = output_dir or get_transcripts_dir()

    podcast_dir = output_dir / podcast.id
    podcast_dir.mkdir(parents=True, exist_ok=True)

    output_path = podcast_dir / f"{episode.id}_digest.md"

    # Format published date
    pub_date = ""
    if episode.published_at:
        pub_date = episode.published_at.strftime("%Y-%m-%d")

    lines = [
        f"# Digest: {episode.title}",
        "",
        f"**Podcast:** {podcast.title}",
    ]
    if pub_date:
        lines.append(f"**Date:** {pub_date}")
    lines.extend([
        "",
        "---",
        "",
        digest_text,
    ])

    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def digest_episodes(
    episodes: list[tuple[Episode, Podcast]],
) -> list[tuple[Episode, Path | None, str | None]]:
    """
    Generate digests for multiple episodes with progress tracking.

    Args:
        episodes: List of (episode, podcast) tuples

    Returns:
        List of (episode, path_if_success, error_if_failed) tuples
    """
    results = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
    ) as progress:
        task = progress.add_task(
            f"[green]Digesting {len(episodes)} episodes...", total=len(episodes)
        )

        for episode, podcast in episodes:
            progress.update(task, description=f"[cyan]Digesting: {episode.title[:40]}...")
            try:
                if not episode.transcript_path:
                    raise ValueError("Episode has no transcript")

                transcript_path = Path(episode.transcript_path)
                if not transcript_path.exists():
                    raise FileNotFoundError(f"Transcript not found: {transcript_path}")

                transcript_text = transcript_path.read_text(encoding="utf-8")
                path = save_digest(episode, podcast, transcript_text)
                results.append((episode, path, None))
            except Exception as e:
                results.append((episode, None, str(e)))
            progress.update(task, advance=1)

    return results
