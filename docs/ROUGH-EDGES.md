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

---

## Round 2 — dogfooding session (2026-07-23, researching r/LocalLLaMA)

Found while doing real research (~40 requests, 9 commands, JSON pipelines,
pagination, thread deep-reads). Unchecked = not yet fixed.

- [x] **R1. CRASH: `find-subs` dies on `subscribers: null`** (high)
  `reddit find-subs "local llm"` → `TypeError: unsupported format string passed
  to NoneType.__format__` at `cli.py` `_render_subreddits` (`f"{item.get('subscribers', 0):,}"`).
  r/StableLM returns `subscribers: null`. Same null-vs-missing class as the old
  active_users bug — formatters should coerce ALL numeric fields (`or 0`), and
  renderers should never trust them. Audit score/num_comments/created_utc too.

- [x] **R2. Stickied bot comments waste top thread slots** (medium)
  Every large r/LocalLLaMA thread leads with a 1-point stickied Discord-promo
  bot comment, shown first in every `thread` read. `_format_comment` doesn't
  capture `stickied`/`distinguished`, so the renderer can't demote or mark it.
  Suggest: capture the flags, sort stickied-last (or skip with a note).

- [x] **R3. `--depth 0` under-fills the requested limit** (medium)
  `thread ... --depth 0 -n 6 --no-expand` returned 3 top-level comments —
  Reddit's `limit` param counts whole comment trees, and depth-filtering then
  discards most nodes. When `--depth` is set, over-request from the API (or
  count only kept comments against the limit) so `-n` means "n comments shown".

- [x] **R4. Comment search ranks by post relevance, not comment relevance** (medium)
  `comments "what do you run locally"` returns top comments of loosely-matching
  posts; the bodies often don't address the query. Inherent to the
  search-posts-then-fetch strategy, but could rank/boost comments whose body
  contains query terms before generic high-score comments.

- [x] **R5. Group-level `--no-cache` position trips users** (medium usability)
  `reddit search "x" --no-cache` → "No such option". It must precede the
  subcommand (`reddit --no-cache search "x"`). I knew this and still typed it
  wrong mid-session. Accept `--no-cache`/`--debug` at the command level too.

- [x] **R6. Thread selftext silently truncated at 500 chars** (low)
  Long post bodies cut mid-sentence in the Post Details panel with no marker.
  Add "… (truncated, N more chars; use -j for full text)" and/or a --full flag.

- [x] **R7. `more_count` can overstate remaining comments** (low, cosmetic)
  "... 493 more comments not fetched" on a 491-comment post showing 12. Reddit
  stub counts are fuzzy; clamp the footer to `num_comments - shown` when known.

- [x] **R8. Bare image/link-only comments render as raw URLs** (low polish)
  Comments that are just a `preview.redd.it/...` image URL print the full URL
  noise; could render as "[image]" with the link dimmed.

Worked great this session: URL-paste thread lookup, indented reply trees,
--depth/--no-expand, pagination cursors, markdown link output, -j piping into
python, NSFW-hidden notes, cache speed on re-reads, zero 429s across ~40 calls.

---

## Round 3 — dogfooding session (2026-07-23, researching AT/Long Trail thru-hiking)

R1–R8 fixes all held up in the wild (no crash on find-subs, --depth 0 filled,
pinned demoted, relevance-ranked comment search visibly better, truncation
marker useful, flag position forgiving). New friction, all rendering polish:

- [x] **R9. Comment-search bodies chop mid-word with no ellipsis** (low)
  `_render_comments` does `body[:300]` then wraps — output ends like "for tax
  reaso". Use a word-boundary shorten with an explicit "…" marker.

- [x] **R10. OP's replies aren't marked in thread view** (low)
  In `thread` output you can't tell which replies are from the post author
  (e.g. an AMA-style data post where OP answers questions). Compare comment
  author to post author and tag `[OP]`.

- [x] **R11. find-subs rows balloon on long descriptions** (low)
  r/ADT_thru_hiking's description wrapped to 9 lines in the table. Cap list-view
  descriptions (~160 chars + "…"); full text stays in `info` / `-j`.

- [x] **R12. Selftext truncation has no slack** (low)
  "… truncated (63 more chars — use -j for full text)" — silly to cut for 63
  chars. Show the full text when it's within ~40% of the cap; only truncate
  when it meaningfully overflows.

- [x] **R13. Markdown post tables had no pagination cursor** — added an italic
  `Next page: --after <cursor>` footer so `-m` reports can be continued.

### Verification round on R1–R8 (adversarial, 20 agents)

All six confirmed findings fixed:

- [x] **V12. R4 ranking neutralized by stopwords/substrings** — "what do you run
  locally" boosted everything ('you' matched 'your'). Stopword list + whole-word
  matching.
- [x] **V13. `clear-cache` missed `@common_options`** — the one command where
  flag position still errored (README overclaimed). Decorator added.
- [x] **V14. `urlparse` ValueError crash (medium)** — URL-only comment body with
  brackets ("https://[example.com](...)") crashed thread/comments rendering.
  Wrapped in `_url_host` with fallback.
- [x] **V15. Markdown thread view lost link-only comment URLs** — now rendered
  as clickable `[link: host](url)` instead of a lossy host tag.
- [x] **V16. `more_count` clamp could under-report** — stale-low `num_comments`
  hid provably-fetched-and-truncated comments; clamp now floors at `truncated`.

Suite after this round: 89 passed.

---

## Round 4 — agent-oriented features (2026-07-24)

Built for agent/cron consumption (see README "Agent / scripting options"):
`--pages` auto-pagination with dedup; `--jsonl` + `--fields` (15× measured
context reduction on a 50-post search); `digest` one-shot recon command;
`thread --author/--min-score`; `--since` + `--seen` delta tracking with a
`seen` management command; multireddit fan-in (`-r a,b,c`, server-side);
structured `{"error", "retryable"}` jsonl errors.

Dogfooding fix during build:
- [x] **F1. thread --jsonl leaked full selftext despite --fields** — post line
  now projected too (warning-free for comment-only fields).

### Verification round on the features (adversarial, 31 agents)

Nine confirmed findings, all fixed:

- [x] **F2. `--since` validated only after API spend (medium)** — a typo'd
  value burned up to 10 rate-limited requests per run (forever, under cron).
  Now validated eagerly via a click callback: fails in 0.2s with zero calls.
- [x] **F3. Filtered threads asserted false reply structure (medium)** —
  `--author`/`--min-score` survivors kept orphaned depths; a depth-2 reply
  rendered as if nested under an unrelated comment. Filtered views now render
  flat with an explicit "(filtered view — N hidden)" header.
- [x] **F4. Five commands emitted cursors they couldn't accept** — user-posts,
  user-comments, popular, find-subs, popular-subs advertised `_meta.after` but
  had no `--after`. All five accept it now.
- [x] **F5. SeenStore recycled long-lived items past the cap** — recency was
  first-record; suppressed-but-visible items were never re-recorded, so hot
  posts re-emitted as "new" after cap eviction. Now: last-occurrence dedup +
  recording the pre-suppression list refreshes recency every run.
- [x] **F6. SeenStore writes were non-atomic and unguarded** — crash mid-write
  could wipe all stores; unwritable path gave a raw traceback after output.
  Now temp-file + rename, and record failures warn on stderr (at-least-once).
- [x] **F7. Empty-page renders dropped partial_error/filter notes** — a mid-run
  429 with all items filtered looked like a clean "nothing new". Notes now
  print in empty branches too.
- [x] **F8. Markdown renderers missing the notes entirely** (same class as F7).
- [x] **F9. digest silently dropped failed thread excerpts** — now noted in
  markdown and as `skipped_threads` in `-j`.
- [x] **F10. thread `filtered_out` invisible in human output** (folded into F3's
  header; all-filtered threads say so instead of rendering empty).

Suite after this round: 127 passed.

---

## Round 5 — research persistence (2026-07-24)

Three features turning one-off sessions into standing research (README
"Research workflow" section): `thread --seen NAME` (delta reads of developing
threads, flat view with "N previously seen"), `--save TOPIC` on
search/comments/posts/thread/digest (append evidence to `research/<topic>/`),
and `reddit topic create/list/update/remove` (standing sweeps: subs + optional
query bound to a folder + delta store; `update` appends only what's new).

Live dogfooding validated crash consistency by accident: a SIGPIPE (`| head`)
killed an update mid-render, and the at-least-once design did exactly the
right thing — nothing recorded, items re-emitted next run, no false
suppression.

### Verification round (adversarial, 26 agents) — 10 confirmed, all fixed

- [x] **T1. Path traversal via dot-only topic names (medium)** — `--save ..`
  escaped the research root (`_slug` allowed dots). Leading dots now stripped;
  `.`/`..` become "untitled", `..hidden` can't create hidden dirs.
- [x] **T2. NSFW silently dropped from topic updates (medium)** — no opt-in, no
  note: a monitoring tool reporting "No new activity" while tagged posts
  existed. `topic create --nsfw` opt-in added; `nsfw_hidden` now surfaces in
  update files, `_meta`, and the no-activity message.
- [x] **T3. Save failure crashed before render** — read-only dir/full disk cost
  the whole fetched result. Saves now warn on stderr and rendering proceeds.
- [x] **T4. `write_text` without utf-8 encoding** — emoji/CJK titles would
  crash saves on non-UTF-8 locales. All research/store writes pinned to utf-8.
- [x] **T5. `--save ''` was a silent no-op** — now rejected at parse time.
- [x] **T6. Query-sweep failures invisible to machines** — a forever-403 query
  looked like clean success in `_meta` and the archive. Now `query_error` in
  `_meta` plus a note in the update file.
- [x] **T7. `create` slugged names but `update`/`remove` didn't** — "a b"
  created 'a_b' then couldn't be updated by the name the user typed. All
  lookups normalize identically now.
- [x] **T8. Stray verifier test dir in repo root** — removed; `research/` is
  now gitignored (user data, not source).
- [x] **T9. `--save` help advertised `--jsonl` on digest (which lacks it)** —
  reworded.
- [x] **T10. (dup of T1 from second lens)** — same fix.

Suite after this round: 148 passed.

---

## Round 6 — media attachments (2026-07-24)

`reddit media <sub|post-url>` downloads image/video attachments (galleries
expanded) to disk with a `manifest.jsonl` mapping each file to its post. The
payoff beyond archiving: downloaded images are readable — an agent can analyze
the actual benchmark chart / rig photo / screenshot instead of seeing a bare
`i.redd.it` URL. New module `reddit/media.py` (extraction lives in
`api._extract_media`, streaming download + manifest in `MediaDownloader`).
Live-validated by downloading r/eink's top-of-month and reading a DASUNG
e-ink-monitor rig photo.

Safety by design: post-id-only filenames (traversal-proof), content-type
verified, size cap enforced mid-stream, host allowlist, separate
unauthenticated session (OAuth token never touches a CDN).

### Verification round (adversarial, 30 agents, highest-scrutiny surface) — 12 confirmed, all fixed

- [x] **M1. SSRF via redirects (security, medium→high)** — `requests` follows
  redirects by default, so an allowlisted URL could 302 to
  `http://169.254.169.254/…` (cloud metadata) or downgrade to http, bypassing
  the host allowlist entirely. A verifier proved it end-to-end against a local
  server. Fixed: `allow_redirects=False` + manual following that re-validates
  the host and https scheme at **every** hop.
- [x] **M2. `--max-files` + `--seen` lost media forever (HIGH, ×2 lenses)** —
  posts past the cap were recorded as seen without being fetched, so a later
  uncapped run skipped them permanently. Fixed: only posts actually processed
  (and failure-free) are recorded; a per-post download budget bounds gallery
  overshoot and marks the run truncated.
- [x] **M3. Crosspost media never extracted (medium)** — the child post carries
  null media; the real media is on `crosspost_parent_list[0]`. Now consulted.
- [x] **M4. imgur `.gifv` always failed (medium)** — `.gifv` is an HTML player;
  the file is the `.mp4` at the same id. Now rewritten on extraction.
- [x] **M5. Single-post path ignored the NSFW rail (medium)** — a pasted NSFW
  post URL downloaded without `--nsfw`. Now blocked with a clear error.
- [x] **M6. jsonl `_meta` hid truncation + dropped partial_error (medium)** — a
  cron consumer couldn't tell a capped run from a complete one. `truncated` and
  `partial_error` now in `_meta`.
- [x] **M7–M12 (low)** — SVG (scriptable image) content-type now blocked;
  `--seen` on a single post warns instead of silently no-op'ing; an
  all-downloads-failed run exits nonzero; default `./media` gitignored;
  gallery overshoot of `--max-files` bounded (dup of M2's budget fix).

Suite after this round: 180 passed.

---

## Round 7 — standing media (2026-07-24)

Phase 2 of the media feature: `topic create --media` makes a standing topic
download each new post's attachments into `<folder>/media/` on every `update`
(`topic update --media/--no-media` overrides per run; `topic list` shows
`+media`). The compounding payoff — a monitored subreddit now accumulates new
posts *and* readable attachments automatically. Live-validated: `eink-watch`
(r/eink, `--media`) downloaded new-post images on update 1, quiet on update 2;
one downloaded image (a modded e-ink iPhone running a "Painter" app) read back
cleanly.

### Verification round (adversarial, 11 agents) — 2 root causes fixed

- [x] **P1. Unguarded `mkdir` aborted the whole update (HIGH)** — a media-dir
  failure (unwritable, full disk, or a file where `media/` should go) raised a
  raw traceback *before* the delta report, save, and seen-recording, destroying
  the command's primary function. Now guarded: warn and continue; the report
  and delta tracking still run. Verified live (file blocking the media path →
  full report still emitted).
- [x] **P2. Budget cap silently dropped media forever (medium, flagged ×3)** — I
  ignored `download_post`'s `complete` flag and marked cap-truncated posts as
  seen, so their un-downloaded attachments never retried and `_meta` looked
  complete. Now: truncation is surfaced (`media_truncated` in `_meta`, a note in
  the update file and stdout) and self-heals — deferred posts stay un-recorded
  so they re-appear as a delta next update and finish downloading (existing
  files skip).

Suite after this round: 186 passed.

---

## Round 8 — phase 3: audio muxing + research index (2026-07-24)

Two capability upgrades:
- **DASH audio muxing** — Reddit serves video and audio as separate tracks, so
  downloaded video was silent. `MediaDownloader` now parses the DASH manifest
  for the audio track, downloads it, and muxes with `ffmpeg` into one `.mp4`
  with sound (falls back to video-only + a note when ffmpeg is absent or no
  audio track exists). Live-validated: a real r/funny video downloaded with
  both video and audio streams (`ffprobe` confirmed), manifest note "muxed with
  audio".
- **Topic INDEX.md** — every `topic update` regenerates `<folder>/INDEX.md`, a
  single entry point linking each update/saved report (newest first) and
  summarizing the media folder; `reddit topic index <name>` rebuilds on demand.

### Verification round (adversarial, 20 agents) — 3 root causes fixed

- [x] **X1. `ffmpeg` timeout orphaned `.mux.tmp.mp4` (flagged ×3)** — the
  temp-cleanup `finally` unlinked the video/audio temps but not the mux output,
  so a timed-out ffmpeg left a partial file that accumulated (and got counted
  in INDEX's media summary). Now unlinked in `finally`; regression test with a
  timeout-then-partial-write ffmpeg confirms zero leftover temps.
- [x] **X2. Unguarded `stat()` crashed index generation (medium)** — a broken
  symlink or a file removed mid-scan in `media/` raised `FileNotFoundError` out
  of both `topic index` and `topic update` (after state was already persisted).
  Per-file `stat()` now guarded.
- [x] **X3. Manifest cap bounded compressed bytes → gzip bomb (low)** — reading
  the DASH manifest via `raw.read(cap, decode_content=True)` capped *compressed*
  input; a 2 MB gzip could inflate to ~2 GB. Now read via `iter_content` with a
  decoded-byte counter, matching `_stream`'s discipline.
- Cosmetic: NOTES.md is linked as the reader's synthesis instead of being
  buried in the auto-generated update list.

Suite after this round: 201 passed.
