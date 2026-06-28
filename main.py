def _run_transcription_and_render(
    job_id, input_path, vocals_wav, minus_path, model, language,
    lyrics_hint, lyrics, safe, title, job_dir, whisper_settings=None, word_timing=False,
    synced_lines=None, display_mode="subtitles", video_bg="color", cover_path="", original_video="",
):
    if whisper_settings is None:
        whisper_settings = RETRY_SETTINGS[0]

    lang_label = language if language != "auto" else "auto-detect"
    _set(job_id, step=f"Transcribing with Whisper ({model}, {lang_label})...", pct=45)

    from faster_whisper import WhisperModel  # noqa: PLC0415
    import gc  # noqa: PLC0415
    import torch  # noqa: PLC0415

    device = os.environ.get("WHISPER_DEVICE", "cuda")
    compute_type = "int8_float16" if device == "cuda" else "int8"
    
    wm = None
    try:
        # Try CUDA, but log the attempt and fallback gracefully
        try:
            print(f"[DEBUG] Attempting to load Whisper model '{model}' on device='{device}' with compute_type='{compute_type}'...", flush=True)
            wm = WhisperModel(model, device=device, compute_type=compute_type)
            print(f"[DEBUG] Successfully loaded on {device}", flush=True)
        except RuntimeError as e:
            error_msg = str(e)
            print(f"[DEBUG] RuntimeError loading model on {device}: {error_msg}", flush=True)
            if "cuda" in error_msg.lower() or "out of memory" in error_msg.lower():
                print(f"[DEBUG] CUDA-related error detected. Falling back to CPU.", flush=True)
                device = "cpu"
                # Clean up any stuck GPU memory before fallback
                gc.collect()
                if hasattr(torch, 'cuda') and torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    print(f"[DEBUG] Cleared GPU cache", flush=True)
                wm = WhisperModel(model, device="cpu", compute_type="int8")
                print(f"[DEBUG] Successfully loaded on CPU", flush=True)
            else:
                raise
        except Exception as e:
            error_msg = str(e)
            print(f"[DEBUG] Unexpected error loading model on {device}: {type(e).__name__}: {error_msg}", flush=True)
            print(f"[DEBUG] Falling back to CPU.", flush=True)
            # Clean up GPU memory before fallback
            gc.collect()
            if hasattr(torch, 'cuda') and torch.cuda.is_available():
                torch.cuda.empty_cache()
                print(f"[DEBUG] Cleared GPU cache", flush=True)
            device = "cpu"
            wm = WhisperModel(model, device="cpu", compute_type="int8")
            print(f"[DEBUG] Successfully loaded on CPU", flush=True)

        BE_PROMPT = "Беларуская мова. Словы песні па-беларуску."
        BE_PROMPT_WORDS = set(re.findall(r"\w+", BE_PROMPT.lower()))
        if language == "be":
            initial_prompt = lyrics_hint[:1000] if lyrics_hint else BE_PROMPT
        else:
            initial_prompt = lyrics[:1000] if lyrics else None

        transcribe_path = vocals_wav if vocals_wav else input_path

        def _do_transcribe(w):
            gen, inf = w.transcribe(
                transcribe_path,
                word_timestamps=True,
                language=None if language == "auto" else language,
                initial_prompt=initial_prompt,
                condition_on_previous_text=False,
                **whisper_settings,
            )
            return gen, inf

        try:
            segments_gen, info = _do_transcribe(wm)
            segments_list_raw = list(segments_gen)
        except Exception as e:
            if "out of memory" in str(e).lower() and device == "cuda":
                _set(job_id, step=f"GPU OOM — retrying on CPU ({model})...", pct=45)
                print(f"[DEBUG] OOM on GPU, cleaning up and retrying on CPU...", flush=True)
                # Cleanup before retry
                del wm
                gc.collect()
                if hasattr(torch, 'cuda') and torch.cuda.is_available():
                    torch.cuda.empty_cache()
                    print(f"[DEBUG] Cleared GPU cache before CPU retry", flush=True)
                wm = WhisperModel(model, device="cpu", compute_type="int8")
                segments_gen, info = _do_transcribe(wm)
                segments_list_raw = list(segments_gen)
            else:
                raise

        _CYR_NORM = str.maketrans("ўіІЎёЁ", "уиИУеЕ")

        def _norm(s):
            return s.lower().translate(_CYR_NORM)

        lyric_words: set[str] = set()
        if lyrics:
            for w in re.findall(r"\w+", _norm(lyrics)):
                lyric_words.add(w)

        def _is_lyric_segment(text):
            words = re.findall(r"\w+", _norm(text))
            if not words:
                return False
            if BE_PROMPT_WORDS and all(w in BE_PROMPT_WORDS for w in re.findall(r"\w+", text.lower())):
                return False
            if not lyric_words:
                return True
            threshold = 0.25 if lyrics_hint else 0.3
            overlap = sum(1 for w in words if w in lyric_words) / len(words)
            return overlap >= threshold

        filtered = []
        duration = info.duration or 1
        text_counts: dict[str, int] = {}
        for seg in segments_list_raw:
            normalized = seg.text.strip().lower()
            text_counts[normalized] = text_counts.get(normalized, 0) + 1
            if text_counts[normalized] >= 3:
                continue
            if not _is_lyric_segment(seg.text):
                continue
            filtered.append(seg)
            seg_pct = min(99, int(seg.end / duration * 100))
            _set(job_id, step=f"Transcribing with Whisper ({model}, {lang_label}) — {seg_pct}%...",
                 pct=45 + int(seg_pct * 0.35))

        # If we have synced LRC lines, use them as segment anchors (better timing than Whisper segments)
        if synced_lines and not lyrics_hint:
            from types import SimpleNamespace  # noqa: PLC0415
            segments = []
            for i, (start_sec, line_text) in enumerate(synced_lines):
                end_sec = synced_lines[i + 1][0] if i + 1 < len(synced_lines) else (
                    filtered[-1].end if filtered else start_sec + 5.0
                )
                words = re.findall(r"\S+", line_text)
                if not words:
                    continue
                # Try to find a Whisper segment near this time to borrow word timestamps
                best_seg = None
                best_overlap = 0
                for seg in filtered:
                    overlap = min(seg.end, end_sec) - max(seg.start, start_sec)
                    if overlap > best_overlap:
                        best_overlap, best_seg = overlap, seg

                new_words = None
                if best_seg:
                    new_words = _transfer_whisper_timing(best_seg, words)
                if not new_words:
                    # Character-weighted fallback
                    dur = max(end_sec - start_sec, 0.1)
                    char_lens = [max(len(w), 1) for w in words]
                    total_c = sum(char_lens)
                    cum = 0
                    new_words = []
                    for w in words:
                        frac_s = cum / total_c
                        cum += max(len(w), 1)
                        frac_e = cum / total_c
                        new_words.append(SimpleNamespace(
                            word=f" {w}", start=start_sec + dur * frac_s,
                            end=start_sec + dur * frac_e, probability=1.0,
                        ))
                segments.append(SimpleNamespace(
                    start=start_sec, end=end_sec, text=f" {line_text}", words=new_words,
                ))
        elif lyrics_hint and filtered:
            # User provided lyrics — replace text with exact lyric lines (keep timestamps)
            lyric_lines = [l.strip() for l in lyrics_hint.splitlines() if l.strip()]
            segments = _align_to_lyrics(filtered, lyric_lines)
        else:
            segments = filtered

        _set(job_id, step="Generating karaoke subtitles...", pct=82)
        from ass_gen import generate_ass  # noqa: PLC0415
        ass_path = job_dir / f"{safe}_karaoke.ass"
        bg_lyrics = (lyrics or lyrics_hint or "") if display_mode == "both" else ""
        duration = info.duration if display_mode == "both" else 0
        generate_ass(segments, str(ass_path), word_timing=word_timing,
                     background_lyrics=bg_lyrics, duration=duration)
        jobs[job_id]["files"]["ass"] = str(ass_path)

        _set(job_id, step="Rendering karaoke video...", pct=88)
        video_path = job_dir / f"{safe}_karaoke.mp4"
        safe_title = title.replace("\\", "\\\\").replace("'", "\\'").replace(":", "\\:")

        title_filter = (
            "drawtext=text='{txt}'"
            ":fontfile=/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"
            ":fontsize=36:fontcolor=white@0.7"
            ":x=(w-text_w)/2:y=30"
            ":shadowcolor=black@0.6:shadowx=2:shadowy=2"
        ).format(txt=safe_title)

        # Build FFmpeg command based on video background mode
        if video_bg == "original" and original_video and Path(original_video).exists():
            # Use original YouTube video as background, overlay subtitles
            ffmpeg_cmd = [
                FFMPEG,
                "-i", original_video,
                "-i", str(minus_path),
                "-vf", f"scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,ass={ass_path},{title_filter}",
                "-map", "0:v", "-map", "1:a",
                "-shortest",
            ]
        elif video_bg == "cover" and cover_path and Path(cover_path).exists():
            # 5-second cover intro, then dark background for the rest of the song.
            # The bg segment must span the remaining song length, otherwise concat
            # produces a ~6s clip and -shortest truncates the whole output to it.
            intro = 5.0
            bg_dur = max(0.1, (info.duration or 0) - intro)
            ffmpeg_cmd = [
                FFMPEG,
                "-loop", "1", "-t", str(intro), "-i", cover_path,
                "-f", "lavfi", "-i", "color=c=0x0d0d1a:size=1920x1080:rate=25",
                "-i", str(minus_path),
                "-filter_complex",
                f"[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1[cover];"
                f"[1:v]trim=duration={bg_dur:.3f},setpts=PTS-STARTPTS[bg];"
                f"[cover][bg]concat=n=2:v=1:a=0[base];"
                f"[base]ass={ass_path},{title_filter}[vout]",
                "-map", "[vout]", "-map", "2:a",
                "-shortest",
            ]
        else:
            # Default: solid dark background
            ffmpeg_cmd = [
                FFMPEG,
                "-f", "lavfi", "-i", "color=c=0x0d0d1a:size=1920x1080:rate=25",
                "-i", str(minus_path),
                "-vf", f"ass={ass_path},{title_filter}",
                "-shortest",
            ]

        ffmpeg_cmd.extend([
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-pix_fmt", "yuv420p", "-profile:v", "high", "-level", "4.1",
            "-c:a", "aac", "-b:a", "192k",
            "-movflags", "+faststart",
            str(video_path), "-y",
        ])
        _run(ffmpeg_cmd)
        jobs[job_id]["files"]["video"] = str(video_path)
        _set(job_id, status="done", step="Done!", pct=100)
        artist, _ = _parse_artist_title(title)
        upsert_song(job_id, title=title, artist=artist, status="done",
                    video_path=str(video_path), minus_path=str(minus_path),
                    ass_path=str(ass_path), lyrics=lyrics or lyrics_hint or "")

    except Exception as e:
        print(f"[ERROR] Transcription failed: {e}", flush=True)
        import traceback
        traceback.print_exc(file=sys.stdout)
        raise
    finally:
        # Always cleanup GPU memory on exit
        if wm is not None:
            print(f"[DEBUG] Cleaning up Whisper model from {device}...", flush=True)
            del wm
        gc.collect()
        if hasattr(torch, 'cuda') and torch.cuda.is_available():
            torch.cuda.empty_cache()
            print(f"[DEBUG] Cleared GPU cache on exit", flush=True)
