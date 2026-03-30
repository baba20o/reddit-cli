"""Tests for reddit.api — Reddit OAuth2 client."""

import json
import time
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from reddit.api import (
    RedditClient, _format_post, _format_comment, _format_subreddit,
    _retry_wait_seconds,
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
    """Create a client with mocked auth."""
    with patch.dict("os.environ", {
        "REDDIT_CLIENT_ID": "test_id",
        "REDDIT_CLIENT_SECRET": "test_secret",
        "REDDIT_USERNAME": "testuser",
        "REDDIT_PASSWORD": "testpass",
    }):
        with patch("reddit.api.get_rate_limiter") as mock_rl:
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
    with patch.dict("os.environ", {}, clear=True):
        with patch("reddit.api.get_rate_limiter") as mock_rl:
            mock_rl.return_value = MagicMock()
            client = RedditClient(use_cache=False)
            with pytest.raises(RuntimeError, match="credentials not set"):
                client._ensure_token()


def test_pagination_after():
    client = _make_client()
    with patch.object(client.session, "get", return_value=_mock_response(SAMPLE_LISTING)):
        result = client.search("test")
        assert result["after"] == "t3_next123"
