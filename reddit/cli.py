"""CLI entry point for Reddit search via OAuth2 API."""

import json
import logging
import re
import textwrap
from datetime import datetime, timezone
from urllib.parse import urlparse

import time

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from reddit.api import RedditClient, SORT_CHOICES, TIME_CHOICES, parse_post_reference
from reddit.cache import SeenStore

console = Console()

SEARCH_SORT = ("relevance", "hot", "top", "new", "comments")
LIMIT_RANGE = click.IntRange(1, 100)
NSFW_FLAG = click.option(
    "--nsfw/--no-nsfw", "include_nsfw", default=False, show_default=True,
    help="Include NSFW (over 18) results",
)


def common_options(f):
    """--no-cache/--debug on every command, so position doesn't matter."""
    f = click.option("--no-cache", "no_cache", is_flag=True,
                     help="Disable response caching")(f)
    f = click.option("--debug", "debug", is_flag=True,
                     help="Enable debug logging")(f)
    return f


def output_options(f):
    """Structured-output flags for agent consumption."""
    f = click.option("--fields", default=None,
                     help="Comma-separated fields to keep in -j/--jsonl output")(f)
    f = click.option("--jsonl", is_flag=True,
                     help="One compact JSON object per line + final _meta line")(f)
    return f


def _validate_since(ctx, param, value):
    """Eager validation: a typo'd --since must fail before any API quota is spent."""
    if value is not None:
        _parse_since(value)
    return value


def listing_options(f):
    """Pagination and delta-tracking flags for listing commands."""
    f = click.option("--seen", "seen_name", default=None, metavar="NAME",
                     help="Only emit items not seen by previous runs under NAME "
                          "(state in ~/.reddit/seen.json)")(f)
    f = click.option("--since", default=None, metavar="AGE|DATE", callback=_validate_since,
                     help="Only items newer than e.g. 90m, 24h, 7d, 2w, or an ISO date")(f)
    f = click.option("--pages", default=1, type=click.IntRange(1, 10), show_default=True,
                     help="Auto-follow pagination cursors, merging up to N pages")(f)
    return f


def _parse_since(value: str) -> float:
    """'90m'/'24h'/'7d'/'2w' or ISO date -> epoch cutoff."""
    m = re.fullmatch(r"(\d+)\s*([mhdw])", value.strip().lower())
    if m:
        mult = {"m": 60, "h": 3600, "d": 86400, "w": 604800}[m.group(2)]
        return time.time() - int(m.group(1)) * mult
    try:
        dt = datetime.fromisoformat(value.strip())
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        raise click.BadParameter(
            f"{value!r} — expected an age like 90m/24h/7d/2w or an ISO date", param_hint="--since")


def _apply_since(result: dict, since: str) -> dict:
    if not since or "error" in result:
        return result
    cutoff = _parse_since(since)
    items = result.get("items", [])
    kept = [i for i in items if i.get("created_utc", 0) >= cutoff]
    if len(kept) != len(items):
        result = {**result, "items": kept, "count": len(kept),
                  "since_filtered": len(items) - len(kept)}
    return result


def _apply_seen(result: dict, seen_name: str) -> dict:
    if not seen_name or "error" in result:
        return result
    items = result.get("items", [])
    kept = SeenStore().filter_new(seen_name, items)
    if len(kept) != len(items):
        result = {**result, "items": kept, "count": len(kept),
                  "seen_filtered": len(items) - len(kept)}
    return result


def _record_seen(result: dict, seen_name: str) -> None:
    """Record after successful output; a store write failure must not crash
    (the items simply re-emit next run — at-least-once semantics)."""
    if seen_name and "error" not in result:
        try:
            SeenStore().record(seen_name, result.get("items", []))
        except OSError as e:
            click.echo(f"warning: could not update seen store: {e}", err=True)


def _project(items: list, fields: str, warn: bool = True) -> list:
    if not fields:
        return items
    keys = [k.strip() for k in fields.split(",") if k.strip()]
    available = set()
    for it in items:
        available.update(it.keys())
    unknown = [k for k in keys if items and k not in available]
    if unknown and warn:
        click.echo(
            f"warning: unknown field(s) {', '.join(unknown)} — available: "
            f"{', '.join(sorted(available))}", err=True)
    return [{k: it[k] for k in keys if k in it} for it in items]


def _jsonl_line(obj) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False)


def _emit_structured(result: dict, jsonl: bool, json_output: bool, fields: str) -> bool:
    """Emit --jsonl or -j output for a listing result. Returns True if handled."""
    if jsonl:
        for item in _project(result.get("items", []), fields):
            click.echo(_jsonl_line(item))
        meta = {k: result[k] for k in
                ("after", "count", "nsfw_hidden", "since_filtered", "seen_filtered", "partial_error")
                if k in result and result[k] is not None}
        click.echo(_jsonl_line({"_meta": meta}))
        return True
    if json_output:
        out = dict(result)
        if fields:
            out["items"] = _project(out.get("items", []), fields)
        click.echo(json.dumps(out, indent=2))
        return True
    return False


def _client(ctx, no_cache: bool = False, debug: bool = False) -> "RedditClient":
    """Build the API client, honoring group-level and command-level flags."""
    if debug or ctx.obj.get("debug"):
        logging.getLogger().setLevel(logging.DEBUG)
    use_cache = not (no_cache or ctx.obj.get("no_cache"))
    return RedditClient(use_cache=use_cache)


_RETRYABLE_HINTS = ("429", "rate limit", "timed out", "timeout", "connection",
                    "500", "502", "503", "504", "max retries")


def _error_exit(result: dict, jsonl: bool = False) -> bool:
    if "error" in result:
        msg = str(result["error"])
        if jsonl:
            retryable = any(h in msg.lower() for h in _RETRYABLE_HINTS)
            click.echo(_jsonl_line({"error": msg, "retryable": retryable}))
        else:
            console.print(f"[red]Error:[/red] {escape(msg)}")
        raise SystemExit(1)
    return False


def _truncate(text: str, width: int = 60) -> str:
    if not text:
        return ""
    text = text.replace("\n", " ").strip()
    return text if len(text) <= width else f"{text[:width - 3]}..."


def _format_age(ts: float) -> str:
    if not ts:
        return ""
    now = datetime.now(timezone.utc).timestamp()
    diff = now - ts
    if diff < 3600:
        return f"{int(diff / 60)}m ago"
    if diff < 86400:
        return f"{int(diff / 3600)}h ago"
    if diff < 2592000:
        return f"{int(diff / 86400)}d ago"
    return f"{int(diff / 2592000)}mo ago"


def _format_date(ts: float) -> str:
    if not ts:
        return ""
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d %H:%M")


def _escape_md(text: str) -> str:
    return (text or "").replace("|", "\\|").replace("\n", " ")


def _md_link(text: str, url: str) -> str:
    # Backslashes first, or the escapes added below get doubled
    text = (text or "").replace("\\", "\\\\").replace("\n", " ")
    text = text.replace("[", "\\[").replace("]", "\\]").replace("|", "\\|")
    return f"[{text}]({url})"


def _nsfw_hidden_note(result: dict) -> str:
    hidden = result.get("nsfw_hidden", 0)
    return f"{hidden} NSFW result(s) hidden — use --nsfw to include" if hidden else ""


def _filter_notes(result: dict) -> list:
    notes = []
    if result.get("since_filtered"):
        notes.append(f"{result['since_filtered']} older result(s) filtered (--since)")
    if result.get("seen_filtered"):
        notes.append(f"{result['seen_filtered']} previously-seen result(s) skipped (--seen)")
    if result.get("partial_error"):
        notes.append(f"pagination stopped early: {result['partial_error']}")
    return notes


_URL_ONLY_RE = re.compile(r"https?://\S+")

SELFTEXT_PREVIEW = 500   # chars of post text shown in thread view
SELFTEXT_SLACK = 200     # don't truncate if the overflow is smaller than this


def _url_host(url: str) -> str:
    try:
        return urlparse(url).netloc or "link"
    except ValueError:  # e.g. brackets in a botched paste -> "Invalid IPv6 URL"
        return "link"


def _comment_snippet(body: str, width: int) -> str:
    """One-line comment preview; bare link/image comments become a short tag."""
    body = (body or "").strip()
    if not body:
        return "(no text)"
    if _URL_ONLY_RE.fullmatch(body):
        return f"(link: {_url_host(body)})"
    return textwrap.shorten(body.replace("\n", " "), width=max(40, width), placeholder="...") or "(no text)"


# ── Renderers ─────────────────────────────────────────────


def _render_posts(result: dict, title: str) -> None:
    items = result.get("items", [])
    hidden_note = _nsfw_hidden_note(result)
    if not items:
        console.print(f"[yellow]No results for {title}[/yellow]")
        if hidden_note:
            console.print(f"[dim]{hidden_note}[/dim]")
        for note in _filter_notes(result):
            console.print(f"[dim]{escape(note)}[/dim]")
        # A filtered-empty page can still have a cursor — don't strand the user
        after = result.get("after")
        if after:
            console.print(f"[dim]Next page: --after {after}[/dim]")
        return

    # Full titles, no pre-truncation — rich wraps to terminal width as needed
    table = Table(title=title)
    table.add_column("Subreddit", style="cyan")
    table.add_column("Title", style="white")
    table.add_column("Author", style="green")
    table.add_column("Score", style="yellow", justify="right")
    table.add_column("Comments", style="magenta", justify="right")
    table.add_column("Age", style="dim")

    for item in items:
        item_title = escape(item.get("title", "").replace("\n", " "))
        if item.get("over_18"):
            item_title = f"[red reverse]NSFW[/red reverse] {item_title}"
        table.add_row(
            f"r/{escape(item.get('subreddit', ''))}",
            item_title,
            escape(item.get("author", "")),
            str(item.get("score", 0)),
            str(item.get("num_comments", 0)),
            _format_age(item.get("created_utc", 0)),
        )

    console.print(table)
    if hidden_note:
        console.print(f"[dim]{hidden_note}[/dim]")
    for note in _filter_notes(result):
        console.print(f"[dim]{escape(note)}[/dim]")
    after = result.get("after")
    if after:
        console.print(f"[dim]Next page: --after {after}[/dim]")


def _render_posts_markdown(result: dict, title: str) -> None:
    items = result.get("items", [])
    click.echo(f"## {title}")
    click.echo("")
    click.echo("| Subreddit | Title | Author | Score | Comments | Date |")
    click.echo("|---|---|---|---|---|---|")
    for item in items:
        item_title = _md_link(_truncate(item.get("title", ""), 80), item.get("permalink", ""))
        if item.get("over_18"):
            item_title = f"**NSFW** {item_title}"
        click.echo(
            f"| r/{_escape_md(item.get('subreddit', ''))} "
            f"| {item_title} "
            f"| {_escape_md(item.get('author', ''))} "
            f"| {item.get('score', 0)} "
            f"| {item.get('num_comments', 0)} "
            f"| {_format_date(item.get('created_utc', 0))} |"
        )
    click.echo("")
    click.echo(f"{result.get('count', len(items))} results returned")
    hidden_note = _nsfw_hidden_note(result)
    if hidden_note:
        click.echo(f"\n_{hidden_note}_")
    for note in _filter_notes(result):
        click.echo(f"\n_{note}_")
    after = result.get("after")
    if after:
        click.echo(f"\n_Next page: `--after {after}`_")


def _render_comments(result: dict, title: str) -> None:
    items = result.get("items", result.get("comments", []))
    hidden_note = _nsfw_hidden_note(result)
    if not items:
        console.print(f"[yellow]No comments found for {title}[/yellow]")
        if hidden_note:
            console.print(f"[dim]{hidden_note}[/dim]")
        for note in _filter_notes(result):
            console.print(f"[dim]{escape(note)}[/dim]")
        return

    console.print(f"[bold]{title}[/bold]\n")

    for item in items:
        body = item.get("body", "")
        if not body:
            snippet = "[dim](no text — deleted or empty)[/dim]"
        elif _URL_ONLY_RE.fullmatch(body.strip()):
            snippet = escape(_comment_snippet(body, 100))
        else:
            # Word-boundary shorten (no mid-word "for tax reaso" chops)
            preview = textwrap.shorten(body, width=300, placeholder=" …")
            snippet = escape(textwrap.fill(preview, width=100))
        link_title = item.get("link_title") or ""
        sub = item.get("subreddit", "")
        header = (
            f"[green]{escape(item.get('author') or '[deleted]')}[/green] "
            f"in [cyan]r/{escape(sub)}[/cyan] "
        )
        if link_title:
            header += f"on [white]{escape(_truncate(link_title, 50))}[/white] "
        header += f"— {_format_age(item.get('created_utc', 0))} ({item.get('score', 0)} pts)"
        console.print(header)
        console.print(snippet)
        console.print(f"[dim]{item.get('permalink', '')}[/dim]\n")

    if hidden_note:
        console.print(f"[dim]{hidden_note}[/dim]")
    for note in _filter_notes(result):
        console.print(f"[dim]{escape(note)}[/dim]")


def _render_comments_markdown(result: dict, title: str) -> None:
    items = result.get("items", result.get("comments", []))
    click.echo(f"## {title}")
    click.echo("")
    for item in items:
        body = _escape_md(item.get("body", "")[:300]) or "_(no text — deleted or empty)_"
        author = item.get("author") or "[deleted]"
        sub = item.get("subreddit", "")
        click.echo(f"**{_escape_md(author)}** in r/{sub} — {_format_date(item.get('created_utc', 0))} ({item.get('score', 0)} pts)")
        click.echo(f"> {body}")
        click.echo(f"[Link]({item.get('permalink', '')})\n")

    hidden_note = _nsfw_hidden_note(result)
    if hidden_note:
        click.echo(f"_{hidden_note}_\n")
    for note in _filter_notes(result):
        click.echo(f"_{note}_\n")


def _render_subreddits(result: dict, title: str) -> None:
    items = result.get("items", [])
    hidden_note = _nsfw_hidden_note(result)
    if not items:
        console.print(f"[yellow]No subreddits found for {title}[/yellow]")
        if hidden_note:
            console.print(f"[dim]{hidden_note}[/dim]")
        return

    # No "Active" column: Reddit stopped populating active-user counts
    table = Table(title=title)
    table.add_column("Subreddit", style="cyan")
    table.add_column("Title", style="white")
    table.add_column("Subscribers", style="yellow", justify="right")
    table.add_column("Description", style="dim")

    for item in items:
        name = f"r/{escape(item.get('name', ''))}"
        if item.get("over_18"):
            name = f"[red reverse]NSFW[/red reverse] {name}"
        table.add_row(
            name,
            escape(item.get("title", "").replace("\n", " ")),
            f"{item.get('subscribers', 0):,}",
            # Cap: some sub descriptions balloon rows to 9+ lines; full text in `info`/-j
            escape(_truncate(item.get("description", ""), 160)),
        )

    console.print(table)
    if hidden_note:
        console.print(f"[dim]{hidden_note}[/dim]")


def _render_subreddits_markdown(result: dict, title: str) -> None:
    items = result.get("items", [])
    click.echo(f"## {title}")
    click.echo("")
    click.echo("| Subreddit | Title | Subscribers | Description |")
    click.echo("|---|---|---|---|")
    for item in items:
        name = _md_link(f"r/{item.get('name', '')}", item.get("url", ""))
        if item.get("over_18"):
            name = f"**NSFW** {name}"
        click.echo(
            f"| {name} "
            f"| {_escape_md(_truncate(item.get('title', ''), 30))} "
            f"| {item.get('subscribers', 0):,} "
            f"| {_escape_md(_truncate(item.get('description', ''), 40))} |"
        )
    click.echo("")
    hidden_note = _nsfw_hidden_note(result)
    if hidden_note:
        click.echo(f"_{hidden_note}_\n")


def _format_active(info: dict) -> str:
    # Reddit returns null active-user counts these days; don't render a fake 0
    active = info.get("active_users")
    return f"{active:,}" if active is not None else "n/a (not reported by Reddit)"


def _render_subreddit_detail(info: dict) -> None:
    lines = [
        f"[bold]Name:[/bold] r/{escape(info.get('name', 'N/A'))}",
        f"[bold]Title:[/bold] {escape(info.get('title', 'N/A'))}",
        f"[bold]Subscribers:[/bold] {info.get('subscribers', 0):,}",
        f"[bold]Active Users:[/bold] {_format_active(info)}",
        f"[bold]NSFW:[/bold] {info.get('over_18', False)}",
        f"[bold]URL:[/bold] {info.get('url', 'N/A')}",
        "",
        f"[bold]Description:[/bold]\n{escape(info.get('description', 'N/A'))}",
    ]
    console.print(Panel("\n".join(lines), title="Subreddit Info", expand=False))


def _render_subreddit_detail_markdown(info: dict) -> None:
    click.echo(f"## r/{info.get('name', '')}")
    click.echo(f"- **Title:** {info.get('title', 'N/A')}")
    click.echo(f"- **Subscribers:** {info.get('subscribers', 0):,}")
    click.echo(f"- **Active Users:** {_format_active(info)}")
    click.echo(f"- **NSFW:** {info.get('over_18', False)}")
    click.echo(f"- **URL:** {info.get('url', 'N/A')}")
    click.echo(f"\n{info.get('description', 'N/A')}")


def _render_user(info: dict) -> None:
    lines = [
        f"[bold]Username:[/bold] u/{info.get('username', 'N/A')}",
        f"[bold]Link Karma:[/bold] {info.get('link_karma', 0):,}",
        f"[bold]Comment Karma:[/bold] {info.get('comment_karma', 0):,}",
        f"[bold]Total Karma:[/bold] {info.get('total_karma', 0):,}",
        f"[bold]Gold:[/bold] {info.get('is_gold', False)}",
        f"[bold]Verified:[/bold] {info.get('verified', False)}",
    ]
    console.print(Panel("\n".join(lines), title="User Profile", expand=False))


def _render_user_markdown(info: dict) -> None:
    click.echo(f"## u/{info.get('username', '')}")
    click.echo(f"- **Link Karma:** {info.get('link_karma', 0):,}")
    click.echo(f"- **Comment Karma:** {info.get('comment_karma', 0):,}")
    click.echo(f"- **Total Karma:** {info.get('total_karma', 0):,}")
    click.echo(f"- **Gold:** {info.get('is_gold', False)}")
    click.echo(f"- **Verified:** {info.get('verified', False)}")


def _render_post_detail(data: dict) -> None:
    post = data.get("post", {})
    comments = data.get("comments", [])

    post_title = escape(post.get("title", "N/A"))
    if post.get("over_18"):
        post_title = f"[red reverse]NSFW[/red reverse] {post_title}"
    lines = [
        f"[bold]Title:[/bold] {post_title}",
        f"[bold]Author:[/bold] u/{escape(post.get('author', 'N/A'))}",
        f"[bold]Subreddit:[/bold] r/{escape(post.get('subreddit', 'N/A'))}",
        f"[bold]Score:[/bold] {post.get('score', 0)} ({post.get('upvote_ratio', 0):.0%} upvoted)",
        f"[bold]Comments:[/bold] {post.get('num_comments', 0)}",
        f"[bold]URL:[/bold] {post.get('url', 'N/A')}",
        f"[bold]Permalink:[/bold] {post.get('permalink', 'N/A')}",
    ]

    selftext = post.get("selftext", "")
    if selftext:
        # Slack margin: don't chop a post that barely overflows the preview cap
        if len(selftext) <= SELFTEXT_PREVIEW + SELFTEXT_SLACK:
            lines.append(f"\n[bold]Text:[/bold]\n{escape(selftext)}")
        else:
            shown = selftext[:SELFTEXT_PREVIEW]
            lines.append(
                f"\n[bold]Text:[/bold]\n{escape(shown)}"
                f"\n[dim]… truncated ({len(selftext) - SELFTEXT_PREVIEW} more chars — use -j for full text)[/dim]"
            )

    console.print(Panel("\n".join(lines), title="Post Details", expand=False))

    # A filtered view is a flat extract, not a tree — indenting by original
    # depth would assert reply structure whose parents may be filtered out
    filtered = "filtered_out" in data
    if comments:
        op = post.get("author", "")
        if filtered:
            console.print(
                f"\n[bold]{len(comments)} comments "
                f"(filtered view — {data['filtered_out']} hidden, reply structure not shown):[/bold]\n")
        else:
            console.print(f"\n[bold]{len(comments)} comments (replies indented):[/bold]\n")
        for c in comments:
            depth = 0 if filtered else c.get("depth", 0)
            indent = "  " * depth
            author = c.get("author") or "[deleted]"
            snippet = _comment_snippet(c.get("body", ""), 120 - 2 * depth)
            score = c.get("score", 0)
            pin = "[dim]\\[pinned][/dim] " if c.get("stickied") else ""
            op_tag = " [cyan]\\[OP][/cyan]" if op and author == op and author != "[deleted]" else ""
            console.print(f"  {indent}{pin}[green]{escape(author)}[/green]{op_tag} ({score} pts): {escape(snippet)}")
    elif filtered:
        console.print(f"\n[yellow]No comments matched the filters "
                      f"({data['filtered_out']} filtered out).[/yellow]")

    more = data.get("more_count", 0)
    if more:
        console.print(f"\n[dim]... {more} more comments not fetched (raise --limit to fetch more)[/dim]")


def _render_post_detail_markdown(data: dict) -> None:
    post = data.get("post", {})
    comments = data.get("comments", [])

    title = post.get("title", "Post")
    if post.get("over_18"):
        title = f"NSFW — {title}"
    click.echo(f"## {title}")
    click.echo(f"- **Author:** u/{post.get('author', 'N/A')}")
    click.echo(f"- **Subreddit:** r/{post.get('subreddit', 'N/A')}")
    click.echo(f"- **Score:** {post.get('score', 0)}")
    click.echo(f"- **Comments:** {post.get('num_comments', 0)}")
    click.echo(f"- **URL:** {post.get('url', 'N/A')}")
    click.echo(f"- **Permalink:** {post.get('permalink', 'N/A')}")

    selftext = post.get("selftext", "")
    if selftext:
        if len(selftext) <= SELFTEXT_PREVIEW + SELFTEXT_SLACK:
            click.echo(f"\n### Text\n{selftext}")
        else:
            click.echo(f"\n### Text\n{selftext[:SELFTEXT_PREVIEW]}")
            click.echo(f"\n_… truncated ({len(selftext) - SELFTEXT_PREVIEW} more chars — use -j for full text)_")

    filtered = "filtered_out" in data
    if not comments and filtered:
        click.echo(f"\n_No comments matched the filters ({data['filtered_out']} filtered out)._")
    if comments:
        op = post.get("author", "")
        if filtered:
            click.echo(f"\n### Comments ({len(comments)} shown, "
                       f"{data['filtered_out']} filtered out — flat view)\n")
        else:
            click.echo(f"\n### Comments ({len(comments)} fetched, replies nested)\n")
        for c in comments:
            indent = "" if filtered else "  " * c.get("depth", 0)
            author = c.get("author") or "[deleted]"
            body = (c.get("body") or "").strip()
            if body and _URL_ONLY_RE.fullmatch(body):
                # Keep the URL clickable in markdown instead of a lossy host tag
                snippet = f"[link: {_url_host(body)}]({body})"
            else:
                snippet = _escape_md(_comment_snippet(body, 120))
            pin = "*(pinned)* " if c.get("stickied") else ""
            op_tag = " *(OP)*" if op and author == op and author != "[deleted]" else ""
            click.echo(f"{indent}- {pin}**{_escape_md(author)}**{op_tag} ({c.get('score', 0)} pts): {snippet}")

    more = data.get("more_count", 0)
    if more:
        click.echo(f"\n_... {more} more comments not fetched_")


# ── CLI Commands ──────────────────────────────────────────


@click.group()
@click.option("--debug", is_flag=True, help="Enable debug logging")
@click.option("--no-cache", is_flag=True, help="Disable response caching")
@click.pass_context
def main(ctx, debug, no_cache):
    """reddit — Reddit search and community intelligence tool (OAuth2 API)."""
    logging.basicConfig(level=logging.DEBUG if debug else logging.WARNING)
    ctx.ensure_object(dict)
    ctx.obj["debug"] = debug
    ctx.obj["no_cache"] = no_cache


@main.command()
@click.argument("query")
@click.option("--subreddit", "-r", default=None,
              help="Restrict to a subreddit (comma-separated for a multireddit fan-in)")
@click.option("--sort", "-s", type=click.Choice(SEARCH_SORT), default="relevance", show_default=True)
@click.option("--time", "-t", "time_filter", type=click.Choice(TIME_CHOICES), default="all", show_default=True)
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True, help="Max results per page (1-100)")
@click.option("--after", default=None, help="Pagination cursor")
@listing_options
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True, help="Output raw JSON")
@click.option("--markdown", "-m", is_flag=True, help="Output as markdown")
@output_options
@common_options
@click.pass_context
def search(ctx, query, subreddit, sort, time_filter, limit, after, pages, since, seen_name,
           include_nsfw, json_output, markdown, jsonl, fields, no_cache, debug):
    """Search Reddit posts.

    The query passes through Reddit's search operators untouched:
    author:name, self:yes, flair:"x", title:x, and boolean OR all work.
    """
    client = _client(ctx, no_cache, debug)
    result = client.paginate(client.search, pages=pages, query=query, subreddit=subreddit,
                             sort=sort, time_filter=time_filter, limit=limit, after=after,
                             include_nsfw=include_nsfw)
    if _error_exit(result, jsonl):
        return
    result = _apply_since(result, since)
    visible_for_seen = result  # suppressed-but-still-visible items refresh recency
    result = _apply_seen(result, seen_name)
    _output_posts(result, f"Search: {query}", json_output, markdown, jsonl, fields)
    _record_seen(visible_for_seen, seen_name)


@main.command(name="comments")
@click.argument("query")
@click.option("--subreddit", "-r", default=None,
              help="Restrict to a subreddit (comma-separated for a multireddit fan-in)")
@click.option("--sort", "-s", type=click.Choice(SEARCH_SORT), default="relevance", show_default=True)
@click.option("--time", "-t", "time_filter", type=click.Choice(TIME_CHOICES), default="all", show_default=True)
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True)
@click.option("--after", default=None)
@listing_options
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@output_options
@common_options
@click.pass_context
def comments_cmd(ctx, query, subreddit, sort, time_filter, limit, after, pages, since, seen_name,
                 include_nsfw, json_output, markdown, jsonl, fields, no_cache, debug):
    """Search Reddit comments."""
    client = _client(ctx, no_cache, debug)
    result = client.paginate(client.search_comments, pages=pages, query=query,
                             subreddit=subreddit, sort=sort, time_filter=time_filter,
                             limit=limit, after=after, include_nsfw=include_nsfw)
    if _error_exit(result, jsonl):
        return
    result = _apply_since(result, since)
    visible_for_seen = result  # suppressed-but-still-visible items refresh recency
    result = _apply_seen(result, seen_name)
    if not _emit_structured(result, jsonl, json_output, fields):
        if markdown:
            _render_comments_markdown(result, f"Comments: {query}")
        else:
            _render_comments(result, f"Comments: {query}")
    _record_seen(visible_for_seen, seen_name)


@main.command()
@click.argument("subreddit")
@click.option("--sort", "-s", type=click.Choice(SORT_CHOICES), default="hot", show_default=True)
@click.option("--time", "-t", "time_filter", type=click.Choice(TIME_CHOICES), default="all", show_default=True)
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True)
@click.option("--after", default=None)
@listing_options
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@output_options
@common_options
@click.pass_context
def posts(ctx, subreddit, sort, time_filter, limit, after, pages, since, seen_name,
          include_nsfw, json_output, markdown, jsonl, fields, no_cache, debug):
    """Get posts from a subreddit (hot, new, top, rising, controversial).

    SUBREDDIT may be comma-separated (a+b multireddit fan-in, server-side).
    """
    client = _client(ctx, no_cache, debug)
    result = client.paginate(client.subreddit_posts, pages=pages, subreddit=subreddit,
                             sort=sort, time_filter=time_filter, limit=limit, after=after,
                             include_nsfw=include_nsfw)
    if _error_exit(result, jsonl):
        return
    result = _apply_since(result, since)
    visible_for_seen = result  # suppressed-but-still-visible items refresh recency
    result = _apply_seen(result, seen_name)
    _output_posts(result, f"r/{subreddit} ({sort})", json_output, markdown, jsonl, fields)
    _record_seen(visible_for_seen, seen_name)


@main.command()
@click.argument("subreddit")
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@output_options
@common_options
@click.pass_context
def info(ctx, subreddit, json_output, markdown, jsonl, fields, no_cache, debug):
    """Get subreddit metadata (subscribers, description, etc.)."""
    client = _client(ctx, no_cache, debug)
    result = client.subreddit_info(subreddit)
    if _error_exit(result, jsonl):
        return
    if jsonl:
        click.echo(_jsonl_line(_project([result], fields)[0]))
    elif json_output:
        click.echo(json.dumps(_project([result], fields)[0] if fields else result, indent=2))
    elif markdown:
        _render_subreddit_detail_markdown(result)
    else:
        _render_subreddit_detail(result)


@main.command()
@click.argument("target")
@click.argument("post_id", required=False)
@click.option("--sort", "-s", type=click.Choice(["best", "top", "new", "controversial", "old", "qa"]),
              default="best", show_default=True)
@click.option("--limit", "-n", default=50, type=click.IntRange(1, 500), show_default=True,
              help="Max comments to fetch, replies included (1-500)")
@click.option("--depth", "-d", default=None, type=click.IntRange(0), help="Max reply depth (default: unlimited)")
@click.option("--no-expand", is_flag=True, help="Don't fetch 'load more' comment stubs")
@click.option("--author", "author_filter", default=None,
              help="Only show comments by this author (e.g. the OP)")
@click.option("--min-score", default=None, type=int,
              help="Only show comments with at least this score")
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@output_options
@common_options
@click.pass_context
def thread(ctx, target, post_id, sort, limit, depth, no_expand, author_filter, min_score,
           json_output, markdown, jsonl, fields, no_cache, debug):
    """Get a post with its comment thread.

    TARGET is a subreddit (with POST_ID as second argument), or a full post
    URL / redd.it shortlink / t3_ fullname / bare post id on its own.

    \b
    Examples:
      reddit thread programming abc123
      reddit thread https://reddit.com/r/programming/comments/abc123/some_title/
      reddit thread t3_abc123
    """
    client = _client(ctx, no_cache, debug)
    try:
        subreddit, pid = parse_post_reference(target, post_id)
    except ValueError as e:
        if jsonl:
            click.echo(_jsonl_line({"error": str(e), "retryable": False}))
        else:
            console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)
    result = client.post_comments(subreddit, pid, sort=sort, limit=limit,
                                  max_depth=depth, expand_more=not no_expand)
    if _error_exit(result, jsonl):
        return

    # Post-fetch filters: mine a thread for specific voices or cut low-signal noise
    if author_filter or min_score is not None:
        comments = result.get("comments", [])
        kept = comments
        if author_filter:
            kept = [c for c in kept
                    if (c.get("author") or "").lower() == author_filter.lower()]
        if min_score is not None:
            kept = [c for c in kept if c.get("score", 0) >= min_score]
        result = {**result, "comments": kept, "total": len(kept),
                  "filtered_out": len(comments) - len(kept)}

    if jsonl:
        # --fields applies to the post line too (shared keys project; the rest
        # drop silently — comment-only fields shouldn't warn here)
        post = result.get("post", {})
        post_line = _project([post], fields, warn=False)[0] if post else {}
        click.echo(_jsonl_line({"post": post_line}))
        for c in _project(result.get("comments", []), fields):
            click.echo(_jsonl_line(c))
        meta = {k: result[k] for k in ("total", "more_count", "filtered_out") if k in result}
        click.echo(_jsonl_line({"_meta": meta}))
    elif json_output:
        if fields:
            result = {**result, "comments": _project(result.get("comments", []), fields)}
        click.echo(json.dumps(result, indent=2))
    elif markdown:
        _render_post_detail_markdown(result)
    else:
        _render_post_detail(result)


@main.command()
@click.argument("username")
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@output_options
@common_options
@click.pass_context
def user(ctx, username, json_output, markdown, jsonl, fields, no_cache, debug):
    """Get user profile."""
    client = _client(ctx, no_cache, debug)
    result = client.user_about(username)
    if _error_exit(result, jsonl):
        return
    if jsonl:
        click.echo(_jsonl_line(_project([result], fields)[0]))
    elif json_output:
        click.echo(json.dumps(_project([result], fields)[0] if fields else result, indent=2))
    elif markdown:
        _render_user_markdown(result)
    else:
        _render_user(result)


@main.command(name="user-posts")
@click.argument("username")
@click.option("--sort", "-s", type=click.Choice(["hot", "new", "top", "controversial"]),
              default="new", show_default=True)
@click.option("--time", "-t", "time_filter", type=click.Choice(TIME_CHOICES), default="all", show_default=True)
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True)
@click.option("--after", default=None, help="Pagination cursor")
@listing_options
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@output_options
@common_options
@click.pass_context
def user_posts(ctx, username, sort, time_filter, limit, after, pages, since, seen_name,
               include_nsfw, json_output, markdown, jsonl, fields, no_cache, debug):
    """Get a user's submitted posts."""
    client = _client(ctx, no_cache, debug)
    result = client.paginate(client.user_posts, pages=pages, username=username, sort=sort,
                             time_filter=time_filter, limit=limit, after=after,
                             include_nsfw=include_nsfw)
    if _error_exit(result, jsonl):
        return
    result = _apply_since(result, since)
    visible_for_seen = result  # suppressed-but-still-visible items refresh recency
    result = _apply_seen(result, seen_name)
    _output_posts(result, f"u/{username} posts", json_output, markdown, jsonl, fields)
    _record_seen(visible_for_seen, seen_name)


@main.command(name="user-comments")
@click.argument("username")
@click.option("--sort", "-s", type=click.Choice(["hot", "new", "top", "controversial"]),
              default="new", show_default=True)
@click.option("--time", "-t", "time_filter", type=click.Choice(TIME_CHOICES), default="all", show_default=True)
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True)
@click.option("--after", default=None, help="Pagination cursor")
@listing_options
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@output_options
@common_options
@click.pass_context
def user_comments(ctx, username, sort, time_filter, limit, after, pages, since, seen_name,
                  json_output, markdown, jsonl, fields, no_cache, debug):
    """Get a user's comments."""
    client = _client(ctx, no_cache, debug)
    result = client.paginate(client.user_comments, pages=pages, username=username,
                             sort=sort, time_filter=time_filter, limit=limit, after=after)
    if _error_exit(result, jsonl):
        return
    result = _apply_since(result, since)
    visible_for_seen = result  # suppressed-but-still-visible items refresh recency
    result = _apply_seen(result, seen_name)
    if not _emit_structured(result, jsonl, json_output, fields):
        if markdown:
            _render_comments_markdown(result, f"u/{username} comments")
        else:
            _render_comments(result, f"u/{username} comments")
    _record_seen(visible_for_seen, seen_name)


@main.command(name="find-subs")
@click.argument("query")
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True)
@click.option("--after", default=None, help="Pagination cursor")
@click.option("--pages", default=1, type=click.IntRange(1, 10), show_default=True,
              help="Auto-follow pagination cursors, merging up to N pages")
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@output_options
@common_options
@click.pass_context
def find_subs(ctx, query, limit, after, pages, include_nsfw, json_output, markdown, jsonl, fields, no_cache, debug):
    """Search for subreddits by name/description."""
    client = _client(ctx, no_cache, debug)
    result = client.paginate(client.search_subreddits, pages=pages, query=query,
                             limit=limit, after=after, include_nsfw=include_nsfw)
    if _error_exit(result, jsonl):
        return
    if not _emit_structured(result, jsonl, json_output, fields):
        if markdown:
            _render_subreddits_markdown(result, f"Subreddits: {query}")
        else:
            _render_subreddits(result, f"Subreddits: {query}")


@main.command(name="popular")
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True)
@click.option("--after", default=None, help="Pagination cursor")
@listing_options
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@output_options
@common_options
@click.pass_context
def popular(ctx, limit, after, pages, since, seen_name, include_nsfw, json_output, markdown,
            jsonl, fields, no_cache, debug):
    """Get popular posts from across Reddit."""
    client = _client(ctx, no_cache, debug)
    result = client.paginate(client.popular_posts, pages=pages, limit=limit, after=after,
                             include_nsfw=include_nsfw)
    if _error_exit(result, jsonl):
        return
    result = _apply_since(result, since)
    visible_for_seen = result  # suppressed-but-still-visible items refresh recency
    result = _apply_seen(result, seen_name)
    _output_posts(result, "Popular", json_output, markdown, jsonl, fields)
    _record_seen(visible_for_seen, seen_name)


@main.command(name="popular-subs")
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True)
@click.option("--after", default=None, help="Pagination cursor")
@click.option("--pages", default=1, type=click.IntRange(1, 10), show_default=True,
              help="Auto-follow pagination cursors, merging up to N pages")
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@output_options
@common_options
@click.pass_context
def popular_subs(ctx, limit, after, pages, include_nsfw, json_output, markdown, jsonl, fields, no_cache, debug):
    """Get popular subreddits."""
    client = _client(ctx, no_cache, debug)
    result = client.paginate(client.popular_subreddits, pages=pages, limit=limit, after=after,
                             include_nsfw=include_nsfw)
    if _error_exit(result, jsonl):
        return
    if not _emit_structured(result, jsonl, json_output, fields):
        if markdown:
            _render_subreddits_markdown(result, "Popular Subreddits")
        else:
            _render_subreddits(result, "Popular Subreddits")


@main.command()
@click.argument("subreddit")
@click.option("--time", "-t", "time_filter", type=click.Choice(TIME_CHOICES), default="week", show_default=True)
@click.option("--limit", "-n", default=15, type=LIMIT_RANGE, show_default=True, help="Top posts to list")
@click.option("--threads", "-T", "thread_count", default=3, type=click.IntRange(0, 10), show_default=True,
              help="Top threads to excerpt comments from")
@click.option("--thread-comments", default=10, type=click.IntRange(1, 50), show_default=True,
              help="Comments to fetch per excerpted thread")
@click.option("--query", "-q", default=None, help="Also run a post search within the subreddit")
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True)
@common_options
@click.pass_context
def digest(ctx, subreddit, time_filter, limit, thread_count, thread_comments, query,
           include_nsfw, json_output, no_cache, debug):
    """One-shot recon: info + top posts + top-thread excerpts (+ optional search).

    Replaces the usual 10-15 command opening sweep of a research session with
    a single markdown document (or -j for the full structured data).
    Cost: 2 + THREADS (+1 with --query) API requests.
    """
    client = _client(ctx, no_cache, debug)

    info_res = client.subreddit_info(subreddit)
    if _error_exit(info_res):
        return
    posts_res = client.subreddit_posts(subreddit, sort="top", time_filter=time_filter,
                                       limit=limit, include_nsfw=include_nsfw)
    if _error_exit(posts_res):
        return

    top_posts = posts_res.get("items", [])
    threads, skipped = [], []
    for p in [p for p in top_posts if not p.get("stickied")][:thread_count]:
        t = client.post_comments(p.get("subreddit") or subreddit, p["id"],
                                 sort="top", limit=thread_comments, expand_more=False)
        if "error" in t:
            skipped.append({"id": p["id"], "title": p.get("title", ""), "error": t["error"]})
        else:
            threads.append(t)

    search_res = None
    if query:
        search_res = client.search(query, subreddit=subreddit, sort="relevance",
                                   time_filter=time_filter, limit=10,
                                   include_nsfw=include_nsfw)
        if "error" in search_res:
            search_res = {"items": [], "error": search_res["error"]}

    if json_output:
        out = {"info": info_res, "top_posts": posts_res, "threads": threads}
        if skipped:
            out["skipped_threads"] = skipped
        if search_res is not None:
            out["search"] = search_res
        click.echo(json.dumps(out, indent=2))
        return

    # Markdown document (default — a digest is a report, not a table)
    click.echo(f"# r/{info_res.get('name', subreddit)} digest — top/{time_filter}")
    click.echo(f"\n{info_res.get('subscribers', 0):,} subscribers — {info_res.get('description', '')}\n")
    _render_posts_markdown(posts_res, f"Top posts ({time_filter})")
    for t in threads:
        click.echo("\n---\n")
        _render_post_detail_markdown(t)
    for s in skipped:
        click.echo(f"\n_Thread excerpt for \"{_escape_md(_truncate(s['title'], 60))}\" "
                   f"skipped: {s['error']}_")
    if search_res is not None:
        click.echo("\n---\n")
        if search_res.get("error"):
            click.echo(f"_Search failed: {search_res['error']}_")
        else:
            _render_posts_markdown(search_res, f"Search: {query}")


@main.command()
@click.option("--clear", "clear_name", default=None, metavar="NAME",
              help="Clear one seen-store (use ALL to clear every store)")
@click.pass_context
def seen(ctx, clear_name):
    """List or clear --seen delta-tracking stores."""
    store = SeenStore()
    if clear_name:
        if clear_name == "ALL":
            count = store.clear()
            console.print(f"[green]Cleared {count} seen store(s)[/green]")
        else:
            removed = store.clear(clear_name)
            if removed:
                console.print(f"[green]Cleared seen store '{clear_name}'[/green]")
            else:
                console.print(f"[yellow]No seen store named '{clear_name}'[/yellow]")
        return
    names = store.names()
    if not names:
        console.print("[dim]No seen stores yet — use --seen NAME on a listing command.[/dim]")
        return
    for name, count in sorted(names.items()):
        console.print(f"{name}: {count} ids tracked")


@main.command(name="clear-cache")
@common_options
@click.pass_context
def clear_cache(ctx, no_cache, debug):
    """Clear local response cache."""
    client = _client(ctx, no_cache, debug)
    if not client.cache:
        console.print("[yellow]Cache is disabled for this run (--no-cache).[/yellow]")
        return
    removed = client.cache.clear()
    console.print(f"[green]Cleared {removed} cached response files[/green]")


# ── Helpers ───────────────────────────────────────────────


def _output_posts(result: dict, title: str, json_output: bool, markdown: bool,
                  jsonl: bool = False, fields: str = None) -> None:
    if _emit_structured(result, jsonl, json_output, fields):
        return
    if markdown:
        _render_posts_markdown(result, title)
    else:
        _render_posts(result, title)


if __name__ == "__main__":
    main()
