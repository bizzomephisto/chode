"""
flow_engine.py — Execution Engine for Node Flows
------------------------------------------------
Traverses the flow graph defined by user in the Flow Builder UI and executes
nodes like search, genimg, post, llm_transform, etc.
"""

import asyncio
import json
from petey import db as sqlite3
import datetime
import os
from contextlib import suppress

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "memories.db")

def _get_enabled_flows():
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        rows = conn.execute("SELECT * FROM flows WHERE enabled=1").fetchall()
        conn.close()
        return [dict(r) for r in rows]
    except Exception as e:
        print(f"[FLOW_ENGINE] Failed to load flows: {e}")
        return []

_flow_run_counts = {}

async def execute_flow(bot, flow_json: str, trigger_id: str, initial_context: dict, flow_id: str = "unknown"):
    """
    Executes a specific flow starting from the trigger node ID.
    initial_context can contain e.g. {"message": "hello world", "author": "user1"}
    """
    initial_context["_flow_id"] = flow_id
    try:
        flow = json.loads(flow_json)
    except Exception as e:
        print(f"[FLOW_ENGINE] Invalid JSON: {e}")
        return

    nodes = {n["id"]: n for n in flow.get("nodes", [])}
    links = flow.get("links", [])
    
    # Adjacency list for fast trailing
    adj = {}
    for link in links:
        from_node = link["from"]
        to_node = link["to"]
        if from_node not in adj:
            adj[from_node] = []
        adj[from_node].append(to_node)

    # Breadth-first execution
    queue = [(trigger_id, initial_context)]
    
    while queue:
        current_id, context = queue.pop(0)
        node = nodes.get(current_id)
        if not node:
            continue
            
        # Execute the node which potentially modifies or returns a new context
        new_context = await execute_node(bot, node, context)
        
        # If execution halted intentionally (e.g. filter failed), new_context is None
        if new_context is None:
            continue
            
        # Enqueue children
        for child_id in adj.get(current_id, []):
            # We copy the context so parallel branches don't overwrite each other
            queue.append((child_id, dict(new_context)))

async def execute_node(bot, node: dict, context: dict) -> dict | None:
    """Executes a single node and returns the updated context."""
    from petey import utils, gemini_api, comfyui
    
    n_type = node.get("type")
    data = node.get("data", {})
    
    # ── TRIGGERS (Pass-through) ──
    if n_type in ("message", "schedule", "rss"):
        return context

    # ── LOGIC ──
    elif n_type == "limit":
        per_day = int(data.get("per_day", 1))
        today = datetime.datetime.now().strftime("%Y-%m-%d")
        flow_id = context.get("_flow_id", "unknown")
        key = f"{flow_id}_{today}"
        
        global _flow_run_counts
        current_runs = _flow_run_counts.get(key, 0)
        
        if current_runs >= per_day:
            print(f"[FLOW_ENGINE] Rate limit hit for flow {flow_id} ({current_runs}/{per_day} today)")
            return None  # Halt branch
            
        _flow_run_counts[key] = current_runs + 1
        return context
    elif n_type == "filter":
        must_contain = data.get("contains", "").lower()
        if must_contain:
            text_values = " ".join([str(v) for v in context.values()]).lower()
            if must_contain not in text_values:
                return None  # Halt branch
        return context

    elif n_type == "random_choice":
        import random
        # Optional: later we can make routing deterministic based on dice rolls
        # For now, it could be a 50/50 pass-through
        if random.random() > 0.5:
            return context
        return None  # Halt

    # ── ACTIONS ──
    elif n_type == "post":
        channel_id = data.get("channel_id")
        msg_template = data.get("message", "")
        
        # Substitute context vars (e.g. {search_result}, {llm_output})
        formatted_msg = msg_template
        for k, v in context.items():
            formatted_msg = formatted_msg.replace(f"{{{k}}}", str(v))
            
        try:
            channel = bot.get_channel(int(channel_id))
            if channel:
                if len(formatted_msg) > 2000:
                    await utils.send_long_message(channel, formatted_msg)
                else:
                    await channel.send(formatted_msg)
        except Exception as e:
            print(f"[FLOW_ENGINE] Post node failed: {e}")
        return context

    elif n_type == "search":
        query = data.get("query", context.get("trigger_text", ""))
        try:
            results = await utils.perform_web_search(query)
            context["search_result"] = results
            context["last_output"] = results
        except Exception as e:
            print(f"[FLOW_ENGINE] Search node failed: {e}")
            context["last_output"] = f"Search failed: {e}"
        return context

    elif n_type == "genimg":
        prompt_template = data.get("prompt", "")
        formatted_prompt = prompt_template
        for k, v in context.items():
            formatted_prompt = formatted_prompt.replace(f"{{{k}}}", str(v))
            
        channel_id = context.get("channel_id")
        author_id = context.get("author_id")
        
        if channel_id:
            try:
                class FakeAuthor:
                    def __init__(self, m): self.mention = m
                class FakeContext:
                    def __init__(self, b, cid, aid):
                        self.channel = b.get_channel(int(cid))
                        self.author = FakeAuthor(f"<@{aid}>" if aid else "User")
                    async def send(self, content=None, file=None):
                        if self.channel:
                            await self.channel.send(content=content, file=file)
                            
                fake_ctx = FakeContext(bot, channel_id, author_id)
                await comfyui.generate_and_send_images_async(formatted_prompt, fake_ctx)
                context["last_output"] = f"Generated image for: {formatted_prompt}"
            except Exception as e:
                print(f"[FLOW_ENGINE] GenImg failed: {e}")
        else:
            print("[FLOW_ENGINE] genimg node cannot run without a channel_id in context.")
        return context

    elif n_type == "llm_transform":
        prompt_template = data.get("prompt", "Summarize this: {last_output}")
        formatted_prompt = prompt_template
        for k, v in context.items():
            formatted_prompt = formatted_prompt.replace(f"{{{k}}}", str(v))
            
        try:
            sys_msg = "You are PETEY. Output exactly what is requested, in character."
            response = await asyncio.to_thread(gemini_api.chat_completion, prompt=formatted_prompt, system_message=sys_msg)
            context["llm_output"] = response
            context["last_output"] = response
        except Exception as e:
            print(f"[FLOW_ENGINE] llm_transform failed: {e}")
            context["last_output"] = ""
        return context

    else:
        print(f"[FLOW_ENGINE] Unknown node type: {n_type}")
        return context

# Background worker loop to check time-based triggers
async def _flow_scheduler_loop(bot):
    await bot.wait_until_ready()
    print("[FLOW_ENGINE] Flow engine scheduler started.")
    
    # Quick simple cron matcher (minutes only)
    import datetime
    while not bot.is_closed():
        now = datetime.datetime.now()
        flows = _get_enabled_flows()
        
        for f in flows:
            try:
                flow_obj = json.loads(f["flow_json"])
            except Exception:
                continue
                
            nodes = flow_obj.get("nodes", [])
            for node in nodes:
                if node.get("type") == "schedule":
                    # Check cron-ish syntax. For MVP, we literally just match exact minutes
                    cron = node.get("data", {}).get("cron", "")
                    # Very naive schedule parsing (e.g. "* * * * *" runs every minute)
                    if cron == "* * * * *" or cron == "every minute":
                        asyncio.create_task(execute_flow(bot, str(f["flow_json"]), node["id"], {"trigger_type": "schedule", "time": str(now)}, str(f["id"])))
                    # Add more robust cron matching here later if needed
                    
        await asyncio.sleep(60)

def register_message_trigger(bot, message):
    """Called from commands.py on_message to check 'message' trigger nodes."""
    flows = _get_enabled_flows()
    for f in flows:
        try:
            flow_obj = json.loads(f["flow_json"])
        except Exception:
            continue
            
        nodes = flow_obj.get("nodes", [])
        for node in nodes:
            if node.get("type") == "message":
                kw = node.get("data", {}).get("keywords", "").lower()
                if kw and kw in message.content.lower():
                    # Trigger matched
                    ctx = {
                        "trigger_type": "message",
                        "trigger_text": message.content,
                        "author": message.author.name,
                        "author_id": message.author.id,
                        "channel_id": message.channel.id
                    }
                    asyncio.create_task(execute_flow(bot, str(f["flow_json"]), node["id"], ctx, str(f["id"])))

def start_flow_engine(bot):
    bot.loop.create_task(_flow_scheduler_loop(bot))
    print("[FLOW_ENGINE] Task queued.")
