"""faces.py - Bifrost ASCII face definitions.

Ported from pwnagotchi/ui/faces.py with full face set.
"""

LOOK_R = '( \u2686_\u2686)'
LOOK_L = '(\u2609_\u2609 )'
LOOK_R_HAPPY = '( \u25d5\u203f\u25d5)'
LOOK_L_HAPPY = '(\u25d5\u203f\u25d5 )'
SLEEP = '(\u21c0\u203f\u203f\u21bc)'
SLEEP2 = '(\u2256\u203f\u203f\u2256)'
AWAKE = '(\u25d5\u203f\u203f\u25d5)'
BORED = '(-__-)'
INTENSE = '(\u00b0\u25c3\u25c3\u00b0)'
COOL = '(\u2310\u25a0_\u25a0)'
HAPPY = '(\u2022\u203f\u203f\u2022)'
GRATEFUL = '(^\u203f\u203f^)'
EXCITED = '(\u1d54\u25e1\u25e1\u1d54)'
MOTIVATED = '(\u263c\u203f\u203f\u263c)'
DEMOTIVATED = '(\u2256__\u2256)'
SMART = '(\u271c\u203f\u203f\u271c)'
LONELY = '(\u0628__\u0628)'
SAD = '(\u2565\u2601\u2565 )'
ANGRY = "(-_-')"
FRIEND = '(\u2665\u203f\u203f\u2665)'
BROKEN = '(\u2613\u203f\u203f\u2613)'
DEBUG = '(#__#)'
UPLOAD = '(1__0)'
UPLOAD1 = '(1__1)'
UPLOAD2 = '(0__1)'
STARTING = '(. .)'
READY = '( ^_^)'

# Map mood name → face constant
MOOD_FACES = {
    'starting':    STARTING,
    'ready':       READY,
    'sleeping':    SLEEP,
    'awake':       AWAKE,
    'bored':       BORED,
    'sad':         SAD,
    'angry':       ANGRY,
    'excited':     EXCITED,
    'lonely':      LONELY,
    'grateful':    GRATEFUL,
    'happy':       HAPPY,
    'cool':        COOL,
    'intense':     INTENSE,
    'motivated':   MOTIVATED,
    'demotivated': DEMOTIVATED,
    'friend':      FRIEND,
    'broken':      BROKEN,
    'debug':       DEBUG,
    'smart':       SMART,
}


def load_from_config(config):
    """Override faces from config dict (e.g. custom emojis)."""
    for face_name, face_value in (config or {}).items():
        key = face_name.upper()
        if key in globals():
            globals()[key] = face_value
        lower = face_name.lower()
        if lower in MOOD_FACES:
            MOOD_FACES[lower] = face_value
