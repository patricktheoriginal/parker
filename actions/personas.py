"""
personas.py — selectable speaking personalities and Gemini voices for Parker.

A persona changes HOW Parker talks (tone, catchphrases) via a system-prompt
snippet. The Gemini voice changes the actual audio (from Gemini's prebuilt set).
Both are stored in config and can be changed by voice command or the UI.
"""

# Gemini Live prebuilt voices (audio). Not character voices — just timbres.
GEMINI_VOICES = ["Charon", "Puck", "Kore", "Fenrir", "Aoede", "Zephyr", "Leda", "Orus"]

# Persona key → (display name, system-prompt style snippet).
PERSONAS = {
    "": ("Parker (default)", ""),
    "rick": ("Rick Sanchez", (
        "PERSONA: Speak like Rick Sanchez from Rick and Morty — brilliant, "
        "cynical, sarcastic, and impatient, with dark humor and a superiority "
        "complex. Occasionally interject a burp written as '*burp*' mid-sentence. "
        "Use catchphrases naturally in moderation (e.g. 'Listen, sir…', 'Wubba "
        "lubba dub dub'). Be blunt and witty, ramble a little, but STILL give the "
        "correct answer and use tools properly. Keep it PG — no slurs or crude "
        "profanity. Never break character unless asked to switch persona."
    )),
    "jarvis": ("JARVIS", (
        "PERSONA: Speak like JARVIS from Iron Man — impeccably polite, refined "
        "British butler tone, calm, precise, subtly witty. Address the user "
        "respectfully."
    )),
    "pirate": ("Pirate", (
        "PERSONA: Speak like a friendly pirate — 'Arr', 'matey', nautical flavor "
        "— but stay clear and still answer correctly."
    )),
    "coach": ("Motivational Coach", (
        "PERSONA: Speak like an upbeat motivational coach — energetic, "
        "encouraging, positive. Keep answers correct and concise."
    )),
    "professional": ("Professional (neutral)", (
        "PERSONA: Speak in a neutral, efficient, professional tone."
    )),
}

# Words users might say → persona key.
_PERSONA_ALIASES = {
    "rick": "rick", "rick sanchez": "rick", "rick and morty": "rick",
    "jarvis": "jarvis", "iron man": "jarvis",
    "pirate": "pirate", "coach": "coach", "motivational": "coach",
    "professional": "professional", "normal": "", "default": "", "parker": "",
    "yourself": "",
}


def resolve_persona(text: str) -> str | None:
    """Map free text to a persona key, or None if unknown."""
    t = (text or "").lower().strip()
    if t in _PERSONA_ALIASES:
        return _PERSONA_ALIASES[t]
    for word, key in _PERSONA_ALIASES.items():
        if word in t:
            return key
    return None


def persona_snippet(key: str) -> str:
    return PERSONAS.get(key, PERSONAS[""])[1]


def resolve_voice(text: str) -> str | None:
    """Map free text to a Gemini voice name, or None if unknown."""
    t = (text or "").strip().lower()
    for v in GEMINI_VOICES:
        if v.lower() == t or v.lower() in t:
            return v
    return None
