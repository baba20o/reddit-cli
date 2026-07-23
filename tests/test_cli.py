"""Tests for reddit.cli — command-line interface behavior."""

from unittest.mock import MagicMock, patch

from click.testing import CliRunner

from reddit.cli import main


def _invoke(args, client=None):
    """Run the CLI with a mocked RedditClient."""
    client = client or MagicMock()
    with patch("reddit.cli.RedditClient", return_value=client):
        runner = CliRunner()
        return runner.invoke(main, args), client


# ── Limit validation (rough edge #7) ──────────────────────


def test_limit_zero_rejected():
    result, _ = _invoke(["search", "test", "-n", "0"])
    assert result.exit_code == 2
    assert "not in the range" in result.output or "Invalid value" in result.output


def test_limit_over_max_rejected():
    result, _ = _invoke(["search", "test", "-n", "500"])
    assert result.exit_code == 2


def test_thread_limit_allows_up_to_500():
    client = MagicMock()
    client.post_comments.return_value = {"post": {}, "comments": [], "total": 0, "more_count": 0}
    result, client = _invoke(["thread", "programming", "abc123", "-n", "500"], client)
    assert result.exit_code == 0


# ── Thread URL parsing (rough edge #5) ────────────────────


def test_thread_accepts_pasted_url():
    client = MagicMock()
    client.post_comments.return_value = {"post": {}, "comments": [], "total": 0, "more_count": 0}
    result, client = _invoke(
        ["thread", "https://reddit.com/r/ClaudeCode/comments/1v0d7iv/some_title/"], client)
    assert result.exit_code == 0
    args, kwargs = client.post_comments.call_args
    assert args[0] == "ClaudeCode"
    assert args[1] == "1v0d7iv"


def test_thread_accepts_subreddit_and_id():
    client = MagicMock()
    client.post_comments.return_value = {"post": {}, "comments": [], "total": 0, "more_count": 0}
    result, client = _invoke(["thread", "programming", "abc123"], client)
    assert result.exit_code == 0
    args, _ = client.post_comments.call_args
    assert (args[0], args[1]) == ("programming", "abc123")


def test_thread_unparseable_reference_errors():
    result, _ = _invoke(["thread", "not a valid ref !!"])
    assert result.exit_code == 1
    assert "Cannot parse" in result.output


# ── NSFW flag wiring (rough edge #4) ──────────────────────


def test_search_passes_nsfw_flag():
    client = MagicMock()
    client.search.return_value = {"items": [], "after": None, "count": 0}
    _invoke(["search", "test", "--nsfw"], client)
    assert client.search.call_args[1]["include_nsfw"] is True
    _invoke(["search", "test"], client)
    assert client.search.call_args[1]["include_nsfw"] is False


def test_hidden_nsfw_note_rendered():
    client = MagicMock()
    client.search.return_value = {"items": [], "after": None, "count": 0, "nsfw_hidden": 3}
    result, _ = _invoke(["search", "test"], client)
    assert "3 NSFW result(s) hidden" in result.output


def test_hidden_nsfw_note_rendered_for_comments():
    client = MagicMock()
    client.search_comments.return_value = {"items": [], "after": None, "count": 0, "nsfw_hidden": 2}
    result, _ = _invoke(["comments", "test"], client)
    assert "2 NSFW result(s) hidden" in result.output


def test_empty_filtered_page_still_shows_cursor():
    client = MagicMock()
    client.search.return_value = {"items": [], "after": "t3_cursor", "count": 0, "nsfw_hidden": 25}
    result, _ = _invoke(["search", "test"], client)
    assert "--after t3_cursor" in result.output


# ── Markdown link escaping (rough edge #8) ────────────────


def test_md_link_escapes_backslashes_and_brackets():
    from reddit.cli import _md_link
    assert _md_link("trailing backslash \\", "https://x") == "[trailing backslash \\\\](https://x)"
    assert _md_link("a]b[c|d", "https://x") == "[a\\]b\\[c\\|d](https://x)"


# ── Thread rendering (rough edge #6) ──────────────────────


def test_thread_renders_indented_replies():
    client = MagicMock()
    client.post_comments.return_value = {
        "post": {"title": "T", "author": "a", "subreddit": "s", "score": 1,
                 "upvote_ratio": 0.9, "num_comments": 2, "url": "u", "permalink": "p"},
        "comments": [
            {"author": "top", "body": "top comment", "score": 5, "depth": 0},
            {"author": "child", "body": "nested reply", "score": 2, "depth": 1},
        ],
        "total": 2,
        "more_count": 7,
    }
    result, _ = _invoke(["thread", "programming", "abc123"], client)
    assert result.exit_code == 0
    assert "top comment" in result.output
    assert "nested reply" in result.output
    assert "7 more comments" in result.output


# ── NSFW marker in table output (rough edge #4) ───────────


def test_nsfw_post_marked_in_table():
    client = MagicMock()
    client.search.return_value = {
        "items": [{"subreddit": "x", "title": "some title", "author": "a",
                   "score": 1, "num_comments": 0, "created_utc": 0, "over_18": True}],
        "after": None, "count": 1,
    }
    result, _ = _invoke(["search", "test", "--nsfw"], client)
    assert "NSFW" in result.output
