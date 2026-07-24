"""Tests for thread --seen delta, --save TOPIC, and the topic command group."""

import json
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from reddit.cache import SeenStore
from reddit.cli import main
from reddit.topics import TopicStore


POST_ITEM = {"id": "a1", "name": "t3_a1", "title": "Post One", "author": "u1",
             "subreddit": "s", "score": 10, "num_comments": 2, "permalink": "pl",
             "created_utc": time.time() - 3600, "over_18": False, "selftext": ""}


def _invoke(args, client=None, tmp_path=None):
    """Run the CLI with mocked client and (optionally) tmp seen/topic stores."""
    client = client or MagicMock()
    client.paginate.side_effect = lambda method, pages=1, **kw: method(**kw)
    runner = CliRunner(mix_stderr=False)  # keep stdout parseable as jsonl
    ctxs = [patch("reddit.cli.RedditClient", return_value=client)]
    if tmp_path is not None:
        ctxs.append(patch("reddit.cli.SeenStore",
                          lambda: SeenStore(str(tmp_path / "seen.json"))))
        ctxs.append(patch("reddit.cli.TopicStore",
                          lambda: TopicStore(str(tmp_path / "topics.json"))))
    with ctxs[0]:
        if tmp_path is not None:
            with ctxs[1], ctxs[2]:
                return runner.invoke(main, args), client
        return runner.invoke(main, args), client


def _thread_client(comments=None):
    client = MagicMock()
    client.post_comments.return_value = {
        "post": {"id": "abc123", "title": "T", "author": "op", "subreddit": "s",
                 "score": 1, "upvote_ratio": 0.9, "num_comments": 2, "url": "u",
                 "permalink": "p"},
        "comments": comments if comments is not None else [
            {"author": "x", "body": "first comment", "score": 5, "depth": 0,
             "id": "c1", "name": "t1_c1"},
            {"author": "y", "body": "a reply", "score": 2, "depth": 1,
             "id": "c2", "name": "t1_c2"},
        ],
        "total": 2, "more_count": 0,
    }
    return client


# ── thread --seen delta ───────────────────────────────────


def test_thread_seen_first_run_shows_tree_and_records(tmp_path):
    result, _ = _invoke(["thread", "s", "abc123", "--seen", "w"], _thread_client(), tmp_path)
    assert result.exit_code == 0
    assert "first comment" in result.output
    assert "replies indented" in result.output  # complete first read keeps the tree
    store = SeenStore(str(tmp_path / "seen.json"))
    assert store.names() == {"w": 2}


def test_thread_seen_second_run_delta_only(tmp_path):
    _invoke(["thread", "s", "abc123", "--seen", "w"], _thread_client(), tmp_path)
    # New comment appears in the thread
    client2 = _thread_client(comments=[
        {"author": "x", "body": "first comment", "score": 5, "depth": 0,
         "id": "c1", "name": "t1_c1"},
        {"author": "y", "body": "a reply", "score": 2, "depth": 1,
         "id": "c2", "name": "t1_c2"},
        {"author": "z", "body": "brand new take", "score": 1, "depth": 0,
         "id": "c3", "name": "t1_c3"},
    ])
    result, _ = _invoke(["thread", "s", "abc123", "--seen", "w"], client2, tmp_path)
    assert "brand new take" in result.output
    assert "first comment" not in result.output
    assert "2 previously seen" in result.output
    assert "flat view" in result.output


def test_thread_seen_no_new_comments_notes_it(tmp_path):
    _invoke(["thread", "s", "abc123", "--seen", "w"], _thread_client(), tmp_path)
    result, _ = _invoke(["thread", "s", "abc123", "--seen", "w"], _thread_client(), tmp_path)
    assert "No comments to show" in result.output
    assert "2 previously seen" in result.output


def test_thread_seen_jsonl_meta(tmp_path):
    _invoke(["thread", "s", "abc123", "--seen", "w", "--jsonl"], _thread_client(), tmp_path)
    result, _ = _invoke(["thread", "s", "abc123", "--seen", "w", "--jsonl"],
                        _thread_client(), tmp_path)
    lines = [json.loads(l) for l in result.output.strip().splitlines()]
    assert lines[-1]["_meta"]["seen_filtered"] == 2
    assert lines[-1]["_meta"]["total"] == 0


# ── --save TOPIC ──────────────────────────────────────────


def _search_client():
    client = MagicMock()
    client.search.return_value = {"items": [POST_ITEM], "after": None, "count": 1}
    return client


def test_save_writes_markdown_file(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    result, _ = _invoke(["search", "x", "--save", "mytopic"], _search_client())
    assert result.exit_code == 0
    files = list((tmp_path / "research" / "mytopic").glob("*-search-x.md"))
    assert len(files) == 1
    content = files[0].read_text()
    assert "Post One" in content
    assert "|" in content  # markdown table, regardless of stdout mode


def test_save_jsonl_variant(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    result, _ = _invoke(["search", "x", "--save", "mytopic", "--jsonl"], _search_client())
    files = list((tmp_path / "research" / "mytopic").glob("*.jsonl"))
    assert len(files) == 1
    lines = [json.loads(l) for l in files[0].read_text().strip().splitlines()]
    assert lines[0]["id"] == "a1"
    assert "_meta" in lines[-1]


def test_save_topic_name_sanitized(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    result, _ = _invoke(["search", "x", "--save", "../evil"], _search_client())
    assert result.exit_code == 0
    assert not (tmp_path / "evil").exists()
    assert (tmp_path / "research" / "_evil").exists()  # slash replaced, dots stripped


def test_save_on_thread_and_digest(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    _invoke(["thread", "s", "abc123", "--save", "t1"], _thread_client())
    assert list((tmp_path / "research" / "t1").glob("*-thread-abc123.md"))

    client = MagicMock()
    client.subreddit_info.return_value = {"name": "s", "subscribers": 1,
                                          "description": "", "active_users": None,
                                          "over_18": False, "url": "", "created_utc": 0,
                                          "title": ""}
    client.subreddit_posts.return_value = {"items": [POST_ITEM], "after": None, "count": 1}
    client.post_comments.return_value = {"post": POST_ITEM, "comments": [],
                                         "total": 0, "more_count": 0}
    _invoke(["digest", "s", "-T", "1", "--save", "t1"], client)
    assert list((tmp_path / "research" / "t1").glob("*-digest-s.md"))


# ── topic command group ───────────────────────────────────


def test_topic_create_list_remove(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    result, _ = _invoke(["topic", "create", "digi", "-r", "DataHoarder,Archiveteam",
                         "-q", "preservation"], tmp_path=tmp_path)
    assert result.exit_code == 0
    assert "Created topic 'digi'" in result.output

    result, _ = _invoke(["topic", "list"], tmp_path=tmp_path)
    assert "digi" in result.output
    assert "DataHoarder" in result.output

    result, _ = _invoke(["topic", "remove", "digi"], tmp_path=tmp_path)
    assert "Removed" in result.output
    result, _ = _invoke(["topic", "list"], tmp_path=tmp_path)
    assert "No topics yet" in result.output


def test_topic_create_duplicate_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    _invoke(["topic", "create", "digi", "-r", "a"], tmp_path=tmp_path)
    result, _ = _invoke(["topic", "create", "digi", "-r", "b"], tmp_path=tmp_path)
    assert result.exit_code == 1
    assert "already exists" in result.output


def test_topic_update_unknown_name(tmp_path):
    result, _ = _invoke(["topic", "update", "nope"], tmp_path=tmp_path)
    assert result.exit_code == 1
    assert "No topic named" in result.output


def _topic_client(items):
    client = MagicMock()
    client.subreddit_posts.return_value = {"items": list(items), "after": None,
                                           "count": len(items)}
    client.search.return_value = {"items": [], "after": None, "count": 0}
    return client


def test_topic_update_writes_file_and_delta(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    _invoke(["topic", "create", "digi", "-r", "DataHoarder"], tmp_path=tmp_path)

    result, client = _invoke(["topic", "update", "digi"],
                             _topic_client([POST_ITEM]), tmp_path)
    assert result.exit_code == 0
    assert "Post One" in result.output
    assert client.subreddit_posts.call_args[1]["sort"] == "new"
    update_files = list((tmp_path / "research" / "digi").glob("*-update.md"))
    assert len(update_files) == 1
    assert "Post One" in update_files[0].read_text()

    # Second update: nothing new -> no file, no repeat
    result, _ = _invoke(["topic", "update", "digi"], _topic_client([POST_ITEM]), tmp_path)
    assert "No new activity" in result.output
    assert len(list((tmp_path / "research" / "digi").glob("*-update.md"))) == 1

    # A new post arrives -> only it is reported
    newer = {**POST_ITEM, "id": "b2", "name": "t3_b2", "title": "Fresh Post"}
    result, _ = _invoke(["topic", "update", "digi", "--jsonl"],
                        _topic_client([POST_ITEM, newer]), tmp_path)
    lines = [json.loads(l) for l in result.output.strip().splitlines()]
    ids = [l.get("id") for l in lines if "id" in l]
    assert ids == ["b2"]
    assert lines[-1]["_meta"]["new"] == 1
    # Same-second runs get a collision suffix, so glob broadly
    assert len(list((tmp_path / "research" / "digi").glob("*-update*.md"))) == 2


def test_topic_update_query_dedups_posts_sweep(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    _invoke(["topic", "create", "digi", "-r", "DataHoarder", "-q", "tape"], tmp_path=tmp_path)
    client = MagicMock()
    client.subreddit_posts.return_value = {"items": [POST_ITEM], "after": None, "count": 1}
    client.search.return_value = {"items": [POST_ITEM], "after": None, "count": 1}
    result, _ = _invoke(["topic", "update", "digi", "--jsonl"], client, tmp_path)
    lines = [json.loads(l) for l in result.output.strip().splitlines()]
    ids = [l.get("id") for l in lines if "id" in l]
    assert ids == ["a1"]  # reported once, by the posts sweep only
    assert lines[-1]["_meta"]["new"] == 1


# ── Verification-round fixes ──────────────────────────────


def test_slug_blocks_dot_only_names():
    from reddit.cli import _slug
    assert _slug("..") == "untitled"
    assert _slug(".") == "untitled"
    assert _slug("..hidden") == "hidden"
    assert _slug("../evil") == "_evil"  # slash replaced, leading dots stripped
    assert _slug("a.b") == "a.b"  # interior dots fine


def test_save_dot_dot_stays_inside_research_root(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    result, _ = _invoke(["search", "x", "--save", ".."], _search_client())
    assert result.exit_code == 0
    # Nothing escapes the root; the file lands in research/untitled/
    outside = [p for p in tmp_path.glob("*.md")]
    assert outside == []
    assert list((tmp_path / "research" / "untitled").glob("*.md"))


def test_save_empty_topic_rejected():
    result, _ = _invoke(["search", "x", "--save", ""], _search_client())
    assert result.exit_code == 2
    assert "non-empty" in result.output + str(result.stderr or "")


def test_save_failure_warns_but_still_renders(tmp_path, monkeypatch):
    root = tmp_path / "research"
    root.mkdir()
    (root / "locked").mkdir(mode=0o555)
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(root))
    result, _ = _invoke(["search", "x", "--save", "locked"], _search_client())
    assert result.exit_code == 0
    assert "Post One" in result.output  # results still shown
    assert "could not save" in (result.stderr or "")


def test_topic_names_round_trip_through_slug(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    _invoke(["topic", "create", "a b", "-r", "x"], tmp_path=tmp_path)
    # update/remove with the same raw name the user typed must resolve
    result, _ = _invoke(["topic", "update", "a b"], _topic_client([]), tmp_path)
    assert "No topic named" not in result.output
    result, _ = _invoke(["topic", "remove", "a b"], tmp_path=tmp_path)
    assert "Removed" in result.output


def test_topic_update_surfaces_nsfw_hidden_and_query_error(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    _invoke(["topic", "create", "digi", "-r", "s", "-q", "tape"], tmp_path=tmp_path)
    client = MagicMock()
    client.subreddit_posts.return_value = {"items": [POST_ITEM], "after": None,
                                           "count": 1, "nsfw_hidden": 3}
    client.search.return_value = {"error": "Forbidden: /r/s/search"}
    result, client = _invoke(["topic", "update", "digi", "--jsonl"], client, tmp_path)
    lines = [json.loads(l) for l in result.output.strip().splitlines()]
    meta = lines[-1]["_meta"]
    assert meta["nsfw_hidden"] == 3
    assert "Forbidden" in meta["query_error"]
    # And a note lands in the archived update file
    update = list((tmp_path / "research" / "digi").glob("*-update*.md"))[0].read_text()
    assert "NSFW result(s) hidden" in update
    assert "Query sweep failed" in update


def test_topic_nsfw_optin_passes_through(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    _invoke(["topic", "create", "spicy", "-r", "s", "--nsfw"], tmp_path=tmp_path)
    client = _topic_client([POST_ITEM])
    result, client = _invoke(["topic", "update", "spicy"], client, tmp_path)
    assert client.subreddit_posts.call_args[1]["include_nsfw"] is True


# ── topic --media (phase 2) ───────────────────────────────


MEDIA_POST = {**POST_ITEM, "id": "m1", "name": "t3_m1",
              "media": [{"url": "https://i.redd.it/m1.jpg", "type": "image"}]}


def _invoke_topic_media(args, client, tmp_path):
    """Like _invoke but also stubs the media downloader to write a dummy file."""
    client.paginate.side_effect = lambda method, pages=1, **kw: method(**kw)
    runner = CliRunner(mix_stderr=False)
    with patch("reddit.cli.RedditClient", return_value=client), \
         patch("reddit.cli.SeenStore", lambda: SeenStore(str(tmp_path / "seen.json"))), \
         patch("reddit.cli.TopicStore", lambda: TopicStore(str(tmp_path / "topics.json"))), \
         patch("reddit.media.MediaDownloader._fetch") as mock_fetch:
        def fake_fetch(url, path_hint):
            p = path_hint("image/jpeg")
            p.write_bytes(b"IMG")
            return p, 3
        mock_fetch.side_effect = fake_fetch
        return runner.invoke(main, args), client


def test_topic_create_media_flag_persisted(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    _invoke(["topic", "create", "vis", "-r", "eink", "--media"], tmp_path=tmp_path)
    cfg = TopicStore(str(tmp_path / "topics.json")).get("vis")
    assert cfg["media"] is True
    result, _ = _invoke(["topic", "list"], tmp_path=tmp_path)
    assert "+media" in result.output


def test_topic_update_downloads_media(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    _invoke(["topic", "create", "vis", "-r", "eink", "--media"], tmp_path=tmp_path)
    client = MagicMock()
    client.subreddit_posts.return_value = {"items": [MEDIA_POST], "after": None, "count": 1}
    result, _ = _invoke_topic_media(["topic", "update", "vis", "--jsonl"], client, tmp_path)
    assert result.exit_code == 0, result.output
    # File landed in the topic's media/ folder
    media_dir = tmp_path / "research" / "vis" / "media"
    assert (media_dir / "m1-1.jpg").exists()
    assert (media_dir / "manifest.jsonl").exists()
    meta = json.loads(result.output.strip().splitlines()[-1])["_meta"]
    assert meta["media"]["downloaded"] == 1


def test_topic_update_media_only_for_new_posts(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    _invoke(["topic", "create", "vis", "-r", "eink", "--media"], tmp_path=tmp_path)
    client = MagicMock()
    client.subreddit_posts.return_value = {"items": [MEDIA_POST], "after": None, "count": 1}
    _invoke_topic_media(["topic", "update", "vis"], client, tmp_path)
    # Second run: post already seen -> no new download attempt
    client2 = MagicMock()
    client2.subreddit_posts.return_value = {"items": [MEDIA_POST], "after": None, "count": 1}
    with patch("reddit.media.MediaDownloader.download_post") as dp:
        result, _ = _invoke(["topic", "update", "vis"], client2, tmp_path)
        assert "No new activity" in result.output
        assert dp.call_count == 0


def test_topic_update_media_override(tmp_path, monkeypatch):
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    # Topic created WITHOUT media; --media overrides for one run
    _invoke(["topic", "create", "vis", "-r", "eink"], tmp_path=tmp_path)
    client = MagicMock()
    client.subreddit_posts.return_value = {"items": [MEDIA_POST], "after": None, "count": 1}
    result, _ = _invoke_topic_media(["topic", "update", "vis", "--media"], client, tmp_path)
    assert (tmp_path / "research" / "vis" / "media" / "m1-1.jpg").exists()

    # And --no-media suppresses it on a media topic
    _invoke(["topic", "create", "vis2", "-r", "eink", "--media"], tmp_path=tmp_path)
    client2 = MagicMock()
    client2.subreddit_posts.return_value = {"items": [MEDIA_POST], "after": None, "count": 1}
    with patch("reddit.media.MediaDownloader.download_post") as dp:
        _invoke(["topic", "update", "vis2", "--no-media"], client2, tmp_path)
        assert dp.call_count == 0


def test_topic_media_dir_failure_does_not_abort_update(tmp_path, monkeypatch):
    """A media-dir failure must warn, not traceback — the delta report is primary."""
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    _invoke(["topic", "create", "vis", "-r", "eink", "--media"], tmp_path=tmp_path)
    # Make the topic's media path un-creatable: put a FILE where media/ should go
    media_path = tmp_path / "research" / "vis" / "media"
    media_path.parent.mkdir(parents=True, exist_ok=True)
    media_path.write_text("i am a file, not a dir")
    client = MagicMock()
    client.subreddit_posts.return_value = {"items": [MEDIA_POST], "after": None, "count": 1}
    result, _ = _invoke_topic_media(["topic", "update", "vis", "--jsonl"], client, tmp_path)
    assert result.exit_code == 0, result.output
    # Delta report still emitted despite the media failure
    lines = [json.loads(l) for l in result.output.strip().splitlines()]
    assert lines[-1]["_meta"]["new"] == 1
    assert "could not prepare media dir" in (result.stderr or "")


def test_topic_media_cap_truncation_signalled_and_deferred(tmp_path, monkeypatch):
    """Cap-truncated media must be flagged AND retried (post left un-seen)."""
    monkeypatch.setenv("REDDIT_RESEARCH_DIR", str(tmp_path / "research"))
    _invoke(["topic", "create", "vis", "-r", "eink", "--media"], tmp_path=tmp_path)
    # A gallery with more items than the (patched tiny) cap
    big = {**POST_ITEM, "id": "big", "name": "t3_big",
           "media": [{"url": f"https://i.redd.it/{i}.jpg", "type": "image"} for i in range(4)]}
    client = MagicMock()
    client.subreddit_posts.return_value = {"items": [big], "after": None, "count": 1}
    with patch("reddit.cli.TOPIC_MEDIA_MAX", 2):
        result, _ = _invoke_topic_media(["topic", "update", "vis", "--jsonl"], client, tmp_path)
    meta = json.loads(result.output.strip().splitlines()[-1])["_meta"]
    assert meta["media_truncated"] is True
    # The post was NOT recorded as seen, so it retries next run
    recorded = SeenStore(str(tmp_path / "seen.json")).names().get("topic:vis", 0)
    assert recorded == 0

    # Next run: post reappears as a delta; remaining images download
    client2 = MagicMock()
    client2.subreddit_posts.return_value = {"items": [big], "after": None, "count": 1}
    with patch("reddit.cli.TOPIC_MEDIA_MAX", 200):
        result2, _ = _invoke_topic_media(["topic", "update", "vis", "--jsonl"], client2, tmp_path)
    meta2 = json.loads(result2.output.strip().splitlines()[-1])["_meta"]
    assert meta2["new"] == 1  # reappeared
    assert "media_truncated" not in meta2  # completed this time
    assert SeenStore(str(tmp_path / "seen.json")).names()["topic:vis"] == 1


def test_topic_store_roundtrip(tmp_path):
    store = TopicStore(str(tmp_path / "topics.json"))
    assert store.all() == {}
    store.set("a", {"subreddits": "x"})
    assert store.get("a") == {"subreddits": "x"}
    assert store.remove("a") is True
    assert store.remove("a") is False
