"""Audio file downloader with progress tracking."""

import asyncio
from pathlib import Path

import httpx
from rich.progress import (
    Progress,
    TaskID,
    TextColumn,
    BarColumn,
    DownloadColumn,
    TransferSpeedColumn,
    TimeRemainingColumn,
)

from podworm.config import get_audio_dir
from podworm.database import Episode


async def download_episode(
    episode: Episode,
    output_dir: Path | None = None,
    progress: Progress | None = None,
    task_id: TaskID | None = None,
) -> Path:
    """
    Download episode audio file.

    Args:
        episode: Episode to download
        output_dir: Directory to save audio (default: data/audio)
        progress: Rich Progress instance for progress tracking
        task_id: Rich task ID for progress updates

    Returns:
        Path to the downloaded file
    """
    output_dir = output_dir or get_audio_dir()

    # Create podcast directory
    podcast_dir = output_dir / episode.podcast_id
    podcast_dir.mkdir(parents=True, exist_ok=True)

    # Determine file extension from URL
    ext = ".mp3"
    if "." in episode.audio_url.split("/")[-1]:
        url_ext = "." + episode.audio_url.split(".")[-1].split("?")[0]
        if url_ext in [".mp3", ".m4a", ".wav", ".ogg", ".aac"]:
            ext = url_ext

    output_path = podcast_dir / f"{episode.id}{ext}"

    # Check if already downloaded
    if output_path.exists():
        return output_path

    # Check for partial download
    partial_path = output_path.with_suffix(output_path.suffix + ".partial")

    async with httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(connect=30.0, read=300.0, write=30.0, pool=30.0),
    ) as client:
        # Get file size
        head_response = await client.head(episode.audio_url)
        total_size = int(head_response.headers.get("content-length", 0))

        # Resume support
        start_byte = 0
        if partial_path.exists():
            start_byte = partial_path.stat().st_size

        headers = {}
        if start_byte > 0 and total_size > 0:
            headers["Range"] = f"bytes={start_byte}-"

        # Download
        async with client.stream("GET", episode.audio_url, headers=headers) as response:
            response.raise_for_status()

            # Update progress bar total
            if progress and task_id is not None:
                progress.update(task_id, total=total_size)
                if start_byte > 0:
                    progress.update(task_id, completed=start_byte)

            mode = "ab" if start_byte > 0 else "wb"
            with open(partial_path, mode) as f:
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    f.write(chunk)
                    if progress and task_id is not None:
                        progress.update(task_id, advance=len(chunk))

    # Rename partial to final
    partial_path.rename(output_path)
    return output_path


async def download_episodes(
    episodes: list[Episode],
    output_dir: Path | None = None,
    max_concurrent: int = 3,
) -> list[tuple[Episode, Path | None, str | None]]:
    """
    Download multiple episodes concurrently with progress tracking.

    Args:
        episodes: List of episodes to download
        output_dir: Directory to save audio
        max_concurrent: Maximum concurrent downloads

    Returns:
        List of tuples: (episode, path_if_success, error_if_failed)
    """
    results: list[tuple[Episode, Path | None, str | None]] = []

    if not episodes:
        return results

    with Progress(
        TextColumn("[bold blue]{task.description}"),
        BarColumn(),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
    ) as progress:
        # Create overall progress task
        overall_task = progress.add_task(
            f"[green]Downloading {len(episodes)} episodes...", total=len(episodes)
        )

        semaphore = asyncio.Semaphore(max_concurrent)

        async def download_with_progress(episode: Episode) -> tuple[Episode, Path | None, str | None]:
            async with semaphore:
                task_id = progress.add_task(
                    f"[cyan]{episode.title[:40]}...",
                    total=None,  # Unknown until we get headers
                )
                try:
                    path = await download_episode(
                        episode, output_dir, progress, task_id
                    )
                    progress.update(overall_task, advance=1)
                    progress.remove_task(task_id)
                    return (episode, path, None)
                except Exception as e:
                    progress.update(overall_task, advance=1)
                    progress.remove_task(task_id)
                    return (episode, None, str(e))

        tasks = [download_with_progress(ep) for ep in episodes]
        results = await asyncio.gather(*tasks)

    return results


def download_episodes_sync(
    episodes: list[Episode],
    output_dir: Path | None = None,
    max_concurrent: int = 3,
) -> list[tuple[Episode, Path | None, str | None]]:
    """Synchronous wrapper for download_episodes."""
    return asyncio.run(download_episodes(episodes, output_dir, max_concurrent))
