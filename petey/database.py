"""
database.py — Petey Memory Module
----------------------------------
Handles short-term (SQLite) and long-term (ChromaDB RAG) memory.

Short-term : Last N messages in a channel — used for immediate context.
Long-term  : Semantic vector search across all past messages — used for RAG recall.

All ChromaDB embedding is done in a background thread so the bot never blocks.
If Gemini or ChromaDB is unavailable, memory still saves to SQLite gracefully.
"""

from petey import db as sqlite3
import os
import asyncio
import datetime
import threading
from typing import Optional

# ── Path setup ────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB_PATH = os.path.join(BASE_DIR, "memories.db")
USER_IMAGES_PATH = os.path.join(BASE_DIR, "user_images")

# ── PostgreSQL initialisation ──────────────────────────────────────────────────
def init_db():
    """
    Create the necessary tables in PostgreSQL if they don't already exist.
    """
    try:
        conn = sqlite3.connect()
        c = conn.cursor()
        
        # 1. Enable the pgvector extension
        c.execute("CREATE EXTENSION IF NOT EXISTS vector;")
        conn.commit()

        # 2. Memories table with pgvector support (768 dimensions for Gemini)
        c.execute("""
            CREATE TABLE IF NOT EXISTS memories (
                id          SERIAL PRIMARY KEY,
                server_id   VARCHAR(64) NOT NULL,
                channel_id  VARCHAR(64) NOT NULL,
                user_id     VARCHAR(64) NOT NULL,
                message     TEXT NOT NULL,
                timestamp   VARCHAR(64) NOT NULL,
                embedded    INTEGER DEFAULT 0,
                embedding   vector(768)
            )
        """)
        
        # 3. Generated profiles
        c.execute("""
            CREATE TABLE IF NOT EXISTS generated_profiles (
                id           SERIAL PRIMARY KEY,
                server_id    VARCHAR(64) NOT NULL,
                user_id      VARCHAR(64) NOT NULL,
                profile_text TEXT NOT NULL,
                timestamp    VARCHAR(64) NOT NULL
            )
        """)
        
        # 4. Custom flows
        c.execute("""
            CREATE TABLE IF NOT EXISTS flows (
                id          SERIAL PRIMARY KEY,
                server_id   VARCHAR(64) NOT NULL,
                name        VARCHAR(256) NOT NULL,
                flow_json   TEXT NOT NULL,
                enabled     INTEGER DEFAULT 1,
                created_at  VARCHAR(64) NOT NULL,
                created_by  VARCHAR(64) NOT NULL
            )
        """)
        
        # 5. Flow states
        c.execute("""
            CREATE TABLE IF NOT EXISTS flow_state (
                flow_id               INTEGER PRIMARY KEY,
                last_run_at           VARCHAR(64),
                execution_count_today INTEGER DEFAULT 0,
                last_error            TEXT
            )
        """)
        
        # 6. Invite codes
        c.execute("""
            CREATE TABLE IF NOT EXISTS invite_codes (
                id          SERIAL PRIMARY KEY,
                code        VARCHAR(256) NOT NULL,
                used        INTEGER DEFAULT 0,
                created_at  VARCHAR(64) NOT NULL
            )
        """)
        
        # 7. Image generations metrics
        c.execute("""
            CREATE TABLE IF NOT EXISTS image_generations (
                id          SERIAL PRIMARY KEY,
                server_id   VARCHAR(64) NOT NULL,
                user_id     VARCHAR(64) NOT NULL,
                prompt      TEXT,
                timestamp   VARCHAR(64) NOT NULL
            )
        """)
        
        conn.commit()
        conn.close()
        print(f"[MEMORY] PostgreSQL ready.")
    except Exception as e:
        print(f"[MEMORY] Failed to initialise PostgreSQL: {e}")

def record_image_generation(server_id: str, user_id: str, prompt: str):
    """Logs an image generation event for metrics dashboard."""
    try:
        conn = sqlite3.connect(DB_PATH)
        conn.execute(
            "INSERT INTO image_generations (server_id, user_id, prompt, timestamp) VALUES (?, ?, ?, ?)",
            (str(server_id), str(user_id), prompt, datetime.datetime.utcnow().isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[MEMORY] Failed to record image generation: {e}")

# ── User Image Filesystem Storage ──
def _get_user_image_dir(user_id: str) -> str:
    path = os.path.join(USER_IMAGES_PATH, str(user_id))
    os.makedirs(path, exist_ok=True)
    return path

def save_recent_image(user_id: str, image_bytes: bytes):
    """Saves the most recent image for a user, overwriting the previous one."""
    try:
        path = _get_user_image_dir(user_id)
        filepath = os.path.join(path, "recent.bin")
        with open(filepath, "wb") as f:
            f.write(image_bytes)
    except Exception as e:
        print(f"[MEMORY] failed to save recent user image: {e}")

def get_recent_image(user_id: str) -> bytes:
    """Returns the most recent image bytes, or None if it doesn't exist."""
    path = _get_user_image_dir(user_id)
    filepath = os.path.join(path, "recent.bin")
    if os.path.exists(filepath):
        try:
            with open(filepath, "rb") as f:
                return f.read()
        except:
            pass
    return None

def delete_recent_image(user_id: str) -> bool:
    """Deletes the most recent image."""
    path = _get_user_image_dir(user_id)
    filepath = os.path.join(path, "recent.bin")
    if os.path.exists(filepath):
        os.remove(filepath)
        return True
    return False


# ── Write path ────────────────────────────────────────────────────────────────
def _embed_and_store(row_id: int, server_id: str, channel_id: str,
                     user_id: str, content: str, timestamp: str):
    """
    Embed the message and save the embedding vector into PostgreSQL.
    Marks the row as embedded on success.
    RAISES exceptions so callers can retry.
    """
    from petey.gemini_api import get_embedding
    embedding = get_embedding(content)
    if not embedding:
        print(f"[MEMORY] get_embedding returned empty for row {row_id} (content: {content[:60]}...)")
        raise RuntimeError("Embedding returned empty")

    conn = sqlite3.connect()
    try:
        cur = conn.cursor()
        cur.execute("UPDATE memories SET embedding=%s, embedded=1 WHERE id=%s", (embedding, row_id))
        conn.commit()
    finally:
        conn.close()


def store_memory(server_id, channel_id, user_id, content: str):
    """
    Save a message to SQLite (synchronous, fast) then kick off embedding
    in a daemon thread so we never block the async event loop.

    Safe to call from a sync or async context.
    """
    if not content or not content.strip():
        return

    timestamp = datetime.datetime.utcnow().isoformat()
    row_id = None

    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "INSERT INTO memories (server_id, channel_id, user_id, message, timestamp) VALUES (?,?,?,?,?)",
            (str(server_id), str(channel_id), str(user_id), content.strip(), timestamp)
        )
        conn.commit()
        row_id = c.lastrowid
        conn.close()
    except Exception as e:
        print(f"[MEMORY] SQLite write failed: {e}")
        return

    # Fire-and-forget embedding in background thread (with error handling wrapper)
    def _safe_embed():
        try:
            _embed_and_store(row_id, server_id, channel_id, user_id, content, timestamp)
        except Exception as e:
            print(f"[MEMORY] Background embed failed for row {row_id}: {e}")
    
    t = threading.Thread(
        target=_safe_embed,
        daemon=True
    )
    t.start()

def chunk_text(text: str, size: int = 800, overlap: int = 150):
    """Basic sliding window chunker for long documents."""
    chunks = []
    i = 0
    while i < len(text):
        chunks.append(text[i:i+size])
        i += size - overlap
    return chunks

def store_document(server_id, filename: str, content: str):
    """
    Chunks a long document and stores it into long-term memory.
    Runs entirely in a background thread to prevent blocking the web UI.
    """
    if not content or not content.strip(): return

    def _process_doc():
        import time
        chunks = chunk_text(content.strip(), size=800, overlap=150)
        total = len(chunks)
        print(f"[MEMORY] Processing {total} chunks for {filename}...")
        embedded_count = 0
        failed_count = 0
        
        for i, chunk in enumerate(chunks):
            timestamp = datetime.datetime.utcnow().isoformat()
            try:
                conn = sqlite3.connect(DB_PATH)
                c = conn.cursor()
                chunk_text_formatted = f"[From {filename} part {i+1}] {chunk}"
                c.execute(
                    "INSERT INTO memories (server_id, channel_id, user_id, message, timestamp) VALUES (?,?,?,?,?)",
                    (str(server_id), f"DOC:{filename}", "SYSTEM_DOC", chunk_text_formatted, timestamp)
                )
                conn.commit()
                row_id = c.lastrowid
                conn.close()
                
                # Retry embedding up to 3 times with backoff
                for attempt in range(3):
                    try:
                        _embed_and_store(row_id, server_id, f"DOC:{filename}", "SYSTEM_DOC", chunk_text_formatted, timestamp)
                        embedded_count += 1
                        break
                    except Exception as embed_err:
                        if attempt < 2:
                            time.sleep(2 ** attempt)  # 1s, 2s backoff
                        else:
                            failed_count += 1
                            print(f"[MEMORY] Embedding failed after 3 attempts for chunk {i+1}: {embed_err}")
                
                # Rate limit: sleep between chunks to avoid Gemini API throttling
                if (i + 1) % 5 == 0:
                    time.sleep(1)  # 1s pause every 5 chunks
                    
                # Progress log every 50 chunks
                if (i + 1) % 50 == 0:
                    print(f"[MEMORY] Progress: {i+1}/{total} chunks processed ({embedded_count} embedded, {failed_count} failed)")
                    
            except Exception as e:
                failed_count += 1
                print(f"[MEMORY] store_document chunk {i+1} failed: {e}")
                
        print(f"[MEMORY] Finished {filename}: {embedded_count}/{total} embedded, {failed_count} failed")
        
    threading.Thread(target=_process_doc, daemon=True).start()

def get_documents(server_id: str) -> list:
    """Returns a list of unique document filenames uploaded to this server."""
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            "SELECT DISTINCT channel_id FROM memories WHERE server_id=? AND channel_id LIKE 'DOC:%%'",
            (str(server_id),)
        )
        rows = c.fetchall()
        conn.close()
        return [row['channel_id'].replace("DOC:", "") for row in rows]
    except Exception as e:
        print(f"[MEMORY] get_documents failed: {e}")
        return []

def delete_document(server_id: str, filename: str) -> bool:
    """Removes a document from memories table in PostgreSQL."""
    try:
        conn = sqlite3.connect()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM memories WHERE server_id=%s AND channel_id=%s", (str(server_id), f"DOC:{filename}"))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception as e:
        print(f"[MEMORY] delete_document failed: {e}")
        return False


# ── Read paths ────────────────────────────────────────────────────────────────
def get_recent_conversation(server_id, channel_id, limit: int = 6) -> str:
    """
    Returns the last `limit` messages for a specific channel as a
    human-readable string, oldest-first.

    Used for short-term context in the LLM prompt.
    """
    try:
        conn = sqlite3.connect(DB_PATH)
        c = conn.cursor()
        c.execute(
            """
            SELECT user_id, message, timestamp FROM memories
            WHERE server_id=? AND channel_id=?
            ORDER BY id DESC LIMIT ?
            """,
            (str(server_id), str(channel_id), limit)
        )
        rows = c.fetchall()
        conn.close()
    except Exception as e:
        print(f"[MEMORY] get_recent_conversation failed: {e}")
        return ""

    if not rows:
        return ""

    # Rows come back newest-first; reverse for chronological order
    rows.reverse()
    lines = []
    for user_id, message, timestamp in rows:
        try:
            ts = datetime.datetime.fromisoformat(timestamp).strftime("%H:%M")
        except Exception:
            ts = "??"
        lines.append(f"[{ts}] <{user_id}>: {message}")

    return "\n".join(lines)


def search_memories(query: str, server_id, limit: int = 5) -> str:
    """
    Semantic RAG search: find the most relevant past messages for a query in PostgreSQL.
    Returns a formatted string of results.

    Used as long-term context in the LLM prompt.
    """
    if not query or not query.strip():
        return ""
    
    print(f"[MEMORY] RAG: Searching for '{query[:80]}' in server_id={server_id}")

    try:
        from petey.gemini_api import get_embedding
        query_embedding = get_embedding(query)
        if not query_embedding:
            print("[MEMORY] RAG: get_embedding returned empty!")
            return ""
        
        print(f"[MEMORY] RAG: Got embedding ({len(query_embedding)} dims)")

        conn = sqlite3.connect()
        try:
            cur = conn.cursor()
            # Perform pgvector cosine distance similarity query filtered by server_id
            cur.execute(
                """
                SELECT message, timestamp, channel_id, (embedding <=> %s::vector) as dist
                FROM memories
                WHERE server_id=%s AND embedding IS NOT NULL
                ORDER BY dist ASC
                LIMIT %s
                """,
                (query_embedding, str(server_id), limit)
            )
            rows = cur.fetchall()
        finally:
            conn.close()

        if not rows:
            return ""

        lines = []
        for row in rows:
            doc = row['message']
            ts = row['timestamp']
            source = row['channel_id']
            dist = row['dist']

            # Cosine distance filter matching ChromaDB threshold
            if dist is not None and dist > 1.5:
                print(f"[MEMORY] RAG: Skipping result (dist={dist:.3f} > 1.5)")
                continue

            try:
                ts_fmt = datetime.datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts_fmt = ts
            
            # Tag document sources differently from chat messages
            if source.startswith("DOC:"):
                label = f"[📄 {source.replace('DOC:', '')} — {ts_fmt}]"
            else:
                label = f"[{ts_fmt}]"
            
            lines.append(f"{label} {doc}")
            print(f"[MEMORY] RAG hit (dist={dist:.3f}): {doc[:80]}...")

        return "\n".join(lines)

    except Exception as e:
        print(f"[MEMORY] search_memories failed: {e}")
        return ""

