"""Tests for the agent-oriented features: jsonl/fields, since/seen, digest,
thread filters, structured errors, seen store."""

import json
import time
from unittest.mock import MagicMock, patch

import click
import pytest
from click.testing import CliRunner

from reddit.cache import SeenStore, SEEN_CAP
from reddit.cli import main, _parse_since, _apply_since, _project


def _invoke(args, client=None, seen_path=None):
    client = client or MagicMock()
    client.paginate.side_effect = lambda method, pages=1, **kw: method(**kw)
    patches = [patch("reddit.cli.RedditClient", return_value=client)]
    if seen_path is not None:
        patches.append(patch("reddit.cli.SeenStore", lambda: SeenStore(str(seen_path))))
    runner = CliRunner()
    with patches[0]:
        if seen_path is not None:
            with patches[1]:
                return runner.invoke(main, args), client
        return runner.invoke(main, args), client


POST_ITEM = {"id": "a1", "name": "t3_a1", "title": "Post One", "author": "u1",
             "subreddit": "s", "score": 10, "num_comments": 2, "permalink": "pl",
             "created_utc": time.time() - 3600, "over_18": False, "selftext": "body"}
OLD_ITEM = {**POST_ITEM, "id": "a2", "name": "t3_a2", "title": "Old Post",
            "created_utc": time.time() - 90 * 86400}


def _search_client(items, after=None):
    client = MagicMock()
    client.search.return_value = {"items": list(items), "after": after, "count": len(items)}
    return client


# ── --jsonl and --fields ──────────────────────────────────


def test_jsonl_emits_items_and_meta():
    client = _search_client([POST_ITEM], after="t3_cursor")
    result, _ = _invoke(["search", "x", "--jsonl"], client)
    lines = [json.loads(l) for l in result.output.strip().splitlines()]
    assert lines[0]["id"] == "a1"
    assert lines[-1]["_meta"]["after"] == "t3_cursor"
    assert lines[-1]["_meta"]["count"] == 1


def test_fields_projection():
    client = _search_client([POST_ITEM])
    result, _ = _invoke(["search", "x", "--jsonl", "--fields", "id,title,score"], client)
    first = json.loads(result.output.strip().splitlines()[0])
    assert set(first.keys()) == {"id", "title", "score"}


def test_unknown_field_warns_on_stderr():
    client = _search_client([POST_ITEM])
    runner = CliRunner(mix_stderr=False)
    client.paginate.side_effect = lambda method, pages=1, **kw: method(**kw)
    with patch("reddit.cli.RedditClient", return_value=client):
        result = runner.invoke(main, ["search", "x", "--jsonl", "--fields", "id,bogus"])
    assert "unknown field(s) bogus" in result.stderr
    assert json.loads(result.output.strip().splitlines()[0]) == {"id": "a1"}


def test_fields_with_json_output():
    client = _search_client([POST_ITEM])
    result, _ = _invoke(["search", "x", "-j", "--fields", "id"], client)
    data = json.loads(result.output)
    assert data["items"] == [{"id": "a1"}]


def test_project_empty_items_no_warning():
    assert _project([], "id,title") == []


# ── Structured errors (--jsonl) ───────────────────────────


def test_jsonl_error_retryable_classification():
    client = MagicMock()
    client.search.return_value = {"error": "Rate limited (HTTP 429) after retries"}
    client.paginate.side_effect = lambda method, pages=1, **kw: method(**kw)
    result, _ = _invoke(["search", "x", "--jsonl"], client)
    assert result.exit_code == 1
    err = json.loads(result.output.strip())
    assert err["retryable"] is True

    client.search.return_value = {"error": "Not found: /r/nope/hot"}
    result, _ = _invoke(["search", "x", "--jsonl"], client)
    err = json.loads(result.output.strip())
    assert err["retryable"] is False


# ── --since ───────────────────────────────────────────────


def test_parse_since_ages():
    now = time.time()
    assert now - _parse_since("90m") == pytest.approx(90 * 60, abs=5)
    assert now - _parse_since("24h") == pytest.approx(24 * 3600, abs=5)
    assert now - _parse_since("7d") == pytest.approx(7 * 86400, abs=5)
    assert now - _parse_since("2w") == pytest.approx(14 * 86400, abs=5)


def test_parse_since_iso_date():
    cutoff = _parse_since("2026-07-20")
    assert cutoff == pytest.approx(1784505600, abs=86400)


def test_parse_since_invalid():
    with pytest.raises(click.BadParameter):
        _parse_since("yesterday-ish")


def test_apply_since_filters_old_items():
    result = {"items": [POST_ITEM, OLD_ITEM], "after": None, "count": 2}
    out = _apply_since(result, "7d")
    assert [i["id"] for i in out["items"]] == ["a1"]
    assert out["since_filtered"] == 1


def test_since_flag_end_to_end():
    client = _search_client([POST_ITEM, OLD_ITEM])
    result, _ = _invoke(["search", "x", "--since", "7d", "--jsonl"], client)
    lines = [json.loads(l) for l in result.output.strip().splitlines()]
    ids = [l.get("id") for l in lines if "id" in l]
    assert ids == ["a1"]
    assert lines[-1]["_meta"]["since_filtered"] == 1


# ── --seen delta tracking ─────────────────────────────────


def test_seen_store_roundtrip(tmp_path):
    store = SeenStore(str(tmp_path / "seen.json"))
    items = [{"name": "t3_a"}, {"name": "t3_b"}]
    assert store.filter_new("w", items) == items
    store.record("w", items)
    assert store.filter_new("w", items) == []
    assert store.filter_new("w", [{"name": "t3_c"}]) == [{"name": "t3_c"}]
    assert store.names() == {"w": 2}
    assert store.clear("w") == 1
    assert store.names() == {}


def test_seen_store_caps_ids(tmp_path):
    store = SeenStore(str(tmp_path / "seen.json"))
    store.record("w", [{"name": f"t3_{i}"} for i in range(SEEN_CAP + 100)])
    counts = store.names()
    assert counts["w"] == SEEN_CAP
    # Oldest ids evicted, newest kept
    assert store.filter_new("w", [{"name": "t3_0"}]) == [{"name": "t3_0"}]
    assert store.filter_new("w", [{"name": f"t3_{SEEN_CAP + 99}"}]) == []


def test_seen_flag_suppresses_repeat_runs(tmp_path):
    seen_path = tmp_path / "seen.json"
    client = _search_client([POST_ITEM])
    r1, _ = _invoke(["search", "x", "--seen", "watch", "--jsonl"], client, seen_path)
    ids1 = [json.loads(l).get("id") for l in r1.output.strip().splitlines()]
    assert "a1" in ids1

    client2 = _search_client([POST_ITEM])
    r2, _ = _invoke(["search", "x", "--seen", "watch", "--jsonl"], client2, seen_path)
    lines2 = [json.loads(l) for l in r2.output.strip().splitlines()]
    assert [l.get("id") for l in lines2 if "id" in l] == []
    assert lines2[-1]["_meta"]["seen_filtered"] == 1


def test_seen_command_lists_and_clears(tmp_path):
    seen_path = tmp_path / "seen.json"
    SeenStore(str(seen_path)).record("watch", [{"name": "t3_a"}])
    with patch("reddit.cli.SeenStore", lambda: SeenStore(str(seen_path))):
        runner = CliRunner()
        result = runner.invoke(main, ["seen"])
        assert "watch: 1 ids tracked" in result.output
        result = runner.invoke(main, ["seen", "--clear", "watch"])
        assert "Cleared" in result.output
        result = runner.invoke(main, ["seen"])
        assert "No seen stores" in result.output


# ── thread --author / --min-score ─────────────────────────


def _thread_client():
    client = MagicMock()
    client.post_comments.return_value = {
        "post": {"title": "T", "author": "op_user", "subreddit": "s", "score": 1,
                 "upvote_ratio": 0.9, "num_comments": 3, "url": "u", "permalink": "p"},
        "comments": [
            {"author": "op_user", "body": "answer one", "score": 50, "depth": 1, "id": "c1", "name": "t1_c1"},
            {"author": "rando", "body": "low effort", "score": 1, "depth": 0, "id": "c2", "name": "t1_c2"},
            {"author": "OP_USER", "body": "answer two", "score": 3, "depth": 2, "id": "c3", "name": "t1_c3"},
        ],
        "total": 3, "more_count": 0,
    }
    return client


def test_thread_jsonl_fields_project_post_line_too():
    """A projected thread must not leak the full selftext via the post line."""
    client = _thread_client()
    client.post_comments.return_value["post"]["selftext"] = "x" * 5000
    result, _ = _invoke(["thread", "s", "abc123", "--jsonl", "--fields", "author,body,score"], client)
    post_line = json.loads(result.output.strip().splitlines()[0])["post"]
    assert "selftext" not in post_line
    assert post_line == {"author": "op_user", "score": 1}


def test_thread_author_filter_case_insensitive():
    result, _ = _invoke(["thread", "s", "abc123", "--author", "op_user", "--jsonl"], _thread_client())
    lines = [json.loads(l) for l in result.output.strip().splitlines()]
    bodies = [l["body"] for l in lines if "body" in l]
    assert bodies == ["answer one", "answer two"]
    assert lines[-1]["_meta"]["filtered_out"] == 1


def test_thread_min_score_filter():
    result, _ = _invoke(["thread", "s", "abc123", "--min-score", "3"], _thread_client())
    assert "low effort" not in result.output
    assert "answer one" in result.output


# ── Verification-round fixes ──────────────────────────────


def test_since_validated_before_api_call():
    """A typo'd --since must fail before any API quota is spent."""
    client = MagicMock()
    result, client = _invoke(["search", "x", "--since", "garbage"], client)
    assert result.exit_code == 2
    assert "expected an age" in result.output
    assert client.paginate.call_count == 0


def test_after_flag_on_all_cursor_commands():
    """Every command that emits a cursor must accept --after to resume."""
    for cmd, stub in [
        (["user-posts", "u"], "user_posts"),
        (["user-comments", "u"], "user_comments"),
        (["popular"], "popular_posts"),
        (["find-subs", "q"], "search_subreddits"),
        (["popular-subs"], "popular_subreddits"),
    ]:
        client = MagicMock()
        getattr(client, stub).return_value = {"items": [], "after": None, "count": 0}
        result, client = _invoke(cmd + ["--after", "t3_resume", "--jsonl"], client)
        assert result.exit_code == 0, f"{cmd} rejected --after: {result.output}"
        assert getattr(client, stub).call_args[1]["after"] == "t3_resume", cmd


def test_empty_filtered_page_shows_partial_error_note():
    client = MagicMock()
    client.search.return_value = {
        "items": [OLD_ITEM], "after": "t3_old", "count": 1,
        "partial_error": "Rate limited (HTTP 429) after retries"}
    result, _ = _invoke(["search", "x", "--since", "1h"], client)
    assert "pagination stopped early" in result.output
    assert "older result(s) filtered" in result.output


def test_markdown_shows_partial_error_note():
    client = MagicMock()
    client.search.return_value = {
        "items": [POST_ITEM], "after": None, "count": 1,
        "partial_error": "Rate limited (HTTP 429) after retries"}
    result, _ = _invoke(["search", "x", "-m"], client)
    assert "pagination stopped early" in result.output


def test_seen_recency_refreshes_for_still_visible_items(tmp_path):
    """A long-lived visible item must not age past the cap and re-emit as new."""
    store = SeenStore(str(tmp_path / "seen.json"))
    store.record("w", [{"name": "t3_longlived"}])
    # Re-recording (item still visible in later fetches) refreshes its position
    store.record("w", [{"name": f"t3_{i}"} for i in range(SEEN_CAP - 1)])
    store.record("w", [{"name": "t3_longlived"}])
    store.record("w", [{"name": f"t3_new{i}"} for i in range(50)])
    assert store.filter_new("w", [{"name": "t3_longlived"}]) == []


def test_record_seen_uses_pre_suppression_list(tmp_path):
    """Suppressed-but-visible items refresh recency on every run."""
    seen_path = tmp_path / "seen.json"
    client = _search_client([POST_ITEM])
    _invoke(["search", "x", "--seen", "w", "--jsonl"], client, seen_path)
    # Second run: item suppressed, but still recorded (position refresh)
    before = json.loads(seen_path.read_text())["w"]
    client2 = _search_client([POST_ITEM, {**POST_ITEM, "id": "b1", "name": "t3_b1"}])
    _invoke(["search", "x", "--seen", "w", "--jsonl"], client2, seen_path)
    after = json.loads(seen_path.read_text())["w"]
    assert "t3_a1" in after and "t3_b1" in after
    # a1 was re-recorded after b1's batch merged — its position moved to the batch order
    assert set(after) >= set(before)


def test_seen_store_atomic_save(tmp_path):
    store = SeenStore(str(tmp_path / "seen.json"))
    store.record("w", [{"name": "t3_a"}])
    # No leftover temp file after a successful save
    assert not (tmp_path / "seen.json.tmp").exists()
    assert store.names() == {"w": 1}


def test_thread_filtered_view_flat_and_counted():
    """Filtered thread view must not indent by orphaned depth."""
    result, _ = _invoke(["thread", "s", "abc123", "--author", "op_user"], _thread_client())
    assert "flat view" in result.output
    assert "1 filtered out" in result.output
    # depth-2 comment renders flat (2 leading spaces from the base indent only)
    assert "\n      " not in result.output.split("comments")[1][:400]


def test_thread_all_filtered_shows_note():
    result, _ = _invoke(["thread", "s", "abc123", "--author", "nobody_matches"], _thread_client())
    assert "No comments to show" in result.output
    assert "3 filtered out" in result.output


def test_digest_notes_skipped_threads():
    client = _digest_client()
    client.subreddit_posts.return_value = {
        "items": [POST_ITEM, {**POST_ITEM, "id": "p2", "name": "t3_p2", "title": "Second"}],
        "after": None, "count": 2}
    client.post_comments.side_effect = [
        {"post": POST_ITEM, "comments": [], "total": 0, "more_count": 0},
        {"error": "Forbidden: private"},
    ]
    result, _ = _invoke(["digest", "testsub", "-T", "2"], client)
    assert "skipped: Forbidden" in result.output


# ── digest ────────────────────────────────────────────────


def _digest_client():
    client = MagicMock()
    client.subreddit_info.return_value = {
        "name": "testsub", "title": "Test", "description": "a test sub",
        "subscribers": 1234, "active_users": None, "over_18": False, "url": "u",
        "created_utc": 0}
    client.subreddit_posts.return_value = {
        "items": [POST_ITEM, {**POST_ITEM, "id": "st", "name": "t3_st", "stickied": True}],
        "after": None, "count": 2}
    client.post_comments.return_value = {
        "post": POST_ITEM,
        "comments": [{"author": "c", "body": "insightful", "score": 9, "depth": 0}],
        "total": 1, "more_count": 0}
    client.search.return_value = {"items": [POST_ITEM], "after": None, "count": 1}
    return client


def test_digest_markdown_document():
    client = _digest_client()
    result, client = _invoke(["digest", "testsub", "-T", "1"], client)
    assert result.exit_code == 0
    assert "# r/testsub digest" in result.output
    assert "1,234 subscribers" in result.output
    assert "Top posts" in result.output
    assert "insightful" in result.output
    # Stickied post is not chosen for thread excerpts
    assert client.post_comments.call_count == 1
    assert client.post_comments.call_args[0][1] == "a1"


def test_digest_with_query_section():
    client = _digest_client()
    result, client = _invoke(["digest", "testsub", "-T", "0", "-q", "needle"], client)
    assert "Search: needle" in result.output
    assert client.search.call_args[1]["subreddit"] == "testsub"


def test_digest_json_mode():
    client = _digest_client()
    result, _ = _invoke(["digest", "testsub", "-T", "1", "-j"], client)
    data = json.loads(result.output)
    assert data["info"]["name"] == "testsub"
    assert len(data["threads"]) == 1
