import random, subprocess, colorsys, sys

def _get_random_font() -> str:
    ignore = ["emoji", "math", "dingbats", "symbols", "webdings", "wingdings", "noto color", "cjk", "arabic", "korean", "bengali", "noto", "hebrew"]
    try:
        out = subprocess.check_output(['fc-list', ':', 'family'], text=True, stderr=subprocess.DEVNULL)
        valid = [line.split(',')[0].strip() for line in out.split('\n') if line.strip() and not any(ig in line.lower() for ig in ignore)]
        if valid: return random.choice(valid)
    except Exception: pass
    return "Arial"

def _generate_color_palette():
    """Generates a mathematically guaranteed high-contrast ASS color palette."""

    # 1. Banish the midtones. Force the canvas to be strictly Dark or strictly Light.
    is_dark_mode = random.choice([True, False])

    bg_h = random.random()
    bg_s = random.uniform(0.3, 0.7) # Keep background saturation slightly muted

    # Clamp background lightness strictly to the outer 20% margins
    bg_l = random.uniform(0.05, 0.20) if is_dark_mode else random.uniform(0.80, 0.95)

    # 2. Text Colors: High saturation for the Primary (Sung) word
    prim_h = random.random()
    prim_s = random.uniform(0.8, 1.0)

    if is_dark_mode:
        # Dark Canvas -> Very Bright Text, Pitch Black Outline
        prim_l = random.uniform(0.75, 0.95)
        sec_l  = random.uniform(0.40, 0.55) # Dimmed enough to drop back, bright enough to read
        outline_l = random.uniform(0.0, 0.05)
    else:
        # Light Canvas -> Very Dark Text, Pure White Outline
        prim_l = random.uniform(0.05, 0.20)
        sec_l  = random.uniform(0.45, 0.60) # Washed out grey
        outline_l = random.uniform(0.95, 1.0)

    # 3. Secondary Text (Unsung): SHIFT hue for true contrast + reduce saturation
    sec_h = (prim_h + random.uniform(0.25, 0.45)) % 1.0  # ← Shift hue by 90-160 degrees for complementary/triadic contrast
    sec_s = random.uniform(0.5, 0.8)  # ← Keep secondary vibrant but less saturated than primary

    def hls_to_ass(h, l, s, alpha="00"):
        # Convert HLS floats to RGB integers (0-255)
        r, g, b = (int(x * 255) for x in colorsys.hls_to_rgb(h, l, s))
        # ASS Hex format: &H[Alpha][Blue][Green][Red]&
        return f"&H{alpha}{b:02X}{g:02X}{r:02X}&"

    return {
        "bg": hls_to_ass(bg_h, bg_l, bg_s),
        "primary": hls_to_ass(prim_h, prim_l, prim_s),
        "secondary": hls_to_ass(sec_h, sec_l, sec_s),
        "outline": hls_to_ass(prim_h, outline_l, 0.0), # Saturation 0 = greyscale outline
        "shadow": hls_to_ass(prim_h, outline_l, 0.0, alpha="80")
    }

def _generate_dynamic_headers(palette: dict, use_dual: bool = False, is_static: bool = False) -> str:
    font = _get_random_font()
    sz = 90 if is_static else 160

    header = f"""[Script Info]
Title: Karaoke
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Align, Ma[...]
Style: Karaoke,{font},{sz},{palette['primary']},{palette['secondary']},{palette['outline']},{palette['shadow']},-1,0,0,0,100,100,2,0,1,4,2,5,10,10,80,1
Style: Canvas,Arial,10,{palette['bg']},&H00000000&,&H00000000&,&H00000000&,0,0,0,0,100,100,0,0,0,0,0,7,0,0,0,1
"""
    if use_dual:
        header += f"Style: BgLyrics,{font},60,&H50FFFFFF&,&H50FFFFFF&,&H00000000&,&H80000000&,0,0,0,0,100,100,1,0,1,2,1,8,60,60,200,1\n"

    return header + "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"

def _fmt_time(sec: float) -> str:
    return f"{int(sec // 3600)}:{int((sec % 3600) // 60):02d}:{sec % 60:05.2f}"

def _segment_to_dialogue(seg, word_timing: bool = True) -> str:
    start, end = _fmt_time(seg.start), _fmt_time(seg.end)
    if not word_timing:
        return f"Dialogue: 0,{start},{end},Karaoke,,0,0,0,,{seg.text.strip()}" if seg.text.strip() else ""
    if not seg.words: 
        print(f"[DEBUG] Segment has no words: start={seg.start}, end={seg.end}, text={seg.text[:50] if seg.text else 'EMPTY'}", flush=True)
        return ""

    parts, prev = [], seg.start
    word_count = 0
    for w in seg.words:
        gap = w.start - prev
        if gap > 0.10: 
            parts.append(f"{{\\kf{max(1, int(round(gap * 100)))}}}")
        parts.append(f"{{\\kf{max(15, int(round((w.end - w.start) * 100)))}}{w.word}")
        prev = w.end
        word_count += 1
    
    print(f"[DEBUG] Dialogue line: start={start}, end={end}, word_count={word_count}, text_preview={seg.text[:60] if seg.text else 'EMPTY'}", flush=True)
    return f"Dialogue: 0,{start},{end},Karaoke,,0,0,0,,{''.join(parts)}"

def generate_static_ass(lyrics: str, duration: float, output_path: str) -> None:
    print(f"[DEBUG] generate_static_ass called: duration={duration}, output_path={output_path}, lyrics_len={len(lyrics)}", flush=True)
    lines = [l.strip() for l in lyrics.splitlines() if l.strip()]
    print(f"[DEBUG] Parsed {len(lines)} lyric lines", flush=True)
    if not lines: 
        print("[DEBUG] No lyric lines found, returning early", flush=True)
        return

    dur_s = duration if duration > 0 else 3600
    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(_generate_dynamic_headers(_generate_color_palette(), False, True))
            f.write(f"Dialogue: -1,0:00:00.00,{_fmt_time(dur_s)},Canvas,,0,0,0,,{{\\pos(0,0)\\p1}}m 0 0 l 1920 0 l 1920 1080 l 0 1080{{\\p0}}\n")
            f.write(f"Dialogue: 0,{_fmt_time(0)},{_fmt_time(duration)},Karaoke,,0,0,0,,{r'\\N'.join(lines)}\n")
        print(f"[DEBUG] Successfully wrote static ASS file: {output_path}", flush=True)
    except Exception as e:
        print(f"[ERROR] Failed to write static ASS file: {e}", flush=True)
        raise

def generate_ass(segments, output_path: str, word_timing: bool = True, background_lyrics: str = "", duration: float = 0) -> None:
    print(f"[DEBUG] generate_ass called: word_timing={word_timing}, bg_lyrics_len={len(background_lyrics)}, duration={duration}, output_path={output_path}", flush=True)
    
    # Cast generator to list immediately to prevent exhaustion
    seg_list = list(segments)
    print(f"[DEBUG] Converted segments generator to list: {len(seg_list)} segments", flush=True)
    
    # Inspect first few segments
    for i, seg in enumerate(seg_list[:3]):
        word_count = len(seg.words) if hasattr(seg, 'words') and seg.words else 0
        print(f"[DEBUG] Segment {i}: start={seg.start:.2f}s, end={seg.end:.2f}s, words={word_count}, text_preview={seg.text[:50] if seg.text else 'EMPTY'}", flush=True)
    
    bg_lines = [l.strip() for l in background_lyrics.splitlines() if l.strip()]
    use_dual = bool(bg_lines)
    print(f"[DEBUG] Background lyrics: {len(bg_lines)} lines, use_dual={use_dual}", flush=True)

    # Safe duration calculation
    dur_s = duration if duration > 0 else (max(s.end for s in seg_list) + 1 if seg_list else 3600)
    print(f"[DEBUG] Final duration: {dur_s}s (calculated or provided)", flush=True)

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(_generate_dynamic_headers(_generate_color_palette(), use_dual, False))
            f.write(f"Dialogue: -1,0:00:00.00,{_fmt_time(dur_s)},Canvas,,0,0,0,,{{\\pos(0,0)\\p1}}m 0 0 l 1920 0 l 1920 1080 l 0 1080{{\\p0}}\n")

            if use_dual:
                f.write(f"Dialogue: 0,{_fmt_time(0)},{_fmt_time(dur_s)},BgLyrics,,0,0,0,,{r'\\N'.join(bg_lines)}\n")
                print(f"[DEBUG] Wrote background lyrics line with {len(bg_lines)} lines", flush=True)

            # Standard loop, no walrus operators
            dialogue_count = 0
            for idx, s in enumerate(seg_list):
                line = _segment_to_dialogue(s, word_timing)
                if line:
                    f.write(f"{line}\n")
                    dialogue_count += 1
            
            print(f"[DEBUG] Wrote {dialogue_count} dialogue lines out of {len(seg_list)} segments", flush=True)
        
        print(f"[DEBUG] Successfully wrote ASS file: {output_path}", flush=True)
    except Exception as e:
        print(f"[ERROR] Failed to write ASS file: {e}", flush=True)
        import traceback
        traceback.print_exc(file=sys.stdout)
        raise
