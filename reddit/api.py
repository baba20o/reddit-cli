"""Reddit OAuth2 API client.

Base URL: https://oauth.reddit.com
Auth: OAuth2 script-type (client_id + client_secret + username + password)
Rate limit: 100 requests/minute (self-limited to 1 req/sec)
Response: JSON
Docs: https://www.reddit.com/dev/api/
"""

import logging
import os
import random
import re
import time
from typing import Optional, Tuple

import requests
from dotenv import load_dotenv

from reddit.cache import RedditCache
from reddit.rate_limiter import get_rate_limiter

logger = logging.getLogger(__name__)

OAUTH_URL = "https://oauth.reddit.com"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"

MAX_RETRIES = 3
DEFAULT_RETRY_WAIT = 2
MAX_RETRY_WAIT = 60
REQUEST_TIMEOUT = 30

SORT_CHOICES = ("hot", "new", "top", "rising", "controversial", "best")
TIME_CHOICES = ("hour", "day", "week", "month", "year", "all")


def _retry_wait_seconds(attempt: int, response: requests.Response = None) -> float:
    """Calculate retry wait with exponential backoff + jitter."""
    if response is not None:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return max(1.0, float(retry_after))
            except ValueError:
                pass

    base = 10 if response is not None and response.status_code == 429 else DEFAULT_RETRY_WAIT
    wait = base * (2 ** attempt) + random.uniform(0, 1.0)
    return min(wait, MAX_RETRY_WAIT)


def _format_post(post: dict) -> dict:
    """Normalize a Reddit post/link into a clean dict.

    Text fields arrive unescaped because every request sends raw_json=1;
    without it Reddit HTML-escapes &, <, > in ALL string fields (even url).
    """
    data = post.get("data", post)
    return {
        "id": data.get("id", ""),
        "name": data.get("name", ""),  # fullname e.g. t3_abc123
        "title": data.get("title", ""),
        "author": data.get("author") or "[deleted]",
        "subreddit": data.get("subreddit", ""),
        "score": data.get("score", 0),
        "upvote_ratio": data.get("upvote_ratio", 0),
        "num_comments": data.get("num_comments", 0),
        "url": data.get("url", ""),
        "selftext": data.get("selftext", ""),
        "created_utc": data.get("created_utc", 0),
        "permalink": f"https://reddit.com{data.get('permalink', '')}",
        "is_self": data.get("is_self", False),
        "link_flair_text": data.get("link_flair_text") or "",
        "over_18": data.get("over_18", False),
        "stickied": data.get("stickied", False),
    }


def _format_comment(comment: dict) -> dict:
    """Normalize a Reddit comment into a clean dict."""
    data = comment.get("data", comment)
    return {
        "id": data.get("id", ""),
        "name": data.get("name", ""),
        "author": data.get("author") or "[deleted]",
        "body": data.get("body", ""),
        "score": data.get("score", 0),
        "subreddit": data.get("subreddit", ""),
        "created_utc": data.get("created_utc", 0),
        "permalink": f"https://reddit.com{data.get('permalink', '')}",
        "parent_id": data.get("parent_id", ""),
        "link_id": data.get("link_id", ""),
        "link_title": data.get("link_title", ""),
        "depth": 0,
    }


def _format_subreddit(sub: dict) -> dict:
    """Normalize a subreddit info dict."""
    data = sub.get("data", sub)
    active = data.get("active_user_count")
    if active is None:
        active = data.get("accounts_active")  # legacy field; Reddit now returns null for both
    return {
        "name": data.get("display_name", ""),
        "title": data.get("title", ""),
        "description": data.get("public_description", ""),
        "subscribers": data.get("subscribers", 0),
        "active_users": active,
        "created_utc": data.get("created_utc", 0),
        "over_18": data.get("over18", False),
        "url": f"https://reddit.com{data.get('url', '')}",
    }


def _filter_nsfw(parsed: dict, include_nsfw: bool) -> dict:
    """Drop over_18 items unless requested; record how many were hidden."""
    if include_nsfw or "error" in parsed:
        return parsed
    items = parsed.get("items", [])
    kept = [i for i in items if not i.get("over_18")]
    hidden = len(items) - len(kept)
    if hidden:
        parsed = {**parsed, "items": kept, "count": len(kept), "nsfw_hidden": hidden}
    return parsed


_POST_URL_RE = re.compile(r"(?:reddit\.com)?/r/(?P<sub>[^/\s]+)/comments/(?P<id>[a-zA-Z0-9]+)")
# Slugless permalinks and gallery share links: reddit.com/comments/<id>, reddit.com/gallery/<id>
_BARE_URL_RE = re.compile(r"reddit\.com/(?:comments|gallery)/(?P<id>[a-zA-Z0-9]+)")
# Anchored so media hosts (v.redd.it, i.redd.it) don't false-match as shortlinks
_SHORTLINK_RE = re.compile(r"(?<![.\w])redd\.it/(?P<id>[a-zA-Z0-9]+)")
# Mobile-app share links; the token is opaque and only resolves via HTTP redirect
_SHARE_LINK_RE = re.compile(r"reddit\.com/r/[^/\s]+/s/[A-Za-z0-9]+")


def parse_post_reference(target: str, post_id: Optional[str] = None) -> Tuple[Optional[str], str]:
    """Resolve (subreddit, post_id) from CLI-style arguments.

    Accepts: subreddit + bare id, subreddit + t3_ fullname, a full permalink URL
    (also slugless /comments/<id> and /gallery/<id> forms), a redd.it shortlink,
    a t3_ fullname, or a bare post id. The subreddit may be None (Reddit
    resolves /comments/{id} without one).
    """

    def _extract(ref: str):
        ref = ref.strip()
        if _SHARE_LINK_RE.search(ref):
            raise ValueError(
                f"{ref!r} is a mobile share link whose token only resolves via redirect — "
                "open it in a browser and paste the full permalink instead"
            )
        m = _POST_URL_RE.search(ref)
        if m:
            return m.group("sub"), m.group("id").lower()
        m = _BARE_URL_RE.search(ref) or _SHORTLINK_RE.search(ref)
        if m:
            return None, m.group("id").lower()
        if ref.startswith("t3_"):
            ref = ref[3:]
        if re.fullmatch(r"[a-zA-Z0-9]+", ref):
            return None, ref.lower()
        return None, None

    if post_id is None:
        sub, pid = _extract(target)
        if pid is None:
            raise ValueError(
                f"Cannot parse post reference {target!r} — expected a post URL, "
                "a t3_ fullname, or a post id"
            )
        return sub, pid

    sub, pid = _extract(post_id)
    if pid is None:
        raise ValueError(f"Cannot parse post id {post_id!r}")
    return sub or target, pid


def _extract_comment_tree(children: list, max_depth: Optional[int], depth: int = 0):
    """Walk a nested comment listing. Returns (comments, more_ids, more_count).

    Comments carry a `depth` field; `more_ids` are unexpanded comment ids from
    "load more" stubs and `more_count` the number of comments they represent.
    """
    comments, more_ids, more_count = [], [], 0
    for child in children:
        kind = child.get("kind")
        data = child.get("data", {})
        if kind == "t1":
            c = _format_comment(child)
            c["depth"] = depth
            comments.append(c)
            replies = data.get("replies")
            if isinstance(replies, dict) and (max_depth is None or depth < max_depth):
                sub_children = replies.get("data", {}).get("children", [])
                sc, si, sm = _extract_comment_tree(sub_children, max_depth, depth + 1)
                comments.extend(sc)
                more_ids.extend(si)
                more_count += sm
        elif kind == "more":
            ids = data.get("children") or []
            more_ids.extend(ids)
            more_count += data.get("count") or len(ids)
    return comments, more_ids, more_count


def _sort_comment_tree(comments: list, link_fullname: str) -> list:
    """Re-order a flat comment list into depth-first tree order.

    Needed after /api/morechildren expansion, which returns comments flat and
    appended out of tree order. Recomputes depth from actual parent links.
    """
    by_parent = {}
    for c in comments:
        by_parent.setdefault(c.get("parent_id") or "", []).append(c)

    ordered = []

    def visit(parent_name: str, depth: int):
        for c in by_parent.get(parent_name, []):
            c["depth"] = depth
            ordered.append(c)
            visit(c.get("name", ""), depth + 1)

    visit(link_fullname, 0)

    if len(ordered) < len(comments):
        # Orphans: parents weren't fetched (e.g. remained in unexpanded "more" stubs)
        seen = {c.get("name") for c in ordered}
        ordered.extend(c for c in comments if c.get("name") not in seen)
    return ordered


class RedditClient:
    """Client for the Reddit OAuth2 API."""

    def __init__(self, use_cache: bool = True):
        load_dotenv()
        self.client_id = os.environ.get("REDDIT_CLIENT_ID", "")
        self.client_secret = os.environ.get("REDDIT_CLIENT_SECRET", "")
        self.username = os.environ.get("REDDIT_USERNAME", "")
        self.password = os.environ.get("REDDIT_PASSWORD", "")

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": f"reddit-cli/0.1.0 by /u/{self.username or 'anonymous'}",
        })
        self.rate_limiter = get_rate_limiter()
        self.use_cache = use_cache
        self.cache = RedditCache() if use_cache else None
        self._token = None
        self._token_expires = 0

    # ── Auth ──────────────────────────────────────────────

    def _ensure_token(self):
        """Get or refresh OAuth2 access token."""
        if self._token and time.time() < self._token_expires:
            return

        if not self.client_id or not self.client_secret:
            raise RuntimeError(
                "Reddit API credentials not set. Set REDDIT_CLIENT_ID and "
                "REDDIT_CLIENT_SECRET environment variables. "
                "Create an app at https://www.reddit.com/prefs/apps"
            )

        auth = requests.auth.HTTPBasicAuth(self.client_id, self.client_secret)

        if self.username and self.password:
            # Script-type app: password grant
            data = {
                "grant_type": "password",
                "username": self.username,
                "password": self.password,
            }
        else:
            # Application-only (read-only public data)
            data = {
                "grant_type": "client_credentials",
            }

        response = requests.post(
            TOKEN_URL,
            auth=auth,
            data=data,
            headers={"User-Agent": self.session.headers["User-Agent"]},
            timeout=(10, REQUEST_TIMEOUT),
        )
        response.raise_for_status()
        token_data = response.json()

        if "error" in token_data:
            raise RuntimeError(f"Reddit auth failed: {token_data['error']}")

        self._token = token_data["access_token"]
        # Tokens expire in 3600s, refresh at 3000s to be safe
        self._token_expires = time.time() + token_data.get("expires_in", 3600) - 600
        self.session.headers["Authorization"] = f"Bearer {self._token}"
        logger.debug("Reddit OAuth token acquired (expires in %ds)", token_data.get("expires_in", 3600))

    # ── Search ────────────────────────────────────────────

    def search(
        self,
        query: str,
        subreddit: Optional[str] = None,
        sort: str = "relevance",
        time_filter: str = "all",
        limit: int = 25,
        after: Optional[str] = None,
        search_type: str = "link",
        include_nsfw: bool = False,
    ) -> dict:
        """Search Reddit posts.

        Args:
            query: Search query string.
            subreddit: Restrict to a specific subreddit.
            sort: relevance, hot, top, new, comments.
            time_filter: hour, day, week, month, year, all.
            limit: Max results (1-100).
            after: Pagination cursor (fullname of last item).
            search_type: link (posts), sr (subreddits), user (users).
            include_nsfw: Include over-18 results (default: excluded).
        """
        if subreddit:
            path = f"/r/{subreddit}/search"
        else:
            path = "/search"

        params = {
            "q": query,
            "sort": sort,
            "t": time_filter,
            "limit": min(limit, 100),
            "type": search_type,
            "restrict_sr": "true" if subreddit else "false",
            "include_over_18": "on" if include_nsfw else "off",
        }
        if after:
            params["after"] = after

        result = self._get(path, params)
        # Server-side include_over_18 is advisory; filter client-side too
        return _filter_nsfw(self._parse_listing(result, item_type=search_type), include_nsfw)

    def search_comments(
        self,
        query: str,
        subreddit: Optional[str] = None,
        sort: str = "relevance",
        time_filter: str = "all",
        limit: int = 25,
        after: Optional[str] = None,
        include_nsfw: bool = False,
    ) -> dict:
        """Search Reddit comments by finding relevant posts and fetching their top comments.

        Reddit's /search?type=comment endpoint is unreliable (returns posts instead
        of comments). This method searches for posts matching the query, then fetches
        top comments from each post to return actual comment bodies with text.
        """
        # Step 1: Search for relevant posts
        post_limit = min(max(limit // 3, 3), 10)  # fetch 3-10 posts
        post_result = self.search(
            query, subreddit=subreddit, sort=sort,
            time_filter=time_filter, limit=post_limit, after=after,
            include_nsfw=include_nsfw,
        )
        if "error" in post_result:
            return post_result

        posts = post_result.get("items", [])
        if not posts:
            empty = {"items": [], "after": post_result.get("after"), "count": 0}
            if post_result.get("nsfw_hidden"):
                empty["nsfw_hidden"] = post_result["nsfw_hidden"]
            return empty

        # Step 2: Fetch top comments from each post
        comments_per_post = max(limit // len(posts), 2) if posts else limit
        all_comments = []
        for post in posts:
            sub = post.get("subreddit", "")
            post_id = post.get("id", "")
            if not sub or not post_id:
                continue
            thread = self.post_comments(
                sub, post_id, sort="top", limit=comments_per_post, expand_more=False,
            )
            if "error" in thread:
                continue
            for c in thread.get("comments", []):
                # Skip deleted/removed/empty comments — useless as search results
                if not c.get("body") or c["body"] in ("[deleted]", "[removed]"):
                    continue
                # Attach post title for context
                c["link_title"] = post.get("title", "")
                all_comments.append(c)

        # Sort by score and trim to requested limit
        all_comments.sort(key=lambda c: c.get("score", 0), reverse=True)
        all_comments = all_comments[:limit]

        result = {
            "items": all_comments,
            "after": post_result.get("after"),
            "count": len(all_comments),
        }
        if post_result.get("nsfw_hidden"):
            result["nsfw_hidden"] = post_result["nsfw_hidden"]
        return result

    # ── Subreddit Listings ────────────────────────────────

    def subreddit_posts(
        self,
        subreddit: str,
        sort: str = "hot",
        time_filter: str = "all",
        limit: int = 25,
        after: Optional[str] = None,
        include_nsfw: bool = False,
    ) -> dict:
        """Get posts from a subreddit.

        Args:
            subreddit: Subreddit name (without r/).
            sort: hot, new, top, rising, controversial.
            time_filter: For top/controversial — hour, day, week, month, year, all.
            limit: Max results (1-100).
            include_nsfw: Include over-18 results (default: excluded).
        """
        path = f"/r/{subreddit}/{sort}"
        params = {"limit": min(limit, 100), "t": time_filter}
        if after:
            params["after"] = after

        result = self._get(path, params)
        return _filter_nsfw(self._parse_listing(result, item_type="link"), include_nsfw)

    def subreddit_info(self, subreddit: str) -> dict:
        """Get subreddit metadata."""
        result = self._get(f"/r/{subreddit}/about", {})
        if "error" in result:
            return result
        return _format_subreddit(result)

    # ── Post & Comments ───────────────────────────────────

    def post_comments(
        self,
        subreddit: Optional[str],
        post_id: str,
        sort: str = "best",
        limit: int = 50,
        max_depth: Optional[int] = None,
        expand_more: bool = True,
    ) -> dict:
        """Get a post with its comment tree (replies included, depth-first order).

        Args:
            subreddit: Subreddit the post is in (None to resolve by id alone).
            post_id: Post ID (without t3_ prefix).
            sort: best, top, new, controversial, old, qa.
            limit: Max comments to return (including replies).
            max_depth: Max reply depth to descend (None = unlimited).
            expand_more: Fetch "load more" stubs via /api/morechildren until
                `limit` is reached.
        """
        if subreddit:
            path = f"/r/{subreddit}/comments/{post_id}"
        else:
            path = f"/comments/{post_id}"
        params = {"sort": sort, "limit": limit}

        result = self._get(path, params)
        if "error" in result:
            return result

        # Reddit returns [post_listing, comments_listing]
        if not (isinstance(result, list) and len(result) >= 2):
            return {"error": "Unexpected response format", "post": {}, "comments": []}

        post_data = result[0].get("data", {}).get("children", [])
        comment_data = result[1].get("data", {}).get("children", [])

        post = _format_post(post_data[0]) if post_data else {}
        link_fullname = post.get("name") or f"t3_{post_id}"

        comments, more_ids, more_count = _extract_comment_tree(comment_data, max_depth)

        # Expand "load more" stubs until we hit the requested limit
        depth_by_name = {c["name"]: c.get("depth", 0) for c in comments}
        excluded = set()  # names dropped by max_depth or missing parents
        expansions = 0
        while expand_more and more_ids and len(comments) < limit and expansions < 20:
            expansions += 1
            batch, more_ids = more_ids[:100], more_ids[100:]
            more_result = self._get("/api/morechildren", {
                "api_type": "json",
                "link_id": link_fullname,
                "children": ",".join(batch),
                "sort": sort,
            })
            if "error" in more_result:
                break
            things = more_result.get("json", {}).get("data", {}).get("things", []) or []
            for thing in things:
                if thing.get("kind") == "more":
                    # Nested stub: queue its ids for later batches but do NOT add
                    # its count — the original stub's count already covered it
                    more_ids.extend(thing.get("data", {}).get("children") or [])

            # Stitch t1 things, iterating to a fixpoint so children can resolve
            # even when they arrive before their parent within the batch
            pending = [t for t in things if t.get("kind") == "t1"]
            new_count = 0
            progress = True
            while pending and progress:
                progress = False
                unresolved = []
                for thing in pending:
                    data = thing.get("data", {})
                    name = data.get("name", "")
                    parent = data.get("parent_id", "")
                    if parent == link_fullname:
                        depth = 0
                    elif parent in depth_by_name:
                        depth = depth_by_name[parent] + 1
                    elif parent in excluded:
                        excluded.add(name)
                        progress = True
                        continue
                    else:
                        unresolved.append(thing)
                        continue
                    progress = True
                    if max_depth is not None and depth > max_depth:
                        excluded.add(name)
                        continue
                    c = _format_comment(thing)
                    c["depth"] = depth
                    depth_by_name[name] = depth
                    comments.append(c)
                    new_count += 1
                pending = unresolved
            # Leftover unresolved comments have parents that never arrived
            # (e.g. still in unexpanded stubs) — drop them; they stay in
            # more_count as "not fetched" rather than render as fake top-levels
            excluded.update(t.get("data", {}).get("name", "") for t in pending)

            more_count = max(0, more_count - new_count)
            if new_count == 0:
                break

        comments = _sort_comment_tree(comments, link_fullname)
        truncated = max(0, len(comments) - limit)
        comments = comments[:limit]

        return {
            "post": post,
            "comments": comments,
            "total": len(comments),
            "more_count": more_count + truncated,
        }

    # ── User ──────────────────────────────────────────────

    def user_about(self, username: str) -> dict:
        """Get user profile info."""
        result = self._get(f"/user/{username}/about", {})
        if "error" in result:
            return result
        data = result.get("data", result)
        return {
            "username": data.get("name", ""),
            "link_karma": data.get("link_karma", 0),
            "comment_karma": data.get("comment_karma", 0),
            "total_karma": data.get("total_karma", 0),
            "created_utc": data.get("created_utc", 0),
            "is_gold": data.get("is_gold", False),
            "verified": data.get("verified", False),
        }

    def user_posts(
        self,
        username: str,
        sort: str = "new",
        time_filter: str = "all",
        limit: int = 25,
        after: Optional[str] = None,
        include_nsfw: bool = False,
    ) -> dict:
        """Get a user's submitted posts."""
        path = f"/user/{username}/submitted"
        params = {"sort": sort, "t": time_filter, "limit": min(limit, 100)}
        if after:
            params["after"] = after
        result = self._get(path, params)
        return _filter_nsfw(self._parse_listing(result, item_type="link"), include_nsfw)

    def user_comments(
        self,
        username: str,
        sort: str = "new",
        time_filter: str = "all",
        limit: int = 25,
        after: Optional[str] = None,
    ) -> dict:
        """Get a user's comments."""
        path = f"/user/{username}/comments"
        params = {"sort": sort, "t": time_filter, "limit": min(limit, 100)}
        if after:
            params["after"] = after
        result = self._get(path, params)
        return self._parse_listing(result, item_type="comment")

    # ── Subreddit Discovery ───────────────────────────────

    def search_subreddits(
        self,
        query: str,
        limit: int = 25,
        after: Optional[str] = None,
        include_nsfw: bool = False,
    ) -> dict:
        """Search for subreddits by name/description."""
        params = {
            "q": query,
            "limit": min(limit, 100),
            "type": "sr",
            "include_over_18": "on" if include_nsfw else "off",
        }
        if after:
            params["after"] = after

        result = self._get("/search", params)
        return _filter_nsfw(self._parse_listing(result, item_type="sr"), include_nsfw)

    def popular_subreddits(self, limit: int = 25, include_nsfw: bool = False) -> dict:
        """Get popular subreddits."""
        result = self._get("/subreddits/popular", {"limit": min(limit, 100)})
        return _filter_nsfw(self._parse_listing(result, item_type="sr"), include_nsfw)

    # ── Trending / Popular ────────────────────────────────

    def popular_posts(
        self,
        limit: int = 25,
        after: Optional[str] = None,
        include_nsfw: bool = False,
    ) -> dict:
        """Get posts from r/popular (cross-subreddit trending)."""
        params = {"limit": min(limit, 100)}
        if after:
            params["after"] = after
        result = self._get("/r/popular/hot", params)
        return _filter_nsfw(self._parse_listing(result, item_type="link"), include_nsfw)

    # ── Internal ──────────────────────────────────────────

    def _parse_listing(self, result: dict, item_type: str = "link") -> dict:
        """Parse a Reddit listing response into a normalized dict."""
        if "error" in result:
            return result

        # Handle bare listing
        data = result.get("data", result)
        children = data.get("children", [])
        after = data.get("after")

        items = []
        for child in children:
            kind = child.get("kind", "")
            if kind == "t3" or item_type == "link":
                items.append(_format_post(child))
            elif kind == "t1" or item_type == "comment":
                items.append(_format_comment(child))
            elif kind == "t5" or item_type == "sr":
                items.append(_format_subreddit(child))
            else:
                items.append(_format_post(child))

        return {
            "items": items,
            "after": after,
            "count": len(items),
        }

    def _get(self, path: str, params: dict) -> dict:
        """Execute an API GET with auth, caching, and rate limiting."""
        self._ensure_token()
        url = f"{OAUTH_URL}{path}"
        # Without raw_json=1 Reddit HTML-escapes &, <, > in every string field
        params = {**params, "raw_json": 1}
        cache_params = {k: str(v) for k, v in params.items()}

        if self.use_cache and self.cache:
            cached = self.cache.get(url, cache_params)
            if cached is not None:
                logger.debug("Cache hit: %s %s", path, params)
                return cached

        last_error = None
        for attempt in range(MAX_RETRIES + 1):
            self.rate_limiter.acquire()
            try:
                response = self.session.get(url, params=params, timeout=(10, REQUEST_TIMEOUT))

                if response.status_code == 429:
                    wait = _retry_wait_seconds(attempt, response)
                    if attempt < MAX_RETRIES:
                        logger.warning(
                            "Reddit 429 — waiting %.1fs (retry %d/%d)",
                            wait, attempt + 1, MAX_RETRIES,
                        )
                        time.sleep(wait)
                        continue
                    return {"error": "Rate limited (HTTP 429) after retries", "items": [], "count": 0}

                if response.status_code == 401:
                    # Token expired, refresh and retry
                    logger.warning("Reddit 401 — refreshing token")
                    self._token = None
                    self._token_expires = 0
                    self._ensure_token()
                    if attempt < MAX_RETRIES:
                        continue
                    return {"error": "Authentication failed after retry", "items": [], "count": 0}

                if response.status_code == 403:
                    return {"error": f"Forbidden: {path} (may be private or quarantined)", "items": [], "count": 0}

                if response.status_code == 404:
                    return {"error": f"Not found: {path}", "items": [], "count": 0}

                response.raise_for_status()
                result = response.json()

                if self.use_cache and self.cache and "error" not in result:
                    self.cache.set(url, cache_params, result)

                return result

            except requests.exceptions.RequestException as e:
                last_error = e
                if attempt < MAX_RETRIES:
                    wait = _retry_wait_seconds(attempt)
                    logger.warning("Request error — waiting %.1fs (retry %d/%d)", wait, attempt + 1, MAX_RETRIES)
                    time.sleep(wait)
                    continue
                return {"error": str(e), "items": [], "count": 0}

        return {"error": str(last_error) if last_error else "max retries exceeded", "items": [], "count": 0}
