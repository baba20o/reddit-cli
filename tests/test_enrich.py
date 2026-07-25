"""Tests for the API-enrichment features: rules, mods, related, crossposts,
bulk get, share-link resolution, surfaced fields, flair/oc filters."""

import json
from unittest.mock import MagicMock, patch

import pytest
import requests
from click.testing import CliRunner

from reddit.api import (
    RedditClient, _format_post, is_share_link, parse_post_reference,
)
from reddit.cli import main


# ── Formatter: surfaced fields ────────────────────────────


def test_format_post_surfaces_signal_fields():
    data = {"kind": "t3", "data": {
        "id": "x", "title": "t", "author": "a", "total_awards_received": 5,
        "edited": 1700000000, "locked": True, "spoiler": True,
        "distinguished": "moderator", "is_original_content": True,
        "num_crossposts": 3}}
    p = _format_post(data)
    assert p["awards"] == 5
    assert p["edited"] is True
    assert p["locked"] is True
    assert p["spoiler"] is True
    assert p["distinguished"] == "moderator"
    assert p["is_oc"] is True
    assert p["num_crossposts"] == 3


def test_format_post_edited_false_stays_false():
    p = _format_post({"kind": "t3", "data": {"id": "x", "title": "t", "edited": False}})
    assert p["edited"] is False


# ── Client methods (mocked HTTP) ──────────────────────────


def _client():
    with patch.dict("os.environ", {"REDDIT_CLIENT_ID": "i", "REDDIT_CLIENT_SECRET": "s"}):
        with patch("reddit.api.load_dotenv"), patch("reddit.api.get_rate_limiter") as rl:
            rl.return_value = MagicMock()
            c = RedditClient(use_cache=False)
            c._token = "t"
            c._token_expires = 9e18
            return c


def test_subreddit_rules():
    c = _client()
    resp = {"rules": [
        {"short_name": "Be nice", "description": "no jerks", "kind": "comment",
         "violation_reason": "rude"},
        {"short_name": "On topic", "description": "", "kind": "link"}]}
    with patch.object(c, "_get", return_value=resp):
        r = c.subreddit_rules("x")
    assert r["count"] == 2
    assert r["rules"][0]["name"] == "Be nice"
    assert r["rules"][0]["kind"] == "comment"


def test_subreddit_moderators():
    c = _client()
    resp = {"data": {"children": [
        {"name": "modA", "mod_permissions": ["all"]},
        {"name": "modB", "mod_permissions": ["posts"]}]}}
    with patch.object(c, "_get", return_value=resp):
        r = c.subreddit_moderators("x")
    assert r["count"] == 2
    assert [m["name"] for m in r["moderators"]] == ["modA", "modB"]


def test_related_subreddits_resolves_fullname_then_queries():
    c = _client()
    about = {"data": {"name": "t5_abc"}}
    similar = {"kind": "Listing", "data": {"after": None, "children": [
        {"kind": "t5", "data": {"display_name": "relatedA", "subscribers": 1}},
        {"kind": "t5", "data": {"display_name": "relatedB", "subscribers": 2}}]}}
    with patch.object(c, "_get", side_effect=[about, similar]) as g:
        r = c.related_subreddits("x", limit=5)
    # second call uses the fullname from about
    assert g.call_args_list[1][0][0] == "/api/similar_subreddits"
    assert g.call_args_list[1][0][1]["sr_fullnames"] == "t5_abc"
    assert [i["name"] for i in r["items"]] == ["relatedA", "relatedB"]


def test_duplicates_splits_original_and_reposts():
    c = _client()
    resp = [
        {"data": {"children": [{"kind": "t3", "data": {"id": "orig", "title": "O", "subreddit": "a"}}]}},
        {"data": {"children": [
            {"kind": "t3", "data": {"id": "d1", "title": "D1", "subreddit": "b"}},
            {"kind": "t3", "data": {"id": "d2", "title": "D2", "subreddit": "c"}}]}},
    ]
    with patch.object(c, "_get", return_value=resp):
        r = c.duplicates("orig")
    assert r["original"]["id"] == "orig"
    assert r["count"] == 2
    assert {i["subreddit"] for i in r["items"]} == {"b", "c"}


def test_info_by_fullnames_joins_ids():
    c = _client()
    listing = {"kind": "Listing", "data": {"after": None, "children": [
        {"kind": "t3", "data": {"id": "a", "title": "A"}}]}}
    with patch.object(c, "_get", return_value=listing) as g:
        r = c.info_by_fullnames(["t3_a", "t3_b"])
    assert g.call_args[0][1]["id"] == "t3_a,t3_b"
    assert r["count"] == 1


# ── Share-link resolution ─────────────────────────────────


def test_is_share_link():
    assert is_share_link("https://www.reddit.com/r/pics/s/AbCd123")
    assert not is_share_link("https://reddit.com/r/pics/comments/abc/x/")


def test_resolve_post_reference_follows_share_link():
    c = _client()
    resolved = MagicMock()
    resolved.url = "https://www.reddit.com/r/pics/comments/abc123/a_title/"
    resolved.close = MagicMock()
    with patch("reddit.api.requests.get", return_value=resolved):
        sub, pid = c.resolve_post_reference("https://www.reddit.com/r/pics/s/XyZ")
    assert (sub, pid) == ("pics", "abc123")


def test_resolve_post_reference_share_link_unresolved_errors():
    c = _client()
    resolved = MagicMock()
    resolved.url = "https://www.reddit.com/r/pics/s/XyZ"  # still a share link
    resolved.close = MagicMock()
    with patch("reddit.api.requests.get", return_value=resolved):
        with pytest.raises(ValueError, match="did not resolve"):
            c.resolve_post_reference("https://www.reddit.com/r/pics/s/XyZ")


def test_resolve_post_reference_passthrough_for_normal_ref():
    c = _client()
    # No network call for a normal permalink
    with patch("reddit.api.requests.get") as g:
        sub, pid = c.resolve_post_reference("https://reddit.com/r/pics/comments/abc/x/")
    assert (sub, pid) == ("pics", "abc")
    assert g.call_count == 0


def test_follow_share_link_rejects_ssrf_before_request():
    """A hostile URL merely CONTAINING a share-link substring must not be fetched."""
    c = _client()
    with patch("reddit.api.requests.get") as g:
        for hostile in [
            "http://169.254.169.254/latest/meta-data/?x=reddit.com/r/a/s/b",
            "http://localhost:6379/reddit.com/r/x/s/abcd",
            "https://evil.example/reddit.com/r/x/s/abcd",
        ]:
            with pytest.raises(ValueError, match="non-Reddit"):
                c._follow_share_link(hostile)
    assert g.call_count == 0  # no request ever issued


def test_follow_share_link_rejects_offhost_redirect():
    c = _client()
    resolved = MagicMock()
    resolved.url = "https://evil.example/comments/abc/"  # redirected off reddit
    resolved.close = MagicMock()
    with patch("reddit.api.requests.get", return_value=resolved):
        with pytest.raises(ValueError, match="did not resolve"):
            c._follow_share_link("https://www.reddit.com/r/x/s/tok")


def test_info_by_fullnames_dispatches_by_kind():
    c = _client()
    listing = {"kind": "Listing", "data": {"after": None, "children": [
        {"kind": "t3", "data": {"id": "p", "title": "P"}},
        {"kind": "t1", "data": {"id": "c", "body": "a comment"}},
        {"kind": "t5", "data": {"display_name": "sub", "subscribers": 9}}]}}
    with patch.object(c, "_get", return_value=listing):
        items = c.info_by_fullnames(["t3_p", "t1_c", "t5_sub"])["items"]
    assert items[0]["title"] == "P"
    assert items[1]["body"] == "a comment"          # comment kept its body
    assert items[2]["subscribers"] == 9             # subreddit parsed as sr


def test_follow_share_link_uses_no_auth_header():
    c = _client()
    c.session.headers["Authorization"] = "Bearer SECRET"
    resolved = MagicMock()
    resolved.url = "https://reddit.com/r/x/comments/abc/y/"
    resolved.close = MagicMock()
    with patch("reddit.api.requests.get", return_value=resolved) as g:
        c._follow_share_link("https://reddit.com/r/x/s/tok")
    headers = g.call_args[1]["headers"]
    assert "Authorization" not in headers


# ── CLI commands ──────────────────────────────────────────


def _invoke(args, client):
    with patch("reddit.cli.RedditClient", return_value=client):
        return CliRunner(mix_stderr=False).invoke(main, args), client


def test_related_command():
    client = MagicMock()
    client.related_subreddits.return_value = {
        "items": [{"name": "learnpython", "subscribers": 100, "title": "t",
                   "description": "d", "over_18": False}],
        "count": 1}
    result, _ = _invoke(["related", "python", "--jsonl"], client)
    assert result.exit_code == 0
    assert json.loads(result.output.strip().splitlines()[0])["name"] == "learnpython"


def test_crossposts_command():
    client = MagicMock()
    client.resolve_post_reference.return_value = (None, "abc123")
    client.duplicates.return_value = {
        "original": {"id": "abc123", "title": "Orig"},
        "items": [{"subreddit": "b", "title": "repost", "author": "u", "score": 3,
                   "num_comments": 1, "created_utc": 0, "over_18": False}],
        "count": 1}
    result, _ = _invoke(["crossposts", "t3_abc123", "--jsonl"], client)
    assert result.exit_code == 0
    lines = [json.loads(l) for l in result.output.strip().splitlines()]
    assert lines[0]["original"]["id"] == "abc123"
    assert lines[-1]["_meta"]["count"] == 1


def test_get_command_bulk():
    client = MagicMock()
    client.info_by_fullnames.return_value = {
        "items": [{"id": "a", "title": "A", "subreddit": "s", "author": "u", "score": 1,
                   "num_comments": 0, "created_utc": 0, "over_18": False}],
        "after": None, "count": 1}
    result, client = _invoke(["get", "t3_a", "b", "--jsonl"], client)
    assert result.exit_code == 0
    # bare id 'b' gets t3_ prefix
    assert client.info_by_fullnames.call_args[0][0] == ["t3_a", "t3_b"]


def test_info_with_rules_and_mods():
    client = MagicMock()
    client.subreddit_info.return_value = {"name": "x", "title": "X", "subscribers": 1,
                                          "active_users": None, "over_18": False, "url": "u",
                                          "description": "d", "created_utc": 0}
    client.subreddit_rules.return_value = {"rules": [{"name": "R1", "description": "d",
                                                      "kind": "all"}], "count": 1}
    client.subreddit_moderators.return_value = {"moderators": [{"name": "m1"}], "count": 1}
    result, _ = _invoke(["info", "x", "--rules", "--mods"], client)
    assert "Rules (1)" in result.output
    assert "R1" in result.output
    assert "Moderators (1)" in result.output
    assert "u/m1" in result.output


def test_info_rules_mods_in_json():
    client = MagicMock()
    client.subreddit_info.return_value = {"name": "x", "subscribers": 1}
    client.subreddit_rules.return_value = {"rules": [{"name": "R1"}], "count": 1}
    client.subreddit_moderators.return_value = {"moderators": [{"name": "m1"}], "count": 1}
    result, _ = _invoke(["info", "x", "--rules", "--mods", "-j"], client)
    data = json.loads(result.output)
    assert data["rules"][0]["name"] == "R1"
    assert data["moderators"][0]["name"] == "m1"


# ── flair / oc filters ────────────────────────────────────


def _posts_client(items):
    client = MagicMock()
    client.paginate.side_effect = lambda method, pages=1, **kw: method(**kw)
    client.subreddit_posts.return_value = {"items": items, "after": None, "count": len(items)}
    return client


BASE = {"subreddit": "s", "author": "u", "score": 1, "num_comments": 0, "created_utc": 0,
        "over_18": False}


def test_flair_filter():
    items = [
        {**BASE, "id": "1", "name": "t3_1", "title": "A", "link_flair_text": "Discussion"},
        {**BASE, "id": "2", "name": "t3_2", "title": "B", "link_flair_text": "News"},
    ]
    result, _ = _invoke(["posts", "s", "--flair", "discuss", "--jsonl"], _posts_client(items))
    lines = [json.loads(l) for l in result.output.strip().splitlines()]
    titles = [l.get("title") for l in lines if "title" in l]
    assert titles == ["A"]
    assert lines[-1]["_meta"].get("post_filtered") == 1


def test_oc_filter():
    items = [
        {**BASE, "id": "1", "name": "t3_1", "title": "A", "is_oc": True},
        {**BASE, "id": "2", "name": "t3_2", "title": "B", "is_oc": False},
    ]
    result, _ = _invoke(["posts", "s", "--oc", "--jsonl"], _posts_client(items))
    lines = [json.loads(l) for l in result.output.strip().splitlines()]
    titles = [l.get("title") for l in lines if "title" in l]
    assert titles == ["A"]
