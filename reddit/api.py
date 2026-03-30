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
import time
from typing import Optional

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
    """Normalize a Reddit post/link into a clean dict."""
    data = post.get("data", post)
    return {
        "id": data.get("id", ""),
        "name": data.get("name", ""),  # fullname e.g. t3_abc123
        "title": data.get("title", ""),
        "author": data.get("author", "[deleted]"),
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
        "author": data.get("author", "[deleted]"),
        "body": data.get("body", ""),
        "score": data.get("score", 0),
        "subreddit": data.get("subreddit", ""),
        "created_utc": data.get("created_utc", 0),
        "permalink": f"https://reddit.com{data.get('permalink', '')}",
        "parent_id": data.get("parent_id", ""),
        "link_id": data.get("link_id", ""),
        "link_title": data.get("link_title", ""),
    }


def _format_subreddit(sub: dict) -> dict:
    """Normalize a subreddit info dict."""
    data = sub.get("data", sub)
    return {
        "name": data.get("display_name", ""),
        "title": data.get("title", ""),
        "description": data.get("public_description", ""),
        "subscribers": data.get("subscribers", 0),
        "active_users": data.get("accounts_active", 0),
        "created_utc": data.get("created_utc", 0),
        "over_18": data.get("over18", False),
        "url": f"https://reddit.com{data.get('url', '')}",
    }


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
        }
        if after:
            params["after"] = after

        result = self._get(path, params)
        return self._parse_listing(result, item_type=search_type)

    def search_comments(
        self,
        query: str,
        subreddit: Optional[str] = None,
        sort: str = "relevance",
        time_filter: str = "all",
        limit: int = 25,
        after: Optional[str] = None,
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
        )
        if "error" in post_result:
            return post_result

        posts = post_result.get("items", [])
        if not posts:
            return {"items": [], "after": None, "count": 0}

        # Step 2: Fetch top comments from each post
        comments_per_post = max(limit // len(posts), 2) if posts else limit
        all_comments = []
        for post in posts:
            sub = post.get("subreddit", "")
            post_id = post.get("id", "")
            if not sub or not post_id:
                continue
            thread = self.post_comments(sub, post_id, sort="top", limit=comments_per_post)
            if "error" in thread:
                continue
            for c in thread.get("comments", []):
                # Attach post title for context
                c["link_title"] = post.get("title", "")
                all_comments.append(c)

        # Sort by score and trim to requested limit
        all_comments.sort(key=lambda c: c.get("score", 0), reverse=True)
        all_comments = all_comments[:limit]

        return {
            "items": all_comments,
            "after": post_result.get("after"),
            "count": len(all_comments),
        }

    # ── Subreddit Listings ────────────────────────────────

    def subreddit_posts(
        self,
        subreddit: str,
        sort: str = "hot",
        time_filter: str = "all",
        limit: int = 25,
        after: Optional[str] = None,
    ) -> dict:
        """Get posts from a subreddit.

        Args:
            subreddit: Subreddit name (without r/).
            sort: hot, new, top, rising, controversial.
            time_filter: For top/controversial — hour, day, week, month, year, all.
            limit: Max results (1-100).
        """
        path = f"/r/{subreddit}/{sort}"
        params = {"limit": min(limit, 100), "t": time_filter}
        if after:
            params["after"] = after

        result = self._get(path, params)
        return self._parse_listing(result, item_type="link")

    def subreddit_info(self, subreddit: str) -> dict:
        """Get subreddit metadata."""
        result = self._get(f"/r/{subreddit}/about", {})
        if "error" in result:
            return result
        return _format_subreddit(result)

    # ── Post & Comments ───────────────────────────────────

    def post_comments(
        self,
        subreddit: str,
        post_id: str,
        sort: str = "best",
        limit: int = 50,
    ) -> dict:
        """Get comments for a specific post.

        Args:
            subreddit: Subreddit the post is in.
            post_id: Post ID (without t3_ prefix).
            sort: best, top, new, controversial, old, qa.
            limit: Max comments to return.
        """
        path = f"/r/{subreddit}/comments/{post_id}"
        params = {"sort": sort, "limit": limit}

        result = self._get(path, params)
        if "error" in result:
            return result

        # Reddit returns [post_listing, comments_listing]
        if isinstance(result, list) and len(result) >= 2:
            post_data = result[0].get("data", {}).get("children", [])
            comment_data = result[1].get("data", {}).get("children", [])

            post = _format_post(post_data[0]) if post_data else {}
            comments = []
            for c in comment_data:
                if c.get("kind") == "t1":
                    comments.append(_format_comment(c))

            return {"post": post, "comments": comments, "total": len(comments)}

        return {"error": "Unexpected response format", "post": {}, "comments": []}

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
    ) -> dict:
        """Get a user's submitted posts."""
        path = f"/user/{username}/submitted"
        params = {"sort": sort, "t": time_filter, "limit": min(limit, 100)}
        if after:
            params["after"] = after
        result = self._get(path, params)
        return self._parse_listing(result, item_type="link")

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
    ) -> dict:
        """Search for subreddits by name/description."""
        params = {"q": query, "limit": min(limit, 100), "type": "sr"}
        if after:
            params["after"] = after

        result = self._get("/search", params)
        return self._parse_listing(result, item_type="sr")

    def popular_subreddits(self, limit: int = 25) -> dict:
        """Get popular subreddits."""
        result = self._get("/subreddits/popular", {"limit": min(limit, 100)})
        return self._parse_listing(result, item_type="sr")

    # ── Trending / Popular ────────────────────────────────

    def popular_posts(
        self,
        limit: int = 25,
        after: Optional[str] = None,
    ) -> dict:
        """Get posts from r/popular (cross-subreddit trending)."""
        params = {"limit": min(limit, 100)}
        if after:
            params["after"] = after
        result = self._get("/r/popular/hot", params)
        return self._parse_listing(result, item_type="link")

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
