"""Download an Instagram reel/post with yt-dlp."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yt_dlp


@dataclass
class Post:
    url: str
    caption: str
    uploader: str
    duration: float | None
    video_path: Path | None


class DownloadError(Exception):
    pass


def download_post(url: str, workdir: Path, cookies_file: str = "") -> Post:
    opts = {
        "outtmpl": str(workdir / "%(id)s.%(ext)s"),
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
    except yt_dlp.utils.DownloadError as e:
        raise DownloadError(
            f"Couldn't download that post ({e}). Photo/carousel posts aren't supported by the "
            "downloader: screenshot it and share the screenshot instead."
        ) from e

    # Carousels with videos come back as a playlist; use the first entry
    if info.get("_type") == "playlist" and info.get("entries"):
        caption = info.get("description") or ""
        info = info["entries"][0]
        info.setdefault("description", caption)

    video_path = None
    for d in info.get("requested_downloads") or []:
        if d.get("filepath"):
            video_path = Path(d["filepath"])
            break

    return Post(
        url=url,
        caption=info.get("description") or info.get("title") or "",
        uploader=info.get("uploader") or info.get("channel") or "",
        duration=info.get("duration"),
        video_path=video_path,
    )
