"""Command-line interface for podworm."""

import os
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

import click
from dotenv import load_dotenv
from rich.console import Console

load_dotenv()
from rich.table import Table

from podworm.config import get_interviews_dir, get_transcripts_dir
from podworm.database import Database, Podcast
from podworm.feed_parser import (
    parse_opml,
    fetch_podcast_info,
    fetch_episodes,
    xiaoyuzhou_url_to_feed,
    scrape_episode_page,
    scrape_podcast_page,
    search_xiaoyuzhou,
)
from podworm.digest import digest_episodes
from podworm.downloader import download_episodes_sync
from podworm.transcriber import transcribe_episodes

console = Console()


@click.group()
@click.version_option()
def cli():
    """小宇宙 Podcast Transcriber - Download and transcribe podcasts."""
    pass


@cli.command("import")
@click.argument("opml_path", type=click.Path(exists=True, path_type=Path))
def import_opml(opml_path: Path):
    """Import subscriptions from OPML file exported from 小宇宙 app."""
    db = Database()

    console.print(f"[bold]Parsing OPML file: {opml_path}[/bold]")
    podcasts = parse_opml(opml_path)

    if not podcasts:
        console.print("[yellow]No 小宇宙 podcasts found in OPML file.[/yellow]")
        return

    console.print(f"[green]Found {len(podcasts)} podcasts[/green]")

    for podcast_id, feed_url, title in podcasts:
        console.print(f"  Adding: {title or podcast_id}...")
        try:
            # Fetch full podcast info from RSS
            podcast = fetch_podcast_info(feed_url)
            db.add_podcast(podcast)

            # Fetch episodes
            episodes = fetch_episodes(feed_url, podcast_id)
            for episode in episodes:
                db.add_episode(episode)

            console.print(f"    [green]✓[/green] {podcast.title} ({len(episodes)} episodes)")
        except Exception as e:
            console.print(f"    [red]✗[/red] Failed: {e}")

    console.print("[bold green]Import complete![/bold green]")


@cli.command()
@click.argument("url")
def add(url: str):
    """Add a podcast by URL."""
    db = Database()

    try:
        podcast_id, feed_url = xiaoyuzhou_url_to_feed(url)
    except ValueError as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)

    console.print(f"[bold]Fetching podcast info...[/bold]")

    try:
        podcast = fetch_podcast_info(feed_url)
        db.add_podcast(podcast)

        episodes = fetch_episodes(feed_url, podcast_id)
        for episode in episodes:
            db.add_episode(episode)

        console.print(f"[green]✓[/green] Added: {podcast.title} ({len(episodes)} episodes)")
    except Exception as e:
        console.print(f"[red]Error:[/red] {e}")
        sys.exit(1)


@cli.command("list")
def list_podcasts():
    """List all subscribed podcasts."""
    db = Database()
    podcasts = db.list_podcasts()

    if not podcasts:
        console.print("[yellow]No podcasts found. Use 'podworm import' or 'podworm add' to add podcasts.[/yellow]")
        return

    table = Table(title="Subscribed Podcasts")
    table.add_column("ID", style="dim", max_width=15)
    table.add_column("Title", style="bold")
    table.add_column("Episodes", justify="right")
    table.add_column("Transcribed", justify="right")

    for podcast in podcasts:
        total = db.count_episodes(podcast.id)
        transcribed = db.count_transcribed(podcast.id)
        table.add_row(
            podcast.id[:12] + "...",
            podcast.title,
            str(total),
            str(transcribed),
        )

    console.print(table)


@cli.command()
@click.argument("podcast_id")
def episodes(podcast_id: str):
    """List episodes for a podcast."""
    db = Database()

    # Find podcast by ID prefix
    podcasts = db.list_podcasts()
    podcast = next(
        (p for p in podcasts if p.id.startswith(podcast_id)),
        None
    )

    if not podcast:
        console.print(f"[red]Podcast not found: {podcast_id}[/red]")
        sys.exit(1)

    eps = db.list_episodes(podcast.id)

    table = Table(title=f"Episodes: {podcast.title}")
    table.add_column("ID", style="dim", max_width=15)
    table.add_column("Title")
    table.add_column("Date", justify="right")
    table.add_column("Downloaded", justify="center")
    table.add_column("Transcribed", justify="center")

    for ep in eps:
        date_str = ep.published_at.strftime("%Y-%m-%d") if ep.published_at else ""
        downloaded = "[green]✓[/green]" if ep.downloaded_at else ""
        transcribed = "[green]✓[/green]" if ep.transcribed_at else ""
        table.add_row(
            ep.id[:12] + "...",
            ep.title[:50],
            date_str,
            downloaded,
            transcribed,
        )

    console.print(table)


@cli.command()
@click.option("--podcast", "-p", help="Only sync specific podcast ID")
@click.option("--limit", "-l", default=5, help="Max episodes to download per podcast")
def sync(podcast: str | None, limit: int):
    """Download new episodes."""
    db = Database()

    # First, refresh episode lists from RSS
    podcasts = db.list_podcasts()
    if podcast:
        podcasts = [p for p in podcasts if p.id.startswith(podcast)]

    console.print("[bold]Refreshing episode lists...[/bold]")
    for p in podcasts:
        try:
            episodes = fetch_episodes(p.feed_url, p.id)
            new_count = 0
            for ep in episodes:
                existing = db.get_episode(ep.id)
                if not existing:
                    db.add_episode(ep)
                    new_count += 1
            if new_count:
                console.print(f"  [green]✓[/green] {p.title}: {new_count} new episodes")
        except Exception as e:
            console.print(f"  [red]✗[/red] {p.title}: {e}")

    # Get episodes to download
    if podcast:
        # Filter by podcast
        all_to_download = db.list_episodes_to_download()
        podcasts_to_sync = [p for p in podcasts if p.id.startswith(podcast)]
        podcast_ids = {p.id for p in podcasts_to_sync}
        to_download = [e for e in all_to_download if e.podcast_id in podcast_ids][:limit]
    else:
        to_download = db.list_episodes_to_download(limit=limit)

    if not to_download:
        console.print("[yellow]No new episodes to download.[/yellow]")
        return

    console.print(f"\n[bold]Downloading {len(to_download)} episodes...[/bold]")
    results = download_episodes_sync(to_download)

    success_count = 0
    for episode, path, error in results:
        if path:
            db.mark_episode_downloaded(episode.id, str(path))
            success_count += 1
        else:
            console.print(f"  [red]✗[/red] {episode.title}: {error}")

    console.print(f"\n[green]Downloaded {success_count}/{len(to_download)} episodes.[/green]")


@cli.command()
@click.option("--podcast", "-p", help="Only transcribe specific podcast ID")
@click.option("--episode", "-e", help="Only transcribe specific episode ID")
@click.option("--limit", "-l", default=5, help="Max episodes to transcribe")
def transcribe(podcast: str | None, episode: str | None, limit: int):
    """Transcribe downloaded episodes."""
    db = Database()

    if episode:
        # Transcribe specific episode
        ep = db.get_episode(episode)
        if not ep:
            # Try prefix match
            all_eps = db.list_episodes()
            ep = next((e for e in all_eps if e.id.startswith(episode)), None)

        if not ep:
            console.print(f"[red]Episode not found: {episode}[/red]")
            sys.exit(1)

        if not ep.audio_path:
            console.print(f"[red]Episode not downloaded yet. Run 'podworm sync' first.[/red]")
            sys.exit(1)

        p = db.get_podcast(ep.podcast_id)
        to_transcribe = [(ep, p)]
    else:
        # Get episodes to transcribe
        all_to_transcribe = db.list_episodes_to_transcribe(limit=limit)

        if podcast:
            podcasts = db.list_podcasts()
            podcast_match = next(
                (p for p in podcasts if p.id.startswith(podcast)),
                None
            )
            if podcast_match:
                all_to_transcribe = [e for e in all_to_transcribe if e.podcast_id == podcast_match.id]

        to_transcribe = []
        for ep in all_to_transcribe:
            p = db.get_podcast(ep.podcast_id)
            if p:
                to_transcribe.append((ep, p))

    if not to_transcribe:
        console.print("[yellow]No episodes to transcribe. Download episodes first with 'podworm sync'.[/yellow]")
        return

    console.print(f"[bold]Transcribing {len(to_transcribe)} episodes...[/bold]")
    results = transcribe_episodes(to_transcribe)

    success_count = 0
    for ep, path, error in results:
        if path:
            db.mark_episode_transcribed(ep.id, str(path))
            success_count += 1
            console.print(f"  [green]✓[/green] {ep.title}")
        else:
            console.print(f"  [red]✗[/red] {ep.title}: {error}")

    console.print(f"\n[green]Transcribed {success_count}/{len(to_transcribe)} episodes.[/green]")


@cli.command()
@click.argument("episode_id")
def show(episode_id: str):
    """Show transcript for an episode."""
    db = Database()

    # Find episode
    all_eps = db.list_episodes()
    episode = next((e for e in all_eps if e.id.startswith(episode_id)), None)

    if not episode:
        console.print(f"[red]Episode not found: {episode_id}[/red]")
        sys.exit(1)

    if not episode.transcript_path:
        console.print(f"[yellow]Episode not transcribed yet. Run 'podworm transcribe' first.[/yellow]")
        sys.exit(1)

    transcript_path = Path(episode.transcript_path)
    if not transcript_path.exists():
        console.print(f"[red]Transcript file not found: {transcript_path}[/red]")
        sys.exit(1)

    # Print transcript
    console.print(transcript_path.read_text(encoding="utf-8"))


@cli.command("open")
@click.argument("episode_id")
def open_transcript(episode_id: str):
    """Open transcript in default editor."""
    db = Database()

    # Find episode
    all_eps = db.list_episodes()
    episode = next((e for e in all_eps if e.id.startswith(episode_id)), None)

    if not episode:
        console.print(f"[red]Episode not found: {episode_id}[/red]")
        sys.exit(1)

    if not episode.transcript_path:
        console.print(f"[yellow]Episode not transcribed yet. Run 'podworm transcribe' first.[/yellow]")
        sys.exit(1)

    transcript_path = Path(episode.transcript_path)
    if not transcript_path.exists():
        console.print(f"[red]Transcript file not found: {transcript_path}[/red]")
        sys.exit(1)

    # Open with default application
    subprocess.run(["open", str(transcript_path)])


@cli.command()
@click.argument("query")
def search(query: str):
    """Search across all transcripts."""
    transcripts_dir = get_transcripts_dir()

    if not transcripts_dir.exists():
        console.print("[yellow]No transcripts found.[/yellow]")
        return

    # Use grep to search
    result = subprocess.run(
        ["grep", "-r", "-i", "-l", query, str(transcripts_dir)],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0 or not result.stdout.strip():
        console.print(f"[yellow]No matches found for: {query}[/yellow]")
        return

    files = result.stdout.strip().split("\n")
    console.print(f"[bold]Found {len(files)} transcripts matching '{query}':[/bold]")

    for file_path in files:
        path = Path(file_path)
        # Show context
        context = subprocess.run(
            ["grep", "-i", "-m", "3", "-C", "1", query, file_path],
            capture_output=True,
            text=True,
        )
        console.print(f"\n[bold cyan]{path.name}[/bold cyan]")
        console.print(context.stdout[:500] + ("..." if len(context.stdout) > 500 else ""))


@cli.command()
@click.option("--limit", "-l", default=5, help="Max episodes to process")
@click.option("--force", "-f", is_flag=True, help="Bypass 24h cooldown")
def auto(limit: int, force: bool):
    """Automated sync and transcribe (for scheduled runs)."""
    db = Database()

    # Check cooldown
    last_run = db.get_last_auto_run()
    if not force and last_run:
        elapsed = datetime.now() - last_run
        if elapsed < timedelta(hours=24):
            hours_ago = elapsed.total_seconds() / 3600
            console.print(f"[yellow]Skipping: last run was {hours_ago:.1f} hours ago (< 24h cooldown)[/yellow]")
            console.print("[dim]Use --force to bypass cooldown[/dim]")
            return

    console.print("[bold]Starting automated sync and transcribe...[/bold]")
    console.print(f"[dim]Limit: {limit} episodes[/dim]\n")

    # Sync
    console.print("[bold]Step 1: Syncing episodes...[/bold]")
    podcasts = db.list_podcasts()

    for p in podcasts:
        try:
            episodes = fetch_episodes(p.feed_url, p.id)
            new_count = 0
            for ep in episodes:
                existing = db.get_episode(ep.id)
                if not existing:
                    db.add_episode(ep)
                    new_count += 1
            if new_count:
                console.print(f"  [green]✓[/green] {p.title}: {new_count} new")
        except Exception as e:
            console.print(f"  [red]✗[/red] {p.title}: {e}")

    # Download
    console.print("\n[bold]Step 2: Downloading episodes...[/bold]")
    to_download = db.list_episodes_to_download(limit=limit)

    if to_download:
        results = download_episodes_sync(to_download)
        for episode, path, error in results:
            if path:
                db.mark_episode_downloaded(episode.id, str(path))
                console.print(f"  [green]✓[/green] Downloaded: {episode.title[:40]}...")
            else:
                console.print(f"  [red]✗[/red] {episode.title[:40]}: {error}")
    else:
        console.print("  [dim]No new episodes to download[/dim]")

    # Transcribe
    console.print("\n[bold]Step 3: Transcribing episodes...[/bold]")
    to_transcribe_list = db.list_episodes_to_transcribe(limit=limit)

    if to_transcribe_list:
        to_transcribe = []
        for ep in to_transcribe_list:
            p = db.get_podcast(ep.podcast_id)
            if p:
                to_transcribe.append((ep, p))

        results = transcribe_episodes(to_transcribe)
        for ep, path, error in results:
            if path:
                db.mark_episode_transcribed(ep.id, str(path))
                console.print(f"  [green]✓[/green] Transcribed: {ep.title[:40]}...")
            else:
                console.print(f"  [red]✗[/red] {ep.title[:40]}: {error}")
    else:
        console.print("  [dim]No episodes to transcribe[/dim]")

    # Update last run time
    db.set_last_auto_run()

    console.print("\n[bold green]Auto run complete![/bold green]")


@cli.command()
@click.argument("podcast_name")
@click.argument("episode_keyword")
def grab(podcast_name: str, episode_keyword: str):
    """Search Xiaoyuzhou for an episode and download it.

    Searches by podcast name and episode keyword (e.g. episode number),
    then downloads the audio file.

    Examples:

        podworm grab 日谈公园 vol.400

        podworm grab "不开玩笑" "229"
    """
    db = Database()
    query = f"{podcast_name} {episode_keyword}"

    console.print(f"[bold]Searching for: {query}[/bold]")
    results = search_xiaoyuzhou(query)

    if not results:
        console.print("[red]No results found.[/red]")
        sys.exit(1)

    # Separate episode and podcast URLs from results
    episode_urls = [r for r in results if "/episode/" in r["url"]]
    podcast_urls = [r for r in results if "/podcast/" in r["url"]]

    podcast = None
    episode = None
    keyword_lower = episode_keyword.lower()

    # Try episode URLs — prefer ones whose title matches the keyword
    if episode_urls:
        # Sort: results with keyword in title come first
        episode_urls.sort(
            key=lambda r: keyword_lower not in r["title"].lower()
        )
        for result in episode_urls:
            console.print(f"  Checking: {result['title']}")
            try:
                p, ep = scrape_episode_page(result["url"])
                if keyword_lower in ep.title.lower():
                    podcast, episode = p, ep
                    break
            except Exception as e:
                console.print(f"  [dim]Failed to load: {e}[/dim]")
                continue

    # Fall back to podcast page + keyword search in latest episodes
    if not episode and podcast_urls:
        for result in podcast_urls:
            console.print(f"  Checking podcast: {result['title']}")
            try:
                p, eps = scrape_podcast_page(result["url"])
                for ep in eps:
                    if keyword_lower in ep.title.lower():
                        podcast, episode = p, ep
                        console.print(f"  Found: {ep.title}")
                        break
                if episode:
                    break
            except Exception as e:
                console.print(f"  [dim]Failed to load: {e}[/dim]")
                continue

    if not episode or not podcast:
        console.print("[red]Could not find the episode.[/red]")
        console.print("[dim]Try a more specific search term.[/dim]")
        sys.exit(1)

    if not episode.audio_url:
        console.print("[red]Episode has no audio URL.[/red]")
        sys.exit(1)

    console.print(f"\n[bold green]Episode:[/bold green] {episode.title}")
    console.print(f"[bold green]Podcast:[/bold green] {podcast.title}")
    console.print(f"[bold green]Audio:[/bold green] {episode.audio_url}")

    # Save to database
    db.add_podcast(podcast)
    db.add_episode(episode)

    # Download
    console.print(f"\n[bold]Downloading...[/bold]")
    results = download_episodes_sync([episode])

    for ep, path, error in results:
        if path:
            db.mark_episode_downloaded(ep.id, str(path))
            console.print(f"[green]Saved to: {path}[/green]")
        else:
            console.print(f"[red]Download failed: {error}[/red]")
            sys.exit(1)


@cli.command("spotify-login")
def spotify_login_cmd():
    """Authenticate with Spotify (opens browser for OAuth)."""
    from podworm.config import get_spotify_client_id, get_spotify_client_secret
    from podworm.spotify import spotify_login

    if not get_spotify_client_id() or not get_spotify_client_secret():
        console.print(
            "[red]Error:[/red] Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET environment variables.\n"
            "[dim]Create an app at https://developer.spotify.com/dashboard\n"
            "Set redirect URI to http://127.0.0.1:8888/callback[/dim]"
        )
        sys.exit(1)

    console.print("[bold]Opening browser for Spotify login...[/bold]")
    try:
        sp = spotify_login()
        user = sp.current_user()
        console.print(f"[green]Logged in as:[/green] {user['display_name']} ({user['id']})")
    except Exception as e:
        console.print(f"[red]Login failed:[/red] {e}")
        sys.exit(1)


@cli.command("spotify-import")
@click.option("--limit", "-l", default=20, help="Max episodes to fetch from Spotify")
@click.option("--date", "-d", "date_str", default=None, help="Only episodes saved on this date (YYYY-MM-DD)")
@click.option("--download/--no-download", default=True, help="Download audio after import")
@click.option("--dry-run", is_flag=True, help="Show matches without importing")
def spotify_import_cmd(limit: int, date_str: str | None, download: bool, dry_run: bool):
    """Import podcast episodes from Spotify saved episodes."""
    from datetime import date as date_type

    from podworm.config import get_spotify_client_id, get_spotify_client_secret
    from podworm.spotify import (
        get_spotify_client,
        fetch_saved_episodes,
        resolve_spotify_episodes,
        record_spotify_mapping,
        get_spotify_mapping,
    )

    if not get_spotify_client_id() or not get_spotify_client_secret():
        console.print(
            "[red]Error:[/red] Set SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET.\n"
            "[dim]Run 'podworm spotify-login' first.[/dim]"
        )
        sys.exit(1)

    # Parse date filter
    date_filter = None
    if date_str:
        try:
            date_filter = date_type.fromisoformat(date_str)
        except ValueError:
            console.print(f"[red]Invalid date format:[/red] {date_str} (expected YYYY-MM-DD)")
            sys.exit(1)

    # Authenticate
    try:
        sp = get_spotify_client()
        sp.current_user()  # verify auth works
    except Exception as e:
        console.print(f"[red]Spotify auth failed:[/red] {e}\n[dim]Run 'podworm spotify-login' first.[/dim]")
        sys.exit(1)

    # Fetch saved episodes
    console.print(f"[bold]Fetching saved episodes from Spotify (limit={limit})...[/bold]")
    spotify_eps = fetch_saved_episodes(sp, limit=limit, date_filter=date_filter)

    if not spotify_eps:
        console.print("[yellow]No saved episodes found.[/yellow]")
        return

    console.print(f"Found {len(spotify_eps)} episodes from {len({e.show_spotify_id for e in spotify_eps})} shows")

    # Resolve RSS feeds and match
    console.print("[bold]Resolving RSS feeds and matching episodes...[/bold]")
    match_results = resolve_spotify_episodes(spotify_eps)

    # Display results table
    table = Table(title="Spotify Episode Matches")
    table.add_column("Show", max_width=25)
    table.add_column("Episode", max_width=35)
    table.add_column("Score", justify="right")
    table.add_column("Status")

    db = Database()
    imported = 0
    skipped_exclusive = 0
    skipped_no_match = 0
    skipped_duplicate = 0
    to_download_eps = []

    for result in match_results:
        sp_ep = result.spotify_episode

        # Check dedup
        existing = get_spotify_mapping(db, sp_ep.spotify_id)
        if existing:
            table.add_row(
                sp_ep.show_name[:25],
                sp_ep.title[:35],
                "",
                "[dim]already imported[/dim]",
            )
            skipped_duplicate += 1
            continue

        if not result.matched:
            if result.podcast is None:
                status = "[yellow]no RSS feed (Spotify exclusive?)[/yellow]"
                skipped_exclusive += 1
            else:
                status = f"[yellow]no match (best: {result.score:.2f})[/yellow]"
                skipped_no_match += 1
            table.add_row(
                sp_ep.show_name[:25],
                sp_ep.title[:35],
                f"{result.score:.2f}",
                status,
            )
            continue

        table.add_row(
            sp_ep.show_name[:25],
            sp_ep.title[:35],
            f"{result.score:.2f}",
            "[green]matched[/green]",
        )

        if not dry_run:
            # Save podcast and episode to DB
            db.add_podcast(result.podcast)
            db.add_episode(result.rss_episode)
            record_spotify_mapping(db, sp_ep.spotify_id, result.rss_episode.id)
            imported += 1
            to_download_eps.append(result.rss_episode)

    console.print(table)

    if dry_run:
        matched = sum(1 for r in match_results if r.matched and not get_spotify_mapping(db, r.spotify_episode.spotify_id))
        console.print(f"\n[dim]Dry run: {matched} would be imported, "
                       f"{skipped_exclusive} Spotify exclusive, "
                       f"{skipped_no_match} no match, "
                       f"{skipped_duplicate} already imported[/dim]")
        return

    console.print(f"\n[green]{imported} imported[/green], "
                   f"[yellow]{skipped_exclusive} skipped (Spotify exclusive)[/yellow], "
                   f"[yellow]{skipped_no_match} skipped (no match)[/yellow], "
                   f"[dim]{skipped_duplicate} skipped (already imported)[/dim]")

    # Download
    if download and to_download_eps:
        console.print(f"\n[bold]Downloading {len(to_download_eps)} episodes...[/bold]")
        results = download_episodes_sync(to_download_eps)

        for episode, path, error in results:
            if path:
                db.mark_episode_downloaded(episode.id, str(path))
                console.print(f"  [green]✓[/green] {episode.title[:50]}")
            else:
                console.print(f"  [red]✗[/red] {episode.title[:50]}: {error}")

        from podworm.config import get_audio_dir
        console.print(f"\n[bold]Audio saved to:[/bold] {get_audio_dir()}")


@cli.command()
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
def reset(yes: bool):
    """Delete all data (db, audio, transcripts)."""
    import shutil

    from podworm.config import get_audio_dir, get_db_path

    console.print(f"[bold red]This will delete:[/bold red]")
    console.print(f"  - Database: {get_db_path()}")
    console.print(f"  - Audio files: {get_audio_dir()}")
    console.print(f"  - Transcripts: {get_transcripts_dir()}")

    if not yes:
        click.confirm("\nAre you sure?", abort=True)

    db_path = get_db_path()
    if db_path.exists():
        db_path.unlink()
        console.print(f"  [green]✓[/green] Deleted database")

    audio_dir = get_audio_dir()
    if audio_dir.exists():
        shutil.rmtree(audio_dir)
        console.print(f"  [green]✓[/green] Deleted audio files")

    transcripts_dir = get_transcripts_dir()
    if transcripts_dir.exists():
        shutil.rmtree(transcripts_dir)
        console.print(f"  [green]✓[/green] Deleted transcripts")

    console.print(f"\n[green]Reset complete.[/green]")


@cli.command()
@click.option("--dry-run", is_flag=True, help="Show what would be deleted without deleting")
@click.option("--podcast", "-p", help="Limit to a specific podcast ID prefix")
def clean(dry_run: bool, podcast: str | None):
    """Delete audio files for already-transcribed episodes to free disk space."""
    db = Database()

    # Resolve podcast ID prefix
    podcast_id = None
    if podcast:
        podcasts = db.list_podcasts()
        match = next((p for p in podcasts if p.id.startswith(podcast)), None)
        if not match:
            console.print(f"[red]Podcast not found: {podcast}[/red]")
            sys.exit(1)
        podcast_id = match.id

    episodes = db.list_episodes_to_clean(podcast_id)

    if not episodes:
        console.print("[yellow]No audio files to clean up.[/yellow]")
        return

    # Build summary table
    table = Table(title="Audio files to delete" + (" (dry run)" if dry_run else ""))
    table.add_column("Episode", max_width=40)
    table.add_column("Size", justify="right")
    table.add_column("Path", style="dim")

    total_size = 0
    cleanable = []

    for ep in episodes:
        path = Path(ep.audio_path)
        if not path.exists():
            # File already gone — just clear the DB reference
            if not dry_run:
                db.clear_audio_path(ep.id)
            continue
        size = path.stat().st_size
        total_size += size
        cleanable.append((ep, path, size))
        table.add_row(
            ep.title[:40],
            _format_size(size),
            str(path),
        )

    if not cleanable:
        console.print("[yellow]No audio files found on disk to clean.[/yellow]")
        return

    console.print(table)
    console.print(f"\n[bold]Total: {len(cleanable)} files, {_format_size(total_size)}[/bold]")

    if dry_run:
        console.print("[dim]Dry run — no files were deleted.[/dim]")
        return

    # Delete files
    deleted = 0
    freed = 0
    for ep, path, size in cleanable:
        try:
            path.unlink()
            db.clear_audio_path(ep.id)
            deleted += 1
            freed += size
        except OSError as e:
            console.print(f"  [red]✗[/red] Failed to delete {path}: {e}")

    console.print(f"\n[green]Deleted {deleted} files, freed {_format_size(freed)}.[/green]")


@cli.command()
@click.option("--podcast", "-p", help="Only digest specific podcast ID")
@click.option("--episode", "-e", help="Only digest specific episode ID")
@click.option("--limit", "-l", default=5, help="Max episodes to digest")
@click.option("--force", "-f", is_flag=True, help="Re-digest already digested episodes")
def digest(podcast: str | None, episode: str | None, limit: int, force: bool):
    """Generate AI summaries for transcribed episodes."""
    db = Database()

    if episode:
        # Digest specific episode
        ep = db.get_episode(episode)
        if not ep:
            all_eps = db.list_episodes()
            ep = next((e for e in all_eps if e.id.startswith(episode)), None)

        if not ep:
            console.print(f"[red]Episode not found: {episode}[/red]")
            sys.exit(1)

        if not ep.transcript_path:
            console.print(f"[red]Episode not transcribed yet. Run 'podworm transcribe' first.[/red]")
            sys.exit(1)

        if ep.digested_at and not force:
            console.print(f"[yellow]Episode already digested. Use --force to re-digest.[/yellow]")
            return

        p = db.get_podcast(ep.podcast_id)
        to_digest = [(ep, p)]
    else:
        if force:
            # With force, get all transcribed episodes
            all_eps = db.list_episodes()
            all_transcribed = [e for e in all_eps if e.transcribed_at]
            if podcast:
                podcasts = db.list_podcasts()
                podcast_match = next((p for p in podcasts if p.id.startswith(podcast)), None)
                if podcast_match:
                    all_transcribed = [e for e in all_transcribed if e.podcast_id == podcast_match.id]
            all_transcribed = all_transcribed[:limit]
        else:
            all_transcribed = db.list_episodes_to_digest(limit=limit)
            if podcast:
                podcasts = db.list_podcasts()
                podcast_match = next((p for p in podcasts if p.id.startswith(podcast)), None)
                if podcast_match:
                    all_transcribed = [e for e in all_transcribed if e.podcast_id == podcast_match.id]

        to_digest = []
        for ep in all_transcribed:
            p = db.get_podcast(ep.podcast_id)
            if p:
                to_digest.append((ep, p))

    if not to_digest:
        console.print("[yellow]No episodes to digest. Transcribe episodes first with 'podworm transcribe'.[/yellow]")
        return

    console.print(f"[bold]Digesting {len(to_digest)} episodes...[/bold]")
    results = digest_episodes(to_digest)

    success_count = 0
    for ep, path, error in results:
        if path:
            db.mark_episode_digested(ep.id, str(path))
            success_count += 1
            console.print(f"  [green]✓[/green] {ep.title}")
        else:
            console.print(f"  [red]✗[/red] {ep.title}: {error}")

    console.print(f"\n[green]Digested {success_count}/{len(to_digest)} episodes.[/green]")


@cli.command()
@click.option("--limit", "-l", default=20, help="Max episodes to process")
@click.option("--date", "-d", "date_str", default=None, help="Only episodes saved on this date (YYYY-MM-DD)")
@click.option("--skip-clean", is_flag=True, help="Skip cleaning audio files")
@click.option("--no-chat", is_flag=True, help="Skip launching Claude Code at the end")
@click.option("--obsidian", is_flag=True, help="Summarize via claude --print and save to Obsidian")
def daily(limit: int, date_str: str | None, skip_clean: bool, no_chat: bool, obsidian: bool):
    """Run full daily pipeline: spotify-import → transcribe → digest → clean."""
    from datetime import date as date_type

    from podworm.config import get_spotify_client_id, get_spotify_client_secret
    from podworm.spotify import (
        get_spotify_client,
        fetch_saved_episodes,
        resolve_spotify_episodes,
        record_spotify_mapping,
        get_spotify_mapping,
    )

    db = Database()

    # Parse date filter — default to today
    if date_str:
        try:
            date_filter = date_type.fromisoformat(date_str)
        except ValueError:
            console.print(f"[red]Invalid date format:[/red] {date_str} (expected YYYY-MM-DD)")
            sys.exit(1)
    else:
        date_filter = date_type.today()

    # --- Step 1: Spotify import ---
    console.print(f"[bold]Step 1: Importing from Spotify (date={date_filter})...[/bold]")

    spotify_ok = True
    if not get_spotify_client_id() or not get_spotify_client_secret():
        console.print("  [yellow]Skipping: Spotify credentials not configured[/yellow]")
        spotify_ok = False

    to_download_eps = []
    if spotify_ok:
        try:
            sp = get_spotify_client()
            sp.current_user()
        except Exception as e:
            console.print(f"  [yellow]Skipping Spotify: {e}[/yellow]")
            spotify_ok = False

    if spotify_ok:
        try:
            spotify_eps = fetch_saved_episodes(sp, limit=limit, date_filter=date_filter)
            if not spotify_eps:
                console.print("  [dim]No saved episodes found for this date[/dim]")
            else:
                console.print(f"  Found {len(spotify_eps)} episodes")
                match_results = resolve_spotify_episodes(spotify_eps)
                imported = 0
                for result in match_results:
                    sp_ep = result.spotify_episode
                    existing_id = get_spotify_mapping(db, sp_ep.spotify_id)
                    if not result.matched:
                        continue
                    if existing_id:
                        # Re-queue for download if previously mapped but never downloaded
                        existing_ep = db.get_episode(existing_id)
                        if existing_ep and not existing_ep.downloaded_at:
                            to_download_eps.append(existing_ep)
                        continue
                    db.add_podcast(result.podcast)
                    db.add_episode(result.rss_episode)
                    record_spotify_mapping(db, sp_ep.spotify_id, result.rss_episode.id)
                    imported += 1
                    to_download_eps.append(result.rss_episode)
                console.print(f"  [green]Imported {imported} new episodes[/green]")
        except Exception as e:
            console.print(f"  [red]Spotify import failed: {e}[/red]")

    # Download newly imported episodes
    if to_download_eps:
        console.print(f"\n  [bold]Downloading {len(to_download_eps)} episodes...[/bold]")
        results = download_episodes_sync(to_download_eps)
        for episode, path, error in results:
            if path:
                db.mark_episode_downloaded(episode.id, str(path))
                console.print(f"    [green]✓[/green] {episode.title[:50]}")
            else:
                console.print(f"    [red]✗[/red] {episode.title[:50]}: {error}")

    # --- Step 2: Transcribe (only newly imported episodes) ---
    console.print(f"\n[bold]Step 2: Transcribing...[/bold]")

    # Re-fetch imported episodes so we pick up the audio_path set during download
    to_transcribe = []
    for ep in to_download_eps:
        fresh_ep = db.get_episode(ep.id)
        if fresh_ep and fresh_ep.audio_path and not fresh_ep.transcribed_at:
            p = db.get_podcast(fresh_ep.podcast_id)
            if p:
                to_transcribe.append((fresh_ep, p))

    if to_transcribe:
        results = transcribe_episodes(to_transcribe)
        for ep, path, error in results:
            if path:
                db.mark_episode_transcribed(ep.id, str(path))
                console.print(f"  [green]✓[/green] {ep.title[:50]}")
            else:
                console.print(f"  [red]✗[/red] {ep.title[:50]}: {error}")
    else:
        console.print("  [dim]No episodes to transcribe[/dim]")

    # --- Step 3: Digest ---
    console.print(f"\n[bold]Step 3: Generating digests...[/bold]")
    to_digest_list = db.list_episodes_to_digest(limit=limit)
    digest_paths: list[Path] = []

    if to_digest_list:
        to_digest = []
        for ep in to_digest_list:
            p = db.get_podcast(ep.podcast_id)
            if p:
                to_digest.append((ep, p))

        results = digest_episodes(to_digest)
        for ep, path, error in results:
            if path:
                db.mark_episode_digested(ep.id, str(path))
                digest_paths.append(path)
                console.print(f"  [green]✓[/green] {ep.title[:50]}")
            else:
                console.print(f"  [red]✗[/red] {ep.title[:50]}: {error}")
    else:
        console.print("  [dim]No episodes to digest[/dim]")

    # --- Step 4: Clean ---
    if not skip_clean:
        console.print(f"\n[bold]Step 4: Cleaning audio files...[/bold]")
        episodes_to_clean = db.list_episodes_to_clean()
        deleted = 0
        for ep in episodes_to_clean:
            path = Path(ep.audio_path)
            if path.exists():
                try:
                    path.unlink()
                    db.clear_audio_path(ep.id)
                    deleted += 1
                except OSError as e:
                    console.print(f"  [red]✗[/red] {path}: {e}")
            else:
                db.clear_audio_path(ep.id)
        if deleted:
            console.print(f"  [green]Deleted {deleted} audio files[/green]")
        else:
            console.print("  [dim]No audio files to clean[/dim]")

    console.print("\n[bold green]Daily pipeline complete![/bold green]")

    # --- Step 5: Summarize ---
    if no_chat and not obsidian:
        return

    # Fall back to today's existing digests when none were newly generated
    if not digest_paths:
        all_eps = db.list_episodes()
        digest_paths = [
            Path(e.digest_path) for e in all_eps
            if e.digest_path
            and e.downloaded_at
            and e.downloaded_at.date() == date_filter
        ]
    parts = []
    for dp in digest_paths:
        try:
            parts.append(dp.read_text())
        except OSError:
            pass

    if not parts:
        console.print("\n[dim]No digests to review.[/dim]")
        return

    instruction = (
        "Here are today's podcast transcripts (in the system prompt). "
        "Please summarize each one in the SAME language as the transcript, "
        "with key topics, main discussion points, and notable quotes with timestamps."
    )

    if obsidian:
        console.print("\n[bold]Generating summary via claude --print...[/bold]")
        summary = _claude_print(instruction, "\n---\n\n".join(parts))
        if summary:
            note_path = _save_obsidian_note(summary, date_filter)
            console.print(f"  [green]Saved Obsidian note:[/green] {note_path}")
        else:
            console.print("  [red]Failed to generate summary[/red]")
    else:
        console.print("\n[bold]Launching Claude Code...[/bold]")
        _launch_claude(instruction, "\n---\n\n".join(parts))


@cli.command()
@click.option("--date", "-d", "date_str", default=None, help="Date to filter by (YYYY-MM-DD, default: today)")
def chat(date_str: str | None):
    """Launch Claude Code with transcripts from a given date."""
    from datetime import date as date_type

    db = Database()

    if date_str:
        try:
            date_filter = date_type.fromisoformat(date_str)
        except ValueError:
            console.print(f"[red]Invalid date format:[/red] {date_str} (expected YYYY-MM-DD)")
            sys.exit(1)
    else:
        date_filter = date_type.today()

    # Find transcribed episodes downloaded on the given date
    all_eps = db.list_episodes()
    episodes = [
        e for e in all_eps
        if e.transcript_path
        and e.downloaded_at
        and e.downloaded_at.date() == date_filter
    ]

    if not episodes:
        console.print(f"[yellow]No transcripts found for {date_filter}.[/yellow]")
        sys.exit(1)

    parts = []
    for ep in episodes:
        path = Path(ep.transcript_path)
        if path.exists():
            parts.append(path.read_text(encoding="utf-8"))

    if not parts:
        console.print("[yellow]Transcript files not found on disk.[/yellow]")
        sys.exit(1)

    instruction = (
        f"Here are podcast transcripts from {date_filter} (in the system prompt). "
        "Please summarize each one in the SAME language as the transcript, "
        "with key topics, main discussion points, and notable quotes with timestamps."
    )

    console.print(f"[bold]Launching Claude Code with {len(parts)} transcripts from {date_filter}...[/bold]")
    _launch_claude(instruction, "\n---\n\n".join(parts))


@cli.command()
@click.argument("podcast")
@click.option("--limit", "-l", default=0, help="Max episodes to process (0=all)")
@click.option("--skip-download", is_flag=True, help="Skip download, use existing transcripts")
def interview(podcast: str, limit: int, skip_download: bool):
    """Learn a host's interview style and role-play as them.

    Downloads and transcribes all episodes from a podcast, then launches
    Claude Code where Claude role-plays as the host interviewing you.

    Examples:

        podworm interview 日谈公园 --skip-download

        podworm interview abc123 -l 5
    """
    db = Database()

    # --- Resolve podcast by prefix match ---
    podcasts = db.list_podcasts()
    pod = next((p for p in podcasts if p.id.startswith(podcast)), None)

    if not pod:
        # Try matching by title substring
        pod = next((p for p in podcasts if podcast.lower() in p.title.lower()), None)

    if not pod:
        # Search Xiaoyuzhou and add the podcast
        console.print(f"[yellow]Podcast not in database. Searching Xiaoyuzhou...[/yellow]")
        results = search_xiaoyuzhou(podcast)
        podcast_urls = [r for r in results if "/podcast/" in r["url"]]

        if not podcast_urls:
            console.print(f"[red]Could not find podcast: {podcast}[/red]")
            sys.exit(1)

        for result in podcast_urls:
            console.print(f"  Checking: {result['title']}")
            try:
                found_pod, found_eps = scrape_podcast_page(result["url"])
                if podcast.lower() in found_pod.title.lower():
                    db.add_podcast(found_pod)
                    for ep in found_eps:
                        db.add_episode(ep)
                    pod = found_pod
                    console.print(f"  [green]✓[/green] Added: {pod.title} ({len(found_eps)} episodes)")
                    break
            except Exception as e:
                console.print(f"  [dim]Failed: {e}[/dim]")
                continue

        if not pod:
            # Fall back to first result if title match failed
            for result in podcast_urls:
                try:
                    found_pod, found_eps = scrape_podcast_page(result["url"])
                    db.add_podcast(found_pod)
                    for ep in found_eps:
                        db.add_episode(ep)
                    pod = found_pod
                    console.print(f"  [green]✓[/green] Added: {pod.title} ({len(found_eps)} episodes)")
                    break
                except Exception:
                    continue

        if not pod:
            console.print(f"[red]Could not find podcast: {podcast}[/red]")
            sys.exit(1)

    console.print(f"[bold]Podcast:[/bold] {pod.title}")

    if not skip_download:
        # --- Step 1: Sync all episodes from RSS ---
        console.print(f"\n[bold]Step 1: Syncing episodes from RSS...[/bold]")
        try:
            episodes = fetch_episodes(pod.feed_url, pod.id)
            new_count = 0
            for ep in episodes:
                existing = db.get_episode(ep.id)
                if not existing:
                    db.add_episode(ep)
                    new_count += 1
            console.print(f"  [green]✓[/green] {len(episodes)} total, {new_count} new")
        except Exception as e:
            console.print(f"  [red]✗[/red] Failed to sync: {e}")
            sys.exit(1)

        # --- Step 2: Download un-downloaded episodes ---
        console.print(f"\n[bold]Step 2: Downloading episodes...[/bold]")
        all_eps = db.list_episodes(pod.id)
        to_download = [e for e in all_eps if not e.downloaded_at and e.audio_url]
        if limit > 0:
            to_download = to_download[:limit]

        if to_download:
            console.print(f"  Downloading {len(to_download)} episodes...")
            results = download_episodes_sync(to_download)
            for episode, path, error in results:
                if path:
                    db.mark_episode_downloaded(episode.id, str(path))
                    console.print(f"    [green]✓[/green] {episode.title[:50]}")
                else:
                    console.print(f"    [red]✗[/red] {episode.title[:50]}: {error}")
        else:
            console.print("  [dim]All episodes already downloaded[/dim]")

        # --- Step 3: Transcribe un-transcribed episodes ---
        console.print(f"\n[bold]Step 3: Transcribing episodes...[/bold]")
        # Re-fetch to pick up download paths
        all_eps = db.list_episodes(pod.id)
        to_transcribe = []
        for ep in all_eps:
            if ep.audio_path and not ep.transcribed_at:
                to_transcribe.append((ep, pod))
        if limit > 0:
            to_transcribe = to_transcribe[:limit]

        if to_transcribe:
            console.print(f"  Transcribing {len(to_transcribe)} episodes...")
            results = transcribe_episodes(to_transcribe)
            for ep, path, error in results:
                if path:
                    db.mark_episode_transcribed(ep.id, str(path))
                    console.print(f"    [green]✓[/green] {ep.title[:50]}")
                else:
                    console.print(f"    [red]✗[/red] {ep.title[:50]}: {error}")
        else:
            console.print("  [dim]All episodes already transcribed[/dim]")

    # --- Step 4: Build style prompt from transcripts ---
    console.print(f"\n[bold]Building interview prompt...[/bold]")

    # Collect all transcript files for this podcast
    transcripts_dir = get_transcripts_dir() / pod.id
    if not transcripts_dir.exists():
        console.print(f"[red]No transcripts found for this podcast.[/red]")
        console.print("[dim]Run without --skip-download to download and transcribe first.[/dim]")
        sys.exit(1)

    transcript_files = sorted(transcripts_dir.glob("*.md"))
    if not transcript_files:
        console.print(f"[red]No transcript files found in {transcripts_dir}[/red]")
        sys.exit(1)

    console.print(f"  Found {len(transcript_files)} transcripts")

    # Pick up to 5 representative transcripts (spread across the collection)
    if len(transcript_files) <= 5:
        sample_files = transcript_files
    else:
        step = len(transcript_files) // 5
        sample_files = [transcript_files[i * step] for i in range(5)]

    # Read excerpts (first ~500 lines each)
    excerpts = []
    for tf in sample_files:
        text = tf.read_text(encoding="utf-8")
        lines = text.split("\n")
        excerpt = "\n".join(lines[:500])
        excerpts.append(excerpt)

    # Create interviews output directory
    interviews_dir = get_interviews_dir() / pod.id
    interviews_dir.mkdir(parents=True, exist_ok=True)

    transcripts = (
        f"Transcript excerpts from {len(excerpts)} episodes of \"{pod.title}\":\n\n"
        + "\n\n---\n\n".join(excerpts)
    )

    instruction = (
        f"You are about to role-play as the host of the podcast \"{pod.title}\". "
        f"Study the {len(excerpts)} transcript excerpts in the system prompt carefully to learn: "
        "the host's name, greeting style, question types (open-ended, probing, personal, technical), "
        "follow-up patterns, tone, catchphrases, topic transitions, and closing style.\n\n"
        "NOW: Conduct an interview with me (the user) as if you are this host. "
        "Stay in character throughout. Use the same language as the transcripts. "
        "Start with the host's typical greeting and opening, then begin asking questions. "
        "Adapt your follow-up questions based on my answers, just as the real host would.\n\n"
        f"When the interview is finished (I'll say we're done, or you feel it's a natural ending), "
        f"save the full interview transcript as a markdown file at:\n"
        f"  {interviews_dir}/interview_{{timestamp}}.md\n"
        f"Use the current timestamp in YYYY-MM-DD_HH-MM format for {{timestamp}}.\n"
    )

    console.print(f"[bold]Launching Claude Code as {pod.title} host...[/bold]")
    _launch_claude(instruction, transcripts)


@cli.command()
@click.argument("episode_id")
@click.option("--clipboard", "-c", is_flag=True, help="Copy to clipboard (macOS pbcopy)")
@click.option("--digest", "-d", "show_digest", is_flag=True, help="Copy digest instead of transcript")
def copy(episode_id: str, clipboard: bool, show_digest: bool):
    """Print transcript or digest to stdout for pasting into LLM tools."""
    db = Database()

    # Find episode
    all_eps = db.list_episodes()
    episode = next((e for e in all_eps if e.id.startswith(episode_id)), None)

    if not episode:
        console.print(f"[red]Episode not found: {episode_id}[/red]")
        sys.exit(1)

    if show_digest:
        if not episode.digest_path:
            console.print(f"[yellow]Episode not digested yet. Run 'podworm digest' first.[/yellow]")
            sys.exit(1)
        file_path = Path(episode.digest_path)
        label = "Digest"
    else:
        if not episode.transcript_path:
            console.print(f"[yellow]Episode not transcribed yet. Run 'podworm transcribe' first.[/yellow]")
            sys.exit(1)
        file_path = Path(episode.transcript_path)
        label = "Transcript"

    if not file_path.exists():
        console.print(f"[red]{label} file not found: {file_path}[/red]")
        sys.exit(1)

    content = file_path.read_text(encoding="utf-8")

    if clipboard:
        try:
            proc = subprocess.run(
                ["pbcopy"], input=content.encode("utf-8"), check=True
            )
            console.print(f"[green]{label} copied to clipboard ({len(content)} chars)[/green]")
        except (FileNotFoundError, subprocess.CalledProcessError):
            console.print(f"[red]pbcopy not available. Printing to stdout instead.[/red]")
            click.echo(content)
    else:
        click.echo(content)


def _claude_print(instruction: str, transcripts: str) -> str | None:
    """Run claude --print non-interactively and return the summary text."""
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    )
    tmp.write(transcripts)
    tmp.close()
    try:
        result = subprocess.run(
            ["claude", "--print", "--append-system-prompt-file", tmp.name, instruction],
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.stderr:
            console.print(f"  [red]claude stderr:[/red] {result.stderr[:500]}")
        return None
    except FileNotFoundError:
        console.print(
            "[yellow]claude not found on PATH. "
            "Install Claude Code: npm install -g @anthropic-ai/claude-code[/yellow]"
        )
        return None
    except subprocess.TimeoutExpired:
        console.print("[red]claude --print timed out after 10 minutes[/red]")
        return None
    finally:
        os.unlink(tmp.name)


def _save_obsidian_note(summary: str, date: "date") -> Path:
    """Save a summary as an Obsidian note and return the file path."""
    from podworm.config import get_obsidian_vault_dir

    vault = get_obsidian_vault_dir()
    podcast_dir = vault / "Podcasts"
    podcast_dir.mkdir(parents=True, exist_ok=True)

    note_path = podcast_dir / f"{date.isoformat()}.md"
    note_path.write_text(
        f"---\ndate: {date.isoformat()}\ntags:\n  - podcast\n---\n\n{summary}\n",
        encoding="utf-8",
    )
    return note_path


def _launch_claude(instruction: str, transcripts: str) -> None:
    """Launch Claude Code with transcripts appended to the system prompt.

    The large transcript content goes into a temp file passed via
    --append-system-prompt-file (avoids ARG_MAX), while the short
    instruction is passed as the positional prompt argument.
    """
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=".md", delete=False, encoding="utf-8"
    )
    tmp.write(transcripts)
    tmp.close()
    try:
        os.execvp("claude", ["claude", "--append-system-prompt-file", tmp.name,
                             instruction])
    except FileNotFoundError:
        os.unlink(tmp.name)
        console.print(
            "[yellow]claude not found on PATH. "
            "Install Claude Code: npm install -g @anthropic-ai/claude-code[/yellow]"
        )


def _format_size(size_bytes: int) -> str:
    """Format byte size as human-readable string."""
    for unit in ("B", "KB", "MB", "GB"):
        if size_bytes < 1024:
            return f"{size_bytes:.1f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.1f} TB"


if __name__ == "__main__":
    cli()
