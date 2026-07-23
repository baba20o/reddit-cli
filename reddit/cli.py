"""CLI entry point for Reddit search via OAuth2 API."""

import json
import logging
import re
import textwrap
from datetime import datetime, timezone
from urllib.parse import urlparse

import click
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from reddit.api import RedditClient, SORT_CHOICES, TIME_CHOICES, parse_post_reference

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


def _client(ctx, no_cache: bool = False, debug: bool = False) -> "RedditClient":
    """Build the API client, honoring group-level and command-level flags."""
    if debug or ctx.obj.get("debug"):
        logging.getLogger().setLevel(logging.DEBUG)
    use_cache = not (no_cache or ctx.obj.get("no_cache"))
    return RedditClient(use_cache=use_cache)


def _error_exit(result: dict) -> bool:
    if "error" in result:
        console.print(f"[red]Error:[/red] {result['error']}")
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

    if comments:
        op = post.get("author", "")
        console.print(f"\n[bold]{len(comments)} comments (replies indented):[/bold]\n")
        for c in comments:
            depth = c.get("depth", 0)
            indent = "  " * depth
            author = c.get("author") or "[deleted]"
            snippet = _comment_snippet(c.get("body", ""), 120 - 2 * depth)
            score = c.get("score", 0)
            pin = "[dim]\\[pinned][/dim] " if c.get("stickied") else ""
            op_tag = " [cyan]\\[OP][/cyan]" if op and author == op and author != "[deleted]" else ""
            console.print(f"  {indent}{pin}[green]{escape(author)}[/green]{op_tag} ({score} pts): {escape(snippet)}")

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

    if comments:
        op = post.get("author", "")
        click.echo(f"\n### Comments ({len(comments)} fetched, replies nested)\n")
        for c in comments:
            indent = "  " * c.get("depth", 0)
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
@click.option("--subreddit", "-r", default=None, help="Restrict to a subreddit")
@click.option("--sort", "-s", type=click.Choice(SEARCH_SORT), default="relevance", show_default=True)
@click.option("--time", "-t", "time_filter", type=click.Choice(TIME_CHOICES), default="all", show_default=True)
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True, help="Max results (1-100)")
@click.option("--after", default=None, help="Pagination cursor")
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True, help="Output raw JSON")
@click.option("--markdown", "-m", is_flag=True, help="Output as markdown")
@common_options
@click.pass_context
def search(ctx, query, subreddit, sort, time_filter, limit, after, include_nsfw, json_output, markdown, no_cache, debug):
    """Search Reddit posts."""
    client = _client(ctx, no_cache, debug)
    result = client.search(query, subreddit=subreddit, sort=sort, time_filter=time_filter,
                          limit=limit, after=after, include_nsfw=include_nsfw)
    if _error_exit(result):
        return
    _output_posts(result, f"Search: {query}", json_output, markdown)


@main.command(name="comments")
@click.argument("query")
@click.option("--subreddit", "-r", default=None, help="Restrict to a subreddit")
@click.option("--sort", "-s", type=click.Choice(SEARCH_SORT), default="relevance", show_default=True)
@click.option("--time", "-t", "time_filter", type=click.Choice(TIME_CHOICES), default="all", show_default=True)
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True)
@click.option("--after", default=None)
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@common_options
@click.pass_context
def comments_cmd(ctx, query, subreddit, sort, time_filter, limit, after, include_nsfw, json_output, markdown, no_cache, debug):
    """Search Reddit comments."""
    client = _client(ctx, no_cache, debug)
    result = client.search_comments(query, subreddit=subreddit, sort=sort,
                                    time_filter=time_filter, limit=limit, after=after,
                                    include_nsfw=include_nsfw)
    if _error_exit(result):
        return
    if json_output:
        click.echo(json.dumps(result, indent=2))
    elif markdown:
        _render_comments_markdown(result, f"Comments: {query}")
    else:
        _render_comments(result, f"Comments: {query}")


@main.command()
@click.argument("subreddit")
@click.option("--sort", "-s", type=click.Choice(SORT_CHOICES), default="hot", show_default=True)
@click.option("--time", "-t", "time_filter", type=click.Choice(TIME_CHOICES), default="all", show_default=True)
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True)
@click.option("--after", default=None)
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@common_options
@click.pass_context
def posts(ctx, subreddit, sort, time_filter, limit, after, include_nsfw, json_output, markdown, no_cache, debug):
    """Get posts from a subreddit (hot, new, top, rising, controversial)."""
    client = _client(ctx, no_cache, debug)
    result = client.subreddit_posts(subreddit, sort=sort, time_filter=time_filter,
                                    limit=limit, after=after, include_nsfw=include_nsfw)
    if _error_exit(result):
        return
    _output_posts(result, f"r/{subreddit} ({sort})", json_output, markdown)


@main.command()
@click.argument("subreddit")
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@common_options
@click.pass_context
def info(ctx, subreddit, json_output, markdown, no_cache, debug):
    """Get subreddit metadata (subscribers, description, etc.)."""
    client = _client(ctx, no_cache, debug)
    result = client.subreddit_info(subreddit)
    if _error_exit(result):
        return
    if json_output:
        click.echo(json.dumps(result, indent=2))
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
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@common_options
@click.pass_context
def thread(ctx, target, post_id, sort, limit, depth, no_expand, json_output, markdown, no_cache, debug):
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
        console.print(f"[red]Error:[/red] {e}")
        raise SystemExit(1)
    result = client.post_comments(subreddit, pid, sort=sort, limit=limit,
                                  max_depth=depth, expand_more=not no_expand)
    if _error_exit(result):
        return
    if json_output:
        click.echo(json.dumps(result, indent=2))
    elif markdown:
        _render_post_detail_markdown(result)
    else:
        _render_post_detail(result)


@main.command()
@click.argument("username")
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@common_options
@click.pass_context
def user(ctx, username, json_output, markdown, no_cache, debug):
    """Get user profile."""
    client = _client(ctx, no_cache, debug)
    result = client.user_about(username)
    if _error_exit(result):
        return
    if json_output:
        click.echo(json.dumps(result, indent=2))
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
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@common_options
@click.pass_context
def user_posts(ctx, username, sort, time_filter, limit, include_nsfw, json_output, markdown, no_cache, debug):
    """Get a user's submitted posts."""
    client = _client(ctx, no_cache, debug)
    result = client.user_posts(username, sort=sort, time_filter=time_filter, limit=limit,
                               include_nsfw=include_nsfw)
    if _error_exit(result):
        return
    _output_posts(result, f"u/{username} posts", json_output, markdown)


@main.command(name="user-comments")
@click.argument("username")
@click.option("--sort", "-s", type=click.Choice(["hot", "new", "top", "controversial"]),
              default="new", show_default=True)
@click.option("--time", "-t", "time_filter", type=click.Choice(TIME_CHOICES), default="all", show_default=True)
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True)
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@common_options
@click.pass_context
def user_comments(ctx, username, sort, time_filter, limit, json_output, markdown, no_cache, debug):
    """Get a user's comments."""
    client = _client(ctx, no_cache, debug)
    result = client.user_comments(username, sort=sort, time_filter=time_filter, limit=limit)
    if _error_exit(result):
        return
    if json_output:
        click.echo(json.dumps(result, indent=2))
    elif markdown:
        _render_comments_markdown(result, f"u/{username} comments")
    else:
        _render_comments(result, f"u/{username} comments")


@main.command(name="find-subs")
@click.argument("query")
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True)
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@common_options
@click.pass_context
def find_subs(ctx, query, limit, include_nsfw, json_output, markdown, no_cache, debug):
    """Search for subreddits by name/description."""
    client = _client(ctx, no_cache, debug)
    result = client.search_subreddits(query, limit=limit, include_nsfw=include_nsfw)
    if _error_exit(result):
        return
    if json_output:
        click.echo(json.dumps(result, indent=2))
    elif markdown:
        _render_subreddits_markdown(result, f"Subreddits: {query}")
    else:
        _render_subreddits(result, f"Subreddits: {query}")


@main.command(name="popular")
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True)
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@common_options
@click.pass_context
def popular(ctx, limit, include_nsfw, json_output, markdown, no_cache, debug):
    """Get popular posts from across Reddit."""
    client = _client(ctx, no_cache, debug)
    result = client.popular_posts(limit=limit, include_nsfw=include_nsfw)
    if _error_exit(result):
        return
    _output_posts(result, "Popular", json_output, markdown)


@main.command(name="popular-subs")
@click.option("--limit", "-n", default=25, type=LIMIT_RANGE, show_default=True)
@NSFW_FLAG
@click.option("--json-output", "-j", is_flag=True)
@click.option("--markdown", "-m", is_flag=True)
@common_options
@click.pass_context
def popular_subs(ctx, limit, include_nsfw, json_output, markdown, no_cache, debug):
    """Get popular subreddits."""
    client = _client(ctx, no_cache, debug)
    result = client.popular_subreddits(limit=limit, include_nsfw=include_nsfw)
    if _error_exit(result):
        return
    if json_output:
        click.echo(json.dumps(result, indent=2))
    elif markdown:
        _render_subreddits_markdown(result, "Popular Subreddits")
    else:
        _render_subreddits(result, "Popular Subreddits")


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


def _output_posts(result: dict, title: str, json_output: bool, markdown: bool) -> None:
    if json_output:
        click.echo(json.dumps(result, indent=2))
    elif markdown:
        _render_posts_markdown(result, title)
    else:
        _render_posts(result, title)


if __name__ == "__main__":
    main()
