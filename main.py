import os, re, sys, subprocess, urllib.parse, urllib.request, json, zipfile, shutil, difflib, uuid, logging, traceback, gc, time
from pathlib import Path
from typing import Any, Dict, Optional
from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from catalog import init_db, upsert_song, get_song, list_songs, count_songs, delete_song, _parse_artist_title
import ass_gen

app = FastAPI(title="Karaoke Generator")
logger = logging.getLogger("karaoke")
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s")

WORK_DIR = Path("work")
WORK_DIR.mkdir(exist_ok=True)
init_db()

FFMPEG = "/usr/bin/ffmpeg"
jobs: Dict[str, Dict[str, Any]] = {}

RETRY_SETTINGS = [
    {"beam_size": 5,  "no_speech_threshold": 0.6, "temperature": 0.0},
    {"beam_size": 10, "no_speech_threshold": 0.4, "temperature": 0.0},
    {"beam_size": 10, "no_speech_threshold": 0.3, "temperature": 0.2},
]

def _cleanup_memory(stage_name=""):
    gc.collect()
    try:
        import torch
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
            logger.info("[MEMORY] Cleared CUDA cache after phase: {}".format(stage_name))
    except ImportError:
        pass

def _set(job_id: str, **kwargs) -> None:
    jobs[job_id].update(kwargs)

def _run(cmd: list, **kwargs) -> str:
    logger.debug("Executing standard subprocess: {}".format(" ".join(cmd)))
    result = subprocess.run(cmd, capture_output=True, text=True, **kwargs)
    if result.returncode != 0:
        logger.error("Subprocess Error: {}".format(result.stderr))
        raise RuntimeError(result.stderr[-3000:] or result.stdout[-3000:])
    return result.stdout

def _run_ffmpeg_with_progress(cmd: list, job_id: str, base_step: str, start_pct: int, end_pct: int, total_duration: float) -> None:
    logger.info("[FFMPEG START] Job {}: Executing command: {}".format(job_id, " ".join(cmd)))
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    start_time = time.time()
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, text=True, universal_newlines=True, env=env)

    for line in proc.stderr:
        m = re.search(r"time=(\d+):(\d+):(\d+\.\d+)", line)
        if m and total_duration > 0:
            h, m_min, s = float(m.group(1)), float(m.group(2)), float(m.group(3))
            elapsed = (h * 3600) + (m_min * 60) + s
            prog_pct = min(1.0, elapsed / total_duration)
            current_pct = int(start_pct + ((end_pct - start_pct) * prog_pct))
            _set(job_id, step="{} ({}%)...".format(base_step, int(prog_pct * 100)), pct=current_pct)

    proc.wait()
    render_time = time.time() - start_time

    if proc.returncode != 0:
        logger.error("[FFMPEG FATAL] Job {} crashed after {:.1f}s. Exit code: {}".format(job_id, render_time, proc.returncode))
        raise RuntimeError("FFmpeg process failed with exit code {}".format(proc.returncode))

    logger.info("[FFMPEG SUCCESS] Job {} completed render in {:.1f} seconds.".format(job_id, render_time))

_TITLE_JUNK = re.compile(
    r"\s*[\(\[](official|music|video|audio|lyrics|hd|hq|mv|clip|live|feat\.?.*|"
    r"official\s+\w+\s+video|4k|full)[\)\]]",
    re.IGNORECASE,
)

def _clean_title(title: str) -> str:
    return _TITLE_JUNK.sub("", title).strip(" -–—")

def _toks(s: str) -> set:
    return set(re.findall(r"\w+", (s or "").lower()))

def _is_relevant(want_title: str, want_artist: str, got_title: str, got_artist: str) -> bool:
    wt = _toks(want_title)
    if not wt: return True
    gt = _toks(got_title)
    title_overlap = len(wt & gt) / len(wt)
    wa, ga = _toks(want_artist), _toks(got_artist)
    artist_match = bool(wa and ga and (wa & ga))
    return title_overlap >= (0.4 if artist_match else 0.6)

def _fetch_lrclib(title: str) -> str:
    try:
        parts = title.split(" - ", 1)
        artist, track = (parts[0], parts[1]) if len(parts) == 2 else ("", title)
        params = urllib.parse.urlencode({"artist_name": artist, "track_name": track})
        req = urllib.request.Request("https://lrclib.net/api/search?{}".format(params), headers={"User-Agent": "karaoke-gen/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            results = json.loads(r.read())
        for item in results:
            plain = (item.get("plainLyrics") or "").strip()
            if not plain: continue
            if not _is_relevant(track, artist, item.get("trackName", ""), item.get("artistName", "")): continue
            return plain
    except Exception: pass
    return ""

def _parse_lrc(lrc_text: str):
    lines = []
    for line in lrc_text.splitlines():
        m = re.match(r"\[(\d+):(\d+(?:\.\d+)?)\]\s*(.*)", line)
        if m:
            mins, secs, text = int(m.group(1)), float(m.group(2)), m.group(3).strip()
            if text: lines.append((mins * 60 + secs, text))
    if lines:
        lines.sort(key=lambda x: x[0])
        plain = "\n".join(text for _, text in lines)
        return lines, plain
    plain = re.sub(r"\[[\d:.]+\]", "", lrc_text).strip()
    return None, plain

def _fetch_yandex_lyrics(title: str) -> str:
    try:
        parts = title.split(" - ", 1)
        want_artist, want_track = (parts[0], parts[1]) if len(parts) == 2 else ("", title)
        query = "{} {}".format(parts[0], parts[1]) if len(parts) == 2 else title
        params = urllib.parse.urlencode({"text": query, "type": "track", "page": 0})
        req = urllib.request.Request("https://music.yandex.ru/handlers/music-search.jsx?{}".format(params), headers={"User-Agent": "karaoke-gen/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())
        tracks = data.get("tracks", {}).get("items", [])
        if not tracks: return ""
        track = None
        for cand in tracks[:5]:
            got_artist = " ".join(a.get("name", "") for a in cand.get("artists", []))
            if _is_relevant(want_track, want_artist, cand.get("title", ""), got_artist):
                track = cand
                break
        if not track: return ""
        track_id, album_id = track.get("id"), track.get("albums", [{}])[0].get("id", "")
        if not track_id: return ""
        lyric_req = urllib.request.Request("https://music.yandex.ru/api/v2.1/handlers/track/{}:{}/lyrics/json".format(track_id, album_id), headers={"User-Agent": "karaoke-gen/1.0", "Accept": "application/json"})
        with urllib.request.urlopen(lyric_req, timeout=8) as r:
            text = json.loads(r.read()).get("lyrics", {}).get("fullLyrics", "")
            if text: return text.strip()
    except Exception: pass
    return ""

def _fetch_genius(title: str) -> str:
    try:
        import lyricsgenius
        token = os.environ.get("GENIUS_ACCESS_TOKEN", "")
        if not token: return ""
        genius = lyricsgenius.Genius(token, verbose=False, timeout=10, retries=1)
        genius.remove_section_headers = True
        parts = title.split(" - ", 1)
        song = genius.search_song(parts[1].strip(), parts[0].strip()) if len(parts) == 2 else genius.search_song(title)
        if song and song.lyrics:
            text = re.sub(r"\d*Embed$", "", song.lyrics).strip()
            lines = text.split("\n")
            if lines and lines[0].endswith("Lyrics"): lines = lines[1:]
            return "\n".join(lines).strip()
    except Exception: pass
    return ""

def _fetch_lyrics(title: str):
    clean = _clean_title(title)
    try:
        import syncedlyrics
        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FTimeout
        def _synced(term: str):
            ex = ThreadPoolExecutor(max_workers=1)
            try: return ex.submit(syncedlyrics.search, term).result(timeout=15)
            except FTimeout: return None
            finally: ex.shutdown(wait=False)
        lrc = _synced(clean) or _synced(title)
        if lrc:
            synced, plain = _parse_lrc(lrc)
            if plain: return synced, plain
    except Exception: pass

    for q in (clean, title):
        plain = _fetch_lrclib(q)
        if plain: return None, plain
    for q in (clean, title):
        plain = _fetch_yandex_lyrics(q)
        if plain: return None, plain
    for q in (clean, title):
        plain = _fetch_genius(q)
        if plain: return None, plain
    return None, ""

def _detect_chorus(lines: list[str], min_block: int = 2, min_repeats: int = 2) -> list[tuple[int, int]]:
    if len(lines) < min_block * min_repeats: return []
    norm_lines = [re.sub(r"[^\w\s]", "", l.lower()).strip() for l in lines]
    found_ranges, used = [], set()

    for block_size in range(min(8, len(lines) // 2), min_block - 1, -1):
        for i in range(len(norm_lines) - block_size + 1):
            if any(j in used for j in range(i, i + block_size)): continue
            block = tuple(norm_lines[i:i + block_size])
            if not any(b for b in block): continue

            occurrences = [j for j in range(len(norm_lines) - block_size + 1) if tuple(norm_lines[j:j + block_size]) == block]
            if len(occurrences) >= min_repeats:
                for idx, occ in enumerate(occurrences):
                    occ_range = range(occ, occ + block_size)
                    if any(j in used for j in occ_range): continue
                    for j in occ_range: used.add(j)
                    if idx > 0: found_ranges.append((occ, occ + block_size))
    return sorted(found_ranges)

def _remove_chorus_lines(lyrics: str) -> str:
    lines = [l for l in lyrics.splitlines() if l.strip()]
    if not lines: return lyrics
    chorus_ranges = _detect_chorus(lines)
    if not chorus_ranges: return lyrics
    remove_idxs = {i for start, end in chorus_ranges for i in range(start, end)}
    return "\n".join(l for i, l in enumerate(lines) if i not in remove_idxs)

@app.post("/api/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    file: Optional[UploadFile] = File(None),
    youtube_url: Optional[str] = Form(None),
    model: str = Form("medium"),
    language: str = Form("auto"),
    lyrics_hint: Optional[str] = Form(None),
    word_timing: bool = Form(True),
    static_video: bool = Form(False),
    keep_chorus: bool = Form(True),
    show_bg_lyrics: bool = Form(False),
    display_mode: str = Form("subtitles"),
    video_bg: str = Form("color"),
    cover_image: Optional[UploadFile] = File(None),
):
    url = (youtube_url or "").strip()
    if not file and not url:
        raise HTTPException(400, "Provide a file or a URL")

    job_id = str(uuid.uuid4())
    job_dir = WORK_DIR / job_id
    job_dir.mkdir()
    jobs[job_id] = {"status": "pending", "step": "Queued", "pct": 0, "error": "", "files": {}}
    hint = (lyrics_hint or "").strip()

    cover_path = ""
    if cover_image and cover_image.filename:
        cover_p = job_dir / "cover{}".format(Path(cover_image.filename).suffix or '.jpg')
        cover_p.write_bytes(await cover_image.read())
        cover_path = str(cover_p)

    if file and file.filename:
        input_path = job_dir / "input{}".format(Path(file.filename).suffix or '.mp3')
        input_path.write_bytes(await file.read())
        file_title = Path(file.filename).stem
        _set(job_id, title=file_title)
        artist, _ = _parse_artist_title(file_title)
        upsert_song(job_id, title=file_title, artist=artist, lyrics=hint)
        background_tasks.add_task(process_audio, job_id, str(input_path), model, language, hint, word_timing, static_video, keep_chorus, display_mode, video_bg, cover_path)
    else:
        _set(job_id, title="track", youtube_url=url)
        upsert_song(job_id, title="track", source_url=url, lyrics=hint)
        background_tasks.add_task(process_url, job_id, url, model, language, hint, word_timing, static_video, keep_chorus, display_mode, video_bg, cover_path)

    return {"job_id": job_id}

@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    if job_id not in jobs: raise HTTPException(404, "Job not found")
    return jobs[job_id]

@app.get("/api/jobs/{job_id}/download/{file_key}")
def download_file(job_id: str, file_key: str):
    if job_id not in jobs: raise HTTPException(404, "Job not found")
    path_str = jobs[job_id].get("files", {}).get(file_key)
    if not path_str or not Path(path_str).exists(): raise HTTPException(404, "File not ready")
    p = Path(path_str)
    return FileResponse(str(p), filename=p.name, media_type="application/octet-stream")

def _detect_source(url: str) -> str:
    u = url.lower()
    if "spotify.com" in u: return "spotify"
    if "soundcloud.com" in u: return "soundcloud"
    if "music.yandex" in u: return "yandex"
    return "youtube"

def _fetch_yt_subtitles(url: str, job_dir: Path) -> str:
    try:
        sub_tpl = str(job_dir / "subs.%(ext)s")
        _run(["yt-dlp", "--skip-download", "--write-subs", "--write-auto-subs", "--sub-langs", "ru,be,uk,en", "--sub-format", "srv3/vtt/srt/best", "--convert-subs", "srt", "-o", sub_tpl, url])
        for f in sorted(job_dir.glob("subs.*.srt")):
            raw = f.read_text(encoding="utf-8", errors="ignore")
            text = re.sub(r"\d+\n\d{2}:\d{2}:\d{2},\d+ --> [^\n]+\n", "", raw)
            text = re.sub(r"<[^>]+>", "", text)
            text = re.sub(r"\n{2,}", "\n", text).strip()
            if text: return text
    except Exception: pass
    return ""

def _download_spotify(url: str, job_dir: Path) -> tuple[str, str]:
    _run(["spotdl", "--output", str(job_dir / "{artists} - {title}.{output-ext}"), "--format", "mp3", "--threads", "1", url])
    mp3_files = list(job_dir.glob("*.mp3"))
    if not mp3_files: raise RuntimeError("spotdl produced no output file")
    return str(mp3_files[0]), mp3_files[0].stem

def _download_yt_dlp(url: str, job_dir: Path) -> tuple[str, str]:
    output_tpl = str(job_dir / "input.%(ext)s")
    _run(["yt-dlp", "-x", "--audio-format", "mp3", "-o", output_tpl, url])
    title = ""
    try: title = _run(["yt-dlp", "--get-title", "--no-playlist", url]).strip()
    except Exception: pass
    audio_files = list(job_dir.glob("input.*"))
    if not audio_files: raise RuntimeError("yt-dlp produced no output file")
    return str(audio_files[0]), title

def _download_yt_video(url: str, job_dir: Path) -> str:
    output_tpl = str(job_dir / "original_video.%(ext)s")
    _run(["yt-dlp", "-f", "bestvideo[height<=1080]+bestaudio/best[height<=1080]", "--merge-output-format", "mp4", "-o", output_tpl, url])
    for f in job_dir.glob("original_video.*"): return str(f)
    return ""

def _fetch_yt_thumbnail(url: str, job_dir: Path) -> str:
    try:
        _run(["yt-dlp", "--skip-download", "--write-thumbnail", "--convert-thumbnails", "jpg", "-o", str(job_dir / "yt_thumb.%(ext)s"), url])
        for f in job_dir.glob("yt_thumb*.jpg"): return str(f)
    except Exception: pass
    return ""

def process_url(job_id: str, url: str, model: str, language: str = "auto", lyrics_hint: str = "", word_timing: bool = True, static_video: bool = False, keep_chorus: bool = True, display_mode: str = "subtitles", video_bg: str = "color", cover_path: str = "") -> None:
    job_dir = WORK_DIR / job_id
    source = _detect_source(url)
    try:
        _set(job_id, status="running", step="Downloading audio from {}...".format(source.title()))
        if source == "spotify": audio_path, title = _download_spotify(url, job_dir)
        else: audio_path, title = _download_yt_dlp(url, job_dir)

        if title:
            _set(job_id, title=title)
            artist, _ = _parse_artist_title(title)
            upsert_song(job_id, title=title, artist=artist, source_url=url)

        original_video = _download_yt_video(url, job_dir) if video_bg == "original" and source == "youtube" else ""
        if not cover_path and source == "youtube" and video_bg == "cover": cover_path = _fetch_yt_thumbnail(url, job_dir)

        if not lyrics_hint and source == "youtube":
            lyrics_hint = _fetch_yt_subtitles(url, job_dir)
            if lyrics_hint: _set(job_id, lyrics_found=True, lyrics_text=lyrics_hint)

        process_audio(job_id, audio_path, model, language, lyrics_hint, word_timing, static_video, keep_chorus, display_mode, video_bg, cover_path, original_video)
    except Exception as exc:
        logger.exception("URL CRASH IN JOB {}".format(job_id))
        _set(job_id, status="error", step="Failed", error=str(exc))

def process_audio(job_id: str, input_path: str, model: str, language: str = "auto", lyrics_hint: str = "", word_timing: bool = True, static_video: bool = False, keep_chorus: bool = True, display_mode: str = "subtitles", video_bg: str = "color", cover_path: str = "", original_video: str = "") -> None:
    job_dir = WORK_DIR / job_id
    title = jobs[job_id].get("title", "track")
    safe = "".join(c for c in title if c.isalnum() or c in " -_").strip() or "track"

    try:
        logger.info("[PHASE 1] Starting Demucs vocal separation for job {}".format(job_id))
        _set(job_id, status="running", step="Separating vocals with Demucs (0%)...", pct=5)
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        proc = subprocess.Popen(
            [sys.executable, "-m", "demucs", "--two-stems", "vocals", "-n", "htdemucs", "--out", str(job_dir), input_path],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1, env=env,
        )
        output_buf, all_output = "", []
        while True:
            chunk = proc.stdout.read(256)
            if not chunk: break
            all_output.append(chunk)
            output_buf += chunk
            for part in re.split(r"[\r\n]", output_buf):
                m = re.search(r"(\d+)%\|", part)
                if m: _set(job_id, step="Separating vocals with Demucs ({}%)...".format(m.group(1)), pct=5 + int(int(m.group(1)) * 0.35))
            output_buf = re.split(r"[\r\n]", output_buf)[-1]
        proc.wait()
        if proc.returncode != 0: raise RuntimeError("".join(all_output)[-3000:])

        no_vocals_wav = list(job_dir.rglob("no_vocals.wav"))[0]
        vocals_candidates = list(job_dir.rglob("vocals.wav"))
        vocals_wav = vocals_candidates[0] if vocals_candidates else None

        minus_path = job_dir / "{}_minus.mp3".format(safe)
        _run([FFMPEG, "-i", str(no_vocals_wav), "-q:a", "2", str(minus_path), "-y"])
        jobs[job_id]["files"]["minus"] = str(minus_path)
        _cleanup_memory("Demucs Separation")

        if (static_video or display_mode == "background") and lyrics_hint:
            logger.info("[PHASE 2 - BYPASS] Executing static text video bypass for job {}".format(job_id))
            _set(job_id, step="Generating static lyrics video...", pct=50)
            dur_out = _run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", input_path])
            duration = float(dur_out.strip() or "0") or 300.0
            ass_path = job_dir / "{}_karaoke.ass".format(safe)
            ass_gen.generate_static_ass(lyrics_hint, duration, str(ass_path))
            jobs[job_id]["files"]["ass"] = str(ass_path)

            video_path = job_dir / "{}_karaoke.mp4".format(safe)
            safe_title_static = title.replace(":", "\\:")
            title_filter_static = "drawtext=text='{}':fontfile=/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf:fontsize=36:fontcolor=white@0.7:x=(w-text_w)/2:y=30:shadowcolor=black@0.6:shadowx=2:shadowy=2".format(safe_title_static)

            cmd = [
                FFMPEG,
                "-y",
                "-f", "lavfi", "-i", "color=c=0x0d0d1a:size=1920x1080:rate=25",
                "-i", str(minus_path),
                "-vf", "ass={},{}".format(ass_path, title_filter_static),
                "-shortest",
                "-c:v", "h264_nvenc",
                "-preset", "p2",
                "-cq", "30",
                "-b:v", "1500k",
                "-pix_fmt", "yuv420p",
                "-profile:v", "high",
                "-c:a", "aac",
                "-b:a", "128k",
                "-movflags", "+faststart",
                str(video_path)
            ]

            _run_ffmpeg_with_progress(cmd, job_id, "Rendering Static Video", 70, 99, duration)
            jobs[job_id]["files"]["video"] = str(video_path)

            _set(job_id, status="done", step="Done!", pct=100)
            artist, _ = _parse_artist_title(title)
            upsert_song(job_id, title=title, artist=artist, status="done", video_path=str(video_path), minus_path=str(minus_path), ass_path=str(ass_path))
            return

        _set(job_id, step="Searching for lyrics online...", pct=42)
        synced_lines, lyrics = (None, lyrics_hint) if lyrics_hint else _fetch_lyrics(title)

        if lyrics:
            chorus_ranges = _detect_chorus([l for l in lyrics.splitlines() if l.strip()])
            _set(job_id, lyrics_found=True, lyrics_text=lyrics, chorus_detected=len(chorus_ranges) > 0, chorus_count=len(chorus_ranges))
            if not keep_chorus:
                lyrics = _remove_chorus_lines(lyrics)
                if lyrics_hint: lyrics_hint = lyrics
                if synced_lines:
                    remaining = set(re.sub(r"[^\w\s]", "", l.lower()).strip() for l in lyrics.splitlines() if l.strip())
                    synced_lines = [(t, text) for t, text in synced_lines if re.sub(r"[^\w\s]", "", text.lower()).strip() in remaining]

        _set(job_id, _input_path=input_path, _vocals_wav=str(vocals_wav) if vocals_wav else None, _minus_path=str(minus_path), _model=model, _language=language, _lyrics_hint=lyrics_hint, _lyrics=lyrics, _synced_lines=synced_lines, _safe=safe, _word_timing=word_timing, _keep_chorus=keep_chorus, _display_mode=display_mode, _video_bg=video_bg, _cover_path=cover_path, _original_video=original_video, retry_count=0)
        _run_transcription_and_render(job_id, input_path, str(vocals_wav) if vocals_wav else None, str(minus_path), model, language, lyrics_hint, lyrics, safe, title, job_dir, RETRY_SETTINGS[0], word_timing, synced_lines, display_mode, video_bg, cover_path, original_video)

    except Exception as exc:
        logger.exception("CRITICAL CRASH IN AUDIO PROC FOR JOB {}".format(job_id))
        _set(job_id, status="error", step="Failed", error=str(exc))

def _transfer_whisper_timing(seg, lyric_words: list[str]):
    from types import SimpleNamespace
    whisper_words = getattr(seg, "words", None)
    if not whisper_words or len(whisper_words) == 0: return None
    def _nw(s): return re.sub(r"[^\w]", "", s.lower())
    w_list = [(_nw(w.word), w) for w in whisper_words]
    l_list = [(_nw(w), w) for w in lyric_words]
    if not w_list or not l_list: return None

    used, aligned = set(), []
    for li, (l_norm, l_text) in enumerate(l_list):
        best_wi, best_score = None, -1
        expected_pos = li / len(l_list) * len(w_list)
        for wi, (w_norm, w_obj) in enumerate(w_list):
            if wi in used: continue
            score = 2.0 if l_norm == w_norm else 1.0 if l_norm in w_norm or w_norm in l_norm else 0.5 if len(l_norm) > 2 and len(w_norm) > 2 and (l_norm[:3] == w_norm[:3]) else -1
            if score < 0: continue
            score -= (abs(wi - expected_pos) / max(len(w_list), 1)) * 0.3
            if score > best_score: best_score, best_wi = score, wi
        if best_wi is not None:
            used.add(best_wi)
            aligned.append(SimpleNamespace(word=" {}".format(l_text), start=w_list[best_wi][1].start, end=w_list[best_wi][1].end, probability=1.0))
        else: aligned.append(None)

    for i, a in enumerate(aligned):
        if a is not None: continue
        prev_end, next_start = seg.start, seg.end
        for j in range(i - 1, -1, -1):
            if aligned[j] is not None: prev_end = aligned[j].end; break
        for j in range(i + 1, len(aligned)):
            if aligned[j] is not None: next_start = aligned[j].start; break
        gap_count = sum(1 for j in range(i, len(aligned)) if aligned[j] is None)
        gap_dur = max(next_start - prev_end, 0.01)
        char_lens = [max(len(lyric_words[i + k]), 1) for k in range(gap_count)]
        total_c, cum = sum(char_lens), 0
        for k in range(gap_count):
            if aligned[i + k] is not None: continue
            aligned[i + k] = SimpleNamespace(word=" {}".format(lyric_words[i + k]), start=prev_end + gap_dur * (cum / total_c), end=prev_end + gap_dur * ((cum + char_lens[k]) / total_c), probability=0.5)
            cum += char_lens[k]

    if sum(1 for a in aligned if a is not None and a.probability == 1.0) < len(lyric_words) * 0.3: return None
    return aligned

def _align_to_lyrics(segments, lyric_lines: list[str]):
    from types import SimpleNamespace
    import difflib

    result = []
    mapped_lines = []

    # Pass 1: Map lyrics to best available Whisper segments
    matched_segments = set()
    for line in lyric_lines:
        clean_line = re.sub(r"[^\w\s]", "", line.lower()).strip()
        if not clean_line: continue

        best_seg = None
        best_score = -1.0

        for j, seg in enumerate(segments):
            if j in matched_segments: continue
            clean_seg = re.sub(r"[^\w\s]", "", seg.text.lower()).strip()
            score = difflib.SequenceMatcher(None, clean_line, clean_seg).ratio()

            # Boost if segment contains the line
            if clean_line in clean_seg:
                score += 0.5

            if score > best_score:
                best_score = score
                best_seg = seg

        lyric_words = re.findall(r"\S+", line)

        if best_seg is not None and best_score > 0.15:
            matched_segments.add(segments.index(best_seg))
            new_words = _transfer_whisper_timing(best_seg, lyric_words)
            if new_words:
                mapped_lines.append({'line': line, 'words': new_words, 'start': new_words[0].start, 'end': new_words[-1].end, 'is_mapped': True})
            else:
                mapped_lines.append({'line': line, 'words': None, 'start': best_seg.start, 'end': best_seg.end, 'is_mapped': True})
        else:
            mapped_lines.append({'line': line, 'words': None, 'start': -1, 'end': -1, 'is_mapped': False})

    # Pass 2: Interpolate missing lines (The Fix)
    # We tether orphans to a 3s max window between valid mapped lines
    last_valid_end = 0.0
    for i, m in enumerate(mapped_lines):
        if not m['is_mapped']:
            # Look ahead for the next mapped anchor
            next_start = last_valid_end + 3.0
            for j in range(i + 1, len(mapped_lines)):
                if mapped_lines[j]['is_mapped']:
                    next_start = mapped_lines[j]['start']
                    break

            # Anchor to previous valid time + small offset
            gap = max(0.5, next_start - last_valid_end)
            m['start'] = last_valid_end + 0.2
            # Force duration to be short (max 3s) so it doesn't drift
            m['end'] = m['start'] + min(3.0, gap * 0.8)
            m['is_mapped'] = True

        last_valid_end = m['end']

    # Pass 3: Finalize segments
    for m in mapped_lines:
        if not m['words']:
            lyric_words = re.findall(r"\S+", m['line'])
            dur = max(m['end'] - m['start'], 0.5)
            char_lengths = [max(len(w), 1) for w in lyric_words]
            total_chars = max(sum(char_lengths), 1)
            new_words = []
            cum = 0
            for w, c_len in zip(lyric_words, char_lengths):
                w_start = m['start'] + dur * (cum / total_chars)
                w_end = m['start'] + dur * ((cum + c_len) / total_chars)
                new_words.append(SimpleNamespace(word=" " + w, start=w_start, end=w_end, probability=0.5))
                cum += c_len
            m['words'] = new_words

        result.append(SimpleNamespace(start=m['start'], end=m['end'], text=" " + m['line'], words=m['words']))

    return result

def _run_transcription_and_render(job_id, input_path, vocals_wav, minus_path, model, language, lyrics_hint, lyrics, safe, title, job_dir, whisper_settings=None, word_timing=False, synced_lines=None, display_mode="subtitles", video_bg="color", cover_path="", original_video=""):
    if whisper_settings is None: whisper_settings = RETRY_SETTINGS[0]
    lang_label = language if language != "auto" else "auto-detect"

    # ---------------------------------------------------------
    # PHASE 2: WHISPER TRANSCRIPTION
    # ---------------------------------------------------------
    logger.info("[PHASE 2] Starting Whisper Transcription for job {}".format(job_id))
    _set(job_id, step="Transcribing with Whisper ({}, {})...".format(model, lang_label), pct=45)

    from faster_whisper import WhisperModel
    device = os.environ.get("WHISPER_DEVICE", "cuda")
    compute_type = "int8"
    wm = None
    segments_list_raw = []
    duration = 1.0

    try:
        try:
            logger.info("Loading Whisper '{}' on '{}' with '{}'".format(model, device, compute_type))
            wm = WhisperModel(model, device=device, compute_type=compute_type)
        except RuntimeError as e:
            if "cuda" in str(e).lower() or "out of memory" in str(e).lower():
                logger.warning("CUDA error. Falling back to CPU for Whisper.")
                device = "cpu"
                _cleanup_memory("Whisper CUDA Fail")
                wm = WhisperModel(model, device="cpu", compute_type="int8")
            else: raise
        except Exception as e:
            logger.warning("Unexpected error loading model: {}. Falling back to CPU.".format(e))
            device = "cpu"
            _cleanup_memory("Whisper General Fail")
            wm = WhisperModel(model, device="cpu", compute_type="int8")

        BE_PROMPT = "Беларуская мова. Словы песні па-беларуску."
        initial_prompt = (lyrics_hint[:1000] if lyrics_hint else BE_PROMPT) if language == "be" else (lyrics[:1000] if lyrics else None)
        transcribe_path = vocals_wav if vocals_wav else input_path

        def _do_transcribe(w):
            return w.transcribe(transcribe_path, word_timestamps=True, language=None if language == "auto" else language, initial_prompt=initial_prompt, condition_on_previous_text=False, **whisper_settings)

        t_start = time.time()
        try:
            segments_gen, info = _do_transcribe(wm)
            duration = info.duration or 1.0
            segments_list_raw = list(segments_gen)
        except Exception as e:
            if "out of memory" in str(e).lower() and device == "cuda":
                _set(job_id, step="GPU OOM — retrying on CPU ({})...".format(model), pct=45)
                del wm
                _cleanup_memory("Whisper Execution OOM")
                wm = WhisperModel(model, device="cpu", compute_type="int8")
                segments_gen, info = _do_transcribe(wm)
                duration = info.duration or 1.0
                segments_list_raw = list(segments_gen)
            else: raise

        logger.info("[PHASE 2 SUCCESS] Transcription took {:.1f}s. Total segments: {}".format(time.time() - t_start, len(segments_list_raw)))
    except Exception as e:
        logger.exception("[PHASE 2 FATAL] Whisper crashed for job {}".format(job_id))
        raise
    finally:
        if wm is not None:
            del wm
        _cleanup_memory("Transcription")


    # ---------------------------------------------------------
    # PHASE 3: ALIGNMENT & FILTERING
    # ---------------------------------------------------------
    logger.info("[PHASE 3] Starting Subtitle Alignment for job {}".format(job_id))
    try:
        BE_PROMPT_WORDS = set(re.findall(r"\w+", BE_PROMPT.lower()))
        def _is_lyric_segment(text):
            words = re.findall(r"\w+", text.lower().translate(str.maketrans("ўіІЎёЁ", "уиИУеЕ")))
            if not words: return False
            if BE_PROMPT_WORDS and all(w in BE_PROMPT_WORDS for w in re.findall(r"\w+", text.lower())): return False
            lyric_words = set(re.findall(r"\w+", (lyrics or "").lower().translate(str.maketrans("ўіІЎёЁ", "уиИУеЕ"))))
            if not lyric_words: return True
            return sum(1 for w in words if w in lyric_words) / len(words) >= (0.25 if lyrics_hint else 0.3)

        filtered, text_counts = [], {}
        for seg in segments_list_raw:
            norm = seg.text.strip().lower()
            text_counts[norm] = text_counts.get(norm, 0) + 1
            if text_counts[norm] >= 3 or not _is_lyric_segment(seg.text): continue
            filtered.append(seg)
            _set(job_id, step="Aligning Text — {}%...".format(min(99, int(seg.end / duration * 100))), pct=45 + int(min(99, int(seg.end / duration * 100)) * 0.35))

        if synced_lines and not lyrics_hint:
            from types import SimpleNamespace
            segments = []
            for i, (start_sec, line_text) in enumerate(synced_lines):
                end_sec = synced_lines[i + 1][0] if i + 1 < len(synced_lines) else (filtered[-1].end if filtered else start_sec + 5.0)
                words = re.findall(r"\S+", line_text)
                if not words: continue
                best_seg, best_overlap = None, 0
                for seg in filtered:
                    overlap = min(seg.end, end_sec) - max(seg.start, start_sec)
                    if overlap > best_overlap: best_overlap, best_seg = overlap, seg
                new_words = _transfer_whisper_timing(best_seg, words) if best_seg else None
                if not new_words:
                    dur, char_lens = max(end_sec - start_sec, 0.1), [max(len(w), 1) for w in words]
                    total_c, cum, new_words = sum(char_lens), 0, []
                    for w in words:
                        new_words.append(SimpleNamespace(word=" {}".format(w), start=start_sec + dur * (cum / total_c), end=start_sec + dur * ((cum + max(len(w), 1)) / total_c), probability=1.0))
                        cum += max(len(w), 1)
                segments.append(SimpleNamespace(start=start_sec, end=end_sec, text=" {}".format(line_text), words=new_words))
        elif lyrics_hint and filtered:
            segments = _align_to_lyrics(filtered, [l.strip() for l in lyrics_hint.splitlines() if l.strip()])
        else:
            segments = filtered

        logger.info("[PHASE 3 SUCCESS] Alignment complete. {} output segments.".format(len(segments)))
    except Exception as e:
        logger.exception("[PHASE 3 FATAL] Alignment logic crashed for job {}".format(job_id))
        raise
    finally:
        del segments_list_raw
        del filtered
        _cleanup_memory("Alignment")


    # ---------------------------------------------------------
    # PHASE 4: ASS GENERATION
    # ---------------------------------------------------------
    logger.info("[PHASE 4] Starting ASS generation for job {}".format(job_id))
    try:
        t_start = time.time()
        _set(job_id, step="Generating karaoke subtitles...", pct=82)
        ass_path = job_dir / "{}_karaoke.ass".format(safe)

        ass_gen.generate_ass(segments, str(ass_path), word_timing=word_timing, background_lyrics=(lyrics or lyrics_hint or "") if display_mode == "both" else "", duration=duration if display_mode == "both" else 0)
        jobs[job_id]["files"]["ass"] = str(ass_path)
        logger.info("[PHASE 4 SUCCESS] ASS generation took {:.2f}s".format(time.time() - t_start))
    except Exception as e:
        logger.exception("[PHASE 4 FATAL] ASS generation failed for job {}".format(job_id))
        raise
    finally:
        del segments
        _cleanup_memory("ASS Generation")


    # ---------------------------------------------------------
    # PHASE 5: FFMPEG VIDEO RENDER
    # ---------------------------------------------------------
    logger.info("[PHASE 5] Starting Final Video Render for job {}".format(job_id))
    try:
        _set(job_id, step="Rendering karaoke video...", pct=88)
        video_path = job_dir / "{}_karaoke.mp4".format(safe)

        safe_title = title.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
        title_filter = "drawtext=text='{}':fontfile=/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf:fontsize=36:fontcolor=white@0.7:x=(w-text_w)/2:y=30:shadowcolor=black@0.6:shadowx=2:shadowy=2".format(safe_title)

        encode_args = [
            "-c:v", "h264_nvenc",
            "-preset", "p2",
            "-cq", "30",
            "-b:v", "1500k",
            "-pix_fmt", "yuv420p",
            "-profile:v", "high",
            "-c:a", "aac",
            "-b:a", "128k",
            "-movflags", "+faststart",
            str(video_path)
        ]

        if video_bg == "original" and original_video and Path(original_video).exists():
            ffmpeg_cmd = [
                FFMPEG,
                "-y",
                "-i", original_video,
                "-i", str(minus_path),
                "-vf", "scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,ass={},{}".format(ass_path, title_filter),
                "-map", "0:v", "-map", "1:a", "-shortest"
            ] + encode_args
        elif video_bg == "cover" and cover_path and Path(cover_path).exists():
            bg_dur = max(0.1, duration - 5.0)
            filter_str = "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[cover];[1:v]trim=duration={0:.3f},setpts=PTS-STARTPTS[bg];[cover][bg]concat=n=2:v=1:a=0[base];[base]ass={1},{2}[vout]".format(bg_dur, ass_path, title_filter)
            ffmpeg_cmd = [
                FFMPEG,
                "-y",
                "-loop", "1",
                "-t", "5.0", "-i", cover_path,
                "-f", "lavfi",
                "-i", "color=c=0x0d0d1a:size=1920x1080:rate=25",
                "-i", str(minus_path),
                "-filter_complex", filter_str,
                "-map", "[vout]",
                "-map", "2:a",
                "-shortest"
            ] + encode_args
        else:
            ffmpeg_cmd = [
                FFMPEG,
                "-y",
                "-f", "lavfi", "-i", "color=c=0x0d0d1a:size=1920x1080:rate=25",
                "-i", str(minus_path),
                "-vf", "ass={},{}".format(ass_path, title_filter),
                "-shortest"
            ] + encode_args

        _run_ffmpeg_with_progress(ffmpeg_cmd, job_id, "Rendering Video", 88, 99, duration)

        jobs[job_id]["files"]["video"] = str(video_path)
        _set(job_id, status="done", step="Done!", pct=100)

        artist, _ = _parse_artist_title(title)
        upsert_song(job_id, title=title, artist=artist, status="done", video_path=str(video_path), minus_path=str(minus_path), ass_path=str(ass_path), lyrics=lyrics or lyrics_hint or "")

    except Exception as e:
        logger.exception("[PHASE 5 FATAL] Final Render crashed for job {}".format(job_id))
        raise
    finally:
        _cleanup_memory("Video Render")


class LyricsPayload(BaseModel): lyrics: str

@app.post("/api/jobs/{job_id}/update-lyrics")
async def update_lyrics(job_id: str, payload: LyricsPayload, background_tasks: BackgroundTasks):
    if job_id not in jobs: raise HTTPException(404, "Job not found")
    if jobs[job_id].get("status") not in ("done", "error"): raise HTTPException(400, "Job still running")
    new_lyrics = payload.lyrics.strip()
    _set(job_id, lyrics_text=new_lyrics, _lyrics_hint=new_lyrics, _lyrics=new_lyrics, retry_count=0, status="running", step="Re-running with updated lyrics...", pct=45, error="", text_stars=None, video_stars=None)
    upsert_song(job_id, lyrics=new_lyrics)
    background_tasks.add_task(retry_transcription, job_id)
    return {"retrying": True}

class RatingPayload(BaseModel): text_stars: int; video_stars: int

@app.post("/api/jobs/{job_id}/rate")
async def rate_job(job_id: str, payload: RatingPayload, background_tasks: BackgroundTasks):
    if job_id not in jobs: raise HTTPException(404, "Job not found")
    job = jobs[job_id]
    if job.get("status") != "done": raise HTTPException(400, "Job not done yet")
    _set(job_id, text_stars=payload.text_stars, video_stars=payload.video_stars)
    retry_count = job.get("retry_count", 0)
    if (payload.text_stars < 4 or payload.video_stars < 4) and retry_count < len(RETRY_SETTINGS) - 1:
        next_retry = retry_count + 1
        _set(job_id, retry_count=next_retry)
        background_tasks.add_task(retry_transcription, job_id)
        return {"retrying": True, "attempt": next_retry}
    return {"retrying": False}

def retry_transcription(job_id: str) -> None:
    job = jobs[job_id]
    retry_count = job.get("retry_count", 1)
    if not job.get("_input_path") or not job.get("_minus_path"):
        _set(job_id, status="error", step="Failed", error="Missing params for retry")
        return
    _set(job_id, status="running", step="Retrying transcription (attempt {})...".format(retry_count), pct=45, error="", text_stars=None, video_stars=None)
    try:
        _run_transcription_and_render(job_id, job["_input_path"], job.get("_vocals_wav"), job["_minus_path"], job.get("_model"), job.get("_language", "auto"), job.get("_lyrics_hint", ""), job.get("_lyrics", ""), job.get("_safe", "track"), job.get("title", ""), WORK_DIR / job_id, RETRY_SETTINGS[min(retry_count, len(RETRY_SETTINGS) - 1)], job.get("_word_timing", False), job.get("_synced_lines"), job.get("_display_mode", "subtitles"), job.get("_video_bg", "color"), job.get("_cover_path", ""), job.get("_original_video", ""))
    except Exception as exc:
        _set(job_id, status="error", step="Failed", error=str(exc))

@app.get("/api/catalog")
def api_catalog(search: str = "", limit: int = 50, offset: int = 0):
    songs = list_songs(search=search, limit=limit, offset=offset)
    for song in songs:
        for key in ("video_path", "minus_path", "ass_path", "thumbnail_path"):
            if song.get(key) and not Path(song[key]).exists(): song[key] = ""
    return {"songs": songs, "total": count_songs(search=search)}

@app.get("/api/catalog/{job_id}")
def api_catalog_song(job_id: str):
    song = get_song(job_id)
    if not song: raise HTTPException(404, "Song not found in catalog")
    return song

@app.delete("/api/catalog/{job_id}")
def api_catalog_delete(job_id: str):
    if not get_song(job_id): raise HTTPException(404, "Song not found")
    job_dir = WORK_DIR / job_id
    if job_dir.exists(): shutil.rmtree(job_dir, ignore_errors=True)
    delete_song(job_id)
    jobs.pop(job_id, None)
    return {"deleted": True}

def _generate_thumbnail(title: str, output_path: str) -> None:
    safe_title = title.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")
    artist, song = _parse_artist_title(title)
    text_val = "{}\\n{}".format(artist, song) if artist else safe_title
    vf_str = "drawtext=text='{}':fontfile=/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf:fontsize=64:fontcolor=white:x=(w-text_w)/2:y=(h-text_h)/2-40:shadowcolor=black:shadowx=3:shadowy=3,drawtext=text='KARAOKE':fontfile=/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf:fontsize=36:fontcolor=yellow:x=(w-text_w)/2:y=(h/2)+50:shadowcolor=black:shadowx=2:shadowy=2".format(text_val)
    _run([FFMPEG, "-y", "-f", "lavfi", "-i", "color=c=0x0d0d1a:size=1280x720:d=1", "-vf", vf_str, "-frames:v", "1", "-update", "1", str(output_path)])

def _generate_youtube_metadata(job_id: str, title: str, lyrics: str, output_path: str) -> None:
    artist, song = _parse_artist_title(title)
    yt_title = "{} - {} (Karaoke)".format(artist, song) if artist else "{} (Karaoke)".format(title)
    tags = ["karaoke", "instrumental", "sing along", "lyrics"] + ([artist.lower(), song.lower()] if artist else [])
    desc_lyrics = "--- Lyrics ---\n" + lyrics[:4500] if lyrics else ""
    metadata = {
        "title": yt_title,
        "description": "{}\n\nKaraoke version with word-by-word highlighting.\nGenerated with Karaoke Generator.\n\n{}".format(yt_title, desc_lyrics),
        "tags": tags, "category": "10", "privacy": "public"
    }
    with open(output_path, "w", encoding="utf-8") as f: json.dump(metadata, f, ensure_ascii=False, indent=2)

@app.post("/api/catalog/{job_id}/prepare-youtube")
async def api_prepare_youtube(job_id: str):
    song = get_song(job_id)
    if not song: raise HTTPException(404, "Song not found")
    if song["status"] != "done": raise HTTPException(400, "Song not ready yet")
    job_dir, title, lyrics = WORK_DIR / job_id, song["title"] or "track", song.get("lyrics", "")
    thumb_path, meta_path = job_dir / "thumbnail.jpg", job_dir / "youtube_metadata.json"
    try: _generate_thumbnail(title, str(thumb_path))
    except Exception: thumb_path = None
    _generate_youtube_metadata(job_id, title, lyrics, str(meta_path))
    safe = "".join(c for c in title if c.isalnum() or c in " -_").strip() or "track"
    zip_path = job_dir / "{}_youtube.zip".format(safe)
    with zipfile.ZipFile(str(zip_path), "w", zipfile.ZIP_DEFLATED) as zf:
        if song.get("video_path") and Path(song["video_path"]).exists(): zf.write(song["video_path"], "{}_karaoke.mp4".format(safe))
        if song.get("minus_path") and Path(song["minus_path"]).exists(): zf.write(song["minus_path"], "{}_minus.mp3".format(safe))
        if song.get("ass_path") and Path(song["ass_path"]).exists(): zf.write(song["ass_path"], "{}_karaoke.ass".format(safe))
        if thumb_path and thumb_path.exists(): zf.write(str(thumb_path), "thumbnail.jpg")
        if meta_path.exists(): zf.write(str(meta_path), "youtube_metadata.json")
    upsert_song(job_id, youtube_ready=1, thumbnail_path=str(thumb_path) if thumb_path and thumb_path.exists() else "")
    jobs.setdefault(job_id, {}).setdefault("files", {})["youtube_zip"] = str(zip_path)
    return {"ready": True, "download_url": "/api/jobs/{}/download/youtube_zip".format(job_id)}

app.mount("/", StaticFiles(directory="static", html=True), name="static")
