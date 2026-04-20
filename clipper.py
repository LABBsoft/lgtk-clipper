"""Rank viewer-made Twitch clips from a VOD by YouTube-worthiness."""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

import requests
from dotenv import load_dotenv
from rich.console import Console
from rich.table import Table

HELIX = "https://api.twitch.tv/helix"
OAUTH_TOKEN_URL = "https://id.twitch.tv/oauth2/token"

VOD_URL_RE = re.compile(r"twitch\.tv/videos/(\d+)")
DURATION_RE = re.compile(r"(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?")

CLUSTER_WINDOW_SEC = 30
DURATION_SWEET_PEAK = 45
DURATION_SWEET_MIN = 20
DURATION_SWEET_MAX = 90

WEIGHT_VIEWS = 0.50
WEIGHT_DENSITY = 0.25
WEIGHT_DURATION = 0.15
WEIGHT_DIVERSITY = 0.10


@dataclass
class Clip:
    id: str
    url: str
    title: str
    creator: str
    views: int
    duration: float
    vod_offset: int | None
    created_at: str
    # Filled in during scoring
    density: int = 0
    unique_clippers: int = 0
    score: float = 0.0
    components: dict[str, float] = field(default_factory=dict)


def parse_vod_id(vod_url: str) -> str:
    m = VOD_URL_RE.search(vod_url)
    if not m:
        sys.exit(f"Could not parse a Twitch VOD id from: {vod_url!r}")
    return m.group(1)


def parse_duration(s: str) -> int:
    """Twitch returns durations like '3h42m10s'."""
    m = DURATION_RE.fullmatch(s)
    if not m:
        raise ValueError(f"Unparseable duration: {s!r}")
    h, mi, se = (int(p) if p else 0 for p in m.groups())
    return h * 3600 + mi * 60 + se


def get_app_token(client_id: str, client_secret: str) -> str:
    resp = requests.post(
        OAUTH_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "grant_type": "client_credentials",
        },
        timeout=15,
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def helix_get(path: str, token: str, client_id: str, params: dict[str, Any]) -> dict:
    resp = requests.get(
        f"{HELIX}{path}",
        headers={"Client-Id": client_id, "Authorization": f"Bearer {token}"},
        params=params,
        timeout=15,
    )
    if resp.status_code == 401:
        sys.exit("Twitch API returned 401 — check TWITCH_CLIENT_ID / TWITCH_CLIENT_SECRET.")
    resp.raise_for_status()
    return resp.json()


def fetch_vod(vod_id: str, token: str, client_id: str) -> dict:
    data = helix_get("/videos", token, client_id, {"id": vod_id})["data"]
    if not data:
        sys.exit(f"No VOD found with id {vod_id}.")
    return data[0]


def fetch_clips(
    broadcaster_id: str,
    vod_id: str,
    started_at: datetime,
    ended_at: datetime,
    token: str,
    client_id: str,
) -> list[Clip]:
    clips: list[Clip] = []
    cursor: str | None = None
    while True:
        params: dict[str, Any] = {
            "broadcaster_id": broadcaster_id,
            "started_at": started_at.isoformat().replace("+00:00", "Z"),
            "ended_at": ended_at.isoformat().replace("+00:00", "Z"),
            "first": 100,
        }
        if cursor:
            params["after"] = cursor
        page = helix_get("/clips", token, client_id, params)
        for c in page["data"]:
            if c.get("video_id") != vod_id:
                continue
            clips.append(
                Clip(
                    id=c["id"],
                    url=c["url"],
                    title=c["title"],
                    creator=c.get("creator_name", ""),
                    views=int(c.get("view_count", 0)),
                    duration=float(c.get("duration", 0)),
                    vod_offset=c.get("vod_offset"),
                    created_at=c.get("created_at", ""),
                )
            )
        cursor = page.get("pagination", {}).get("cursor")
        if not cursor:
            break
    return clips


def duration_score(d: float) -> float:
    """Triangle: 0 below MIN, 1 at PEAK, 0 at MAX and beyond."""
    if d <= DURATION_SWEET_MIN or d >= DURATION_SWEET_MAX:
        return 0.0
    if d <= DURATION_SWEET_PEAK:
        return (d - DURATION_SWEET_MIN) / (DURATION_SWEET_PEAK - DURATION_SWEET_MIN)
    return 1 - (d - DURATION_SWEET_PEAK) / (DURATION_SWEET_MAX - DURATION_SWEET_PEAK)


def compute_clusters(clips: list[Clip]) -> None:
    """Fill clip.density and clip.unique_clippers using a ±CLUSTER_WINDOW_SEC window."""
    positioned = [c for c in clips if c.vod_offset is not None]
    for clip in clips:
        if clip.vod_offset is None:
            clip.density = 0
            clip.unique_clippers = 1
            continue
        neighbors = [
            other
            for other in positioned
            if other.id != clip.id
            and abs((other.vod_offset or 0) - clip.vod_offset) <= CLUSTER_WINDOW_SEC
        ]
        clip.density = len(neighbors)
        clippers = {clip.creator} | {n.creator for n in neighbors if n.creator}
        clip.unique_clippers = len(clippers)


def score_clips(clips: list[Clip]) -> None:
    if not clips:
        return
    compute_clusters(clips)

    max_log_views = max(math.log10(c.views + 1) for c in clips) or 1.0
    max_density = max((c.density for c in clips), default=0) or 1
    max_diversity = max((c.unique_clippers for c in clips), default=1) or 1

    for c in clips:
        views_n = math.log10(c.views + 1) / max_log_views
        density_n = c.density / max_density
        duration_n = duration_score(c.duration)
        diversity_n = c.unique_clippers / max_diversity

        c.components = {
            "views": round(views_n, 3),
            "density": round(density_n, 3),
            "duration": round(duration_n, 3),
            "diversity": round(diversity_n, 3),
        }
        c.score = round(
            100
            * (
                WEIGHT_VIEWS * views_n
                + WEIGHT_DENSITY * density_n
                + WEIGHT_DURATION * duration_n
                + WEIGHT_DIVERSITY * diversity_n
            ),
            2,
        )


def fmt_timestamp(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    return str(timedelta(seconds=int(seconds)))


def vod_timestamp_url(vod_url: str, offset: int | None) -> str:
    if offset is None:
        return vod_url
    h, rem = divmod(int(offset), 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    sep = "&" if "?" in vod_url else "?"
    return f"{vod_url}{sep}t={''.join(parts)}"


def render_table(clips: list[Clip], vod_url: str, limit: int) -> None:
    console = Console()
    table = Table(title=f"Top {min(limit, len(clips))} clips (of {len(clips)})")
    table.add_column("#", justify="right", style="bold")
    table.add_column("Score", justify="right")
    table.add_column("Title", overflow="fold", max_width=40)
    table.add_column("Views", justify="right")
    table.add_column("Dur", justify="right")
    table.add_column("VOD @", justify="right")
    table.add_column("Clip", overflow="fold")

    for i, c in enumerate(clips[:limit], start=1):
        table.add_row(
            str(i),
            f"{c.score:.1f}",
            c.title,
            f"{c.views:,}",
            f"{int(c.duration)}s",
            f"[link={vod_timestamp_url(vod_url, c.vod_offset)}]{fmt_timestamp(c.vod_offset)}[/link]",
            f"[link={c.url}]{c.url}[/link]",
        )
    console.print(table)


def write_json(clips: list[Clip], path: str, vod_url: str) -> None:
    payload = [
        {
            "rank": i,
            "score": c.score,
            "components": c.components,
            "title": c.title,
            "creator": c.creator,
            "views": c.views,
            "duration_sec": c.duration,
            "vod_offset_sec": c.vod_offset,
            "vod_timestamp_url": vod_timestamp_url(vod_url, c.vod_offset),
            "clip_url": c.url,
            "clip_id": c.id,
            "created_at": c.created_at,
            "density": c.density,
            "unique_clippers": c.unique_clippers,
        }
        for i, c in enumerate(clips, start=1)
    ]
    with open(path, "w") as f:
        json.dump({"vod_url": vod_url, "clips": payload}, f, indent=2)


def main(argv: Iterable[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(
        description="Rank viewer-made Twitch clips from a VOD by YouTube-worthiness.",
    )
    parser.add_argument("vod_url", help="Twitch VOD URL, e.g. https://www.twitch.tv/videos/123456789")
    parser.add_argument("--limit", type=int, default=20, help="Rows to print (default 20).")
    parser.add_argument("--min-views", type=int, default=0, help="Drop clips below this view count.")
    parser.add_argument("--json", dest="json_out", help="Also write a JSON report to this path.")
    args = parser.parse_args(list(argv) if argv is not None else None)

    client_id = os.environ.get("TWITCH_CLIENT_ID")
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET")
    if not client_id or not client_secret:
        sys.exit("Set TWITCH_CLIENT_ID and TWITCH_CLIENT_SECRET (see .env.example).")

    vod_id = parse_vod_id(args.vod_url)
    token = get_app_token(client_id, client_secret)

    vod = fetch_vod(vod_id, token, client_id)
    created_at = datetime.fromisoformat(vod["created_at"].replace("Z", "+00:00"))
    duration_sec = parse_duration(vod["duration"])
    # Pad the search window slightly — clips can be created a few seconds past stream end.
    started_at = created_at - timedelta(minutes=1)
    ended_at = created_at + timedelta(seconds=duration_sec) + timedelta(minutes=5)

    clips = fetch_clips(vod["user_id"], vod_id, started_at, ended_at, token, client_id)
    clips = [c for c in clips if c.views >= args.min_views]
    if not clips:
        print("No clips found for this VOD.")
        return 0

    score_clips(clips)
    clips.sort(key=lambda c: c.score, reverse=True)

    render_table(clips, args.vod_url, args.limit)
    if args.json_out:
        write_json(clips, args.json_out, args.vod_url)
        print(f"Wrote report to {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
