# reddit-cli

A CLI for searching Reddit via the OAuth2 API. Search posts, comments, subreddits, users, and discussions.

## Setup

### 1. Create a Reddit App

1. Go to https://www.reddit.com/prefs/apps
2. Click "create another app..."
3. Select **script** type
4. Fill in name, redirect URI (use `http://localhost:8080`)
5. Note your **client ID** (under the app name) and **client secret**

### 2. Set Environment Variables

```bash
export REDDIT_CLIENT_ID="your_client_id"
export REDDIT_CLIENT_SECRET="your_client_secret"
export REDDIT_USERNAME="your_username"      # optional, for higher rate limits
export REDDIT_PASSWORD="your_password"      # optional, for higher rate limits
```

Or create a `.env` file (never commit this):

```
REDDIT_CLIENT_ID=your_client_id
REDDIT_CLIENT_SECRET=your_client_secret
REDDIT_USERNAME=your_username
REDDIT_PASSWORD=your_password
```

**Note:** Without username/password, the CLI uses application-only auth (read-only public data, lower rate limits). With credentials, it uses script auth (full read access, 100 req/min).

### 3. Install

```bash
pip install -e .
```

## Quick Start

```bash
# Search posts
reddit search "autonomous agents"

# Search within a subreddit
reddit search "vibe coding" -r programming

# Get hot posts from a subreddit
reddit posts python --sort hot

# Search comments
reddit comments "cursor vs claude"

# Markdown output for piping
reddit search "LLM" -m
```

## Commands

### Search

| Command | Description |
|---------|-------------|
| `reddit search <query>` | Search posts by relevance |
| `reddit comments <query>` | Search comments |
| `reddit find-subs <query>` | Find subreddits by name/description |

### Subreddit

| Command | Description |
|---------|-------------|
| `reddit posts <subreddit>` | Get posts (hot, new, top, rising, controversial) |
| `reddit info <subreddit>` | Subreddit metadata (subscribers, description) |
| `reddit thread <subreddit> <post_id>` | Post with its comment tree (replies included) |
| `reddit thread <url>` | Same, from a pasted permalink, redd.it link, t3_ fullname, or bare id |

### User

| Command | Description |
|---------|-------------|
| `reddit user <username>` | User profile (karma, account age) |
| `reddit user-posts <username>` | User's submitted posts |
| `reddit user-comments <username>` | User's comments |

### Discovery

| Command | Description |
|---------|-------------|
| `reddit popular` | Trending posts from r/popular |
| `reddit popular-subs` | Popular subreddits |

### Recon

| Command | Description |
|---------|-------------|
| `reddit digest <subreddit>` | One-shot recon: info + top posts + thread excerpts (+ `-q` search) |

### Utility

| Command | Description |
|---------|-------------|
| `reddit seen` | List/clear `--seen` delta-tracking stores (`--clear NAME`) |
| `reddit clear-cache` | Clear local response cache |

## Common Options

| Flag | Description |
|------|-------------|
| `-r, --subreddit` | Restrict search to a subreddit |
| `-s, --sort` | Sort order (relevance, hot, top, new, comments, rising, controversial) |
| `-t, --time` | Time filter (hour, day, week, month, year, all) |
| `-n, --limit` | Max results (1-100, default: 25; `thread` allows up to 500) |
| `--after` | Pagination cursor (from previous result) |
| `--nsfw / --no-nsfw` | Include NSFW (over 18) results (default: hidden, with a note) |
| `-j, --json-output` | Raw JSON output |
| `-m, --markdown` | Markdown table output |
| `--no-cache` | Disable response caching (works before or after the subcommand) |
| `--debug` | Enable debug logging (works before or after the subcommand) |

Stickied (bot/mod) comments are demoted to the end of thread output and marked
`[pinned]`; comment search skips them and ranks comments mentioning your query
terms first.

### Agent / scripting options

Built for LLM-agent and cron use — dense output, fewer invocations, delta
tracking:

| Flag | Description |
|------|-------------|
| `--pages N` | Auto-follow pagination cursors, merge + dedup up to N pages (max 10) |
| `--jsonl` | One compact JSON object per line, then a `{"_meta": ...}` line with the cursor/counts; errors become `{"error", "retryable"}` lines with exit 1 |
| `--fields a,b,c` | Project `-j`/`--jsonl` items to just these fields (unknown fields warn on stderr) |
| `--since AGE\|DATE` | Only items newer than `90m`/`24h`/`7d`/`2w` or an ISO date |
| `--seen NAME` | Only emit items not already emitted under NAME (state in `~/.reddit/seen.json`) — turns any listing into a monitoring delta feed |
| `-r a,b,c` | Multireddit fan-in: searches/lists `r/a+b+c` server-side in one request |
| `thread --author X` | Only X's comments in a thread (e.g. mine an OP's answers) |
| `thread --min-score N` | Drop low-signal comments before they cost tokens |

```bash
# 100 dense records, 4 pages merged, only the fields you need
reddit search "rust async" --pages 4 -n 100 --jsonl --fields title,score,permalink

# Cron-friendly delta feed: only new posts since the last run
reddit posts LocalLLaMA,ollama --sort new --since 1d --seen ai-watch --jsonl

# One-command recon document for a research session
reddit digest MachineLearning -t week -T 3 -q "interpretability" > recon.md

# Mine the OP's answers out of an AMA-style thread
reddit thread <url> -n 200 --author some_op --jsonl
```

Reddit's native search operators pass through unchanged: `author:name`,
`self:yes`, `flair:"Discussion"`, `title:foo`, and boolean `OR` all work inside
the query string.

### Thread options

| Flag | Description |
|------|-------------|
| `-d, --depth` | Max reply depth to descend (default: unlimited) |
| `--no-expand` | Don't fetch "load more" comment stubs (fewer API calls) |

## Examples

```bash
# Top posts from r/MachineLearning this week
reddit posts MachineLearning --sort top --time week

# Search comments about a specific tool in r/LocalLLaMA
reddit comments "ollama" -r LocalLLaMA

# Find AI-related subreddits
reddit find-subs "artificial intelligence"

# Get a specific post's comment thread (replies indented)
reddit thread programming abc123 --sort top

# Or just paste a permalink
reddit thread "https://reddit.com/r/programming/comments/abc123/some_title/"

# Top-level comments only, no reply descent
reddit thread programming abc123 --depth 0

# User activity
reddit user-posts spez -n 10
reddit user-comments spez --sort top --time year

# Popular posts right now
reddit popular -n 10

# JSON output for piping
reddit search "startup" -n 50 -j | jq '.items[].title'

# Markdown for reports
reddit posts Python --sort top --time month -m >> research-notes.md

# Paginate through results
reddit search "rust" -n 100
# Use the 'after' cursor from output:
reddit search "rust" -n 100 --after t3_next123
```

## API

- **Auth:** OAuth2 script-type (client_id + client_secret + username + password)
- **Base URL:** `https://oauth.reddit.com`
- **Rate limit:** 100 requests/minute (self-limited to 1 req/sec)
- **Cache:** 30-minute TTL file cache at `~/.reddit_cache/`
- **Token:** Auto-refreshes before expiry (3600s lifetime, refresh at 3000s)

## Development

```bash
pip install -e ".[dev]"
pytest tests/ -v
```

## License

MIT
