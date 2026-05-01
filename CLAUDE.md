# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Podcast management and transcription CLI tool for 小宇宙 (Xiaoyuzhou) and Spotify podcasts. Downloads audio, transcribes via Deepgram, and generates digests for review in Claude Code.

## Commands

Run with `uv run podworm <command>`. Key commands:

- `daily` — Full pipeline: Spotify import → download → transcribe → digest → clean → launch Claude Code
- `reset -y` — Wipe all data (db, audio, transcripts)
- `chat -d YYYY-MM-DD` — Launch Claude Code with transcripts from a date
- `transcribe` — Transcribe downloaded episodes (standalone)
- `sync` — Download new episodes from RSS feeds
- `grab <podcast> <episode>` — Search and download a specific episode

## Environment Variables

Required: `DEEPGRAM_API_KEY`
Optional: `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET`, `PODWORM_DATA_DIR`

## Architecture

All source in `src/podworm/`. CLI entry point: `cli.py` (Click framework, Rich for UI).

Pipeline stages, each tracked by timestamp columns in SQLite (`~/.local/share/podworm/podcasts.db`):
1. **Import** — `feed_parser.py` / `spotify.py` → adds Podcast + Episode rows
2. **Download** — `downloader.py` → async httpx, resume support, sets `downloaded_at` + `audio_path`
3. **Transcribe** — `transcriber.py` → Deepgram nova-2 with `detect_language=True`, sets `transcribed_at` + `transcript_path`
4. **Digest** — `digest.py` → saves transcript with metadata headers as `_digest.md`, sets `digested_at`
5. **Clean** — deletes audio for transcribed episodes

Database ORM: `database.py` with `Podcast`/`Episode` dataclasses over `sqlite-utils`.

Output files: `~/.local/share/podworm/transcripts/{podcast_id}/{episode_id}.md`

## No automated test suite

`test_favorites.py` is a manual script for Xiaoyuzhou API auth testing, not part of a test framework.
