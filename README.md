<img width="692" height="910" alt="image" src="https://github.com/user-attachments/assets/92034869-a8e5-458f-9a93-55063aff7ce2" />


# Karaoke Generator

Local web app that takes a music track (file upload or URL from YouTube, Spotify, SoundCloud, Yandex Music) and produces:

* **`_karaoke.mp4`** — 1080p video with massive, center-screen dynamic karaoke subtitles (word-by-word highlighting)
* **`_minus.mp3`** — Instrumental (vocals removed)
* **`_karaoke.ass`** — Raw ASS subtitle file for use in VLC, mpv, Aegisub, etc.

## Stack

| Step | Tool |
|---|---|
| YouTube / SoundCloud / Yandex Music download | `yt-dlp` |
| Spotify download | `spotdl` (isolated via pipx) |
| YouTube subtitle extraction | `yt-dlp --write-subs` |
| Vocal separation | `demucs` (`htdemucs` model) |
| Transcription | `faster-whisper` (CUDA `int8_float16` / CPU `int8`) |
| ASS generation | custom `ass_gen.py` (Dynamic HLS colors, native vector canvas) |
| Video rendering | `ffmpeg` (libx264 + libass burn-in) |
| Song catalog | SQLite (`work/catalog.db`) |
| Web UI | FastAPI + vanilla JS |

## Quick start (Docker — recommended)

```bash
# GPU (default)
docker compose up --build

# CPU only
WHISPER_DEVICE=cpu docker compose up --build

# open http://localhost:8000
```

Requires [NVIDIA Container Toolkit](https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html) for GPU mode.

### Loading Custom Fonts
The app dynamically polls the OS font cache (`fc-list`) to randomize subtitle fonts. To inject custom fonts at runtime without rebuilding the image, map a host directory in your `docker-compose.yml`:

```yaml
    volumes:
      - ./work:/app/work
      - /path/to/your/custom/fonts:/usr/share/fonts/custom:ro
```
The container's `entrypoint.sh` automatically runs `fc-cache -fv` on startup to register mounted fonts.

## Features

### Audio sources
* **File upload** — MP3, WAV, FLAC, MP4, WEBM, MKV
* **YouTube / YouTube Music** — paste URL, audio downloaded via yt-dlp
* **Spotify** — paste `open.spotify.com/track/...` URL, downloaded via spotdl
* **SoundCloud** — paste URL, downloaded via yt-dlp
* **Yandex Music** — paste URL, downloaded via yt-dlp

### Visual Engine & Dynamic Styling
* **Native Vector Canvas** — Video backgrounds are rendered natively using ASS Layer -1 vector drawing commands (`{\p1}m 0 0 l 1920 0...`), completely eliminating the need for external video background assets.
* **HLS Contrast Math** — Color palettes are generated using Hue/Lightness/Saturation algorithms to guarantee high contrast. If the background canvas is dark, text is brightly illuminated; if light, text is heavily darkened.

### Subtitle timing
* **Character-weighted word distribution** — longer words get proportionally more time (not equal per word).
* **Whisper-to-lyrics word alignment** — when both Whisper timestamps and lyrics exist, a greedy alignment transfers Whisper's timing to the correct lyric words.
* **Minimum word duration** — 150ms floor to prevent flicker.
* **Gap threshold** — gaps under 100ms are absorbed into word duration.

### Lyrics Pipeline
Priority chain (highest to lowest):
1. **User-pasted lyrics** — used as subtitle text, timed by Whisper.
2. **YouTube subtitles** — auto-extracted from the video if available.
3. **syncedlyrics** — searches Spotify, Musixmatch, Genius, NetEase.
4. **lrclib.net** — free API with good Cyrillic coverage.
5. **Yandex Music** — good for Russian/Belarusian songs.
6. **Genius** — via `lyricsgenius` (requires `GENIUS_ACCESS_TOKEN` env var).

### Chorus detection
* Auto-detects repeated lyric blocks (chorus/refrain).
* Option to **keep** (default) or **remove** repeated chorus sections. When removed, only the first occurrence is kept.

### Hallucination filtering
* Segments repeated 3+ times are dropped.
* Segments with < 25-30% word overlap with lyrics vocabulary are dropped.
* Belarusian seed prompt echo detection.

### Rating & auto-retry
After results are ready, rate the output (1-5 stars) for text accuracy and video sync. If either rating is < 4, transcription is automatically retried with adjusted Whisper settings (up to 2 retries). Demucs vocal separation is reused.

| Attempt | beam_size | no_speech_threshold | temperature |
|---|---|---|---|
| 1 (initial) | 5 | 0.6 | 0.0 |
| 2 | 10 | 0.4 | 0.0 |
| 3 | 10 | 0.3 | 0.0 / 0.2 / 0.4 |

### Output format — ASS karaoke
Each Whisper segment becomes one `Dialogue:` line. Words use `{\kf<cs>}` tags:
* `cs` = centiseconds (1/100 s)
* `\kf` = karaoke fill — text wipes from the low-saturation secondary color (unsung) to the high-saturation primary color (sung).
