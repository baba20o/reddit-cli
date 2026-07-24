"""Tests for reddit.api — Reddit OAuth2 client."""

import json
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from reddit.api import (
    RedditClient, _format_post, _format_comment, _format_subreddit,
    _retry_wait_seconds, _filter_nsfw, _extract_comment_tree,
    _sort_comment_tree, parse_post_reference,
)


# ── Sample Data ───────────────────────────────────────────

SAMPLE_POST = {
    "kind": "t3",
    "data": {
        "id": "abc123",
        "name": "t3_abc123",
        "title": "Show Reddit: My Cool Project",
        "author": "testuser",
        "subreddit": "programming",
        "score": 420,
        "upvote_ratio": 0.95,
        "num_comments": 73,
        "url": "https://example.com",
        "selftext": "",
        "created_utc": 1773756000,
        "permalink": "/r/programming/comments/abc123/show_reddit_my_cool_project/",
        "is_self": False,
        "link_flair_text": "Project",
        "over_18": False,
        "stickied": False,
    },
}

SAMPLE_COMMENT = {
    "kind": "t1",
    "data": {
        "id": "xyz789",
        "name": "t1_xyz789",
        "author": "commenter",
        "body": "This is a great project! Love the architecture.",
        "score": 42,
        "subreddit": "programming",
        "created_utc": 1773759600,
        "permalink": "/r/programming/comments/abc123/show_reddit/xyz789/",
        "parent_id": "t3_abc123",
        "link_id": "t3_abc123",
        "link_title": "Show Reddit: My Cool Project",
    },
}

SAMPLE_SUBREDDIT = {
    "kind": "t5",
    "data": {
        "display_name": "programming",
        "title": "programming",
        "public_description": "Computer Programming",
        "subscribers": 5000000,
        "accounts_active": 12000,
        "created_utc": 1169971000,
        "over18": False,
        "url": "/r/programming/",
    },
}

SAMPLE_LISTING = {
    "kind": "Listing",
    "data": {
        "after": "t3_next123",
        "children": [SAMPLE_POST],
    },
}

SAMPLE_COMMENT_LISTING = {
    "kind": "Listing",
    "data": {
        "after": None,
        "children": [SAMPLE_COMMENT],
    },
}

SAMPLE_TOKEN_RESPONSE = {
    "access_token": "test_token_123",
    "token_type": "bearer",
    "expires_in": 3600,
    "scope": "read",
}

SAMPLE_USER = {
    "kind": "t2",
    "data": {
        "name": "testuser",
        "link_karma": 5000,
        "comment_karma": 12000,
        "total_karma": 17000,
        "created_utc": 1500000000,
        "is_gold": True,
        "verified": True,
    },
}


def _mock_response(data, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.headers = {}
    resp.raise_for_status = MagicMock()
    return resp


# ── Unit Tests: Formatters ────────────────────────────────


def test_format_post():
    result = _format_post(SAMPLE_POST)
    assert result["id"] == "abc123"
    assert result["title"] == "Show Reddit: My Cool Project"
    assert result["author"] == "testuser"
    assert result["subreddit"] == "programming"
    assert result["score"] == 420
    assert result["num_comments"] == 73
    assert "reddit.com" in result["permalink"]


def test_format_comment():
    result = _format_comment(SAMPLE_COMMENT)
    assert result["id"] == "xyz789"
    assert result["author"] == "commenter"
    assert result["body"] == "This is a great project! Love the architecture."
    assert result["score"] == 42
    assert result["link_title"] == "Show Reddit: My Cool Project"


def test_format_subreddit():
    result = _format_subreddit(SAMPLE_SUBREDDIT)
    assert result["name"] == "programming"
    assert result["subscribers"] == 5000000
    assert result["active_users"] == 12000
    assert result["over_18"] is False


def test_retry_wait_seconds_base():
    wait = _retry_wait_seconds(0)
    assert 2.0 <= wait <= 3.0


def test_retry_wait_seconds_429():
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {}
    wait = _retry_wait_seconds(0, resp)
    assert 10.0 <= wait <= 11.0


def test_retry_wait_respects_retry_after():
    resp = MagicMock()
    resp.status_code = 429
    resp.headers = {"Retry-After": "30"}
    wait = _retry_wait_seconds(0, resp)
    assert wait == 30.0


def test_retry_wait_capped():
    wait = _retry_wait_seconds(10)
    assert wait <= 60.0


# ── Client Tests (mocked HTTP) ───────────────────────────


def _make_client():
    """Create a client with mocked auth. load_dotenv is patched so a local
    .env file can't leak into the test environment."""
    with patch.dict("os.environ", {
        "REDDIT_CLIENT_ID": "test_id",
        "REDDIT_CLIENT_SECRET": "test_secret",
        "REDDIT_USERNAME": "testuser",
        "REDDIT_PASSWORD": "testpass",
    }):
        with patch("reddit.api.load_dotenv"), patch("reddit.api.get_rate_limiter") as mock_rl:
            mock_rl.return_value = MagicMock()
            client = RedditClient(use_cache=False)
            # Pre-set token to skip auth
            client._token = "test_token"
            client._token_expires = time.time() + 3600
            client.session.headers["Authorization"] = "Bearer test_token"
            return client


def test_search():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_LISTING)):
        result = client.search("test query")
        assert result["count"] == 1
        assert result["items"][0]["id"] == "abc123"


def test_search_in_subreddit():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_LISTING)) as mock_get:
        result = client.search("test", subreddit="python")
        url = mock_get.call_args[0][0]
        assert "/r/python/search" in url
        assert mock_get.call_args[1]["params"]["restrict_sr"] == "true"


def test_search_comments():
    """search_comments searches posts then fetches their top comments."""
    client = _make_client()
    # Mock both the post search and the post_comments fetch
    post_thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [SAMPLE_COMMENT]}},
    ]
    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [
            _mock_response(SAMPLE_LISTING),           # search() call
            _mock_response(post_thread_response),      # post_comments() call
        ]
        result = client.search_comments("great project")
        assert result["count"] >= 1
        assert result["items"][0]["body"] == "This is a great project! Love the architecture."


def test_subreddit_posts():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_LISTING)) as mock_get:
        result = client.subreddit_posts("programming", sort="top")
        url = mock_get.call_args[0][0]
        assert "/r/programming/top" in url


def test_subreddit_info():
    client = _make_client()
    about_response = {"kind": "t5", "data": SAMPLE_SUBREDDIT["data"]}
    with patch.object(client.session, "get", return_value=_mock_response(about_response)):
        result = client.subreddit_info("programming")
        assert result["name"] == "programming"
        assert result["subscribers"] == 5000000


def test_post_comments():
    client = _make_client()
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [SAMPLE_COMMENT]}},
    ]
    with patch.object(client.session, "get", return_value=_mock_response(thread_response)):
        result = client.post_comments("programming", "abc123")
        assert result["post"]["title"] == "Show Reddit: My Cool Project"
        assert len(result["comments"]) == 1
        assert result["comments"][0]["author"] == "commenter"


def test_user_about():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_USER)):
        result = client.user_about("testuser")
        assert result["username"] == "testuser"
        assert result["total_karma"] == 17000


def test_user_posts():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_LISTING)) as mock_get:
        result = client.user_posts("testuser")
        url = mock_get.call_args[0][0]
        assert "/user/testuser/submitted" in url


def test_user_comments():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_COMMENT_LISTING)) as mock_get:
        result = client.user_comments("testuser")
        url = mock_get.call_args[0][0]
        assert "/user/testuser/comments" in url


def test_search_subreddits():
    client = _make_client()
    sub_listing = {
        "kind": "Listing",
        "data": {"after": None, "children": [SAMPLE_SUBREDDIT]},
    }
    with patch.object(client.session, "get", return_value=_mock_response(sub_listing)) as mock_get:
        result = client.search_subreddits("programming")
        assert mock_get.call_args[1]["params"]["type"] == "sr"


def test_popular_posts():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_LISTING)) as mock_get:
        result = client.popular_posts()
        url = mock_get.call_args[0][0]
        assert "/r/popular/hot" in url


def test_429_retry():
    client = _make_client()
    resp_429 = MagicMock()
    resp_429.status_code = 429
    resp_429.headers = {}
    resp_ok = _mock_response(SAMPLE_LISTING)

    with patch.object(client.session, "get", side_effect=[resp_429, resp_ok]):
        with patch("reddit.api.time.sleep"):
            result = client.search("test")
            assert result["count"] == 1


def test_401_token_refresh():
    client = _make_client()
    resp_401 = MagicMock()
    resp_401.status_code = 401
    resp_401.headers = {}
    resp_ok = _mock_response(SAMPLE_LISTING)

    with patch.object(client.session, "get", side_effect=[resp_401, resp_ok]):
        with patch("requests.post", return_value=_mock_response(SAMPLE_TOKEN_RESPONSE)):
            result = client.search("test")
            assert result["count"] == 1


def test_404_returns_error():
    client = _make_client()
    resp_404 = MagicMock()
    resp_404.status_code = 404
    resp_404.headers = {}

    with patch.object(client.session, "get", return_value=resp_404):
        result = client.user_about("nonexistent_user_12345")
        assert "error" in result
        assert "Not found" in result["error"]


def test_403_returns_error():
    client = _make_client()
    resp_403 = MagicMock()
    resp_403.status_code = 403
    resp_403.headers = {}

    with patch.object(client.session, "get", return_value=resp_403):
        result = client.subreddit_posts("quarantined_sub")
        assert "error" in result
        assert "Forbidden" in result["error"]


def test_missing_credentials():
    # load_dotenv patched: a real .env on disk must not repopulate the cleared env
    with patch.dict("os.environ", {}, clear=True):
        with patch("reddit.api.load_dotenv"), patch("reddit.api.get_rate_limiter") as mock_rl:
            mock_rl.return_value = MagicMock()
            client = RedditClient(use_cache=False)
            with pytest.raises(RuntimeError, match="credentials not set"):
                client._ensure_token()


def test_pagination_after():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_LISTING)):
        result = client.search("test")
        assert result["after"] == "t3_next123"


# ── HTML entities (rough edge #2) ─────────────────────────
# Fixed via raw_json=1: Reddit returns unescaped strings in ALL fields
# (including url), so no client-side unescaping is needed or safe.


def test_get_sends_raw_json():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_LISTING)) as mock_get:
        client.search("test")
        assert mock_get.call_args[1]["params"]["raw_json"] == 1


def test_thread_request_sends_raw_json():
    client = _make_client()
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [SAMPLE_COMMENT]}},
    ]
    with patch.object(client.session, "get", return_value=_mock_response(thread_response)) as mock_get:
        client.post_comments("programming", "abc123")
        assert mock_get.call_args[1]["params"]["raw_json"] == 1


# ── Deleted/empty authors (rough edge #3) ─────────────────


def test_format_post_empty_author_marked_deleted():
    post = {"kind": "t3", "data": {**SAMPLE_POST["data"], "author": ""}}
    assert _format_post(post)["author"] == "[deleted]"


def test_format_comment_empty_author_marked_deleted():
    comment = {"kind": "t1", "data": {**SAMPLE_COMMENT["data"], "author": None}}
    assert _format_comment(comment)["author"] == "[deleted]"


def test_search_comments_skips_empty_bodies():
    client = _make_client()
    deleted = {"kind": "t1", "data": {**SAMPLE_COMMENT["data"], "id": "del1",
               "name": "t1_del1", "body": "[removed]"}}
    empty = {"kind": "t1", "data": {**SAMPLE_COMMENT["data"], "id": "del2",
             "name": "t1_del2", "body": ""}}
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [SAMPLE_COMMENT, deleted, empty]}},
    ]
    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [
            _mock_response(SAMPLE_LISTING),
            _mock_response(thread_response),
        ]
        result = client.search_comments("great project")
        bodies = [c["body"] for c in result["items"]]
        assert "This is a great project! Love the architecture." in bodies
        assert "[removed]" not in bodies
        assert "" not in bodies


# ── Active users (rough edge #1) ──────────────────────────


def test_format_subreddit_prefers_active_user_count():
    sub = {"kind": "t5", "data": {**SAMPLE_SUBREDDIT["data"],
           "active_user_count": 777, "accounts_active": 5}}
    assert _format_subreddit(sub)["active_users"] == 777


def test_format_subreddit_active_users_none_when_unreported():
    data = dict(SAMPLE_SUBREDDIT["data"])
    data.pop("accounts_active", None)
    data["active_user_count"] = None
    assert _format_subreddit({"kind": "t5", "data": data})["active_users"] is None


# ── Null numeric fields (R1) ──────────────────────────────


def test_formatters_coerce_explicit_nulls():
    """Reddit returns explicit nulls (e.g. r/StableLM subscribers) — must not
    crash ':,'-formatting downstream."""
    sub = {"kind": "t5", "data": {**SAMPLE_SUBREDDIT["data"], "subscribers": None,
           "created_utc": None}}
    assert _format_subreddit(sub)["subscribers"] == 0

    post = {"kind": "t3", "data": {**SAMPLE_POST["data"], "score": None,
            "num_comments": None, "upvote_ratio": None, "created_utc": None}}
    fp = _format_post(post)
    assert (fp["score"], fp["num_comments"], fp["upvote_ratio"], fp["created_utc"]) == (0, 0, 0, 0)

    comment = {"kind": "t1", "data": {**SAMPLE_COMMENT["data"], "score": None,
               "created_utc": None}}
    fc = _format_comment(comment)
    assert (fc["score"], fc["created_utc"]) == (0, 0)


# ── Stickied comments (R2) ────────────────────────────────


def test_format_comment_captures_stickied():
    comment = {"kind": "t1", "data": {**SAMPLE_COMMENT["data"], "stickied": True,
               "distinguished": "moderator"}}
    fc = _format_comment(comment)
    assert fc["stickied"] is True
    assert fc["distinguished"] == "moderator"


def test_sort_comment_tree_demotes_stickied_top_levels():
    pinned = {"name": "t1_bot", "parent_id": "t3_link", "stickied": True}
    normal = {"name": "t1_real", "parent_id": "t3_link", "stickied": False}
    ordered = _sort_comment_tree([pinned, normal], "t3_link")
    assert [c["name"] for c in ordered] == ["t1_real", "t1_bot"]


def test_search_comments_skips_stickied():
    client = _make_client()
    bot = {"kind": "t1", "data": {**SAMPLE_COMMENT["data"], "id": "bot1",
           "name": "t1_bot1", "body": "Join our Discord!", "stickied": True}}
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [bot, SAMPLE_COMMENT]}},
    ]
    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [
            _mock_response(SAMPLE_LISTING),
            _mock_response(thread_response),
        ]
        result = client.search_comments("great project")
        assert all(not c.get("stickied") for c in result["items"])
        assert all("Discord" not in c["body"] for c in result["items"])


# ── Depth param + relevance ranking (R3, R4) ──────────────


def test_post_comments_passes_depth_param():
    client = _make_client()
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [SAMPLE_COMMENT]}},
    ]
    with patch.object(client.session, "get", return_value=_mock_response(thread_response)) as mock_get:
        client.post_comments("programming", "abc123", max_depth=0)
        assert mock_get.call_args[1]["params"]["depth"] == 1
        client.post_comments("programming", "abc123")
        assert "depth" not in mock_get.call_args[1]["params"]


def test_search_comments_ranking_ignores_stopwords_and_substrings():
    """'you' must not match 'your'; 'what/do/you' must not neutralize ranking."""
    client = _make_client()
    generic = {"kind": "t1", "data": {**SAMPLE_COMMENT["data"], "id": "g1",
               "name": "t1_g1", "body": "Thanks for sharing your work!", "score": 900}}
    on_topic = {"kind": "t1", "data": {**SAMPLE_COMMENT["data"], "id": "t2x",
                "name": "t1_t2x", "body": "I run Mistral locally on a 3090.", "score": 5}}
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [generic, on_topic]}},
    ]
    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [
            _mock_response(SAMPLE_LISTING),
            _mock_response(thread_response),
        ]
        result = client.search_comments("what do you run locally")
        assert result["items"][0]["body"].startswith("I run Mistral")


def test_more_count_floors_at_truncated():
    """num_comments stale-low must not hide provably-remaining comments."""
    client = _make_client()
    extra = [{"kind": "t1", "data": {**SAMPLE_COMMENT["data"], "id": f"c{i}",
              "name": f"t1_c{i}"}} for i in range(5)]
    post = {"kind": "t3", "data": {**SAMPLE_POST["data"], "num_comments": 3}}
    thread_response = [
        {"kind": "Listing", "data": {"children": [post]}},
        {"kind": "Listing", "data": {"children": extra}},
    ]
    with patch.object(client.session, "get", return_value=_mock_response(thread_response)):
        result = client.post_comments("programming", "abc123", limit=3, expand_more=False)
        # 5 fetched, 2 cut by limit -> at least 2 remain even though 3-3=0
        assert result["more_count"] == 2


def test_search_comments_ranks_matching_bodies_first():
    client = _make_client()
    generic = {"kind": "t1", "data": {**SAMPLE_COMMENT["data"], "id": "g1",
               "name": "t1_g1", "body": "Nice post, thanks for sharing!", "score": 900}}
    on_topic = {"kind": "t1", "data": {**SAMPLE_COMMENT["data"], "id": "t1x",
                "name": "t1_t1x", "body": "For ollama setups I run quantized models.", "score": 5}}
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [generic, on_topic]}},
    ]
    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [
            _mock_response(SAMPLE_LISTING),
            _mock_response(thread_response),
        ]
        result = client.search_comments("ollama setups")
        bodies = [c["body"] for c in result["items"]]
        assert bodies[0].startswith("For ollama")


# ── more_count clamp (R7) ─────────────────────────────────


def test_more_count_clamped_to_num_comments():
    client = _make_client()
    # Stub claims 500 remain but the post only has 10 comments total
    more_stub = {"kind": "more", "data": {"children": [], "count": 500}}
    post = {"kind": "t3", "data": {**SAMPLE_POST["data"], "num_comments": 10}}
    thread_response = [
        {"kind": "Listing", "data": {"children": [post]}},
        {"kind": "Listing", "data": {"children": [SAMPLE_COMMENT, more_stub]}},
    ]
    with patch.object(client.session, "get", return_value=_mock_response(thread_response)):
        result = client.post_comments("programming", "abc123", expand_more=False)
        assert result["more_count"] == 9  # 10 total - 1 shown


# ── NSFW filtering (rough edge #4) ────────────────────────


NSFW_POST = {"kind": "t3", "data": {**SAMPLE_POST["data"], "id": "nsfw1",
             "name": "t3_nsfw1", "over_18": True}}


def test_nsfw_filtered_by_default():
    parsed = {"items": [_format_post(SAMPLE_POST), _format_post(NSFW_POST)],
              "after": None, "count": 2}
    result = _filter_nsfw(parsed, include_nsfw=False)
    assert result["count"] == 1
    assert result["nsfw_hidden"] == 1
    assert all(not i["over_18"] for i in result["items"])


def test_nsfw_kept_when_requested():
    parsed = {"items": [_format_post(NSFW_POST)], "after": None, "count": 1}
    result = _filter_nsfw(parsed, include_nsfw=True)
    assert result["count"] == 1
    assert "nsfw_hidden" not in result


def test_search_passes_include_over_18():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_LISTING)) as mock_get:
        client.search("test")
        assert mock_get.call_args[1]["params"]["include_over_18"] == "off"
        client.search("test", include_nsfw=True)
        assert mock_get.call_args[1]["params"]["include_over_18"] == "on"


def test_subreddit_posts_filters_nsfw():
    client = _make_client()
    listing = {"kind": "Listing", "data": {"after": None, "children": [NSFW_POST]}}
    with patch.object(client.session, "get", return_value=_mock_response(listing)):
        result = client.subreddit_posts("somesub")
        assert result["count"] == 0
        assert result["nsfw_hidden"] == 1
        result = client.subreddit_posts("somesub", include_nsfw=True)
        assert result["count"] == 1


def test_search_comments_propagates_nsfw_hidden_and_cursor():
    """An all-NSFW post page must not silently vanish from the comments command."""
    client = _make_client()
    nsfw_listing = {"kind": "Listing", "data": {"after": "t3_cursor", "children": [NSFW_POST]}}
    with patch.object(client.session, "get", return_value=_mock_response(nsfw_listing)):
        result = client.search_comments("query")
        assert result["count"] == 0
        assert result["nsfw_hidden"] == 1
        assert result["after"] == "t3_cursor"


# ── Post reference parsing (rough edge #5) ────────────────


def test_parse_post_reference_two_args():
    assert parse_post_reference("programming", "abc123") == ("programming", "abc123")


def test_parse_post_reference_fullname():
    assert parse_post_reference("t3_abc123") == (None, "abc123")
    assert parse_post_reference("programming", "t3_abc123") == ("programming", "abc123")


def test_parse_post_reference_url():
    sub, pid = parse_post_reference(
        "https://www.reddit.com/r/programming/comments/abc123/some_title/")
    assert (sub, pid) == ("programming", "abc123")


def test_parse_post_reference_url_as_second_arg():
    sub, pid = parse_post_reference(
        "ignored", "https://reddit.com/r/ClaudeCode/comments/1v0d7iv/foo/")
    assert (sub, pid) == ("ClaudeCode", "1v0d7iv")


def test_parse_post_reference_shortlink():
    assert parse_post_reference("https://redd.it/abc123") == (None, "abc123")


def test_parse_post_reference_bare_id():
    assert parse_post_reference("abc123") == (None, "abc123")


def test_parse_post_reference_invalid():
    with pytest.raises(ValueError):
        parse_post_reference("not a post!! ref")


def test_parse_post_reference_slugless_and_gallery_urls():
    assert parse_post_reference("https://www.reddit.com/comments/1abc2d") == (None, "1abc2d")
    assert parse_post_reference("https://www.reddit.com/gallery/1abc2d") == (None, "1abc2d")


def test_parse_post_reference_share_link_gets_specific_error():
    with pytest.raises(ValueError, match="share link"):
        parse_post_reference("https://www.reddit.com/r/interestingasfuck/s/AbCdEfGh12")


def test_parse_post_reference_rejects_media_hosts():
    # v.redd.it / i.redd.it ids are media ids, not post ids
    with pytest.raises(ValueError):
        parse_post_reference("https://v.redd.it/k3xyz9abc")
    with pytest.raises(ValueError):
        parse_post_reference("https://i.redd.it/k3xyz9abc.jpg")


# ── Comment tree (rough edge #6) ──────────────────────────


def _nested_comment(cid, parent_id, body, replies=None):
    data = {**SAMPLE_COMMENT["data"], "id": cid, "name": f"t1_{cid}",
            "parent_id": parent_id, "body": body}
    data["replies"] = replies if replies is not None else ""
    return {"kind": "t1", "data": data}


def test_extract_comment_tree_walks_replies():
    reply = _nested_comment("child1", "t1_top1", "a reply")
    reply_listing = {"kind": "Listing", "data": {"children": [reply]}}
    top = _nested_comment("top1", "t3_abc123", "top comment", replies=reply_listing)
    more = {"kind": "more", "data": {"children": ["m1", "m2"], "count": 9}}

    comments, more_ids, more_count = _extract_comment_tree([top, more], max_depth=None)
    assert [c["id"] for c in comments] == ["top1", "child1"]
    assert [c["depth"] for c in comments] == [0, 1]
    assert more_ids == ["m1", "m2"]
    assert more_count == 9


def test_extract_comment_tree_respects_max_depth():
    reply = _nested_comment("child1", "t1_top1", "a reply")
    reply_listing = {"kind": "Listing", "data": {"children": [reply]}}
    top = _nested_comment("top1", "t3_abc123", "top comment", replies=reply_listing)

    comments, _, _ = _extract_comment_tree([top], max_depth=0)
    assert [c["id"] for c in comments] == ["top1"]


def test_sort_comment_tree_orders_depth_first():
    a = {"name": "t1_a", "parent_id": "t3_link", "depth": 0}
    b = {"name": "t1_b", "parent_id": "t3_link", "depth": 0}
    a_child = {"name": "t1_ac", "parent_id": "t1_a", "depth": 0}
    ordered = _sort_comment_tree([a, b, a_child], "t3_link")
    assert [c["name"] for c in ordered] == ["t1_a", "t1_ac", "t1_b"]
    assert [c["depth"] for c in ordered] == [0, 1, 0]


def test_post_comments_returns_tree():
    client = _make_client()
    reply = _nested_comment("child1", "t1_xyz789", "a nested reply")
    reply_listing = {"kind": "Listing", "data": {"children": [reply]}}
    top_data = {**SAMPLE_COMMENT["data"], "replies": reply_listing}
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [{"kind": "t1", "data": top_data}]}},
    ]
    with patch.object(client.session, "get", return_value=_mock_response(thread_response)):
        result = client.post_comments("programming", "abc123")
        assert result["total"] == 2
        assert [c["depth"] for c in result["comments"]] == [0, 1]
        assert result["comments"][1]["body"] == "a nested reply"
        assert result["more_count"] == 0


def test_post_comments_expands_more_stubs():
    client = _make_client()
    more_stub = {"kind": "more", "data": {"children": ["m1"], "count": 1}}
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [SAMPLE_COMMENT, more_stub]}},
    ]
    more_comment = _nested_comment("m1", "t1_xyz789", "a late-loaded reply")
    more_response = {"json": {"data": {"things": [more_comment]}}}
    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [
            _mock_response(thread_response),
            _mock_response(more_response),
        ]
        result = client.post_comments("programming", "abc123", limit=50)
        assert mock_get.call_count == 2
        assert "/api/morechildren" in mock_get.call_args[0][0]
        bodies = [c["body"] for c in result["comments"]]
        assert "a late-loaded reply" in bodies
        # Stitched under its parent with the right depth
        by_id = {c["id"]: c for c in result["comments"]}
        assert by_id["m1"]["depth"] == by_id["xyz789"]["depth"] + 1
        assert result["more_count"] == 0


def test_morechildren_respects_max_depth_across_chain():
    """Descendants of depth-filtered comments must not reappear as top-levels."""
    client = _make_client()
    more_stub = {"kind": "more", "data": {"children": ["a1", "a2", "a3", "a4"], "count": 4}}
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [more_stub]}},
    ]
    # Chain a1(d0) -> a2(d1) -> a3(d2) -> a4(d3), delivered flat
    chain = [
        _nested_comment("a1", "t3_abc123", "depth 0"),
        _nested_comment("a2", "t1_a1", "depth 1"),
        _nested_comment("a3", "t1_a2", "depth 2"),
        _nested_comment("a4", "t1_a3", "depth 3"),
    ]
    more_response = {"json": {"data": {"things": chain}}}
    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [
            _mock_response(thread_response),
            _mock_response(more_response),
        ]
        result = client.post_comments("programming", "abc123", max_depth=1)
        ids = [c["id"] for c in result["comments"]]
        assert ids == ["a1", "a2"]
        assert [c["depth"] for c in result["comments"]] == [0, 1]
        # a3/a4 were filtered, not delivered — still counted as not fetched
        assert result["more_count"] == 2


def test_morechildren_resolves_child_before_parent_in_batch():
    """Fixpoint stitching: a child listed before its parent still resolves."""
    client = _make_client()
    more_stub = {"kind": "more", "data": {"children": ["x2", "x1"], "count": 2}}
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [more_stub]}},
    ]
    things = [
        _nested_comment("x2", "t1_x1", "the reply"),   # child first
        _nested_comment("x1", "t3_abc123", "the parent"),
    ]
    more_response = {"json": {"data": {"things": things}}}
    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [
            _mock_response(thread_response),
            _mock_response(more_response),
        ]
        result = client.post_comments("programming", "abc123")
        assert [c["id"] for c in result["comments"]] == ["x1", "x2"]
        assert [c["depth"] for c in result["comments"]] == [0, 1]
        assert result["more_count"] == 0


def test_morechildren_drops_orphans_instead_of_faking_top_levels():
    """A comment whose parent never arrives must not render as depth 0."""
    client = _make_client()
    more_stub = {"kind": "more", "data": {"children": ["y2"], "count": 2}}
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [SAMPLE_COMMENT, more_stub]}},
    ]
    orphan = _nested_comment("y2", "t1_never_fetched", "orphan reply")
    more_response = {"json": {"data": {"things": [orphan]}}}
    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [
            _mock_response(thread_response),
            _mock_response(more_response),
        ]
        result = client.post_comments("programming", "abc123")
        ids = [c["id"] for c in result["comments"]]
        assert "y2" not in ids
        # Orphan stays in the not-fetched count
        assert result["more_count"] == 2


def test_morechildren_no_double_count_of_nested_stubs():
    """A nested stub's count is a subset of the original stub's count."""
    client = _make_client()
    more_stub = {"kind": "more", "data": {"children": ["b1"], "count": 5}}
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [more_stub]}},
    ]
    b1 = _nested_comment("b1", "t3_abc123", "delivered")
    nested_stub = {"kind": "more", "data": {"children": ["b2"], "count": 4}}
    more_response = {"json": {"data": {"things": [b1, nested_stub]}}}
    with patch.object(client.session, "get") as mock_get:
        # limit reached after first expansion; second call must not happen
        mock_get.side_effect = [
            _mock_response(thread_response),
            _mock_response(more_response),
        ]
        result = client.post_comments("programming", "abc123", limit=1)
        # 5 in the subtree, 1 delivered -> 4 remain (not 5 + 4 - 1 = 8)
        assert result["more_count"] == 4


# ── paginate() (feature: --pages) ─────────────────────────


def _listing_page(ids, after):
    children = [{"kind": "t3", "data": {**SAMPLE_POST["data"], "id": i, "name": f"t3_{i}"}}
                for i in ids]
    return {"kind": "Listing", "data": {"after": after, "children": children}}


def test_paginate_merges_and_dedups():
    client = _make_client()
    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [
            _mock_response(_listing_page(["a", "b"], "t3_b")),
            _mock_response(_listing_page(["b", "c"], None)),  # 'b' repeats across pages
        ]
        result = client.paginate(client.search, pages=3, query="x")
        assert [i["id"] for i in result["items"]] == ["a", "b", "c"]
        assert result["after"] is None
        assert result["count"] == 3
        assert mock_get.call_count == 2  # stopped at cursor exhaustion, not pages=3


def test_paginate_single_page_matches_direct_call_shape():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_LISTING)):
        result = client.paginate(client.search, pages=1, query="x")
        assert result["count"] == 1
        assert result["after"] == "t3_next123"


def test_paginate_partial_error_keeps_gathered_items():
    client = _make_client()
    err = MagicMock()
    err.status_code = 404
    err.headers = {}
    with patch.object(client.session, "get") as mock_get:
        mock_get.side_effect = [
            _mock_response(_listing_page(["a"], "t3_a")),
            err,
        ]
        result = client.paginate(client.search, pages=3, query="x")
        assert [i["id"] for i in result["items"]] == ["a"]
        assert "partial_error" in result
        assert "error" not in result


def test_paginate_error_on_first_page_propagates():
    client = _make_client()
    err = MagicMock()
    err.status_code = 404
    err.headers = {}
    with patch.object(client.session, "get", return_value=err):
        result = client.paginate(client.search, pages=2, query="x")
        assert "error" in result


# ── Multireddit fan-in (feature: -r a,b,c) ────────────────


def test_search_normalizes_multireddit():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_LISTING)) as mock_get:
        client.search("x", subreddit="python, rust golang")
        assert "/r/python+rust+golang/search" in mock_get.call_args[0][0]


def test_subreddit_posts_normalizes_multireddit():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_LISTING)) as mock_get:
        client.subreddit_posts("a,b")
        assert "/r/a+b/hot" in mock_get.call_args[0][0]


def test_popular_subreddits_accepts_after():
    client = _make_client()
    sub_listing = {"kind": "Listing", "data": {"after": None, "children": [SAMPLE_SUBREDDIT]}}
    with patch.object(client.session, "get", return_value=_mock_response(sub_listing)) as mock_get:
        client.popular_subreddits(after="t5_abc")
        assert mock_get.call_args[1]["params"]["after"] == "t5_abc"


def test_post_comments_without_subreddit():
    client = _make_client()
    thread_response = [
        {"kind": "Listing", "data": {"children": [SAMPLE_POST]}},
        {"kind": "Listing", "data": {"children": [SAMPLE_COMMENT]}},
    ]
    with patch.object(client.session, "get", return_value=_mock_response(thread_response)) as mock_get:
        client.post_comments(None, "abc123")
        url = mock_get.call_args[0][0]
        assert "/comments/abc123" in url
        assert "/r/" not in url
