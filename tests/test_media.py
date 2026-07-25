"""Tests for media extraction (api._extract_media) and MediaDownloader."""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from reddit.api import _extract_media, _format_post
from reddit.cache import SeenStore
from reddit.media import MediaDownloader, host_allowed, media_url_ext


# ── Extraction ────────────────────────────────────────────


def test_extract_direct_image():
    data = {"url": "https://i.redd.it/abc.jpeg"}
    assert _extract_media(data) == [{"url": "https://i.redd.it/abc.jpeg", "type": "image"}]


def test_extract_imgur_direct():
    data = {"url": "https://i.imgur.com/xyz.png"}
    assert _extract_media(data)[0]["type"] == "image"


def test_extract_external_link_is_not_media():
    assert _extract_media({"url": "https://youtube.com/watch?v=x"}) == []
    assert _extract_media({"url": "https://example.com/article"}) == []


def test_extract_gallery_in_order():
    data = {
        "is_gallery": True,
        "gallery_data": {"items": [{"media_id": "m2"}, {"media_id": "m1"}]},
        "media_metadata": {
            "m1": {"e": "Image", "s": {"u": "https://preview.redd.it/m1.jpg"}},
            "m2": {"e": "Image", "s": {"u": "https://preview.redd.it/m2.jpg"}},
        },
    }
    urls = [m["url"] for m in _extract_media(data)]
    assert urls == ["https://preview.redd.it/m2.jpg", "https://preview.redd.it/m1.jpg"]


def test_extract_gallery_animated_prefers_mp4():
    data = {
        "is_gallery": True,
        "gallery_data": {"items": [{"media_id": "g1"}]},
        "media_metadata": {
            "g1": {"e": "AnimatedImage",
                   "s": {"gif": "https://i.redd.it/g1.gif", "mp4": "https://i.redd.it/g1.mp4"}},
        },
    }
    assert _extract_media(data) == [{"url": "https://i.redd.it/g1.mp4", "type": "animated"}]


def test_extract_reddit_video_fallback():
    data = {"url": "https://v.redd.it/xyz",
            "secure_media": {"reddit_video": {
                "fallback_url": "https://v.redd.it/xyz/DASH_720.mp4?source=fallback"}}}
    out = _extract_media(data)
    assert out[0]["type"] == "video"
    assert "DASH_720" in out[0]["url"]


def test_format_post_includes_media():
    post = {"kind": "t3", "data": {"id": "x", "url": "https://i.redd.it/a.png",
            "title": "t", "author": "a"}}
    assert _format_post(post)["media"] == [{"url": "https://i.redd.it/a.png", "type": "image"}]


# ── URL policy ────────────────────────────────────────────


def test_media_url_ext():
    assert media_url_ext("https://i.redd.it/a.jpeg?width=100") == ".jpg"
    assert media_url_ext("https://x.com/a.mp4") == ".mp4"
    assert media_url_ext("https://x.com/page") == ""


def test_host_allowlist():
    assert host_allowed("https://i.redd.it/a.jpg") is True
    assert host_allowed("https://v.redd.it/x/DASH_720.mp4") is True
    assert host_allowed("https://evil.example/a.jpg") is False
    assert host_allowed("https://evil.example/a.jpg", any_host=True) is True
    assert host_allowed("https://evil.example/page", any_host=True) is False
    assert host_allowed("http://i.redd.it/a.jpg") is False  # https only


# ── Downloader ────────────────────────────────────────────


POST = {"id": "abc123", "name": "t3_abc123", "title": "A Post", "author": "u",
        "subreddit": "s", "permalink": "pl",
        "media": [{"url": "https://i.redd.it/abc.jpg", "type": "image"}]}


def _mock_response(content=b"IMAGEDATA", content_type="image/jpeg", length=None,
                   status=200, location=None):
    resp = MagicMock()
    resp.status_code = status
    resp.headers = {"Content-Type": content_type}
    if length is not None:
        resp.headers["Content-Length"] = str(length)
    if location is not None:
        resp.headers["Location"] = location
    resp.iter_content = lambda chunk_size: iter([content[i:i + chunk_size]
                                                 for i in range(0, len(content), chunk_size)])
    resp.raise_for_status = MagicMock()
    resp.close = MagicMock()
    resp.__enter__ = lambda self: self
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _downloader(tmp_path, **kwargs):
    d = MediaDownloader(tmp_path / "media", delay=0, **kwargs)
    return d


def _post(d, post, budget=None):
    """download_post now returns (entries, downloaded, complete)."""
    entries, _, _ = d.download_post(post, budget=budget)
    return entries


def test_download_writes_file_and_manifest(tmp_path):
    d = _downloader(tmp_path)
    with patch.object(d.session, "get", return_value=_mock_response()):
        entries = _post(d, POST)
    assert entries[0]["status"] == "downloaded"
    assert entries[0]["file"] == "abc123-1.jpg"
    assert (tmp_path / "media" / "abc123-1.jpg").read_bytes() == b"IMAGEDATA"
    manifest = [json.loads(l) for l in
                (tmp_path / "media" / "manifest.jsonl").read_text().splitlines()]
    assert manifest[0]["post_id"] == "abc123"
    assert manifest[0]["permalink"] == "pl"


def test_download_skips_existing(tmp_path):
    d = _downloader(tmp_path)
    (tmp_path / "media" / "abc123-1.jpg").write_bytes(b"OLD")
    with patch.object(d.session, "get") as mock_get:
        entries = _post(d, POST)
    assert entries[0]["status"] == "exists"
    assert mock_get.call_count == 0  # no re-fetch


def test_download_rejects_html_masquerading_as_image(tmp_path):
    d = _downloader(tmp_path)
    with patch.object(d.session, "get",
                      return_value=_mock_response(b"<html>err</html>", "text/html")):
        entries = _post(d, POST)
    assert entries[0]["status"] == "failed"
    assert "not media" in entries[0]["error"]
    assert not list((tmp_path / "media").glob("*.jpg"))


def test_download_size_cap_declared(tmp_path):
    d = _downloader(tmp_path, max_bytes=10)
    with patch.object(d.session, "get",
                      return_value=_mock_response(b"x" * 5, length=999999)):
        entries = _post(d, POST)
    assert entries[0]["status"] == "failed"
    assert "too large" in entries[0]["error"]


def test_download_size_cap_midstream_removes_partial(tmp_path):
    d = _downloader(tmp_path, max_bytes=10)
    with patch.object(d.session, "get",
                      return_value=_mock_response(b"x" * 100)):  # no Content-Length
        entries = _post(d, POST)
    assert entries[0]["status"] == "failed"
    assert "mid-stream" in entries[0]["error"]
    assert list((tmp_path / "media").glob("*.part")) == []
    assert list((tmp_path / "media").glob("*.jpg")) == []


def test_download_disallowed_host_skipped(tmp_path):
    d = _downloader(tmp_path)
    post = {**POST, "media": [{"url": "https://evil.example/a.jpg", "type": "image"}]}
    with patch.object(d.session, "get") as mock_get:
        entries = _post(d, post)
    assert entries[0]["status"] == "skipped"
    assert mock_get.call_count == 0


def test_download_ext_from_content_type_when_url_lacks_one(tmp_path):
    d = _downloader(tmp_path)
    post = {**POST, "media": [{"url": "https://i.redd.it/noext", "type": "image"}]}
    with patch.object(d.session, "get", return_value=_mock_response(content_type="image/png")):
        entries = _post(d, post)
    assert entries[0]["file"] == "abc123-1.png"


def test_downloader_session_has_no_auth_header(tmp_path):
    d = _downloader(tmp_path)
    assert "Authorization" not in d.session.headers


def test_filename_safe_by_construction(tmp_path):
    d = _downloader(tmp_path)
    post = {**POST, "id": "../../etc/passwd",
            "media": [{"url": "https://i.redd.it/a.jpg", "type": "image"}]}
    with patch.object(d.session, "get", return_value=_mock_response()):
        entries = _post(d, post)
    assert entries[0]["status"] == "downloaded"
    assert entries[0]["file"] == "etcpasswd-1.jpg"  # alnum-only post id
    assert (tmp_path / "media" / "etcpasswd-1.jpg").exists()


# ── Verification-round fixes ──────────────────────────────


def test_redirect_to_disallowed_host_rejected(tmp_path):
    """SSRF guard: a redirect to a non-allowlisted host must not be followed."""
    d = _downloader(tmp_path, any_host=True)
    post = {**POST, "media": [{"url": "https://cdn.example/a.jpg", "type": "image"}]}
    responses = [
        _mock_response(status=302, location="http://169.254.169.254/latest/meta-data/"),
    ]
    with patch.object(d.session, "get", side_effect=responses):
        entries = _post(d, post)
    assert entries[0]["status"] == "failed"
    assert "disallowed host" in entries[0]["error"]
    assert not list((tmp_path / "media").glob("*.jpg"))


def test_redirect_to_allowed_host_followed(tmp_path):
    d = _downloader(tmp_path)
    responses = [
        _mock_response(status=302, location="https://i.redd.it/final.jpg"),
        _mock_response(b"REALDATA", "image/jpeg"),
    ]
    with patch.object(d.session, "get", side_effect=responses):
        entries = _post(d, POST)
    assert entries[0]["status"] == "downloaded"
    assert (tmp_path / "media" / "abc123-1.jpg").read_bytes() == b"REALDATA"


def test_redirect_downgrade_to_http_rejected(tmp_path):
    d = _downloader(tmp_path)
    responses = [_mock_response(status=302, location="http://i.redd.it/final.jpg")]
    with patch.object(d.session, "get", side_effect=responses):
        entries = _post(d, POST)
    assert entries[0]["status"] == "failed"  # http:// fails host_allowed's https check


def test_svg_content_type_blocked(tmp_path):
    d = _downloader(tmp_path)
    with patch.object(d.session, "get",
                      return_value=_mock_response(b"<svg/>", "image/svg+xml")):
        entries = _post(d, POST)
    assert entries[0]["status"] == "failed"
    assert "blocked content type" in entries[0]["error"]


def test_budget_stops_gallery_and_reports_incomplete(tmp_path):
    d = _downloader(tmp_path)
    post = {**POST, "media": [
        {"url": "https://i.redd.it/1.jpg", "type": "image"},
        {"url": "https://i.redd.it/2.jpg", "type": "image"},
        {"url": "https://i.redd.it/3.jpg", "type": "image"},
    ]}
    with patch.object(d.session, "get", return_value=_mock_response()):
        entries, downloaded, complete = d.download_post(post, budget=2)
    assert downloaded == 2
    assert complete is False
    assert sum(1 for e in entries if e["status"] == "downloaded") == 2


def test_extract_gifv_rewrites_to_mp4():
    data = {"url": "https://i.imgur.com/CctepEn.gifv"}
    assert _extract_media(data) == [{"url": "https://i.imgur.com/CctepEn.mp4", "type": "video"}]


def test_extract_crosspost_uses_parent():
    data = {"url": "https://v.redd.it/child", "secure_media": None, "media": None,
            "crosspost_parent_list": [
                {"url": "https://i.redd.it/parent.jpg"}]}
    assert _extract_media(data) == [{"url": "https://i.redd.it/parent.jpg", "type": "image"}]


# ── Audio muxing (phase 3) ────────────────────────────────


def test_extract_video_carries_audio_info():
    data = {"url": "https://v.redd.it/x", "secure_media": {"reddit_video": {
        "fallback_url": "https://v.redd.it/x/CMAF_480.mp4?source=fallback",
        "has_audio": True, "dash_url": "https://v.redd.it/x/DASHPlaylist.mpd?a=sig"}}}
    item = _extract_media(data)[0]
    assert item["type"] == "video"
    assert item["has_audio"] is True
    assert "DASHPlaylist" in item["dash_url"]


def test_extract_video_without_audio_omits_fields():
    data = {"url": "https://v.redd.it/x", "secure_media": {"reddit_video": {
        "fallback_url": "https://v.redd.it/x/CMAF_480.mp4?source=fallback",
        "has_audio": False}}}
    item = _extract_media(data)[0]
    assert "has_audio" not in item


def test_resolve_audio_url_from_manifest(tmp_path):
    d = _downloader(tmp_path)
    manifest = (
        '<MPD><AdaptationSet contentType="video">'
        '<Representation><BaseURL>CMAF_480.mp4</BaseURL></Representation></AdaptationSet>'
        '<AdaptationSet contentType="audio">'
        '<Representation><BaseURL>CMAF_AUDIO_64.mp4</BaseURL></Representation>'
        '<Representation><BaseURL>CMAF_AUDIO_128.mp4</BaseURL></Representation>'
        '</AdaptationSet></MPD>')
    resp = MagicMock()
    resp.status_code = 200
    resp.iter_content = lambda chunk_size: iter([manifest.encode()])
    resp.__enter__ = lambda self: self
    resp.__exit__ = MagicMock(return_value=False)
    with patch.object(d.session, "get", return_value=resp):
        audio = d._resolve_audio_url(
            "https://v.redd.it/x/CMAF_480.mp4?source=fallback",
            "https://v.redd.it/x/DASHPlaylist.mpd?a=sig")
    # Highest-bitrate audio track, resolved against the video base
    assert audio == "https://v.redd.it/x/CMAF_AUDIO_128.mp4"


def test_resolve_audio_url_rejects_disallowed_dash_host(tmp_path):
    d = _downloader(tmp_path)
    assert d._resolve_audio_url("https://v.redd.it/x/v.mp4",
                                "https://evil.example/manifest.mpd") is None


def test_video_muxed_falls_back_to_video_only_without_audio(tmp_path):
    """No audio resolvable -> video-only file, muxed=False, no crash."""
    d = _downloader(tmp_path)
    video_resp = _mock_response(b"VIDEODATA", "video/mp4")
    with patch.object(d.session, "get", return_value=video_resp), \
         patch.object(d, "_resolve_audio_url", return_value=None):
        written, muxed = d._fetch_video_muxed(
            "https://v.redd.it/x/CMAF_480.mp4?source=fallback", "", tmp_path / "media" / "v.mp4")
    assert muxed is False
    assert (tmp_path / "media" / "v.mp4").read_bytes() == b"VIDEODATA"
    assert not list((tmp_path / "media").glob("*.tmp*"))  # temps cleaned up


def test_video_muxed_note_when_muxed(tmp_path):
    d = _downloader(tmp_path)
    post = {**POST, "media": [{"url": "https://v.redd.it/x/CMAF_480.mp4?source=fallback",
                               "type": "video", "has_audio": True,
                               "dash_url": "https://v.redd.it/x/DASHPlaylist.mpd?a=s"}]}
    with patch.object(d, "_fetch_video_muxed", return_value=(1234, True)):
        entries = _post(d, post)
    assert entries[0]["status"] == "downloaded"
    assert entries[0]["note"] == "muxed with audio"


def test_video_muxed_cleans_out_tmp_on_timeout(tmp_path):
    """ffmpeg timeout must not orphan a .mux.tmp.mp4 in the dest dir."""
    import subprocess as sp
    d = _downloader(tmp_path)
    video_resp = _mock_response(b"VID", "video/mp4")
    audio_resp = _mock_response(b"AUD", "audio/mp4")

    def get(url, **kw):
        return audio_resp if "AUDIO" in url else video_resp

    def fake_run(args, **kw):
        # ffmpeg opens the -y output early, then hangs past the timeout
        Path(args[-1]).write_bytes(b"PARTIAL")
        raise sp.TimeoutExpired(args, kw.get("timeout", 1))

    with patch("reddit.media.ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
         patch.object(d, "_resolve_audio_url",
                      return_value="https://v.redd.it/x/CMAF_AUDIO_128.mp4"), \
         patch.object(d.session, "get", side_effect=get), \
         patch("reddit.media.subprocess.run", side_effect=fake_run):
        written, muxed = d._fetch_video_muxed(
            "https://v.redd.it/x/CMAF_480.mp4?source=fallback",
            "https://v.redd.it/x/DASHPlaylist.mpd?a=s", tmp_path / "media" / "v.mp4")
    assert muxed is False
    assert (tmp_path / "media" / "v.mp4").read_bytes() == b"VID"  # salvaged
    assert list((tmp_path / "media").glob("*.tmp*")) == []  # NO orphaned mux temp


def test_resolve_audio_url_caps_decoded_manifest(tmp_path):
    """A gzip-inflated manifest larger than the cap must be rejected, not OOM."""
    d = _downloader(tmp_path)
    big = b"<x>" * (2 * 1024 * 1024)  # > MANIFEST_MAX_BYTES when concatenated
    resp = MagicMock()
    resp.status_code = 200
    resp.iter_content = lambda chunk_size: iter([big, big])
    resp.__enter__ = lambda self: self
    resp.__exit__ = MagicMock(return_value=False)
    with patch.object(d.session, "get", return_value=resp):
        assert d._resolve_audio_url("https://v.redd.it/x/v.mp4",
                                    "https://v.redd.it/x/m.mpd") is None


def test_video_muxed_cleans_temps_on_ffmpeg_failure(tmp_path):
    d = _downloader(tmp_path)
    video_resp = _mock_response(b"VID", "video/mp4")
    audio_resp = _mock_response(b"AUD", "audio/mp4")

    def get(url, **kw):
        return audio_resp if "AUDIO" in url else video_resp

    fail = MagicMock(returncode=1)
    with patch("reddit.media.ffmpeg_path", return_value="/usr/bin/ffmpeg"), \
         patch.object(d, "_resolve_audio_url",
                      return_value="https://v.redd.it/x/CMAF_AUDIO_128.mp4"), \
         patch.object(d.session, "get", side_effect=get), \
         patch("reddit.media.subprocess.run", return_value=fail):
        written, muxed = d._fetch_video_muxed(
            "https://v.redd.it/x/CMAF_480.mp4?source=fallback",
            "https://v.redd.it/x/DASHPlaylist.mpd?a=s", tmp_path / "media" / "v.mp4")
    assert muxed is False  # ffmpeg failed -> video-only salvage
    assert (tmp_path / "media" / "v.mp4").read_bytes() == b"VID"
    assert not list((tmp_path / "media").glob("*.tmp*"))


# ── CLI integration ───────────────────────────────────────


from click.testing import CliRunner
from reddit.cli import main


def _invoke_media(args, client, tmp_path):
    client.paginate.side_effect = lambda method, pages=1, **kw: method(**kw)
    from reddit.api import parse_post_reference as _ppr
    client.resolve_post_reference.side_effect = _ppr
    runner = CliRunner(mix_stderr=False)
    with patch("reddit.cli.RedditClient", return_value=client):
        with patch("reddit.media.MediaDownloader._fetch") as mock_fetch:
            def fake_fetch(url, path_hint):
                p = path_hint("image/jpeg")
                p.write_bytes(b"X")
                return p, 1
            mock_fetch.side_effect = fake_fetch
            return runner.invoke(main, args), client


def test_media_command_listing(tmp_path):
    client = MagicMock()
    client.subreddit_posts.return_value = {
        "items": [POST, {**POST, "id": "nomedia", "name": "t3_nomedia", "media": []}],
        "after": None, "count": 2}
    result, _ = _invoke_media(
        ["media", "eink", "--dir", str(tmp_path / "dl"), "--jsonl"], client, tmp_path)
    assert result.exit_code == 0, result.output
    lines = [json.loads(l) for l in result.output.strip().splitlines()]
    assert lines[0]["status"] == "downloaded"
    assert lines[-1]["_meta"]["posts_scanned"] == 2
    assert lines[-1]["_meta"]["downloaded"] == 1


def test_media_command_post_url(tmp_path):
    client = MagicMock()
    client.post_comments.return_value = {"post": POST, "comments": [],
                                         "total": 0, "more_count": 0}
    result, client = _invoke_media(
        ["media", "https://reddit.com/r/s/comments/abc123/x/", "--dir",
         str(tmp_path / "dl"), "--jsonl"], client, tmp_path)
    assert result.exit_code == 0, result.output
    args, kwargs = client.post_comments.call_args
    assert args[1] == "abc123"


def test_media_save_topic_dest(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    client = MagicMock()
    client.subreddit_posts.return_value = {"items": [POST], "after": None, "count": 1}
    result, _ = _invoke_media(["media", "eink", "--save", "digi"], client, tmp_path)
    assert result.exit_code == 0, result.output
    assert (tmp_path / "research" / "digi" / "media" / "abc123-1.jpg").exists()


def test_media_max_files_does_not_record_unfetched_as_seen(tmp_path):
    """The data-loss fix: posts past --max-files must remain un-seen."""
    posts = [{**POST, "id": f"p{i}", "name": f"t3_p{i}",
              "media": [{"url": f"https://i.redd.it/{i}.jpg", "type": "image"}]}
             for i in range(3)]
    client = MagicMock()
    client.subreddit_posts.return_value = {"items": posts, "after": None, "count": 3}
    with patch("reddit.cli.SeenStore", lambda: SeenStore(str(tmp_path / "seen.json"))):
        result, _ = _invoke_media(
            ["media", "sub", "--dir", str(tmp_path / "dl"), "--seen", "cap",
             "--max-files", "1"], client, tmp_path)
        assert result.exit_code == 0
        recorded = SeenStore(str(tmp_path / "seen.json")).names().get("cap", 0)
        assert recorded == 1  # only the one post we actually fetched


def test_media_single_post_nsfw_blocked_without_flag(tmp_path):
    client = MagicMock()
    client.post_comments.return_value = {
        "post": {**POST, "over_18": True}, "comments": [], "total": 0, "more_count": 0}
    result, _ = _invoke_media(
        ["media", "https://reddit.com/r/s/comments/abc123/x/", "--dir",
         str(tmp_path / "dl"), "--jsonl"], client, tmp_path)
    assert result.exit_code == 1
    assert "NSFW" in result.output


def test_media_all_failed_exits_nonzero(tmp_path):
    client = MagicMock()
    client.subreddit_posts.return_value = {
        "items": [{**POST, "media": [{"url": "https://evil.example/a.jpg", "type": "image"}]}],
        "after": None, "count": 1}
    # skipped != failed, so use a failing allowed host instead
    client.subreddit_posts.return_value = {"items": [POST], "after": None, "count": 1}
    runner = CliRunner(mix_stderr=False)
    client.paginate.side_effect = lambda method, pages=1, **kw: method(**kw)
    from reddit.api import parse_post_reference as _ppr
    client.resolve_post_reference.side_effect = _ppr
    with patch("reddit.cli.RedditClient", return_value=client):
        with patch("reddit.media.MediaDownloader._fetch",
                   side_effect=ValueError("not media (Content-Type: text/html)")):
            result = runner.invoke(main, ["media", "sub", "--dir", str(tmp_path / "dl")])
    assert result.exit_code == 1


def test_media_jsonl_meta_truncated_flag(tmp_path):
    posts = [{**POST, "id": f"p{i}", "name": f"t3_p{i}",
              "media": [{"url": f"https://i.redd.it/{i}.jpg", "type": "image"}]}
             for i in range(3)]
    client = MagicMock()
    client.subreddit_posts.return_value = {"items": posts, "after": None, "count": 3}
    result, _ = _invoke_media(
        ["media", "sub", "--dir", str(tmp_path / "dl"), "--max-files", "1", "--jsonl"],
        client, tmp_path)
    meta = json.loads(result.output.strip().splitlines()[-1])["_meta"]
    assert meta["truncated"] is True
