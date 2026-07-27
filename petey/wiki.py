import os
import asyncio
from pathlib import Path
import discord

# Local wiki directory (relative to this file)
WIKI_DIR = Path(__file__).parent / "wiki"
WIKI_DIR.mkdir(exist_ok=True)

MAX_MSG_CHUNK = 1900  # leave room for markdown/embeds

# Optional remote wiki base (set to the wiki you mentioned)
# If you want to enable remote lookups, set this to 'https://arcraiders.wiki'
REMOTE_WIKI_BASE: str | None = "https://arcraiders.wiki"

# Enforce read-only mode: set to True to prevent any creation or editing of local wiki files via the bot.
# The module will only read local files and optionally query a remote wiki. No write operations will be performed.
READ_ONLY = True


def _safe_page_path(name: str) -> Path:
    """Return a normalized safe path inside WIKI_DIR for a page name.
    Prevents directory traversal by resolving and ensuring the path is inside WIKI_DIR.
    """
    # Normalize filename: allow only alphanum, dash, underscore, spaces -> convert to underscore
    safe_name = "".join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in name).strip()
    if not safe_name:
        safe_name = "untitled"
    # try common extensions
    candidates = [WIKI_DIR / (safe_name + ext) for ext in ('.md', '.txt', '.markdown')]
    candidates.append(WIKI_DIR / safe_name)  # fallback, maybe user included extension
    for p in candidates:
        try:
            p_resolved = p.resolve()
        except Exception:
            continue
        try:
            if WIKI_DIR.resolve() in p_resolved.parents or p_resolved == WIKI_DIR.resolve():
                return p_resolved
        except Exception:
            continue
    # fallback path (create as .md)
    return (WIKI_DIR / (safe_name + '.md')).resolve()


def list_pages() -> list:
    """Return a list of wiki page names found in the wiki directory."""
    pages = []
    for p in WIKI_DIR.iterdir():
        if p.is_file():
            pages.append(p.name)
    return sorted(pages)


def search_pages(query: str, limit: int = 10) -> list:
    """Simple case-insensitive substring search over page filenames and first-line of file."""
    q = query.lower()
    results = []
    for p in WIKI_DIR.iterdir():
        if not p.is_file():
            continue
        name = p.name
        if q in name.lower():
            results.append(name)
            continue
        # check first line/title
        try:
            with p.open('r', encoding='utf-8') as fh:
                first = fh.readline().strip()
            if q in first.lower():
                results.append(name)
        except Exception:
            continue
        if len(results) >= limit:
            break
    return results[:limit]


def read_page(name: str) -> str:
    """Read the contents of a wiki page. Returns empty string if not found."""
    path = _safe_page_path(name)
    if not path.exists():
        return ""
    try:
        with path.open('r', encoding='utf-8') as fh:
            return fh.read()
    except Exception:
        return ""


def _api_url(base: str) -> str:
    base = base.rstrip('/')
    return base + '/api.php'


def _http_get_json(url: str, params: dict | None = None, timeout: int = 10):
    """Blocking helper to perform a GET and parse JSON using urllib (safe fallback without extra deps)."""
    import urllib.parse
    import urllib.request
    import json

    if params:
        url = url + ('?' + urllib.parse.urlencode(params))
    req = urllib.request.Request(url, headers={
        'User-Agent': 'petey-wiki-bot/1.0 (+https://example.local)'
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = resp.read()
    return json.loads(data.decode('utf-8', errors='replace'))


async def remote_search(query: str, limit: int = 10, base: str | None = None) -> list:
    """Search the remote MediaWiki (api.php) for titles matching query.
    Returns a list of titles (strings)."""
    if not base:
        base = REMOTE_WIKI_BASE
    if not base:
        return []
    api = _api_url(base)
    params = {
        'action': 'query',
        'format': 'json',
        'list': 'search',
        'srsearch': query,
        'srlimit': limit,
    }
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: _http_get_json(api, params))
    except Exception:
        return []
    titles = []
    try:
        for item in data.get('query', {}).get('search', []):
            titles.append(item.get('title'))
    except Exception:
        return []
    return titles


async def remote_get_page(title: str, base: str | None = None) -> str:
    """Get plaintext extract of a remote wiki page using the MediaWiki API extracts prop.
    Returns plaintext or empty string on failure."""
    if not base:
        base = REMOTE_WIKI_BASE
    if not base:
        return ""
    api = _api_url(base)
    params = {
        'action': 'query',
        'format': 'json',
        'prop': 'extracts',
        'explaintext': 1,
        'titles': title,
        'redirects': 1,
    }
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: _http_get_json(api, params))
    except Exception:
        return ""
    try:
        pages = data.get('query', {}).get('pages', {})
        # pages is a dict keyed by pageid
        for pageid, page in pages.items():
            extract = page.get('extract', '')
            if extract:
                return extract
    except Exception:
        return ""
    return ""


async def _send_long(ctx, text: str):
    """Send long text split into chunks to the ctx channel (handles code block formatting)."""
    chunks = []
    while text:
        chunk = text[:MAX_MSG_CHUNK]
        # try to cut at newline
        last_nl = chunk.rfind('\n')
        if last_nl > int(MAX_MSG_CHUNK * 0.6):
            chunk = chunk[:last_nl]
        chunks.append(chunk)
        text = text[len(chunk):]
    for c in chunks:
        # wrap in codeblock for readability; build the backticks programmatically to avoid parser edge-cases
        codeblock = "`" * 3
        payload = codeblock + "\n" + c + "\n" + codeblock
        try:
            await ctx.send(payload)
        except Exception:
            # fallback: send plain
            await ctx.send(c)


async def wiki_command(ctx, *, topic: str = None):
    """Discord command handler: !wiki <topic>
    - If no topic: list pages (first 20)
    - If exact match page exists: send page contents
    - If no exact match: list search results (up to 10)
    """
    if not topic:
        pages = list_pages()
        if not pages:
            await ctx.send("No local wiki pages found. This wiki is read-only; add files directly on disk if you control the host.")
            return
        await ctx.send("Wiki pages:\n" + "\n".join(pages[:50]))
        return

    # Try exact filename match first
    exact = _safe_page_path(topic)
    if exact.exists():
        content = read_page(topic)
        if not content:
            await ctx.send("Page is empty or could not be read.")
            return
        await _send_long(ctx, content)
        return

    # Search for matches
    matches = search_pages(topic)
    if not matches:
        # Try remote wiki if configured
        if REMOTE_WIKI_BASE:
            remote_matches = await remote_search(topic)
            if not remote_matches:
                await ctx.send(f"No wiki pages matching '{topic}' were found (local or remote).")
                return
            # If only one remote match, fetch and show it
            if len(remote_matches) == 1:
                content = await remote_get_page(remote_matches[0])
                if content:
                    await _send_long(ctx, content)
                    await ctx.send(f"Source: {REMOTE_WIKI_BASE}/wiki/{remote_matches[0].replace(' ', '_')}")
                    return
                else:
                    await ctx.send(f"Found page {remote_matches[0]} on remote wiki but could not retrieve content. URL: {REMOTE_WIKI_BASE}/wiki/{remote_matches[0].replace(' ', '_')}")
                    return
            # multiple remote matches: list them with links
            embed = discord.Embed(title=f"Remote wiki results for '{topic}'", color=discord.Color.blue())
            embed.description = '\n'.join(f"{i+1}. [{m}]({REMOTE_WIKI_BASE.rstrip('/')}/wiki/{m.replace(' ', '_')})" for i, m in enumerate(remote_matches[:10]))
            await ctx.send(embed=embed)
            return
        else:
            await ctx.send(f"No wiki pages matching '{topic}' were found.")
            return
    # If one match, show it
    if len(matches) == 1:
        content = read_page(matches[0])
        if not content:
            await ctx.send("Page found but empty or unreadable.")
            return
        await _send_long(ctx, content)
        return
    # multiple matches: list them
    embed = discord.Embed(title=f"Wiki results for '{topic}'", color=discord.Color.blue())
    embed.description = '\n'.join(f"{i+1}. {m}" for i, m in enumerate(matches))
    await ctx.send(embed=embed)


def _extract_keywords(text: str, min_len: int = 3) -> list:
    """Very small keyword extractor: split words, remove common stopwords and short tokens."""
    stopwords = {
        'the', 'a', 'an', 'and', 'or', 'of', 'in', 'on', 'for', 'to', 'how', 'what', 'is', 'are', 'i', 'you', 'my',
        'need', 'do', 'does', 'do', 'me', 'please', 'please', 'by', 'with'
    }
    toks = [w.strip(".,()?\"'`).:;") for w in text.lower().split()]
    kws = [w for w in toks if w and w not in stopwords and len(w) >= min_len]
    # return unique preserving order
    seen = set()
    out = []
    for k in kws:
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _split_sentences(text: str) -> list:
    import re
    # naive sentence splitter
    parts = re.split(r'(?<=[.!?])\s+', text)
    return [p.strip() for p in parts if p.strip()]


def _score_sentence(sentence: str, keywords: list) -> int:
    s = sentence.lower()
    score = 0
    for k in keywords:
        if k in s:
            score += 1
    return score


async def answer_question(ctx, question: str, max_pages: int = 5):
    """Attempt to answer a natural-language question by searching the remote wiki and extracting the most relevant sentence.

    Behavior:
    - Extract keywords from the question
    - Search remote wiki for titles matching the question
    - Fetch page extracts for top matches (concurrently)
    - Score sentences for keyword overlap and return the best snippet with a source link
    """
    if not question or not question.strip():
        await ctx.send("Please ask a question about the wiki, for example: `how many sentinel firing cores do I need to upgrade my gunsmith table`")
        return

    keywords = _extract_keywords(question)
    if not keywords:
        await ctx.send("Couldn't extract keywords from your question. Try a shorter phrase or include item names.")
        return

    # First try remote search (preferred for arcraiders.wiki)
    titles = []
    if REMOTE_WIKI_BASE:
        try:
            titles = await remote_search(question, limit=max_pages)
        except Exception:
            titles = []

    # Fallback to local search if remote yields nothing
    if not titles:
        titles = search_pages(question, limit=max_pages)

    if not titles:
        await ctx.send("No wiki pages found matching your question.")
        return

    # Fetch page extracts concurrently
    coros = []
    for t in titles[:max_pages]:
        # prefer remote_get_page if remote base configured
        if REMOTE_WIKI_BASE:
            coros.append(remote_get_page(t))
        else:
            coros.append(asyncio.get_event_loop().run_in_executor(None, lambda tt=t: read_page(tt)))
    pages = await asyncio.gather(*coros, return_exceptions=True)

    best = None
    best_score = 0
    best_title = None
    best_snippet = None

    for title, content in zip(titles[:max_pages], pages):
        if isinstance(content, Exception) or not content:
            continue
        sentences = _split_sentences(content)
        for s in sentences:
            sc = _score_sentence(s, keywords)
            if sc > best_score:
                best_score = sc
                best = content
                best_title = title
                best_snippet = s

    if best_score > 0 and best_snippet:
        # prepare a short reply with the snippet and a link
        src = ''
        if REMOTE_WIKI_BASE:
            src = REMOTE_WIKI_BASE.rstrip('/') + '/wiki/' + best_title.replace(' ', '_')
        embed = discord.Embed(title=f"Answer from {best_title}", description=best_snippet, color=discord.Color.green())
        if src:
            embed.add_field(name='Source', value=src, inline=False)
        await ctx.send(embed=embed)
        return

    # If no sentence scored, fall back to showing the top page intro
    top_title = titles[0]
    top_content = None
    try:
        if REMOTE_WIKI_BASE:
            top_content = await remote_get_page(top_title)
        else:
            top_content = read_page(top_title)
    except Exception:
        top_content = None

    if top_content:
        snippet = top_content.split('\n\n', 1)[0][:800]
        src = REMOTE_WIKI_BASE.rstrip('/') + '/wiki/' + top_title.replace(' ', '_') if REMOTE_WIKI_BASE else None
        embed = discord.Embed(title=f"Found page: {top_title}", description=snippet, color=discord.Color.blue())
        if src:
            embed.add_field(name='Source', value=src, inline=False)
        await ctx.send(embed=embed)
        return

    await ctx.send("Sorry, I couldn't find a good answer on the wiki.")


# Read-only mode: this module does not create or edit local wiki files.
