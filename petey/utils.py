import discord
import asyncio
from petey import gemini_api
from petey import config

def ordinal(n):
    if 11 <= (n % 100) <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"

def format_timestamp(timestamp_str):
    import datetime
    ts = datetime.datetime.fromisoformat(timestamp_str)
    return f"{ts.strftime('%A')} the {ordinal(ts.day)} of {ts.strftime('%b').lower()}"

async def send_long_message(channel, message):
    for i in range(0, len(message), 2000):
        await channel.send(message[i:i+2000])

def reword_prompt(prompt: str, max_tokens=80) -> str:
    custom_system = (
        f"Reword the following prompt to be more descriptive and detailed, "
        f"while keeping it to approximately {max_tokens} tokens. Return only the reworded prompt."
    )
    new_prompt = gemini_api.call_gemini(prompt, system_message=custom_system)
    return new_prompt.strip()

def read_whatsnew():
    try:
        import os
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(script_dir, "whatsnew.txt")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[ERROR] Failed to read whatsnew.txt: {e}")
        return "Error: 'whatsnew.txt' could not be read."

def read_peteyhelp():
    try:
        import os
        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        help_path = os.path.join(script_dir, "peteyhelp.txt")
        with open(help_path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        return "Error: 'peteyhelp.txt' could not be read."

def read_setup_guide():
    try:
        import os
        script_dir = os.path.dirname(os.path.abspath(__file__))
        path = os.path.join(script_dir, "setup_guide.md")
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception as e:
        print(f"[ERROR] Failed to read setup_guide.md: {e}")
        return "Error: Setup guide could not be found."

def get_member_info(member: discord.abc.User) -> str:
    """
    Returns a string describing the member's current status and activities.
    For discord.Member objects, 'status' and 'activities' are used.
    For DM users (discord.User), these attributes may not exist, so they default to "unknown" and "no activity info".
    """
    # Use getattr to safely get status; if not available, default to "unknown"
    status = str(getattr(member, "status", "unknown"))
    
    activities = []
    if hasattr(member, "activities") and member.activities:
        for activity in member.activities:
            if hasattr(activity, "name") and activity.name:
                activities.append(activity.name)
    if activities:
        activity_str = ", ".join(activities)
    else:
        activity_str = "no activity info"
        
    # Use display_name if available; fallback to name.
    display = getattr(member, "display_name", member.name)
    return f"{display} is {status} and currently {activity_str}."

async def add_reaction_if_interesting(message: discord.Message):
    import random
    # Only proceed with a 10% chance.
    if random.random() > 0.1:
        return
    if len(message.content) < 5:
        return
    prompt_text = f"Given the following message:\n\"{message.content}\"\n"
    system_message = (
        "Suggest one reaction emoji that best expresses an appropriate reaction to this message. "
        "If the message is not interesting, reply with 'none'. Return only the emoji or 'none'."
    )
    reaction = await asyncio.to_thread(gemini_api.call_gemini, prompt_text, system_message=system_message)
    reaction = reaction.strip()
    if reaction.lower() != "none" and reaction:
        try:
            await message.add_reaction(reaction)
        except Exception as e:
            print(f"[DEBUG] Error adding reaction: {e}")

async def perform_web_search(query: str, max_results: int = 3) -> str:
    """Performs a web search using DuckDuckGo and returns a formatted string of results."""
    import asyncio

    def _search():
        try:
            from ddgs import DDGS
            results_raw = DDGS().text(query, max_results=max_results)
        except ImportError:
            # Fallback to old package name if ddgs isn't installed
            try:
                from duckduckgo_search import DDGS
                results_raw = DDGS().text(query, max_results=max_results)
            except Exception as e:
                print(f"[ERROR] Web search fallback failed: {e}")
                return f"Error: search packages not available ({e})"

        if not results_raw:
            return "No results found."

        results = []
        for i, r in enumerate(results_raw, 1):
            title = r.get("title", "No title")
            snippet = r.get("body", "")
            link = r.get("href", "")
            results.append(f"{i}. {title}\n{snippet}\nSource: {link}")

        return "\n\n".join(results)

    return await asyncio.to_thread(_search)


# ── Channel assignment enforcement ──────────────────────────────────────────
# Maps activity categories to their config key and friendly name
_CHANNEL_CATEGORIES = {
    "media_generation": "🎨 Media Generation",
    "music": "🎵 Music",
    "chat": "💬 AI Chat",
    "research": "🔍 Research",
}

async def check_channel_assignment(ctx, category):
    """
    Enforce the server's configured channel assignment for a command category.
    Direct messages are not subject to server channel routing.
    """
    allowed, error_message = await check_channel_assignment_raw(
        ctx.guild, ctx.channel, category
    )
    if not allowed and error_message:
        await ctx.send(error_message)
    return allowed

async def check_channel_assignment_raw(guild, channel, category):
    """
    Return whether a command category may run in the given server channel.

    Enforcement is opt-in per server. Once enabled, configured categories fail
    closed when their assigned channel is missing or does not match.
    """
    if guild is None or channel is None:
        return True, None

    server_config = config.load_server_config(guild.id)
    if not server_config.get("channel_enforcement_enabled", False):
        return True, None

    friendly_name = _CHANNEL_CATEGORIES.get(category, category.replace("_", " ").title())
    assignments = server_config.get("channel_assignments", {})
    assigned_channel_id = str(assignments.get(category, "")).strip()

    if not assigned_channel_id:
        return (
            False,
            f"{friendly_name} commands are disabled until a server administrator "
            "assigns a channel in the Petey dashboard.",
        )

    current_channel_ids = {
        str(getattr(channel, "id", "")),
        str(getattr(channel, "parent_id", "")),
    }
    if assigned_channel_id in current_channel_ids:
        return True, None

    return (
        False,
        f"{friendly_name} commands can only be used in <#{assigned_channel_id}>.",
    )
