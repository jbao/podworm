Available commands:
  podworm import <opml.xml>     # Import from 小宇宙 OPML export
  podworm add <url>             # Add single podcast
  podworm list                  # Show subscribed podcasts
  podworm episodes <id>         # Show episodes for a podcast
  podworm sync                  # Download new episodes
  podworm transcribe            # Transcribe downloaded episodes
  podworm show <episode_id>     # View transcript in terminal
  podworm open <episode_id>     # Open transcript in editor
  podworm search "关键词"        # Search all transcripts
  podworm auto                  # Automated daily job (sync + transcribe)

  To get started:
  1. Get a Groq API key at https://console.groq.com (free tier available)
  2. Export OPML from 小宇宙 app (Settings → Export subscriptions)
  3. Run:
  cd ~/code/github/podworm
  export GROQ_API_KEY="your-key-here"
  uv run podworm import ~/path/to/subscriptions.opml
  uv run podworm sync --limit 1
  uv run podworm transcribe --limit 1

  Transcripts saved to: ~/.local/share/podworm/transcripts/

## Launchd

  To activate the schedule

  # Copy plist and load it
  cp com.podworm.daily.plist ~/Library/LaunchAgents/
  launchctl load ~/Library/LaunchAgents/com.podworm.daily.plist

  # Test-trigger immediately
  launchctl start com.podworm.daily

  # Check logs
  tail -f ~/.local/share/podworm/logs/daily-$(date +%Y-%m-%d).log
