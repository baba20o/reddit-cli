"""Media attachment extraction and downloading.

Extraction reads normalized post media (see api._extract_media); downloading
streams files to disk with size caps, an allowlisted host policy, and a
jsonl manifest mapping every file back to its source post.

Downloads use a bare session (no OAuth header — media CDNs are not the API,
and the bearer token must not leak to third-party hosts) and their own
politeness delay separate from the API rate limiter.
"""

import json
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse

import requests

logger = logging.getLogger(__name__)


def ffmpeg_path():
    """Path to ffmpeg if installed, else None (audio muxing is then skipped)."""
    return shutil.which("ffmpeg")

# Hosts we download from by default; --any-host relaxes to any host serving
# a media-extension URL (content-type is verified either way)
ALLOWED_MEDIA_HOSTS = frozenset({
    "i.redd.it", "preview.redd.it", "v.redd.it", "i.imgur.com",
})

MEDIA_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4"}

CONTENT_TYPE_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "video/mp4": ".mp4",
}

# SVG is an image content-type but scriptable — never save it as media
BLOCKED_CONTENT_TYPES = frozenset({"image/svg+xml"})

DEFAULT_MAX_BYTES = 50 * 1024 * 1024  # 50MB per file
DOWNLOAD_DELAY = 0.3  # politeness toward media CDNs
DOWNLOAD_TIMEOUT = (10, 60)
MAX_REDIRECTS = 5
MANIFEST_MAX_BYTES = 2 * 1024 * 1024  # DASH manifests are small
FFMPEG_TIMEOUT = 180


def media_url_ext(url: str) -> str:
    """Extension from a URL path, if it's a known media extension."""
    path = urlparse(url).path
    for ext in MEDIA_EXTENSIONS:
        if path.lower().endswith(ext):
            return ".jpg" if ext == ".jpeg" else ext
    return ""


def host_allowed(url: str, any_host: bool = False) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return False
    if parsed.hostname in ALLOWED_MEDIA_HOSTS:
        return True
    return any_host and bool(media_url_ext(url))


class MediaDownloader:
    """Streams media files into a directory, appending a jsonl manifest."""

    def __init__(self, dest_dir, max_bytes: int = DEFAULT_MAX_BYTES,
                 any_host: bool = False, delay: float = DOWNLOAD_DELAY,
                 session: requests.Session = None):
        self.dest = Path(dest_dir)
        self.dest.mkdir(parents=True, exist_ok=True)
        self.max_bytes = max_bytes
        self.any_host = any_host
        self.delay = delay
        # Deliberately NOT the API session: no Authorization header here
        self.session = session or requests.Session()
        self.session.headers.setdefault("User-Agent", "reddit-cli/0.1.0 media fetcher")
        self._last_fetch = 0.0

    # ── Internals ─────────────────────────────────────────

    def _wait(self):
        elapsed = time.time() - self._last_fetch
        if elapsed < self.delay:
            time.sleep(self.delay - elapsed)
        self._last_fetch = time.time()

    def _target_path(self, post_id: str, index: int, url: str, content_type: str) -> Path:
        # Filename is built ONLY from post id + index + whitelisted extension —
        # never from URL text — so traversal is impossible by construction
        ext = media_url_ext(url) or CONTENT_TYPE_EXT.get((content_type or "").split(";")[0], "")
        safe_id = "".join(c for c in post_id if c.isalnum()) or "post"
        return self.dest / f"{safe_id}-{index}{ext or '.bin'}"

    def _entry(self, post: dict, item: dict, status: str, **extra) -> dict:
        entry = {
            "status": status,
            "url": item.get("url", ""),
            "type": item.get("type", ""),
            "post_id": post.get("id", ""),
            "title": post.get("title", "")[:120],
            "permalink": post.get("permalink", ""),
            "author": post.get("author", ""),
            "subreddit": post.get("subreddit", ""),
            **extra,
        }
        with (self.dest / "manifest.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, separators=(",", ":"), ensure_ascii=False) + "\n")
        return entry

    def _open(self, url: str):
        """GET with manual redirect following, re-validating the host at EVERY
        hop. requests' default allow_redirects would let an allowlisted URL
        bounce to an internal/metadata host (SSRF) or downgrade to http."""
        current = url
        for _ in range(MAX_REDIRECTS):
            resp = self.session.get(current, stream=True, timeout=DOWNLOAD_TIMEOUT,
                                    allow_redirects=False)
            if resp.status_code in (301, 302, 303, 307, 308):
                location = resp.headers.get("Location")
                resp.close()
                if not location:
                    raise ValueError("redirect without Location")
                current = urljoin(current, location)
                if not host_allowed(current, self.any_host):
                    raise ValueError(f"redirect to disallowed host: {urlparse(current).hostname}")
                continue
            return resp
        raise ValueError("too many redirects")

    def _stream(self, resp, path: Path) -> int:
        """Stream a validated response body to path atomically. Returns bytes."""
        tmp = path.with_suffix(path.suffix + ".part")
        written = 0
        try:
            with tmp.open("wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    written += len(chunk)
                    if written > self.max_bytes:
                        raise ValueError(f"exceeded size cap mid-stream ({self.max_bytes:,} bytes)")
                    f.write(chunk)
            tmp.replace(path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return written

    def _fetch(self, url: str, path_hint) -> tuple:
        """Returns (final_path, bytes_written) or raises ValueError/OSError."""
        self._wait()
        with self._open(url) as resp:
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            if content_type in BLOCKED_CONTENT_TYPES:
                raise ValueError(f"blocked content type ({content_type})")
            if not (content_type.startswith("image/") or content_type.startswith("video/")):
                raise ValueError(f"not media (Content-Type: {content_type or 'unknown'})")
            declared = resp.headers.get("Content-Length")
            if declared and int(declared) > self.max_bytes:
                raise ValueError(f"file too large ({int(declared):,} bytes > cap)")
            path = path_hint(content_type)
            return path, self._stream(resp, path)

    def _fetch_to(self, url: str, path: Path) -> int:
        """Download a media URL to an exact path (used for audio track). Bytes."""
        self._wait()
        with self._open(url) as resp:
            resp.raise_for_status()
            content_type = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            if not (content_type.startswith("audio/") or content_type.startswith("video/")):
                raise ValueError(f"audio track not media (Content-Type: {content_type or 'unknown'})")
            return self._stream(resp, path)

    def _resolve_audio_url(self, video_url: str, dash_url: str):
        """Parse the DASH manifest for the best audio track URL, or None."""
        if not dash_url or not host_allowed(dash_url, self.any_host):
            return None
        self._wait()
        try:
            with self.session.get(dash_url, stream=True, timeout=DOWNLOAD_TIMEOUT,
                                  allow_redirects=False) as resp:
                if resp.status_code != 200:
                    return None
                # Count DECODED bytes (iter_content inflates gzip) so a
                # compression bomb can't blow past the cap the way raw.read would
                chunks, total = [], 0
                for chunk in resp.iter_content(chunk_size=16384):
                    total += len(chunk)
                    if total > MANIFEST_MAX_BYTES:
                        return None  # manifest implausibly large — bail
                    chunks.append(chunk)
                body = b"".join(chunks)
        except requests.RequestException:
            return None
        text = body.decode("utf-8", "replace")
        audio_names = []
        for block in re.findall(r"<AdaptationSet[^>]*audio.*?</AdaptationSet>", text, re.S):
            audio_names += re.findall(r"<BaseURL>([^<]+)</BaseURL>", block)
        if not audio_names:
            return None
        # Prefer the highest-bitrate track (numeric suffix), else the last listed
        def bitrate(name):
            m = re.search(r"(\d+)", name)
            return int(m.group(1)) if m else 0
        best = max(audio_names, key=bitrate)
        audio_url = urljoin(video_url, best)
        return audio_url if host_allowed(audio_url, self.any_host) else None

    def _fetch_video_muxed(self, url: str, dash_url: str, path: Path) -> tuple:
        """Download video + its audio track and mux with ffmpeg into path.

        Returns (bytes_written, muxed: bool). Falls back to video-only (still
        writing `path`) when ffmpeg is missing or the audio can't be fetched.
        """
        # Download the video-only stream to a temp file first
        video_tmp = path.with_suffix(".video.tmp")
        self._wait()
        with self._open(url) as resp:
            resp.raise_for_status()
            ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip()
            if not ct.startswith("video/"):
                raise ValueError(f"not media (Content-Type: {ct or 'unknown'})")
            declared = resp.headers.get("Content-Length")
            if declared and int(declared) > self.max_bytes:
                raise ValueError(f"file too large ({int(declared):,} bytes > cap)")
            self._stream(resp, video_tmp)

        ffmpeg = ffmpeg_path()
        audio_url = self._resolve_audio_url(url, dash_url) if ffmpeg else None
        if not (ffmpeg and audio_url):
            video_tmp.replace(path)  # video-only fallback
            return path.stat().st_size, False

        audio_tmp = path.with_suffix(".audio.tmp")
        out_tmp = path.with_suffix(".mux.tmp.mp4")
        try:
            self._fetch_to(audio_url, audio_tmp)
            proc = subprocess.run(
                [ffmpeg, "-loglevel", "error", "-y", "-i", str(video_tmp),
                 "-i", str(audio_tmp), "-c", "copy", str(out_tmp)],
                capture_output=True, timeout=FFMPEG_TIMEOUT)
            if proc.returncode != 0 or not out_tmp.exists():
                video_tmp.replace(path)  # mux failed — keep video-only
                return path.stat().st_size, False
            out_tmp.replace(path)
            return path.stat().st_size, True
        except (requests.RequestException, ValueError, OSError, subprocess.SubprocessError):
            # ffmpeg timeout/crash may have left a partial out_tmp — the finally
            # cleans it; salvage the video-only stream if we haven't written path
            if not path.exists():
                video_tmp.replace(path)
            return path.stat().st_size, False
        finally:
            video_tmp.unlink(missing_ok=True)
            audio_tmp.unlink(missing_ok=True)
            out_tmp.unlink(missing_ok=True)

    # ── Public ────────────────────────────────────────────

    def download_post(self, post: dict, budget: int = None) -> tuple:
        """Download a post's media items. Returns (entries, downloaded, complete).

        `budget` caps NEW downloads (not skips/exists) so a gallery can't
        overshoot a global --max-files. `complete` is False when the budget cut
        the post short — the caller must not mark such a post as fully seen.
        """
        entries = []
        downloaded = 0
        for index, item in enumerate(post.get("media") or [], start=1):
            url = item.get("url", "")
            if not url:
                continue
            if not host_allowed(url, self.any_host):
                entries.append(self._entry(post, item, "skipped",
                                           reason="host not in allowlist (use --any-host)"))
                continue

            probe = self._target_path(post.get("id", ""), index, url, "")
            if probe.suffix != ".bin" and probe.exists():
                entries.append(self._entry(post, item, "exists", file=probe.name,
                                           bytes=probe.stat().st_size))
                continue

            if budget is not None and downloaded >= budget:
                return entries, downloaded, False  # budget exhausted mid-post

            try:
                if item.get("type") == "video" and item.get("has_audio"):
                    path = self._target_path(post.get("id", ""), index, url, "video/mp4")
                    written, muxed = self._fetch_video_muxed(url, item.get("dash_url", ""), path)
                    extra = {"file": path.name, "bytes": written}
                    extra["note"] = ("muxed with audio" if muxed
                                     else "video stream only (ffmpeg missing or audio unavailable)")
                else:
                    path, written = self._fetch(
                        url, lambda ct: self._target_path(post.get("id", ""), index, url, ct))
                    extra = {"file": path.name, "bytes": written}
                    if item.get("type") == "video":
                        extra["note"] = "video stream only (no audio track)"
                entries.append(self._entry(post, item, "downloaded", **extra))
                downloaded += 1
            except (requests.RequestException, ValueError, OSError) as e:
                entries.append(self._entry(post, item, "failed", error=str(e)))
        return entries, downloaded, True
