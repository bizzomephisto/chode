# PETEY Optimal Server Setup Guide

> ⚠️ **CRITICAL TIP (Access is Everything):** PETEY is an AI with long-term memory that builds context based on your server's conversations. To get the smartest, most personalized experience, **give PETEY "View Channel" and "Read Message History" permissions in as many text channels as possible.** He learns and remembers what your server talks about by passively reading. If he is locked out of your general chats, his brain will be empty when you talk to him!

PETEY is designed to integrate seamlessly into your existing server layout without requiring multiple dedicated bot channels. 

---

## 🔧 1. The Config Channel
The only dedicated channel PETEY needs is a **Config Channel** (e.g. `#petey-control` or `#admin-chat`).
- **Access:** Restricted to Server Admins only.
- **Purpose:** A private space where server owners and admins can configure PETEY, verify prompts, and upload reference files directly to PETEY's long-term memory via the `/ingest` command.

## 💬 2. General Chatting
PETEY is allowed to chat with the general population in all public channels where he has permissions. Simply ping `@PETEY` or mention him by name to start a conversation. He will read and remember the discussion to build his RAG memories.

## 🎭 3. Channel-Specific Personalities
You can set up unique personalities for specific channels (up to a maximum of 3 channels). For example, you can configure PETEY to act as a strict Dungeon Master in `#dnd-games`, a snarky code reviewer in `#coding`, or a creative brainstorming partner in `#design-lab`. Manage these channel overrides from the dashboard.
