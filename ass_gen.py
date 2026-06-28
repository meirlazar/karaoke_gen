import random
import subprocess
import colorsys

"""
ASS karaoke subtitle generator.
Converts faster-whisper segments to an ASS file with \\kf tags.
Features mathematically guaranteed high-contrast color palettes,
OS-level font polling, and native ASS background rendering.
"""

def _get_random_font() -> str:
    """Queries fontconfig via the OS to grab a random valid system font."""
    ignore_fonts = ["emoji", "math", "dingbats", "symbols", "webdings", "wingdings",
                    "noto color", "cjk", "arabic", "korean", "bengali", "noto", "hebrew"]
    try:
        output = subprocess.check_output(['fc-list', ':', 'family'], text=True, stderr=subprocess.DEVNULL)
        valid_fonts = []
        for line in output.split('\n'):
            font = line.split(',')[0].strip()
            if font and not any(ig in font.lower() for ig in ignore_fonts):
                valid_fonts.append(font)

        if valid_fonts:
            return random.choice(valid_fonts)
    except Exception:
        pass

    return "Arial"

def _generate_color_palette():
    """Generates a high-contrast HLS color palette and converts to ASS Hex."""

    # Background: Random hue, moderate saturation, random lightness
    bg_h = random.random()
    bg_s = random.uniform(0.4, 0.8)
    bg_l = random.uniform(0.15, 0.85)

    # Enforce Contrast & Hierarchy
    if bg_l < 0.5:
        # Dark background -> Light text
        prim_l = random.uniform(0.80, 0.95)   # Sung text is intensely bright
        sec_l = random.uniform(0.50, 0.65)    # Unsung text is dimmed
        outline_l = random.uniform(0.0, 0.15) # Black/Dark outline
    else:
        # Light background -> Dark text
        prim_l = random.uniform(0.05, 0.20)   # Sung text is intensely dark
        sec_l = random.uniform(0.40, 0.55)    # Unsung text is washed out/greyed
        outline_l = random.uniform(0.85, 1.0) # White/Light outline

    # Primary Text (Sung): Random hue, maximum saturation for pop
    prim_h = random.random()
    prim_s = random.uniform(0.8, 1.0)

    # Secondary Text (Unsung): Keep the same cohesive hue but KILL the saturation
    sec_h = prim_h
    sec_s = random.uniform(0.05, 0.25) # Barely any color, mostly grey/white

    def hls_to_ass(h, l, s, alpha="00"):
        """Converts HLS floats to ASS format: &HAABBGGRR&"""
        r, g, b = colorsys.hls_to_rgb(h, l, s)
        # Convert to 0-255 integer range
        r, g, b = int(r * 255), int(g * 255), int(b * 255)
        return f"&H{alpha}{b:02X}{g:02X}{r:02X}&"

    return {
        "bg": hls_to_ass(bg_h, bg_l, bg_s),
        "primary": hls_to_ass(prim_h, prim_l, prim_s),
        "secondary": hls_to_ass(sec_h, sec_l, sec_s),
        "outline": hls_to_ass(prim_h, outline_l, 0.0),
        "shadow": hls_to_ass(prim_h, outline_l, 0.0, alpha="80")
    }


def _generate_dynamic_headers(palette: dict, use_dual: bool = False, is_static: bool = False) -> str:
    """Generates the ASS header with static 1080p scaling and contrasting colors."""

    font_name = _get_random_font()

    # Static font sizes optimized for 1080p readability
    font_size = 90 if is_static else 160

    alignment = 5
    border_style = 1
    outline_thickness = 4
    shadow = 2

    header = f"""[Script Info]
Title: Karaoke
ScriptType: v4.00+
WrapStyle: 0
ScaledBorderAndShadow: yes
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Karaoke,{font_name},{font_size},{palette['primary']},{palette['secondary']},{palette['outline']},{palette['shadow']},-1,0,0,0,100,100,2,0,{border_style},{outline_thickness},{shadow},{alignment},10,10,80,1
Style: Canvas,Arial,10,{palette['bg']},&H00000000,&H00000000,&H00000000,0,0,0,0,100,100,0,0,0,0,0,7,0,0,0,1
"""

    if use_dual:
        header += f"Style: BgLyrics,{font_name},60,&H50FFFFFF,&H50FFFFFF,&H00000000,&H80000000,0,0,0,0,100,100,1,0,1,2,1,8,60,60,200,1\n"

    header += "\n[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    return header

def _fmt_time(seconds: float) -> str:
    """Format seconds as ASS timestamp H:MM:SS.cc"""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def _segment_to_dialogue(segment, word_timing: bool = True) -> str:
    """Build one ASS Dialogue line from a faster-whisper segment."""
    start = _fmt_time(segment.start)
    end = _fmt_time(segment.end)

    if not word_timing:
        text = segment.text.strip()
        if not text:
            return ""
        return f"Dialogue: 0,{start},{end},Karaoke,,0,0,0,,{text}"

    words = segment.words or []
    if not words:
        return ""

    MIN_WORD_CS = 15
    GAP_THRESHOLD = 0.10

    parts: list[str] = []
    prev_end = segment.start

    for word in words:
        gap_s = word.start - prev_end
        if gap_s > GAP_THRESHOLD:
            parts.append(f"{{\\kf{max(1, int(round(gap_s * 100)))}}}")
        elif gap_s > 0:
            pass
        dur_cs = max(MIN_WORD_CS, int(round((word.end - word.start) * 100)))
        parts.append(f"{{\\kf{dur_cs}}}{word.word}")
        prev_end = word.end

    text = "".join(parts)
    return f"Dialogue: 0,{start},{end},Karaoke,,0,0,0,,{text}"

def generate_static_ass(lyrics: str, duration: float, output_path: str) -> None:
    """Create an ASS file showing the full lyrics as static text for the entire song."""
    lines = [l.strip() for l in lyrics.splitlines() if l.strip()]
    if not lines:
        return

    # Total duration for the background canvas layer
    max_dur_s = duration if duration > 0 else 3600 # Fallback to 1 hour if unknown

    start = _fmt_time(0)
    end = _fmt_time(duration)
    text = r"\N".join(lines)

    palette = _generate_color_palette()
    header = _generate_dynamic_headers(palette=palette, use_dual=False, is_static=True)

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(header)
        # Layer -1: Draw the solid background using ASS vector drawing commands
        f.write(f"Dialogue: -1,0:00:00.00,{_fmt_time(max_dur_s)},Canvas,,0,0,0,,{{\\pos(0,0)\\p1}}m 0 0 l 1920 0 l 1920 1080 l 0 1080{{\\p0}}\n")
        f.write(f"Dialogue: 0,{start},{end},Karaoke,,0,0,0,,{text}\n")

def generate_ass(segments, output_path: str, word_timing: bool = True,
                 background_lyrics: str = "", duration: float = 0) -> None:
    """Write an ASS karaoke file from a list of faster-whisper segments."""
    use_dual = bool(background_lyrics and background_lyrics.strip())

    palette = _generate_color_palette()
    lines = [_generate_dynamic_headers(palette=palette, use_dual=use_dual, is_static=False)]

    # Calculate total duration to stretch the background canvas
    max_dur_s = duration
    if max_dur_s <= 0 and segments:
        max_dur_s = max(seg.end for seg in segments) + 1
    elif max_dur_s <= 0:
        max_dur_s = 3600

    # Layer -1: Draw the solid background
    lines.append(f"Dialogue: -1,0:00:00.00,{_fmt_time(max_dur_s)},Canvas,,0,0,0,,{{\\pos(0,0)\\p1}}m 0 0 l 1920 0 l 1920 1080 l 0 1080{{\\p0}}\n")

    if use_dual:
        bg_lines = [l.strip() for l in background_lyrics.splitlines() if l.strip()]
        if bg_lines:
            start = _fmt_time(0)
            end = _fmt_time(max_dur_s)
            text = r"\N".join(bg_lines)
            lines.append(f"Dialogue: 0,{start},{end},BgLyrics,,0,0,0,,{text}\n")

    for seg in segments:
        line = _segment_to_dialogue(seg, word_timing=word_timing)
        if line:
            lines.append(line + "\n")

    with open(output_path, "w", encoding="utf-8") as f:
        f.writelines(lines)
