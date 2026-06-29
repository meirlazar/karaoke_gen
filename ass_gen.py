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
    is_dark_mode = random.choice([True, False])

    bg_h = random.random()
    bg_s = random.uniform(0.3, 0.7)
    bg_l = random.uniform(0.05, 0.20) if is_dark_mode else random.uniform(0.80, 0.95)

    prim_h = random.random()
    prim_s = random.uniform(0.8, 1.0)

    if is_dark_mode:
        prim_l = random.uniform(0.75, 0.95)
        sec_l  = random.uniform(0.40, 0.55)
        outline_l = random.uniform(0.0, 0.05)
    else:
        prim_l = random.uniform(0.05, 0.20)
        sec_l  = random.uniform(0.45, 0.60)
        outline_l = random.uniform(0.95, 1.0)

    sec_h = prim_h
    sec_s = random.uniform(0.1, 0.3)

    def hls_to_ass(h, l, s, alpha="00"):
        r, g, b = (int(x * 255) for x in colorsys.hls_to_rgb(h, l, s))
        return "&H{}{:02X}{:02X}{:02X}&".format(alpha, b, g, r)

    return {
        "bg": hls_to_ass(bg_h, bg_l, bg_s),
        "primary": hls_to_ass(prim_h, prim_l, prim_s),
        "secondary": hls_to_ass(sec_h, sec_l, sec_s),
        "outline": hls_to_ass(prim_h, outline_l, 0.0),
        "shadow": hls_to_ass(prim_h, outline_l, 0.0, alpha="80")
    }

def _generate_dynamic_headers(palette: dict, use_dual: bool = False, is_static: bool = False) -> str:
    font = _get_random_font()
    sz = 90 if is_static else 160

    header = """[Script Info]
Title: Karaoke
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,{},{},{},{},{},{},-1,0,0,0,100,100,2,0,1,4,2,5,10,10,80,1
Style: Canvas,Arial,10,{},&H00000000&,&H00000000&,&H00000000&,0,0,0,0,100,100,0,0,0,0,0,7,0,0,0,1
""".format(font, sz, palette['primary'], palette['secondary'], palette['outline'], palette['shadow'], palette['bg'])

    if use_dual:
        header += "Style: BgLyrics,{},60,&H50FFFFFF&,&H50FFFFFF&,&H00000000&,&H80000000&,0,0,0,0,100,100,1,0,1,2,1,8,60,60,200,1\n".format(font)

    return header + "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"

def _fmt_time(sec: float) -> str:
    return "{}:{:02d}:{:05.2f}".format(int(sec // 3600), int((sec % 3600) // 60), sec % 60)

def _segment_to_dialogue(seg, word_timing: bool = True) -> str:
    start, end = _fmt_time(seg.start), _fmt_time(seg.end)
    if not word_timing:
        return "Dialogue: 0,{},{},Karaoke,,0,0,0,,{}".format(start, end, seg.text.strip()) if seg.text.strip() else ""
    if not seg.words: return ""

    parts, prev = [], seg.start
    for w in seg.words:
        gap = w.start - prev
        if gap > 0.10: parts.append("{{\\kf{}}}".format(max(1, int(round(gap * 100)))))
        parts.append("{{\\kf{}}}{}".format(max(15, int(round((w.end - w.start) * 100))), w.word))
        prev = w.end

    return "Dialogue: 0,{},{},Karaoke,,0,0,0,,{}".format(start, end, ''.join(parts))

def generate_static_ass(lyrics: str, duration: float, output_path: str) -> None:
    lines = [l.strip() for l in lyrics.splitlines() if l.strip()]
    if not lines: return

    dur_s = duration if duration > 0 else 3600
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(_generate_dynamic_headers(_generate_color_palette(), False, True))
        f.write("Dialogue: -1,0:00:00.00,{},Canvas,,0,0,0,,{{\\pos(0,0)\\p1}}m 0 0 l 1920 0 l 1920 1080 l 0 1080{{\\p0}}\n".format(_fmt_time(dur_s)))
        f.write("Dialogue: 0,{},{},Karaoke,,0,0,0,,{}\n".format(_fmt_time(0), _fmt_time(duration), '\\N'.join(lines)))

def generate_ass(segments, output_path: str, word_timing: bool = True, background_lyrics: str = "", duration: float = 0) -> None:
    seg_list = list(segments)
    bg_lines = [l.strip() for l in background_lyrics.splitlines() if l.strip()]
    use_dual = bool(bg_lines)

    print("[DEBUG] generate_ass called... bg_lyrics: {} lines, use_dual={}".format(len(bg_lines), use_dual), flush=True)

    dur_s = duration if duration > 0 else (max(s.end for s in seg_list) + 1 if seg_list else 3600)
    print("[DEBUG] Final duration: {}s (calculated or provided)".format(dur_s), flush=True)

    try:
        with open(output_path, "w", encoding="utf-8") as f:
            f.write(_generate_dynamic_headers(_generate_color_palette(), use_dual, False))
            f.write("Dialogue: -1,0:00:00.00,{},Canvas,,0,0,0,,{{\\pos(0,0)\\p1}}m 0 0 l 1920 0 l 1920 1080 l 0 1080{{\\p0}}\n".format(_fmt_time(dur_s)))

            if use_dual:
                f.write("Dialogue: 0,{},{},BgLyrics,,0,0,0,,{}\n".format(_fmt_time(0), _fmt_time(dur_s), '\\N'.join(bg_lines)))
                print("[DEBUG] Wrote background lyrics line with {} lines".format(len(bg_lines)), flush=True)

            dialogue_count = 0
            for idx, s in enumerate(seg_list):
                line = _segment_to_dialogue(s, word_timing)
                if line:
                    f.write("{}\n".format(line))
                    dialogue_count += 1
            print("[DEBUG] Successfully wrote {} dialogue lines".format(dialogue_count), flush=True)
    except Exception as e:
        print("[ERROR] Failed to write ASS file: {}".format(e), flush=True)
        raise
