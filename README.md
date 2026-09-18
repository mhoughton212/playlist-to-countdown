# Playlist to Countdown

Playlist to Countdown is a local tool for turning a Spotify playlist into a countdown compilation. It saves clip choices locally and can export MP3 or 1920×1080 video.

## What it does

- Loads a Spotify playlist and reverses it into countdown order.
- Finds or accepts local MP3 files and lets you trim each excerpt.
- Supports editable card text, artwork, performance images, and optional text frames.
- Builds preview or full MP3 and MP4 exports.

## Run it locally

You need Python 3.11 or 3.12, Node.js 18 or newer, and FFmpeg 6 or newer.

1. Copy [`.env.example`](.env.example) to `.env` and fill in your Spotify developer application values. The redirect URI must be `http://127.0.0.1:8765/callback`.
2. Install the dependencies:

   ```powershell
   python -m pip install -r requirements.txt -c requirements.lock
   python -m playwright install chromium
   ```

3. Start **Start Countdown.cmd** from the project folder.
4. Open <http://127.0.0.1:8765/> if it does not open automatically.

The server is local-only. Spotify credentials, downloaded audio, saved project
data, cached images, and exports stay outside version control.

## Typical workflow

Load a playlist, connect Spotify when prompted, download or add missing audio,
choose clips in the waveform editor, then build a preview or final export.
Missing audio is skipped and listed in the export result.

For development checks:

```powershell
$env:PYTHONPATH='.'
pytest -q
node tests/test_clip_range.cjs
```

The optional Spotify commands are run from the project folder:

```powershell
python -m tools.playlist_delta PLAYLIST_ID_1 PLAYLIST_ID_2
python -m tools.shuffle_spotify PLAYLIST_ID NUMBER_TO_PRESERVE
python -m tools.generate_thumbnail
```

The first command compares two playlists. The second shuffles the tracks after
a specified number of fixed tracks in a Spotify playlist. The third creates a
thumbnail from the current project.
