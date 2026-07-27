import os
import json
import datetime
from flask import Flask, redirect, request, session, url_for, render_template, jsonify
import requests
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env'))

app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY")

try:
    from chode.database import init_db
    init_db()
except Exception as e:
    print(f"[WEB] DB init failed: {e}")

DISCORD_CLIENT_ID = os.getenv("DISCORD_CLIENT_ID")
DISCORD_CLIENT_SECRET = os.getenv("DISCORD_CLIENT_SECRET")
DISCORD_REDIRECT_URI = os.getenv("DISCORD_REDIRECT_URI", "http://localhost:5000/callback")
DISCORD_API_BASE_URL = "https://discord.com/api/v10"

@app.route("/")
def index():
    if "user_token" in session:
        return redirect(url_for("dashboard"))
    return render_template("index.html")

@app.route("/privacy")
def privacy():
    return render_template("privacy.html")

@app.route("/tos")
def tos():
    return render_template("tos.html")

@app.route("/guide")
def guide():
    return render_template("guide.html")

@app.route("/login")
def login():
    if not DISCORD_CLIENT_ID:
        return "Error: DISCORD_CLIENT_ID not set in .env", 500
    
    auth_url = (
        f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
        f"&redirect_uri={DISCORD_REDIRECT_URI}&response_type=code&scope=identify%20guilds"
    )
    return redirect(auth_url)

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Error: No code provided", 400

    data = {
        "client_id": DISCORD_CLIENT_ID,
        "client_secret": DISCORD_CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": DISCORD_REDIRECT_URI
    }
    
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    r = requests.post(f"{DISCORD_API_BASE_URL}/oauth2/token", data=data, headers=headers)
    
    if r.status_code != 200:
        return f"Failed to authenticate: {r.text}", 400
        
    session["user_token"] = r.json().get("access_token")
    return redirect(url_for("dashboard"))

@app.route("/logout")
def logout():
    session.pop("user_token", None)
    return redirect(url_for("index"))

@app.route("/dashboard")
def dashboard():
    if "user_token" not in session:
        return redirect(url_for("login"))
        
    token = session["user_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    # Get user info
    user_r = requests.get(f"{DISCORD_API_BASE_URL}/users/@me", headers=headers)
    if user_r.status_code != 200:
        session.pop("user_token", None)
        return redirect(url_for("login"))
    user = user_r.json()
    
    # Get guilds
    guilds_r = requests.get(f"{DISCORD_API_BASE_URL}/users/@me/guilds", headers=headers)
    if guilds_r.status_code != 200:
        return "Failed to fetch guilds", 500
    all_guilds = guilds_r.json()
    
    # Filter guilds where user is owner, has MANAGE_GUILD (0x20), or ADMINISTRATOR (0x8)
    admin_guilds = []
    member_guilds = []
    for g in all_guilds:
        perms = int(g.get("permissions", 0))
        is_owner = g.get("owner", False)
        if is_owner or (perms & 0x20) == 0x20 or (perms & 0x8) == 0x8:
            admin_guilds.append(g)
        else:
            member_guilds.append(g)

    # Fetch bot guilds
    bot_token = os.getenv("DISCORD_TOKEN")
    bot_headers = {"Authorization": f"Bot {bot_token}"}
    bot_guilds_r = requests.get(f"{DISCORD_API_BASE_URL}/users/@me/guilds", headers=bot_headers)
    bot_guild_ids = set()
    if bot_guilds_r.status_code == 200:
        bot_guild_ids = {g["id"] for g in bot_guilds_r.json()}

    active_admin_guilds = []
    invitable_guilds = []
    active_user_guilds = []

    for g in admin_guilds:
        if g["id"] in bot_guild_ids:
            active_admin_guilds.append(g)
        else:
            invitable_guilds.append(g)
            
    for g in member_guilds:
        if g["id"] in bot_guild_ids:
            active_user_guilds.append(g)
            
    # Fetch DEAPI models (cached at module level)
    deapi_models = _get_cached_deapi_models()
    print(f"[WEB] Using {len(deapi_models)} cached DEAPI models for dashboard")

    return render_template("dashboard.html", user=user, active_admin_guilds=active_admin_guilds, invitable_guilds=invitable_guilds, active_user_guilds=active_user_guilds, client_id=DISCORD_CLIENT_ID, deapi_models=deapi_models)


def _fetch_guild_details(guild_id, bot_headers):
    try:
        r = requests.get(f"{DISCORD_API_BASE_URL}/guilds/{guild_id}?with_counts=true", headers=bot_headers)
        if r.status_code == 200:
            return r.json()
    except Exception as e:
        print(f"[WEB CREATOR] Error fetching guild details for {guild_id}: {e}")
    return None

@app.route("/creator")
def creator_dashboard():
    if "user_token" not in session:
        return redirect(url_for("login"))
        
    token = session["user_token"]
    headers = {"Authorization": f"Bearer {token}"}
    
    # Get user info
    user_r = requests.get(f"{DISCORD_API_BASE_URL}/users/@me", headers=headers)
    if user_r.status_code != 200:
        session.pop("user_token", None)
        return redirect(url_for("login"))
    user = user_r.json()
    
    # Restrict to creator Bizzo (ID: 98537446764974080)
    creator_ids = ["98537446764974080"]
    if str(user.get("id")) not in creator_ids:
        return "Unauthorized: Creator only area.", 403
        
    # Fetch all servers the bot is installed on
    bot_token = os.getenv("DISCORD_TOKEN")
    bot_headers = {"Authorization": f"Bot {bot_token}"}
    bot_guilds_r = requests.get(f"{DISCORD_API_BASE_URL}/users/@me/guilds", headers=bot_headers)
    
    servers = []
    total_users = 0
    if bot_guilds_r.status_code == 200:
        raw_servers = bot_guilds_r.json()
        
        # Concurrently fetch detailed guild objects to get member counts
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=15) as executor:
            detailed_servers = list(executor.map(lambda g: _fetch_guild_details(g["id"], bot_headers), raw_servers))
        
        for i, ds in enumerate(detailed_servers):
            if ds:
                servers.append(ds)
                total_users += ds.get("approximate_member_count", 0)
            else:
                servers.append(raw_servers[i])
    else:
        print(f"[WEB CREATOR] Failed to fetch bot guilds: {bot_guilds_r.status_code} - {bot_guilds_r.text}")
        
    return render_template("creator.html", user=user, servers=servers, total_users=total_users)


# ── DEAPI model cache (refreshed once per 24 hours) ──
_deapi_model_cache = []
_deapi_cache_time = 0
_is_fetching_models = False

def _get_cached_deapi_models():
    global _deapi_model_cache, _deapi_cache_time, _is_fetching_models
    import time as _time
    
    # Return cache if still valid (24 hours = 86400 seconds)
    if _deapi_model_cache and (_time.time() - _deapi_cache_time) < 86400:
        return _deapi_model_cache
        
    if _is_fetching_models:
        return _deapi_model_cache
        
    _is_fetching_models = True
    
    deapi_key = os.getenv("DEAPI_KEY")
    if not deapi_key:
        print("[WEB] WARNING: DEAPI_KEY not set")
        _is_fetching_models = False
        return _deapi_model_cache
    
    deapi_headers = {"Authorization": f"Bearer {deapi_key}"}
    
    try:
        print("[WEB] Fetching ALL models from DEAPI (single call)...")
        # Short timeout to prevent hanging the web server
        r = requests.get("https://api.deapi.ai/api/v1/client/models", headers=deapi_headers, timeout=5)
        
        if r.status_code == 200:
            _deapi_model_cache = r.json().get("data", [])
            _deapi_cache_time = _time.time()
            print(f"[WEB] Cached {len(_deapi_model_cache)} DEAPI models (expires in 24h)")
        elif r.status_code == 429:
            retry_after = r.headers.get("Retry-After", "unknown")
            print(f"[WEB] Rate limited by DEAPI! Retry after: {retry_after}s. Will try again later.")
            # Pretend we fetched it but it expires in 5 minutes to prevent spamming
            _deapi_cache_time = _time.time() - 86400 + 300 
        else:
            print(f"[WEB] DEAPI returned {r.status_code}: {r.text[:200]}")
    except Exception as e:
        print(f"[WEB] Error fetching DEAPI models: {e}")
    finally:
        _is_fetching_models = False
    
    return _deapi_model_cache

@app.route("/invite/<server_id>")
def oauth_invite(server_id):
    if not DISCORD_CLIENT_ID:
        return "Error: DISCORD_CLIENT_ID not set", 500
    invite_url = (
        f"https://discord.com/api/oauth2/authorize?client_id={DISCORD_CLIENT_ID}"
        f"&permissions=3525696&scope=bot%20applications.commands"
        f"&guild_id={server_id}&disable_guild_select=true"
    )
    return redirect(invite_url)

@app.route("/api/models")
def get_deapi_models():
    return jsonify(_get_cached_deapi_models())

@app.route("/api/channels/<server_id>", methods=["GET"])
def get_channels(server_id):
    """Returns the list of text channels for a server (used by dashboard dropdowns)."""
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    bot_token = os.getenv("DISCORD_TOKEN")
    bot_headers = {"Authorization": f"Bot {bot_token}"}
    try:
        ch_r = requests.get(f"{DISCORD_API_BASE_URL}/guilds/{server_id}/channels", headers=bot_headers, timeout=5)
        if ch_r.status_code == 200:
            text_channels = [{"id": c["id"], "name": c.get("name", c["id"])} for c in ch_r.json() if c.get("type") == 0]
            text_channels.sort(key=lambda c: c["name"])
            return jsonify({"channels": text_channels})
        return jsonify({"channels": [], "error": f"Discord API returned {ch_r.status_code}"}), 200
    except Exception as e:
        return jsonify({"channels": [], "error": str(e)}), 200


@app.route("/api/config/<server_id>", methods=["GET", "POST"])
def manage_config(server_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
        
    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), f"config_{server_id}.json")
    
    if request.method == "GET":
        try:
            with open(config_path, "r") as f:
                data = json.load(f)
                if "config_channel" not in data:
                    data["config_channel"] = ""
                return jsonify(data)
        except FileNotFoundError:
            return jsonify({"personality": "You are Petey, a friendly chatbot.", "sleep_enabled": False, "sleep_hour": 3, "sleep_minute": 0, "sleep_timezone": "America/Chicago", "config_channel": ""})
            
    elif request.method == "POST":
        new_config = request.json
        
        # Enforce maximum of 3 channel-specific personalities
        channel_personalities = new_config.get("channel_personalities", {})
        if len(channel_personalities) > 3:
            # Truncate to first 3 entries
            keys = list(channel_personalities.keys())[:3]
            new_config["channel_personalities"] = {k: channel_personalities[k] for k in keys}
            
        with open(config_path, "w") as f:
            json.dump(new_config, f, indent=4)
        return jsonify({"status": "success"})

@app.route("/api/reword_prompt/<server_id>", methods=["POST"])
def reword_prompt(server_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    data = request.json
    original_prompt = data.get("prompt", "").strip()
    if not original_prompt:
        return jsonify({"error": "No prompt provided"}), 400
        
    meta_prompt = (
        "You are an expert prompt engineer. The user has provided a rough idea for a chatbot identity or instruction. "
        "Rewrite it into a detailed, robust, and highly effective system prompt for an LLM starting with 'You are...'. "
        "Do not include any introductory filler text, conversation, quotes, or markdown formatting, literally ONLY output the final exact system prompt that will be injected into the chatbot."
    )
    
    try:
        from chode import gemini_api
        enhanced = gemini_api.chat_completion(original_prompt, system_message=meta_prompt)
        return jsonify({"enhanced_prompt": enhanced.strip()})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/upload_doc/<server_id>", methods=["POST"])
def upload_doc(server_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    
    if 'file' not in request.files:
        return jsonify({"error": "No file part provided."}), 400
        
    file = request.files['file']
    if file.filename == '':
        return jsonify({"error": "No selected file."}), 400
        
    if file and (file.filename.endswith('.txt') or file.filename.endswith('.md') or file.filename.endswith('.pdf') or file.filename.endswith('.json')):
        try:
            from chode import database
            
            if file.filename.endswith('.pdf'):
                try:
                    import PyPDF2
                    pdf_reader = PyPDF2.PdfReader(file)
                    text = ""
                    for page in pdf_reader.pages:
                        extracted = page.extract_text()
                        if extracted: text += extracted + "\n"
                except ImportError:
                    return jsonify({"error": "PyPDF2 is not installed. Please run 'pip install PyPDF2' on the server."}), 500
                except Exception as e:
                    return jsonify({"error": f"Failed reading PDF: {e}"}), 500
            else:
                text = file.read().decode('utf-8', errors='ignore')
                
            database.store_document(server_id, file.filename, text)
            return jsonify({"status": "success", "message": f"Queued {file.filename} for Long-Term Memory!"})
        except Exception as e:
            return jsonify({"error": str(e)}), 500
            
    return jsonify({"error": "Only .txt, .md, .pdf, and .json files are allowed."}), 400

@app.route("/api/docs/<server_id>", methods=["GET"])
def get_docs(server_id):
    if "user_token" not in session: return jsonify({"error": "Unauthorized"}), 401
    try:
        from chode import database
        docs = database.get_documents(server_id)
        return jsonify({"documents": docs})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/docs/<server_id>/<filename>", methods=["DELETE"])
def delete_doc(server_id, filename):
    if "user_token" not in session: return jsonify({"error": "Unauthorized"}), 401
    try:
        from chode import database
        success = database.delete_document(server_id, filename)
        if success:
            return jsonify({"status": "success"})
        else:
            return jsonify({"error": "DB Delete failed"}), 500
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/metrics/<server_id>", methods=["GET"])
def get_metrics(server_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    from chode import db as _sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memories.db")
    metrics = {"total_messages": 0, "unique_users": 0, "images_generated": 0}
    try:
        conn = _sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT COUNT(*), COUNT(DISTINCT user_id) FROM memories WHERE server_id=?",
            (server_id,)
        ).fetchone()
        if row:
            metrics["total_messages"] = row[0]
            metrics["unique_users"]   = row[1]

        img_row = conn.execute(
            "SELECT COUNT(*) FROM image_generations WHERE server_id=?",
            (server_id,)
        ).fetchone()
        if img_row:
            metrics["images_generated"] = img_row[0]

        conn.close()
    except Exception as e:
        print(f"[METRICS] Error: {e}")

    return jsonify(metrics)


# ── Scheduled Tasks ───────────────────────────────────────────────────────────

@app.route("/tasks/<server_id>")
def tasks_page(server_id):
    if "user_token" not in session:
        return redirect(url_for("login"))

    token = session["user_token"]
    headers = {"Authorization": f"Bearer {token}"}
    user_r = requests.get(f"{DISCORD_API_BASE_URL}/users/@me", headers=headers)
    if user_r.status_code != 200:
        session.pop("user_token", None)
        return redirect(url_for("login"))
    user = user_r.json()

    # Verify admin in this server
    guilds_r = requests.get(f"{DISCORD_API_BASE_URL}/users/@me/guilds", headers=headers)
    if guilds_r.status_code != 200:
        return "Failed to fetch guilds", 500
    guilds = guilds_r.json()
    is_admin = False
    guild_name = server_id
    for g in guilds:
        if g["id"] == server_id:
            guild_name = g.get("name", server_id)
            perms = int(g.get("permissions", 0))
            if g.get("owner", False) or (perms & 0x20) == 0x20 or (perms & 0x8) == 0x8:
                is_admin = True
            break
    if not is_admin:
        return "Access denied: Server admin required.", 403

    # Fetch text channels (type 0 = GUILD_TEXT)
    bot_token = os.getenv("DISCORD_TOKEN")
    bot_headers = {"Authorization": f"Bot {bot_token}"}
    text_channels = []
    ch_r = requests.get(f"{DISCORD_API_BASE_URL}/guilds/{server_id}/channels", headers=bot_headers)
    if ch_r.status_code == 200:
        text_channels = [c for c in ch_r.json() if c.get("type") == 0]
        text_channels.sort(key=lambda c: c.get("position", 0))

    return render_template("tasks.html", user=user, server_id=server_id,
                           guild_name=guild_name, text_channels=text_channels)


@app.route("/api/flows/<server_id>", methods=["GET"])
def get_flows(server_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    from chode import db as _sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memories.db")
    try:
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM flows WHERE server_id=? ORDER BY id DESC", (server_id,)
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/flows/<server_id>", methods=["POST"])
def create_flow(server_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    token = session["user_token"]
    headers = {"Authorization": f"Bearer {token}"}
    user_r = requests.get(f"{DISCORD_API_BASE_URL}/users/@me", headers=headers)
    if user_r.status_code != 200:
        return jsonify({"error": "Unauthorized"}), 401
    user = user_r.json()

    data = request.json or {}
    name = data.get("name", "").strip()
    flow = data.get("flow")
    if not name or not flow:
        return jsonify({"error": "name and flow are required"}), 400

    flow_json = json.dumps(flow)
    created_at = datetime.datetime.utcnow().isoformat()

    from chode import db as _sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memories.db")
    try:
        conn = _sqlite3.connect(db_path)
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO flows (server_id, name, flow_json, enabled, created_at, created_by) VALUES (?,?,?,?,?,?)",
            (server_id, name, flow_json, 1, created_at, user.get("id"))
        )
        flow_id = cur.lastrowid
        conn.commit()
        conn.close()
        return jsonify({"status": "created", "id": flow_id})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/flows/<server_id>/<int:flow_id>", methods=["PUT"])
def update_flow(server_id, flow_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    name = data.get("name", "").strip()
    flow = data.get("flow")
    if not name or not flow:
        return jsonify({"error": "name and flow are required"}), 400

    flow_json = json.dumps(flow)

    from chode import db as _sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memories.db")
    try:
        conn = _sqlite3.connect(db_path)
        conn.execute(
            "UPDATE flows SET name=?, flow_json=? WHERE id=? AND server_id=?",
            (name, flow_json, flow_id, server_id)
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/flows/<server_id>/<int:flow_id>/toggle", methods=["POST"])
def toggle_flow(server_id, flow_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    from chode import db as _sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memories.db")
    try:
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row
        row = conn.execute("SELECT enabled FROM flows WHERE id=? AND server_id=?",
                           (flow_id, server_id)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Flow not found"}), 404
        new_state = 0 if row["enabled"] else 1
        conn.execute("UPDATE flows SET enabled=? WHERE id=?", (new_state, flow_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "enabled": new_state})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/flows/<server_id>/<int:flow_id>/delete", methods=["POST"])
def delete_flow(server_id, flow_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    from chode import db as _sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memories.db")
    try:
        conn = _sqlite3.connect(db_path)
        conn.execute("DELETE FROM flows WHERE id=? AND server_id=?", (flow_id, server_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/tasks/<server_id>", methods=["GET"])
def get_tasks(server_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    from chode import db as _sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memories.db")
    try:
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM tasks WHERE server_id=? ORDER BY id DESC", (server_id,)
        ).fetchall()
        conn.close()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tasks/<server_id>", methods=["POST"])
def create_task(server_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    token = session["user_token"]
    headers = {"Authorization": f"Bearer {token}"}
    user_r = requests.get(f"{DISCORD_API_BASE_URL}/users/@me", headers=headers)
    if user_r.status_code != 200:
        return jsonify({"error": "Unauthorized"}), 401
    user = user_r.json()

    data = request.json or {}
    name         = data.get("name", "").strip()
    channel_id   = data.get("channel_id", "").strip()
    task_type    = data.get("task_type", "search")
    search_query = data.get("search_query", "").strip()
    sources      = json.dumps(data.get("sources", []))
    suppress_embeds = 1 if data.get("suppress_embeds") else 0
    lookback_days = max(1, min(3, int(data.get("lookback_days", 1))))
    days         = json.dumps(data.get("days", []))
    hour         = int(data.get("hour", 8))
    minute       = int(data.get("minute", 0))
    timezone     = data.get("timezone", "America/Chicago")

    if not name or not channel_id:
        return jsonify({"error": "name and channel_id are required"}), 400
    if task_type == "search" and not search_query:
        return jsonify({"error": "search_query required for search tasks"}), 400
    if task_type == "fetch" and not json.loads(sources):
        return jsonify({"error": "At least one RSS source required for fetch tasks"}), 400

    from chode import db as _sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memories.db")
    try:
        conn = _sqlite3.connect(db_path)
        conn.execute(
            """INSERT INTO tasks
               (server_id, channel_id, name, task_type, search_query, sources,
                suppress_embeds, lookback_days, days, hour, minute, timezone, enabled, created_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,1,?)""",
            (server_id, channel_id, name, task_type, search_query, sources,
             suppress_embeds, lookback_days, days, hour, minute, timezone, user["id"])
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "created"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tasks/<server_id>/<int:task_id>/delete", methods=["POST"])
def delete_task(server_id, task_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    from chode import db as _sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memories.db")
    try:
        conn = _sqlite3.connect(db_path)
        conn.execute("DELETE FROM tasks WHERE id=? AND server_id=?", (task_id, server_id))
        conn.execute("DELETE FROM task_seen_items WHERE task_id=?", (task_id,))
        conn.commit()
        conn.close()
        return jsonify({"status": "deleted"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/tasks/<server_id>/<int:task_id>/toggle", methods=["POST"])
def toggle_task(server_id, task_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    from chode import db as _sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memories.db")
    try:
        conn = _sqlite3.connect(db_path)
        conn.row_factory = _sqlite3.Row
        row = conn.execute("SELECT enabled FROM tasks WHERE id=? AND server_id=?",
                           (task_id, server_id)).fetchone()
        if not row:
            conn.close()
            return jsonify({"error": "Task not found"}), 404
        new_state = 0 if row["enabled"] else 1
        conn.execute("UPDATE tasks SET enabled=? WHERE id=?", (new_state, task_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "ok", "enabled": new_state})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/tasks/<server_id>/<int:task_id>", methods=["PUT"])
def update_task(server_id, task_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    data = request.json or {}
    name         = data.get("name", "").strip()
    channel_id   = data.get("channel_id", "").strip()
    task_type    = data.get("task_type", "search")
    search_query = data.get("search_query", "").strip()
    sources      = json.dumps(data.get("sources", []))
    suppress_embeds = 1 if data.get("suppress_embeds") else 0
    lookback_days = max(1, min(3, int(data.get("lookback_days", 1))))
    days         = json.dumps(data.get("days", []))
    hour         = int(data.get("hour", 8))
    minute       = int(data.get("minute", 0))
    timezone     = data.get("timezone", "America/Chicago")
    if not name or not channel_id:
        return jsonify({"error": "name and channel_id are required"}), 400
    from chode import db as _sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memories.db")
    try:
        conn = _sqlite3.connect(db_path)
        conn.execute(
            """UPDATE tasks SET name=?, channel_id=?, task_type=?, search_query=?,
               sources=?, suppress_embeds=?, lookback_days=?, days=?, hour=?, minute=?, timezone=?
               WHERE id=? AND server_id=?""",
            (name, channel_id, task_type, search_query, sources,
             suppress_embeds, lookback_days, days, hour, minute, timezone, task_id, server_id)
        )
        conn.commit()
        conn.close()
        return jsonify({"status": "updated"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500



@app.route("/api/tasks/<server_id>/<int:task_id>/run", methods=["POST"])
def force_run_task(server_id, task_id):
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401
    from chode import db as _sqlite3
    db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memories.db")
    try:
        conn = _sqlite3.connect(db_path)
        conn.execute("UPDATE tasks SET force_run=1 WHERE id=? AND server_id=?",
                     (task_id, server_id))
        conn.commit()
        conn.close()
        return jsonify({"status": "queued"})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# --- EXCLUSIVE BOT INSTALL GATEKEEPER ---
@app.route("/add_bot/<server_id>", methods=["GET", "POST"])
def invite_bot(server_id):
    from chode import db as _sqlite3
    
    if request.method == "POST":
        code = request.form.get("code", "").strip().upper()
        if not code:
            return render_template("invite.html", server_id=server_id, error="Please enter an access code.")
            
        db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "memories.db")
        try:
            conn = _sqlite3.connect(db_path)
            c = conn.cursor()
            c.execute("SELECT id FROM invite_codes WHERE code = ? AND used = 0", (code,))
            row = c.fetchone()
            
            if not row:
                return render_template("invite.html", server_id=server_id, error="Invalid or already used access code. Please try again.")
                
            # Valid code! Mark as used.
            c.execute("UPDATE invite_codes SET used = 1 WHERE id = ?", (row[0],))
            conn.commit()
        except Exception as e:
            return render_template("invite.html", server_id=server_id, error="Database error verifying code.")
        finally:
            if 'conn' in locals():
                conn.close()
                
        # Success! Redirect them to the Discord bot authorization screen! 
        return redirect(url_for("oauth_invite", server_id=server_id))
        
    return render_template("invite.html", server_id=server_id)

# ── Setup Wizard ──────────────────────────────────────────────────────────────

@app.route("/wizard/<server_id>")
def wizard_page(server_id):
    if "user_token" not in session:
        return redirect(url_for("login"))

    token = session["user_token"]
    headers = {"Authorization": f"Bearer {token}"}

    # Get user info
    user_r = requests.get(f"{DISCORD_API_BASE_URL}/users/@me", headers=headers)
    if user_r.status_code != 200:
        session.pop("user_token", None)
        return redirect(url_for("login"))
    user = user_r.json()

    # Verify admin in this server
    guilds_r = requests.get(f"{DISCORD_API_BASE_URL}/users/@me/guilds", headers=headers)
    if guilds_r.status_code != 200:
        return "Failed to fetch guilds", 500
    guilds = guilds_r.json()
    is_admin = False
    guild_name = server_id
    guild_icon = None
    for g in guilds:
        if g["id"] == server_id:
            guild_name = g.get("name", server_id)
            guild_icon = g.get("icon")
            perms = int(g.get("permissions", 0))
            if g.get("owner", False) or (perms & 0x20) == 0x20 or (perms & 0x8) == 0x8:
                is_admin = True
            break
    if not is_admin:
        return "Access denied: Server admin required.", 403

    # Fetch text channels
    bot_token = os.getenv("DISCORD_TOKEN")
    bot_headers = {"Authorization": f"Bot {bot_token}"}
    text_channels = []
    ch_r = requests.get(f"{DISCORD_API_BASE_URL}/guilds/{server_id}/channels", headers=bot_headers, timeout=5)
    if ch_r.status_code == 200:
        text_channels = [c for c in ch_r.json() if c.get("type") == 0]
        text_channels.sort(key=lambda c: c.get("position", 0))

    return render_template("wizard.html",
                           user=user, server_id=server_id,
                           guild_name=guild_name, guild_icon=guild_icon,
                           text_channels=text_channels)


@app.route("/api/wizard/<server_id>", methods=["GET"])
def get_wizard_config(server_id):
    """Return persona config for the wizard (personas, global_settings, persona_channel_map)."""
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    from chode.config import make_default_persona, DEFAULT_GLOBAL_SETTINGS, PERSONA_PRESETS
    import copy

    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), f"config_{server_id}.json")
    try:
        with open(config_path, "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        data = {}

    # Return existing persona data or bootstrap defaults
    personas = data.get("personas")
    if not personas:
        # Bootstrap from legacy config
        default_persona = make_default_persona(1)
        legacy_prompt = data.get("personality", "")
        if legacy_prompt:
            default_persona["system_prompt"] = legacy_prompt
            default_persona["preset_key"] = ""  # custom since it's from legacy
        personas = [default_persona]
    else:
        # Pronouns are no longer part of persona setup. Do not send legacy
        # values back through the wizard.
        personas = [
            {key: value for key, value in persona.items() if key != "pronouns"}
            for persona in personas
            if isinstance(persona, dict)
        ]

    global_settings = data.get("global_settings", copy.deepcopy(DEFAULT_GLOBAL_SETTINGS))
    persona_channel_map = data.get("persona_channel_map", {})

    # Include custom_presets from legacy config so wizard can show them
    custom_presets = data.get("custom_presets", [])

    return jsonify({
        "personas": personas,
        "global_settings": global_settings,
        "persona_channel_map": persona_channel_map,
        "custom_presets": custom_presets,
        "presets": {k: {kk: vv for kk, vv in v.items() if kk != "slot_affinity"} for k, v in PERSONA_PRESETS.items()}
    })


@app.route("/api/wizard/<server_id>", methods=["POST"])
def save_wizard_config(server_id):
    """Save persona config from the wizard, merging into the full server config."""
    if "user_token" not in session:
        return jsonify({"error": "Unauthorized"}), 401

    wizard_data = request.json
    if not wizard_data:
        return jsonify({"error": "No data provided"}), 400

    config_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), f"config_{server_id}.json")

    # Load existing config
    try:
        with open(config_path, "r") as f:
            full_config = json.load(f)
    except FileNotFoundError:
        full_config = {}

    # Validate personas (max 3)
    personas = [
        {key: value for key, value in persona.items() if key != "pronouns"}
        for persona in wizard_data.get("personas", [])[:3]
        if isinstance(persona, dict)
    ]

    # Merge wizard data into full config
    full_config["personas"] = personas
    full_config["global_settings"] = wizard_data.get("global_settings", full_config.get("global_settings", {}))
    full_config["persona_channel_map"] = wizard_data.get("persona_channel_map", {})

    # Also update the legacy personality field to the default persona's prompt
    # so the old chat cog path still works if someone reverts
    for p in personas:
        if p.get("is_default") or p.get("slot") == 1:
            full_config["personality"] = p.get("system_prompt", full_config.get("personality", ""))
            break

    with open(config_path, "w") as f:
        json.dump(full_config, f, indent=4)

    # Bust the config cache so the bot picks up changes immediately
    from chode.config import _config_cache
    _config_cache.pop(str(server_id), None)

    return jsonify({"status": "success"})


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
