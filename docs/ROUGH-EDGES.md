# Rough Edges — Working Doc

Findings from the 2026-07-23 test drive of all 12 commands, all three output modes,
pagination, caching, error paths, and the test suite. Check items off as we fix them.

Core functionality confirmed working: OAuth2 auth (password grant), search, subreddit
listings, comment search (real bodies), user commands, discovery, response caching,
rate limiting, 404/403 error handling with exit code 1, JSON/markdown output, pagination.

---

## Data correctness

- [x] **1. Active users always shows 0**
  Everywhere it appears (`info`, `find-subs`, `popular-subs`). `_format_subreddit`
  reads `accounts_active` (`reddit/api.py:100`), but Reddit now returns
  `active_user_count`. The column is pure noise right now.

- [x] **2. HTML entities never unescaped**
  Selftext and comment bodies come back with `&lt;3`, `&amp;`, `&gt;` intact.
  Reddit's API HTML-escapes text fields; the formatters (`_format_post`,
  `_format_comment`, `_format_subreddit`) should run `html.unescape()`.

- [x] **3. Deleted/empty comments render as blank lines**
  Comment search printed entries with no author and no body. `item.get("author", "?")`
  doesn't catch empty-string values (key exists, value is `""`), and empty bodies
  aren't filtered out or marked as `[deleted]`.

## Safety / UX

- [x] **4. NSFW results unmarked and unfilterable**
  A plain `reddit search "test"` led with porn. `over_18` is already captured in the
  data model but never surfaced or used. Add a marker in output plus a
  `--nsfw/--no-nsfw` filter (sensible default: exclude or mark).

- [x] **5. `thread` chokes on pasted URLs**
  `reddit thread ClaudeCode <permalink>` gives a confusing "Not found" (the URL gets
  embedded in the API path). Should parse a pasted permalink and extract the post ID —
  and arguably the subreddit too, making the first arg optional.

- [x] **6. Thread depth is shallow — no reply tree**
  A 670-comment post shows only 7 top-level comments. No reply-tree traversal, no
  "load more" (`more` children) handling. Biggest capability gap for a community
  intelligence tool. Likely wants a `--depth` option and indented rendering.

- [x] **7. Loose limit validation**
  `-n 0` is accepted (Reddit silently falls back to its default), `-n 500` silently
  returns 100. Use `click.IntRange(1, 100)` on all `--limit` options so bad values
  fail loudly at the CLI boundary.

## Output polish

- [x] **8. Markdown mode has no permalinks for posts/subreddits**
  You can't click through from a report, which undercuts the `-m >> notes.md`
  workflow. (Comments markdown does include links; posts and subreddit tables don't.)

- [x] **9. Double truncation in tables**
  Titles get cut to 50 chars by `_truncate` and then rich wraps them anyway at
  narrow widths, so you get `...` mid-wrap. Let rich handle column width alone
  (e.g. `overflow="ellipsis"` / `max_width`) instead of pre-truncating.

## Dev hygiene

- [x] **10. `test_missing_credentials` fails when a `.env` exists**
  `RedditClient.__init__` calls `load_dotenv()`, so with a `.env` in the repo the
  "no credentials" test finds credentials and fails (`tests/test_api.py:352`).
  Suite result depends on local files — mock the dotenv call or make it injectable.
  Currently: 1 failed, 23 passed.

---

## Verification round (2026-07-23)

After implementing 1–10, an adversarial multi-agent review of the diff (each
finding execution-verified by two independent refuters) surfaced 11 follow-up
issues. All fixed:

- [x] **V1. Post `url` field still HTML-escaped** — root cause: the client never
  sent `raw_json=1`, so Reddit HTML-escapes every string field. Fixed properly:
  `_get` now sends `raw_json=1` on all requests (as PRAW does) and the
  client-side `html.unescape` was removed (it would corrupt user-typed literal
  entities once responses arrive raw).
- [x] **V2. `comments` hid NSFW silently** — `search_comments` dropped
  `nsfw_hidden` and the `after` cursor in both return paths; comment renderers
  never showed the hidden note. All propagated/rendered now.
- [x] **V3. Fully-filtered posts page stranded pagination** — empty-after-filter
  pages now still print the `Next page: --after` hint.
- [x] **V4. Slugless `reddit.com/comments/<id>` URLs rejected** — now parsed.
- [x] **V5. Mobile share links (`/r/<sub>/s/<token>`) gave a misleading error** —
  now detected with a specific message (token only resolves via HTTP redirect).
- [x] **V6. `v.redd.it`/`i.redd.it` media links false-matched as shortlinks** —
  shortlink regex anchored; media links now fail parsing with a clear error
  instead of a confusing API 404.
- [x] **V7. `reddit.com/gallery/<id>` share URLs rejected** — now parsed.
- [x] **V8. `--depth` bypass + fake top-level orphans (HIGH)** — in the
  `morechildren` loop, comments with unknown parents got depth 0, so descendants
  of depth-filtered comments leaked past `--depth` and orphans rendered as fake
  top-level comments. Rewritten: fixpoint stitching resolves child-before-parent
  ordering, and comments whose parents were filtered or never fetched are
  dropped (kept in `more_count`) instead of mis-rendered.
- [x] **V9. `more_count` double-counted nested stubs** — a stub's `count` covers
  its whole subtree, so nested stubs' counts are no longer re-added; the footer
  count is accurate now.
- [x] **V10. `_md_link` broke on backslashes** — titles with trailing `\` or
  `\]` produced unparseable links; backslashes are escaped first now.
- [x] **V11. `ratio=2` on table columns was inert** — rich ignores `ratio`
  without `expand=True`; removed the dead parameters (flexible defaults were
  verified fine at 30–200 columns).

Suite after this round: 69 passed.
