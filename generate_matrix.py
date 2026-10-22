"""Render the Matrix profile as a looping GIF and a static PNG."""

import json
import random
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent
WIDTH, HEIGHT = 900, 610
FRAMES, DURATION = 80, 150
CHARS = "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ<>/{}[]:#*+-"


def font(size):
    try:
        return ImageFont.truetype("consola.ttf", size)
    except OSError:
        return ImageFont.truetype("DejaVuSansMono.ttf", size)


def information(profile, stats):
    overlay = Image.new("RGBA", (WIDTH, HEIGHT))
    draw = ImageDraw.Draw(overlay)
    draw.rounded_rectangle(
        (30, 24, WIDTH - 30, HEIGHT - 24),
        radius=12,
        fill=(1, 10, 5, 207),
        outline=(29, 65, 39, 220),
    )
    face = font(19)
    green, label, value = "#54ff89", "#a1d9ae", "#e4f5e8"

    def text(x, y, content, color=value):
        draw.text((x, y), content, font=face, fill=color, anchor="lt")

    def row(y, title, content):
        text(58, y, title, label)
        text(205, y, content)

    def heading(y, title):
        text(58, y, "─ " + title, green)
        start = 58 + face.getlength("─ " + title) + 18
        draw.line((start, y + 11, WIDTH - 60, y + 11), fill="#174c2a")

    text(58, 52, profile["username"] + "@github:~$ whoami", green)
    for y, key, title in [
        (106, "title", "Title"),
        (144, "ide", "IDE"),
        (182, "languages", "Languages"),
        (220, "hobbies", "Hobbies"),
    ]:
        row(y, title, profile[key])
    heading(276, "Contact")
    row(314, "Email", profile["email"])
    heading(370, "Stats")
    row(408, "Repos", f"{stats['repos']:,}")
    for y, title, key in [(446, "PRs", "prs"), (484, "Commits", "commits")]:
        total = f"{stats[key]:,}"
        row(y, title, total)
        recent = stats["recent_" + key]
        if recent > 0:
            text(205 + face.getlength(total), y, f" (+{recent:,})", green)
    text(58, 522, "Lines", label)
    x = 205
    for content, color in [
        (f"{stats['lines']:,} (", value),
        (f"+{stats['recent_additions']:,}", green),
        (" / ", value),
        (f"-{stats['recent_deletions']:,}", "#ff7b81"),
        (")", value),
    ]:
        text(x, 522, content, color)
        x += face.getlength(content)
    return overlay


def generate(profile, stats, destination):
    overlay = information(profile, stats)
    randomizer = random.Random(42)
    streams = [
        (
            x,
            randomizer.randrange(HEIGHT),
            randomizer.choice((1, 2)),
            randomizer.randrange(10, 24),
            randomizer.randrange(1000),
        )
        for x in range(4, WIDTH, 17)
    ]
    rain_font = font(14)
    frames = []
    for frame in range(FRAMES):
        image = Image.new("RGBA", (WIDTH, HEIGHT), "#020805")
        draw = ImageDraw.Draw(image)
        for x, offset, turns, length, seed in streams:
            # Integer turns guarantee the position repeats after exactly 80 frames.
            head = (offset + frame * HEIGHT * turns / FRAMES) % HEIGHT
            for tail in range(length):
                y = (head - tail * 17) % HEIGHT
                intensity = (1 - tail / length) * 0.8
                color = (
                    (150, 255, 180)
                    if tail == 0
                    else (
                        round(26 * intensity),
                        round(237 * intensity),
                        round(91 * intensity),
                    )
                )
                char = CHARS[(seed + tail * 13 + frame // 4) % len(CHARS)]
                draw.text((x, y), char, font=rain_font, fill=color, anchor="lt")
        image = Image.alpha_composite(image, overlay).convert("RGB")
        frames.append(image)

    frames[0].save(destination.with_suffix(".png"))
    frames[0].save(
        destination,
        save_all=True,
        append_images=frames[1:],
        duration=DURATION,
        loop=0,
        optimize=True,
        disposal=1,
    )
    with Image.open(destination) as result:
        assert result.n_frames == FRAMES
        assert result.size == (WIDTH, HEIGHT)
        assert result.info["loop"] == 0
        assert (
            sum(result.seek(i) or result.info["duration"] for i in range(FRAMES))
            == FRAMES * DURATION
        )
    assert destination.stat().st_size < 10_000_000
    print(
        f"Generated {destination}: {destination.stat().st_size:,} bytes, {FRAMES * DURATION / 1000:g} seconds"
    )


if __name__ == "__main__":
    generate(
        json.loads((ROOT / "profile.json").read_text(encoding="utf-8")),
        json.loads((ROOT / "assets/stats.json").read_text(encoding="utf-8")),
        ROOT / "assets/profile.gif",
    )
