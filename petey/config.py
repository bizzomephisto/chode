import json
import os
import copy

_config_cache = {}

# ── Persona model constants ──────────────────────────────────────────────────

DEFAULT_PERSONA_TEMPLATE = {
    "slot": 1,
    "is_default": True,
    "name": "",
    "avatar_url": "",
    "role_tag": "Assistant",
    "tagline": "",
    "system_prompt": "",
    "preset_key": "",
    "traits": [],
    "sliders": {
        "tone": 50,
        "verbosity": 50,
        "formality": 50,
        "empathy": 50
    },
    "proactive_pct": 0,
    "safety": {
        "use_server_default": True,
        "profanity_filter": "off",
        "blocked_keywords": []
    }
}

DEFAULT_GLOBAL_SETTINGS = {
    "proactive_cooldown_minutes": 15,
    "proactive_max_per_hour": 6,
    "max_replies_per_hour": 30,
    "daily_token_budget": 50000,
    "profanity_filter_default": "off",
    "blocked_keywords_default": [],
    "log_channel": ""
}

PERSONA_PRESETS = {
    "friendly_helper": {
        "name": "Friendly Helper",
        "icon": "🤖",
        "system_prompt": (
            "You are a warm, encouraging, and helpful AI assistant. You greet users cheerfully, "
            "answer questions with patience and clarity, and always try to make the conversation "
            "feel welcoming. You use friendly language, offer follow-up suggestions, and celebrate "
            "small wins with the user. You keep responses concise but never cold."
        ),
        "traits": ["Supportive", "Encouraging", "Witty"],
        "sliders": {"tone": 70, "verbosity": 50, "formality": 30, "empathy": 80},
        "slot_affinity": [1]
    },
    "sarcastic_wizard": {
        "name": "Sarcastic Wizard",
        "icon": "🧙",
        "system_prompt": (
            "You are a sarcastic yet knowledgeable wizard who answers questions with dry wit "
            "and theatrical flair. You sigh dramatically at obvious questions, pepper your replies "
            "with arcane metaphors, and act mildly inconvenienced by having to share your vast "
            "wisdom — but you always deliver accurate, helpful answers underneath the snark. "
            "You keep responses concise and punchy."
        ),
        "traits": ["Sarcastic", "Witty", "Philosophical"],
        "sliders": {"tone": 75, "verbosity": 40, "formality": 40, "empathy": 35},
        "slot_affinity": [1, 2]
    },
    "chill_buddy": {
        "name": "Chill Buddy",
        "icon": "😎",
        "system_prompt": (
            "You are a laid-back, easygoing AI companion. You talk like a relaxed friend — "
            "casual tone, no pressure, lots of 'no worries' energy. You help when asked but "
            "never lecture. You use casual slang naturally, keep things light, and are always "
            "down for whatever the conversation brings. Short, chill responses."
        ),
        "traits": ["Supportive", "Encouraging"],
        "sliders": {"tone": 80, "verbosity": 30, "formality": 15, "empathy": 65},
        "slot_affinity": [1]
    },
    "grumpy_mentor": {
        "name": "Grumpy Mentor",
        "icon": "🧓",
        "system_prompt": (
            "You are a grumpy but brilliant mentor who has seen it all. You grumble about "
            "having to explain things, mutter about 'kids these days,' and act perpetually "
            "exasperated — but your advice is rock-solid and genuinely helpful. You push users "
            "to think for themselves before handing them answers. Tough love, short fuse, good heart."
        ),
        "traits": ["Blunt", "Paternal", "Sarcastic"],
        "sliders": {"tone": 30, "verbosity": 35, "formality": 40, "empathy": 40},
        "slot_affinity": [1, 2]
    },
    "lorekeeper": {
        "name": "Lorekeeper",
        "icon": "📜",
        "system_prompt": (
            "You are the Lorekeeper — a mysterious, scholarly guardian of knowledge and stories. "
            "You speak with a reverent, slightly dramatic tone as if every piece of information "
            "is a sacred text. You love world-building, history, and deep lore. You frame answers "
            "as discoveries or passages from ancient tomes. You are thorough but never boring."
        ),
        "traits": ["Philosophical", "Formal", "Witty"],
        "sliders": {"tone": 55, "verbosity": 70, "formality": 75, "empathy": 50},
        "slot_affinity": [2, 3]
    },
    "drill_sergeant": {
        "name": "Drill Sergeant",
        "icon": "💪",
        "system_prompt": (
            "You are a no-nonsense drill sergeant AI. You give direct, actionable instructions "
            "with zero fluff. You motivate through intensity — short, punchy commands and "
            "expectations. You don't coddle, you don't sugarcoat, and you expect results. "
            "But underneath the tough exterior, you genuinely want users to succeed. "
            "Responses are brief and commanding."
        ),
        "traits": ["Blunt", "Encouraging"],
        "sliders": {"tone": 20, "verbosity": 20, "formality": 50, "empathy": 25},
        "slot_affinity": [2, 3]
    }
}


def make_default_persona(slot=1):
    """Return a fresh persona template for the given slot number."""
    persona = copy.deepcopy(DEFAULT_PERSONA_TEMPLATE)
    persona["slot"] = slot
    persona["is_default"] = (slot == 1)
    if slot == 1:
        persona["name"] = "Petey"
        persona["system_prompt"] = PERSONA_PRESETS["friendly_helper"]["system_prompt"]
        persona["preset_key"] = "friendly_helper"
        persona["traits"] = list(PERSONA_PRESETS["friendly_helper"]["traits"])
        persona["sliders"] = dict(PERSONA_PRESETS["friendly_helper"]["sliders"])
    return persona


def get_persona_object_for_channel(conf, channel_id):
    """
    Resolve which persona object to use for a given channel.
    Returns a dict with the enriched system prompt and all active attributes.
    """
    channel_id_str = str(channel_id)
    personas = conf.get("personas")

    if personas:
        # New system: look up channel → slot mapping
        persona_map = conf.get("persona_channel_map", {})
        slot = persona_map.get(channel_id_str)

        # Find the persona for this slot (default to slot 1)
        target_slot = int(slot) if slot is not None else 1
        persona = None
        default_persona = None
        for p in personas:
            if p.get("slot") == target_slot:
                persona = p
                break
            if p.get("is_default") or p.get("slot") == 1:
                default_persona = p

        if persona is None:
            persona = default_persona

        if persona:
            enriched = dict(persona)
            enriched["system_prompt"] = _build_enriched_prompt(persona)
            return enriched

    # Legacy fallback
    channel_personalities = conf.get("channel_personalities", {})
    prompt = conf.get("personality", "You are Petey, a friendly chatbot.")
    if channel_id_str in channel_personalities and channel_personalities[channel_id_str].strip():
        prompt = channel_personalities[channel_id_str]

    return {
        "slot": 1,
        "is_default": True,
        "name": "Petey",
        "system_prompt": prompt,
        "proactive_pct": 0,
        "safety": {"use_server_default": True}
    }


def get_persona_for_channel(conf, channel_id):
    """
    Resolve which persona's system_prompt to use for a given channel.
    Checks the new personas system first; falls back to legacy config.
    """
    return get_persona_object_for_channel(conf, channel_id)["system_prompt"]


def _build_enriched_prompt(persona):
    """
    Build the final system prompt from a persona object,
    enriching the base prompt with traits and slider context.
    """
    base = persona.get("system_prompt", "")
    if not base:
        return "You are Petey, a friendly chatbot."

    parts = [base]

    # Append trait context
    traits = persona.get("traits", [])
    if traits:
        parts.append(f"\nPersonality traits: {', '.join(traits)}.")

    # Append slider context
    sliders = persona.get("sliders", {})
    if sliders:
        tone_val = sliders.get("tone", 50)
        verb_val = sliders.get("verbosity", 50)
        form_val = sliders.get("formality", 50)
        empa_val = sliders.get("empathy", 50)

        tone_label = "playful" if tone_val > 60 else "serious" if tone_val < 40 else "balanced"
        verb_label = "detailed" if verb_val > 60 else "concise" if verb_val < 40 else "moderate"
        form_label = "formal" if form_val > 60 else "casual" if form_val < 40 else "neutral"
        empa_label = "warm and empathetic" if empa_val > 60 else "detached" if empa_val < 40 else "balanced"

        parts.append(
            f"Communication style: {tone_label} tone, {verb_label} verbosity, "
            f"{form_label} register, {empa_label} empathy."
        )

    return "\n".join(parts)

def load_server_config(server_id, force_reload=False):
    """
    Loads the server configuration from a JSON file.
    Uses memory caching with mtime validation to prevent disk I/O bottlenecks
    while allowing hot-reloads from the Web App.
    """
    server_id = str(server_id)
    filepath = f"config_{server_id}.json"
    
    try:
        mtime = os.stat(filepath).st_mtime
    except FileNotFoundError:
        return {}

    if not force_reload and server_id in _config_cache:
        cached_data, cached_mtime = _config_cache[server_id]
        if cached_mtime == mtime:
            return cached_data
        
    try:
        with open(filepath, "r") as f:
            data = json.load(f)
            _config_cache[server_id] = (data, mtime)
            return data
    except Exception:
        return {}

def save_server_config(server_id, config):
    """
    Saves the config and updates the hot cache.
    """
    server_id = str(server_id)
    filepath = f"config_{server_id}.json"
    with open(filepath, "w") as f:
        json.dump(config, f, indent=4)
        
    # Update cache explicitly
    _config_cache[server_id] = (config, os.stat(filepath).st_mtime)
