"""Transcription using Deepgram's Nova API."""

import subprocess
from pathlib import Path

from deepgram import DeepgramClient
from rich.progress import Progress, SpinnerColumn, TextColumn

from podworm.config import get_transcripts_dir, get_deepgram_api_key
from podworm.database import Episode, Podcast


def get_client() -> DeepgramClient:
    """Get Deepgram API client."""
    api_key = get_deepgram_api_key()
    if not api_key:
        raise ValueError(
            "DEEPGRAM_API_KEY environment variable not set. "
            "Get a key at https://console.deepgram.com"
        )
    return DeepgramClient(api_key=api_key, timeout=600)


def detect_language(text: str) -> str | None:
    """Heuristic language detection from text. Returns a Deepgram language code.

    None means "let Deepgram auto-detect". Counts Unicode ranges for the major
    scripts we encounter; thresholds are intentionally loose because podcast
    metadata routinely mixes scripts (English brand names in a Chinese show).
    """
    if not text:
        return None
    cjk = hira_kata = hangul = latin = 0
    for ch in text:
        cp = ord(ch)
        if 0x4E00 <= cp <= 0x9FFF:
            cjk += 1
        elif 0x3040 <= cp <= 0x30FF:
            hira_kata += 1
        elif 0xAC00 <= cp <= 0xD7AF:
            hangul += 1
        elif (0x41 <= cp <= 0x5A) or (0x61 <= cp <= 0x7A):
            latin += 1
    if hira_kata >= 10:
        return "ja"
    if hangul >= 10:
        return "ko"
    if cjk >= 20:
        return "zh"
    if latin >= 50:
        return "en"
    return None


def transcribe_audio(
    audio_path: Path,
    language: str | None = None,
) -> tuple[str, list[dict]]:
    """
    Transcribe audio file using Deepgram's Nova API.

    Args:
        audio_path: Path to the audio file
        language: ISO language code (e.g. "zh", "en"). If None, Deepgram
            auto-detects.

    Returns:
        Tuple of (full_text, segments) where segments have timestamps
    """
    client = get_client()

    with open(audio_path, "rb") as f:
        buffer_data = f.read()

    kwargs: dict = {
        "request": buffer_data,
        "model": "nova-2",
        "smart_format": True,
        "utterances": True,
    }
    if language:
        kwargs["language"] = language
    else:
        kwargs["detect_language"] = True

    response = client.listen.v1.media.transcribe_file(**kwargs)

    # Extract full text
    full_text = response.results.channels[0].alternatives[0].transcript

    # Extract segments from utterances
    segments = []
    if response.results.utterances:
        for utterance in response.results.utterances:
            segments.append({
                "start": utterance.start,
                "end": utterance.end,
                "text": utterance.transcript,
            })

    if not full_text.strip() and not segments:
        detected = getattr(
            response.results.channels[0], "detected_language", None
        )
        raise RuntimeError(
            "Deepgram returned an empty transcript "
            f"(language passed={language!r}, detected={detected!r}). "
            "Audio may be unsupported by nova-2 or the request silently failed."
        )

    return full_text, segments


def get_audio_duration(audio_path: Path) -> float | None:
    """Get audio duration in seconds using ffprobe."""
    try:
        cmd = [
            "ffprobe",
            "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(audio_path),
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        return float(result.stdout.strip())
    except (subprocess.CalledProcessError, ValueError):
        return None


def format_timestamp(seconds: float) -> str:
    """Format seconds as [HH:MM:SS] timestamp."""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    return f"[{hours:02d}:{minutes:02d}:{secs:02d}]"


def save_transcript(
    episode: Episode,
    podcast: Podcast,
    text: str,
    segments: list[dict],
    output_dir: Path | None = None,
) -> Path:
    """
    Save transcript as markdown file.

    Args:
        episode: Episode metadata
        podcast: Podcast metadata
        text: Full transcript text
        segments: List of segments with timestamps
        output_dir: Directory to save transcripts

    Returns:
        Path to the saved transcript file
    """
    output_dir = output_dir or get_transcripts_dir()

    # Create podcast directory
    podcast_dir = output_dir / podcast.id
    podcast_dir.mkdir(parents=True, exist_ok=True)

    output_path = podcast_dir / f"{episode.id}.md"

    # Format duration
    duration_str = ""
    if episode.duration_seconds:
        hours = episode.duration_seconds // 3600
        minutes = (episode.duration_seconds % 3600) // 60
        seconds = episode.duration_seconds % 60
        if hours:
            duration_str = f"{hours}:{minutes:02d}:{seconds:02d}"
        else:
            duration_str = f"{minutes}:{seconds:02d}"

    # Format published date
    pub_date = ""
    if episode.published_at:
        pub_date = episode.published_at.strftime("%Y-%m-%d")

    # Build markdown content
    lines = [
        f"# {episode.title}",
        "",
        f"**Podcast:** {podcast.title}",
    ]

    if pub_date:
        lines.append(f"**Date:** {pub_date}")
    if duration_str:
        lines.append(f"**Duration:** {duration_str}")
    if episode.audio_path:
        lines.append(f"**Audio:** {Path(episode.audio_path).name}")

    lines.extend([
        "",
        "---",
        "",
    ])

    if episode.description:
        lines.extend([
            "## Shownotes",
            "",
            episode.description,
            "",
        ])

    lines.extend([
        "## Transcript",
        "",
    ])

    # Add segments with timestamps
    if segments:
        for seg in segments:
            timestamp = format_timestamp(seg["start"])
            lines.append(f"{timestamp} {seg['text'].strip()}")
            lines.append("")
    else:
        # No segments, just add full text
        lines.append(text)

    # Write file
    output_path.write_text("\n".join(lines), encoding="utf-8")
    return output_path


def transcribe_episode(
    episode: Episode,
    podcast: Podcast,
) -> Path:
    """
    Transcribe an episode and save the transcript.

    Args:
        episode: Episode to transcribe (must have audio_path set)
        podcast: Podcast metadata

    Returns:
        Path to the saved transcript
    """
    if not episode.audio_path:
        raise ValueError(f"Episode {episode.id} has no audio_path")

    audio_path = Path(episode.audio_path)
    if not audio_path.exists():
        raise FileNotFoundError(f"Audio file not found: {audio_path}")

    metadata_text = " ".join(
        s for s in (episode.title, episode.description, podcast.title) if s
    )
    language = detect_language(metadata_text)

    text, segments = transcribe_audio(audio_path, language=language)

    return save_transcript(episode, podcast, text, segments)


def transcribe_episodes(
    episodes: list[tuple[Episode, Podcast]],
) -> list[tuple[Episode, Path | None, str | None]]:
    """
    Transcribe multiple episodes with progress tracking.

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
            f"[green]Transcribing {len(episodes)} episodes...", total=len(episodes)
        )

        for episode, podcast in episodes:
            progress.update(task, description=f"[cyan]Transcribing: {episode.title[:40]}...")
            try:
                path = transcribe_episode(episode, podcast)
                results.append((episode, path, None))
            except Exception as e:
                results.append((episode, None, str(e)))
            progress.update(task, advance=1)

    return results
