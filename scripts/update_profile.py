#!/usr/bin/env python3
"""Refresh the profile signal console and latest-signal README block.

The website's daily sync is the sole HTB API caller. It publishes a sanitized
JSON projection of already-public HTB, writing, research, and disclosure data;
this script validates that projection and mirrors it into static profile assets.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import os
from dataclasses import dataclass
from pathlib import Path
import re
import tempfile
from typing import Iterable
from urllib.parse import urlparse
import urllib.request
import xml.etree.ElementTree as ET


PROFILE_DATA_URL = "https://foobarto.me/profile-signals.json"
HTB_PROFILE_URL = "https://app.hackthebox.com/public/users/6198"
MAX_SOURCE_BYTES = 256_000
MAX_HTB_AGE = dt.timedelta(days=3)
CONSOLE_START = "<!-- profile-console:start -->"
CONSOLE_END = "<!-- profile-console:end -->"
SIGNALS_START = "<!-- profile-signals:start -->"
SIGNALS_END = "<!-- profile-signals:end -->"

LAND = (
    "................................................................",
    "......................####......................................",
    "...........#########.######..............##################.....",
    "....################.######.....####.#########################..",
    "..###################.####..#..###############################..",
    "...###################.........###############################..",
    ".....#################.......##.##############################..",
    "......################.......#.##############################...",
    "........#############.........#############################.....",
    "........#############.........#############################.....",
    "..........###########..........###########################......",
    "............#######...........#########################.........",
    ".............########.........##############.###..####..........",
    "...............#######........############...###..#####.........",
    "................###.#####.....#############.......######........",
    "....................#######....############.......#######.......",
    "....................########....###########........#######......",
    "....................########....###########.........#######.....",
    ".....................#######....##########...........#######....",
    ".....................#######.....########...........########....",
    ".....................######.......######.#..........########....",
    ".....................#####........#####..............######.....",
    "......................###.............................####...#..",
    "......................##.....................................##.",
    "......................##........................................",
    "......................##........................................",
    "................................................................",
    "................................................................",
    "................................................................",
    "################################################################",
    "....#########################################################...",
    "................................................................",
)

REGIONS = (
    (-100, 45),
    (-60, -15),
    (14, 50),
    (85, 61),
    (46, 27),
    (19, 3),
    (112, 26),
    (144, -27),
)

THEMES = {
    "dark": {
        "bg": "#05100a",
        "border": "#244b31",
        "grid": "#163a26",
        "land": "#1f5c39",
        "accent": "#9fef00",
        "fg": "#d8f5dc",
        "dim": "#6fa77a",
        "scan": "#000000",
    },
    "light": {
        "bg": "#f4f1e7",
        "border": "#91aa98",
        "grid": "#d4ded5",
        "land": "#9bc2a3",
        "accent": "#1f7a34",
        "fg": "#17321f",
        "dim": "#58715e",
        "scan": "#1f7a34",
    },
}


@dataclass(frozen=True)
class Signal:
    kind: str
    title: str
    date: str
    url: str


@dataclass(frozen=True)
class HtbProfile:
    name: str
    rank: str
    ranking: int
    user_owns: int
    system_owns: int
    xp_level: int
    xp_level_title: str
    xp_level_grade: int
    synced_at: dt.datetime


def _https_host_allowed(url: str, hosts: set[str]) -> bool:
    parsed = urlparse(url)
    return (
        parsed.scheme == "https"
        and parsed.hostname in hosts
        and parsed.username is None
        and parsed.password is None
        and parsed.port in (None, 443)
    )


def read_source(source: str) -> str:
    parsed = urlparse(source)
    if parsed.scheme:
        if not _https_host_allowed(source, {"foobarto.me"}):
            raise ValueError(f"source URL is outside the allowlist: {source}")
        request = urllib.request.Request(
            source,
            headers={
                "Accept": "application/json",
                "User-Agent": "foobarto-profile-refresh/1.0",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            final_url = response.geturl()
            if not _https_host_allowed(final_url, {"foobarto.me"}):
                raise ValueError(f"source redirected outside the allowlist: {final_url}")
            payload = response.read(MAX_SOURCE_BYTES + 1)
        if len(payload) > MAX_SOURCE_BYTES:
            raise ValueError(f"source exceeds {MAX_SOURCE_BYTES} bytes: {source}")
        return payload.decode("utf-8", "strict")
    return Path(source).read_text(encoding="utf-8")


def clean_text(value: object, *, field: str, limit: int = 160) -> str:
    if not isinstance(value, str):
        raise ValueError(f"invalid text field: {field}")
    text = re.sub(r"\s+", " ", value).strip()
    if not text:
        raise ValueError(f"empty {field}")
    if any(ord(char) < 32 for char in text):
        raise ValueError(f"control character in {field}")
    if len(text) > limit:
        text = text[: limit - 1].rstrip() + "…"
    return text


def require_int(data: dict[str, object], key: str, *, maximum: int = 10_000_000) -> int:
    value = data.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"invalid integer field: {key}")
    if value < 0 or value > maximum:
        raise ValueError(f"out-of-range integer field: {key}")
    return value


def parse_utc(value: object, *, field: str) -> dt.datetime:
    raw = clean_text(value, field=field, limit=64)
    parsed = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=dt.timezone.utc)
    return parsed.astimezone(dt.timezone.utc)


def parse_date(value: object, *, field: str) -> str:
    raw = clean_text(value, field=field, limit=64)
    try:
        return dt.date.fromisoformat(raw).isoformat()
    except ValueError as error:
        raise ValueError(f"invalid date in {field}: {raw}") from error


def validate_link(url: object, *, hosts: set[str]) -> str:
    value = clean_text(url, field="link", limit=500)
    if re.search(r'''[\s<>()\[\]\\"']''', value):
        raise ValueError(f"signal link contains unsafe characters: {value}")
    if not _https_host_allowed(value, hosts):
        raise ValueError(f"signal link is outside the allowlist: {value}")
    return value


def parse_payload(
    text: str, *, now: dt.datetime | None = None
) -> tuple[HtbProfile, list[Signal]]:
    payload = json.loads(text)
    if not isinstance(payload, dict) or payload.get("schema_version") != 1:
        raise ValueError("unsupported profile-signals schema")

    htb = payload.get("htb")
    if not isinstance(htb, dict):
        raise ValueError("missing HTB object")
    synced_at = parse_utc(htb.get("synced_at"), field="htb.synced_at")
    current = (now or dt.datetime.now(dt.timezone.utc)).astimezone(dt.timezone.utc)
    age = current - synced_at
    if age < dt.timedelta(days=-1) or age > MAX_HTB_AGE:
        raise ValueError(f"HTB snapshot is stale or future-dated: {synced_at.isoformat()}")
    name = clean_text(htb.get("name"), field="htb.name", limit=32)
    if name.casefold() != "foobarto":
        raise ValueError(f"unexpected HTB profile identity: {name}")
    profile = HtbProfile(
        name=name,
        rank=clean_text(htb.get("rank"), field="htb.rank", limit=48),
        ranking=require_int(htb, "ranking"),
        user_owns=require_int(htb, "user_owns"),
        system_owns=require_int(htb, "system_owns"),
        xp_level=require_int(htb, "xp_level", maximum=100_000),
        xp_level_title=clean_text(
            htb.get("xp_level_title"), field="htb.xp_level_title", limit=48
        ),
        xp_level_grade=require_int(htb, "xp_level_grade", maximum=3),
        synced_at=synced_at,
    )

    raw_signals = payload.get("signals")
    if not isinstance(raw_signals, list) or len(raw_signals) != 4:
        raise ValueError("expected exactly four profile signals")
    allowed_links = {
        "Writing": {"foobarto.me"},
        "Research": {"doi.org", "foobarto.me"},
        "Disclosure": {"foobarto.me"},
        "HTB": {"foobarto.me"},
    }
    signals: list[Signal] = []
    seen: set[str] = set()
    for item in raw_signals:
        if not isinstance(item, dict):
            raise ValueError("profile signal must be an object")
        kind = clean_text(item.get("kind"), field="signal kind", limit=24)
        if kind not in allowed_links or kind in seen:
            raise ValueError(f"unexpected or duplicate signal kind: {kind}")
        seen.add(kind)
        signals.append(
            Signal(
                kind=kind,
                title=clean_text(item.get("title"), field=f"{kind} title", limit=140),
                date=parse_date(item.get("date"), field=f"{kind} date"),
                url=validate_link(item.get("url"), hosts=allowed_links[kind]),
            )
        )
    if seen != set(allowed_links):
        raise ValueError("profile signals are incomplete")
    return profile, signals


def _land_rects(*, x: float, y: float, cell: float, color: str) -> str:
    rects: list[str] = []
    for row, line in enumerate(LAND):
        column = 0
        while column < len(line):
            if line[column] != "#":
                column += 1
                continue
            end = column + 1
            while end < len(line) and line[end] == "#":
                end += 1
            rects.append(
                f'<rect x="{x + column * cell:.1f}" y="{y + row * cell:.1f}" '
                f'width="{(end - column) * cell - 0.7:.1f}" height="{cell - 0.7:.1f}" '
                f'rx="0.4" fill="{color}"/>'
            )
            column = end
    return "\n    ".join(rects)


def _map_grid(*, x: float, y: float, cell: float, color: str) -> str:
    lines: list[str] = []
    width = 64 * cell
    height = 32 * cell
    for column in range(0, 65, 8):
        px = x + column * cell
        lines.append(
            f'<line x1="{px:.1f}" y1="{y:.1f}" x2="{px:.1f}" '
            f'y2="{y + height:.1f}" stroke="{color}" stroke-width="0.7"/>'
        )
    for row in range(0, 33, 4):
        py = y + row * cell
        lines.append(
            f'<line x1="{x:.1f}" y1="{py:.1f}" x2="{x + width:.1f}" '
            f'y2="{py:.1f}" stroke="{color}" stroke-width="0.7"/>'
        )
    return "\n    ".join(lines)


def _map_blips(*, x: float, y: float, cell: float, accent: str) -> str:
    width = 64 * cell
    height = 32 * cell
    circles: list[str] = []
    for index, (longitude, latitude) in enumerate(REGIONS):
        px = x + (longitude + 180) / 360 * width
        py = y + (90 - latitude) / 180 * height
        opacity = 0.90 if index in (0, 2, 6) else 0.55
        circles.extend(
            (
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="4.0" fill="none" '
                f'stroke="{accent}" stroke-width="1.0" opacity="{opacity:.2f}"/>',
                f'<circle cx="{px:.1f}" cy="{py:.1f}" r="1.6" '
                f'fill="{accent}" opacity="{opacity:.2f}"/>',
            )
        )
    return "\n    ".join(circles)


def render_svg(profile: HtbProfile, *, theme: str) -> str:
    if theme not in THEMES:
        raise ValueError(f"unknown theme: {theme}")
    colors = THEMES[theme]
    grade = "●" * profile.xp_level_grade + "○" * (3 - profile.xp_level_grade)
    ranking = f"#{profile.ranking} GLOBAL" if profile.ranking else "GLOBAL"
    title = f"Hack The Box profile signal for {profile.name}"
    description = (
        f"{profile.rank}, {ranking.lower()}, level {profile.xp_level} "
        f"{profile.xp_level_title} grade {profile.xp_level_grade}, "
        f"{profile.user_owns} user owns and {profile.system_owns} system owns."
    )
    esc = lambda value: html.escape(str(value), quote=True)
    map_x, map_y, cell = 17.0, 9.0, 4.1
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 960 150" role="img" aria-labelledby="title desc">
  <title id="title">{esc(title)}</title>
  <desc id="desc">{esc(description)} A WOPR-style world map decorates the panel.</desc>
  <defs>
    <pattern id="scanlines" width="4" height="4" patternUnits="userSpaceOnUse">
      <rect width="4" height="1" fill="{colors['scan']}" opacity="0.055"/>
    </pattern>
  </defs>
  <rect x="0.5" y="0.5" width="959" height="149" rx="6" fill="{colors['bg']}" stroke="{colors['border']}"/>
  <g aria-hidden="true">
    {_map_grid(x=map_x, y=map_y, cell=cell, color=colors['grid'])}
    {_land_rects(x=map_x, y=map_y, cell=cell, color=colors['land'])}
    {_map_blips(x=map_x, y=map_y, cell=cell, accent=colors['accent'])}
    <text x="18" y="18" fill="{colors['dim']}" font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace" font-size="7" letter-spacing="1">GLOBAL GRID // PASSIVE</text>
    <line x1="300" y1="10" x2="300" y2="140" stroke="{colors['border']}"/>
  </g>
  <g font-family="ui-monospace, SFMono-Regular, Menlo, Consolas, monospace">
    <text x="328" y="23" fill="{colors['accent']}" font-size="13" font-weight="700" letter-spacing="1.4">WOPR // OPERATOR SIGNAL</text>
    <text x="928" y="23" fill="{colors['dim']}" font-size="8" text-anchor="end" letter-spacing="1">PUBLIC SNAPSHOT</text>
    <line x1="328" y1="34" x2="928" y2="34" stroke="{colors['border']}"/>
    <text x="328" y="60" fill="{colors['dim']}" font-size="10" letter-spacing="1">RANK</text>
    <text x="410" y="60" fill="{colors['fg']}" font-size="14" font-weight="700">{esc(profile.rank.upper())}</text>
    <text x="928" y="60" fill="{colors['accent']}" font-size="12" text-anchor="end">{esc(ranking)}</text>
    <text x="328" y="88" fill="{colors['dim']}" font-size="10" letter-spacing="1">LEVEL</text>
    <text x="410" y="88" fill="{colors['fg']}" font-size="14"><tspan font-weight="700">{profile.xp_level}</tspan><tspan fill="{colors['dim']}"> // </tspan>{esc(profile.xp_level_title.upper())}</text>
    <text x="928" y="88" fill="{colors['accent']}" font-size="13" text-anchor="end" letter-spacing="2">{grade}</text>
    <text x="328" y="116" fill="{colors['dim']}" font-size="10" letter-spacing="1">OWNS</text>
    <text x="410" y="116" fill="{colors['fg']}" font-size="13">USER {profile.user_owns}<tspan fill="{colors['dim']}"> // </tspan>SYSTEM {profile.system_owns}</text>
    <text x="328" y="137" fill="{colors['dim']}" font-size="8" letter-spacing="0.8">SOURCE HTB // PUBLIC SNAPSHOT</text>
    <rect x="918" y="128" width="9" height="11" fill="{colors['accent']}"/>
  </g>
  <rect x="1" y="1" width="958" height="148" rx="5" fill="url(#scanlines)" pointer-events="none"/>
</svg>
'''


def markdown_escape(text: str) -> str:
    escaped = html.escape(text, quote=False)
    return re.sub(r"([\\`*_\[\]])", r"\\\1", escaped)


def console_block(profile: HtbProfile) -> str:
    alt = (
        f"Hack The Box profile snapshot: {profile.rank}, global rank "
        f"{profile.ranking}, level {profile.xp_level} {profile.xp_level_title} "
        f"grade {profile.xp_level_grade}, {profile.user_owns} user owns and "
        f"{profile.system_owns} system owns. Open the public Hack The Box profile."
    )
    return "\n".join(
        (
            f'<a href="{HTB_PROFILE_URL}">',
            "  <picture>",
            '    <source media="(prefers-color-scheme: dark)" srcset="./assets/signal-console-dark.svg">',
            '    <source media="(prefers-color-scheme: light)" srcset="./assets/signal-console-light.svg">',
            f'    <img alt="{html.escape(alt, quote=True)}" src="./assets/signal-console-dark.svg">',
            "  </picture>",
            "</a>",
        )
    )


def signals_block(signals: Iterable[Signal]) -> str:
    latest = sorted(signals, key=lambda signal: signal.date, reverse=True)
    lines = [
        "## Latest signals",
        "",
        "```bash",
        f"curl -fsS {PROFILE_DATA_URL} "
        "| jq -c '.signals|sort_by(.date)|reverse[]'",
        "```",
        "",
    ]
    for signal in latest:
        lines.append(
            f"- `{signal.date}` `{markdown_escape(signal.kind.lower())}` "
            f"— [{markdown_escape(signal.title)}]({signal.url})"
        )
    lines.extend(
        (
            "",
            "[blog/](https://foobarto.me/blog/) · "
            "[research/](https://foobarto.me/research/) · "
            "[disclosures/](https://foobarto.me/disclosures/) · "
            "[htb/](https://foobarto.me/htb/)",
        )
    )
    return "\n".join(lines)


def replace_block(text: str, start: str, end: str, body: str) -> str:
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError(f"expected exactly one marker pair: {start} / {end}")
    pattern = re.compile(re.escape(start) + r".*?" + re.escape(end), re.DOTALL)
    replacement = f"{start}\n{body.rstrip()}\n{end}"
    return pattern.sub(lambda _: replacement, text)


def atomic_write(path: Path, content: str) -> bool:
    encoded = content.encode("utf-8")
    if path.exists() and path.read_bytes() == encoded:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise
    return True


def generate(
    root: Path,
    source: str,
    *,
    now: dt.datetime | None = None,
) -> list[Path]:
    profile, signals = parse_payload(read_source(source), now=now)
    dark_svg = render_svg(profile, theme="dark")
    light_svg = render_svg(profile, theme="light")
    ET.fromstring(dark_svg)
    ET.fromstring(light_svg)

    readme_path = root / "README.md"
    readme = readme_path.read_text(encoding="utf-8")
    readme = replace_block(readme, CONSOLE_START, CONSOLE_END, console_block(profile))
    readme = replace_block(readme, SIGNALS_START, SIGNALS_END, signals_block(signals))

    outputs = {
        root / "assets" / "signal-console-dark.svg": dark_svg,
        root / "assets" / "signal-console-light.svg": light_svg,
        readme_path: readme,
    }
    changed: list[Path] = []
    for path, content in outputs.items():
        if atomic_write(path, content):
            changed.append(path)
    return changed


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--source", default=PROFILE_DATA_URL)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    root = args.root.resolve()
    changed = generate(root, args.source)
    if changed:
        for path in changed:
            print(f"updated {path.relative_to(root)}")
    else:
        print("profile signals already current")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
