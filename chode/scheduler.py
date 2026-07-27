"""
scheduler.py — Chode Scheduled Task Runner
-------------------------------------------
Three task types:
  search   — web search + LLM summary, always posts
  fetch    — parse RSS feeds, post only NEW items (deduplication via hash)
  summarize — read today's Discord messages, post an LLM digest

Timezone-aware. Checks every 60 seconds.
"""

import asyncio
import hashlib
from chode import db as sqlite3
import json
import datetime
import os
import pytz

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH  = os.path.join(BASE_DIR, "memories.db")

# ── DB init ───────────────────────────────────────────────────────────────────
def init_tasks_db():
    try:
        conn = sqlite3.connect()
        c = conn.cursor()

        # Main tasks table
        c.execute("""
            CREATE TABLE IF NOT EXISTS tasks (
                id              SERIAL PRIMARY KEY,
                server_id       VARCHAR(64) NOT NULL,
                channel_id      VARCHAR(64) NOT NULL,
                name            VARCHAR(256) NOT NULL,
                task_type       VARCHAR(64) NOT NULL DEFAULT 'search',
                search_query    TEXT NOT NULL DEFAULT '',
                sources         TEXT NOT NULL DEFAULT '[]',
                suppress_embeds INTEGER NOT NULL DEFAULT 0,
                days            VARCHAR(256) NOT NULL DEFAULT '[]',
                hour            INTEGER NOT NULL DEFAULT 8,
                minute          INTEGER NOT NULL DEFAULT 0,
                timezone        VARCHAR(64) NOT NULL DEFAULT 'America/Chicago',
                enabled         INTEGER NOT NULL DEFAULT 1,
                last_run        VARCHAR(64),
                created_by      VARCHAR(64),
                force_run       INTEGER NOT NULL DEFAULT 0,
                lookback_days   INTEGER NOT NULL DEFAULT 1
            )
        """)

        # Seen items for FETCH deduplication
        c.execute("""
            CREATE TABLE IF NOT EXISTS task_seen_items (
                id        SERIAL PRIMARY KEY,
                task_id   INTEGER NOT NULL,
                item_hash VARCHAR(256) NOT NULL,
                seen_at   VARCHAR(64) NOT NULL,
                UNIQUE(task_id, item_hash)
            )
        """)
        conn.commit()
        conn.close()
        print("[SCHEDULER] Tasks DB ready.")
    except Exception as e:
        print(f"[SCHEDULER] Failed to initialise tasks DB: {e}")


# ── Schedule helpers ──────────────────────────────────────────────────────────
DAY_MAP = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}

def _is_due(task: dict, now_utc: datetime.datetime) -> bool:
    try:
        tz  = pytz.timezone(task["timezone"])
        now = now_utc.astimezone(tz)
        days = json.loads(task["days"])
        if not any(DAY_MAP.get(d) == now.weekday() for d in days):
            return False
        if now.hour != task["hour"] or now.minute != task["minute"]:
            return False
        if task["last_run"]:
            last = datetime.datetime.fromisoformat(task["last_run"])
            last_local = last.astimezone(tz)
            if (last_local.date() == now.date() and
                    last_local.hour == now.hour and
                    last_local.minute == now.minute):
                return False
        return True
    except Exception as e:
        print(f"[SCHEDULER] _is_due error for task {task['id']}: {e}")
        return False


def _get_enabled_tasks() -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM tasks WHERE enabled=1").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[SCHEDULER] Failed to load tasks: {e}")
        return []


def _get_all_force_run_tasks() -> list:
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM tasks WHERE force_run=1").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[SCHEDULER] Failed to load force_run tasks: {e}")
        return []


def _clear_force_run(task_id: int):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET force_run=0 WHERE id=?", (task_id,))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[SCHEDULER] Failed to clear force_run: {e}")


def _mark_last_run(task_id: int):
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute("UPDATE tasks SET last_run=? WHERE id=?",
                     (datetime.datetime.utcnow().isoformat(), task_id))
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[SCHEDULER] Failed to update last_run: {e}")


# ── FETCH helpers (RSS dedup) ─────────────────────────────────────────────────
def _item_hash(entry) -> str:
    """Stable hash from RSS entry link or title."""
    key = getattr(entry, "link", "") or getattr(entry, "title", "") or str(entry)
    return hashlib.sha1(key.encode()).hexdigest()


def _is_seen(task_id: int, h: str) -> bool:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute(
        "SELECT 1 FROM task_seen_items WHERE task_id=? AND item_hash=?", (task_id, h)
    ).fetchone()
    conn.close()
    return row is not None


def _mark_seen(task_id: int, h: str):
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT OR IGNORE INTO task_seen_items (task_id, item_hash, seen_at) VALUES (?,?,?)",
        (task_id, h, datetime.datetime.utcnow().isoformat())
    )
    conn.commit()
    conn.close()


# ── Shared post helper ────────────────────────────────────────────────────────
async def _send(channel, text: str, suppress_embeds: bool = False):
    from chode import utils as _utils
    if len(text) > 2000:
        await _utils.send_long_message(channel, text)
        return
    try:
        if suppress_embeds:
            await channel.send(text, suppress_embeds=True)
        else:
            await channel.send(text)
    except TypeError:
        msg = await channel.send(text)
        if suppress_embeds:
            try:
                await msg.edit(suppress=True)
            except Exception:
                pass


# ══════════════════════════════════════════════════════════════════════════════
# Task type: SEARCH
# ══════════════════════════════════════════════════════════════════════════════
async def _run_search_task(bot, task: dict, channel):
    from chode import utils, gemini_api

    try:
        results = await utils.perform_web_search(task["search_query"])
    except Exception as e:
        await channel.send(f"⏰ **{task['name']}** — Search failed: {e}")
        return

    if not results or not results.strip():
        await channel.send(f"⏰ **{task['name']}** — No results for: `{task['search_query']}`")
        return

    prompt = (
        f"System: You are Petey, a helpful assistant.\n"
        f"A scheduled task '{task['name']}' just ran a web search for '{task['search_query']}'.\n"
        f"Summarize the most interesting findings as a concise Discord post. "
        f"Use bullet points. Include notable links. Be direct.\n\n"
        f"== Search Results ==\n{results}\n\nPetey's Report:"
    )
    try:
        response = await asyncio.to_thread(gemini_api.chat_completion, prompt)
    except Exception as e:
        response = results[:1500]

    await _send(channel, f"🔍 **{task['name']}**\n{response}")


# ══════════════════════════════════════════════════════════════════════════════
# Task type: FETCH (RSS)
# ══════════════════════════════════════════════════════════════════════════════
async def _run_fetch_task(bot, task: dict, channel):
    from chode import gemini_api

    sources = json.loads(task.get("sources") or "[]")
    suppress_embeds = bool(task.get("suppress_embeds") or 0)
    if not sources:
        await channel.send(f"📡 **{task['name']}** — No RSS sources configured.")
        return

    try:
        import feedparser
    except ImportError:
        await channel.send(f"📡 **{task['name']}** — `feedparser` not installed. Run: `pip install feedparser`")
        return

    new_items = []

    for url in sources:
        try:
            feed = await asyncio.to_thread(feedparser.parse, url)
            feed_title = feed.feed.get("title", url)
            for entry in feed.entries[:20]:   # check at most 20 newest
                h = _item_hash(entry)
                if _is_seen(task["id"], h):
                    continue
                _mark_seen(task["id"], h)
                title = getattr(entry, "title", "(no title)")
                link  = getattr(entry, "link",  "")
                summary = getattr(entry, "summary", "")[:300]

                # Try to extract an image from RSS media fields
                image = ""
                if hasattr(entry, "media_thumbnail") and entry.media_thumbnail:
                    image = entry.media_thumbnail[0].get("url", "")
                elif hasattr(entry, "media_content") and entry.media_content:
                    for mc in entry.media_content:
                        if mc.get("medium") == "image" or mc.get("type", "").startswith("image"):
                            image = mc.get("url", "")
                            break
                elif hasattr(entry, "enclosures") and entry.enclosures:
                    for enc in entry.enclosures:
                        if enc.get("type", "").startswith("image"):
                            image = enc.get("href", enc.get("url", ""))
                            break

                new_items.append({
                    "feed":    feed_title,
                    "title":   title,
                    "link":    link,
                    "summary": summary,
                    "image":   image,
                })
        except Exception as e:
            print(f"[SCHEDULER] FETCH error on {url}: {e}")

    if not new_items:
        print(f"[SCHEDULER] FETCH task '{task['name']}': nothing new, skipping post.")
        return   # ← silent skip — nothing new

    # Format with LLM
    raw = "\n\n".join(
        f"[{i['feed']}] {i['title']}\n{i['link']}\n{i['summary']}"
        for i in new_items
    )
    prompt = (
        f"System: You are Petey.\n"
        f"Here are {len(new_items)} new RSS items for the task '{task['name']}'.\n"
        f"Write a concise, engaging Discord post highlighting the most interesting ones. "
        f"Use bullet points with hyperlinks. Keep it punchy.\n\n"
        f"== New Items ==\n{raw}\n\nPetey's Report:"
    )
    try:
        response = await asyncio.to_thread(gemini_api.chat_completion, prompt)
    except Exception as e:
        # Fallback: plain list
        response = "\n".join(f"• [{i['title']}](<{i['link']}>)" for i in new_items[:10])

    await _send(channel, f"📡 **{task['name']}** — {len(new_items)} new item(s)\n{response}", suppress_embeds=suppress_embeds)





# ══════════════════════════════════════════════════════════════════════════════
# Task type: SUMMARIZE
# ══════════════════════════════════════════════════════════════════════════════
async def _run_summarize_task(bot, task: dict, channel):
    from chode import gemini_api

    guild = channel.guild
    if not guild:
        await channel.send(f"📝 **{task['name']}** — Cannot summarize: not in a guild.")
        return

    lookback = max(1, min(3, int(task.get("lookback_days") or 1)))
    tz        = pytz.timezone(task["timezone"])
    now_local = datetime.datetime.now(tz)
    start     = (now_local - datetime.timedelta(days=lookback)).replace(
                    hour=0, minute=0, second=0, microsecond=0)
    start_utc = start.astimezone(pytz.utc)

    all_messages = []

    for ch in guild.text_channels:
        if not ch.permissions_for(guild.me).read_message_history:
            continue
        try:
            async for msg in ch.history(after=start_utc, limit=500):
                if msg.author.bot:
                    continue
                all_messages.append({
                    "channel": ch.name,
                    "author":  msg.author.display_name,
                    "content": msg.clean_content[:300],
                    "ts":      msg.created_at.astimezone(tz).strftime("%m/%d %H:%M"),
                })
        except Exception as e:
            print(f"[SCHEDULER] SUMMARIZE: skipping #{ch.name}: {e}")

    if not all_messages:
        period = "today" if lookback == 1 else f"the last {lookback} days"
        await channel.send(
            f"📝 **{task['name']}** — No human messages found for {period}. Nothing to recap!"
        )
        return

    # Build compact transcript (cap ~4000 chars)
    transcript_lines = [
        f"[#{m['channel']} {m['ts']}] {m['author']}: {m['content']}"
        for m in all_messages
    ]
    transcript = "\n".join(transcript_lines)
    if len(transcript) > 4000:
        transcript = transcript[:4000] + "\n...(truncated)"

    if lookback == 1:
        period_label = now_local.strftime("%A, %B %d")
    else:
        end_date   = now_local.strftime("%B %d")
        start_date = start.strftime("%B %d")
        period_label = f"{start_date} – {end_date}"

    prompt = (
        f"System: You are Petey. Below are REAL chat messages from the server.\n"
        f"IMPORTANT: Only discuss topics that appear in the messages below. "
        f"Do NOT make up or invent any conversations that are not listed. "
        f"If only a few messages exist, keep the recap very short.\n\n"
        f"Write a friendly recap of what the server talked about ({period_label}). "
        f"Group by topic if there were multiple conversations. "
        f"Keep it concise — highlight the best moments. Max 3-4 paragraphs.\n\n"
        f"== Messages ({len(all_messages)} total) ==\n{transcript}\n\nPetey's Recap:"
    )
    try:
        response = await asyncio.to_thread(gemini_api.chat_completion, prompt)
    except Exception as e:
        response = f"(LLM failed: {e})"

    header = f"📝 **Recap — {period_label}** ({len(all_messages)} messages)"
    await _send(channel, f"{header}\n{response}")


# ══════════════════════════════════════════════════════════════════════════════
# Dispatcher
# ══════════════════════════════════════════════════════════════════════════════
async def _run_task(bot, task: dict):
    print(f"[SCHEDULER] Running task '{task['name']}' type={task.get('task_type','search')} (id={task['id']})")

    channel = bot.get_channel(int(task["channel_id"]))
    if not channel:
        print(f"[SCHEDULER] Channel {task['channel_id']} not found — skipping.")
        return

    try:
        t = task.get("task_type", "search")
        if t == "fetch":
            await _run_fetch_task(bot, task, channel)
        elif t == "summarize":
            await _run_summarize_task(bot, task, channel)
        else:
            await _run_search_task(bot, task, channel)
    except Exception as e:
        print(f"[SCHEDULER] Task '{task['name']}' raised: {e}")
        try:
            await channel.send(f"⚠️ Task **{task['name']}** encountered an error: {e}")
        except Exception:
            pass

    _mark_last_run(task["id"])
    print(f"[SCHEDULER] Task '{task['name']}' done.")


# ── Main loop ─────────────────────────────────────────────────────────────────
async def _scheduler_loop(bot):
    await bot.wait_until_ready()
    print("[SCHEDULER] Scheduler started.")
    while not bot.is_closed():
        try:
            now_utc = datetime.datetime.now(datetime.timezone.utc)

            for task in _get_all_force_run_tasks():
                _clear_force_run(task["id"])
                print(f"[SCHEDULER] Force-running '{task['name']}' (id={task['id']})")
                asyncio.create_task(_run_task(bot, task))

            for task in _get_enabled_tasks():
                if _is_due(task, now_utc):
                    asyncio.create_task(_run_task(bot, task))

        except Exception as e:
            print(f"[SCHEDULER] Loop error: {e}")

        await asyncio.sleep(60)


def start_scheduler(bot):
    init_tasks_db()
    bot.loop.create_task(_scheduler_loop(bot))
    print("[SCHEDULER] Scheduler task queued.")



