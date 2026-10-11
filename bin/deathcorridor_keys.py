"""deathcorridor_keys: offer a keyboard shortcut for opening the task manager, with the user's consent.

Omarchy keeps personal shortcuts in ~/.config/hypr/bindings.lua. We only ever add (or remove) one block of lines between two
marker comments there, after a backup, and only when the user chose a shortcut in deathcorridor. Nothing is touched at install time.
"""
import os, re, shutil, subprocess, time, json
import deathcorridor_private as PV

STATE_DIR = os.path.expanduser("~/.local/state/deathcorridor")
STATE_FILE = os.path.join(STATE_DIR, "keys.json")
BEGIN = "-- >>> deathcorridor shortcut (added by deathcorridor; press k inside it to remove, or delete these lines)"
END = "-- <<< deathcorridor shortcut"
MODS = ("SUPER", "CTRL", "ALT", "SHIFT")
ALIAS = {"WIN": "SUPER", "META": "SUPER", "MOD4": "SUPER", "CONTROL": "CTRL", "OPTION": "ALT", "DEL": "DELETE", "SUPR": "DELETE",
         "ENTER": "RETURN", "ESC": "ESCAPE"}
NAMED = {"DELETE", "RETURN", "SPACE", "ESCAPE", "TAB", "PRINT", "BACKSPACE", "INSERT", "HOME", "END", "UP", "DOWN", "LEFT", "RIGHT"} | {f"F{i}" for i in range(1, 13)}
PRESETS = [("CTRL + ALT + DELETE", "Ctrl + Alt + Delete"), ("SUPER + CTRL + M", "Super + Ctrl + M")]


def bindings_file():
    return os.environ.get("DEATHCORRIDOR_BINDINGS") or os.path.expanduser("~/.config/hypr/bindings.lua")


def launcher():
    return os.path.join(os.path.dirname(os.path.realpath(__file__)), "deathcorridor-open")


def parse_combo(text):
    """'ctrl+alt+del' / 'SUPER CTRL + M' -> 'CTRL + ALT + DELETE' (canonical), or None when it is not a usable shortcut."""
    toks = [ALIAS.get(t, t) for t in re.split(r"[\s+]+", text.upper().strip()) if t]
    if len(toks) < 2: return None
    key, mods = toks[-1], toks[:-1]
    if any(m not in MODS for m in mods) or len(set(mods)) != len(mods): return None
    if not (len(key) == 1 and key.isalnum()) and key not in NAMED: return None
    return " + ".join([m for m in MODS if m in mods] + [key])


def _norm(mods, key):
    return (frozenset(ALIAS.get(m, m) for m in mods.upper().split()), ALIAS.get(key.strip().upper(), key.strip().upper()))


def existing():
    """{(mods, key): description} for every shortcut Omarchy knows about (defaults + the user's)."""
    try: out = subprocess.run(["omarchy", "menu", "keybindings", "--print"], capture_output=True, timeout=6).stdout.decode("utf-8", "replace")
    except Exception: return {}
    d = {}
    for line in out.splitlines():
        m = re.match(r"\s*(.+?)\s+\+\s+(\S+)\s+→\s+(.*)$", line)
        if m: d[_norm(m[1], m[2])] = m[3].strip()
    return d


def conflict(combo):
    """What the shortcut does today ('' when it is free). Our own deathcorridor binding does not count."""
    mods, _, key = combo.rpartition(" + ")
    desc = existing().get(_norm(mods.replace(" + ", " "), key), "")
    return "" if "deathcorridor" in desc.lower() or desc.lower().startswith("task manager") else desc


def read():
    try: return open(bindings_file(), encoding="utf-8").read()
    except OSError: return ""


def our_block(text=None):
    text = read() if text is None else text
    m = re.search(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", text, re.S)
    return m


def our_combo():
    m = our_block()
    b = re.search(r'o\.bind\("([^"]+)"', m[0]) if m else None
    return b[1] if b else None


def user_has_own_binding():
    """The user already binds deathcorridor by hand: nothing to ask."""
    text = read(); m = our_block(text)
    if m: text = text.replace(m[0], "")
    return any("deathcorridor-open" in l and not l.strip().startswith("--") for l in text.splitlines())


def state():
    try: return json.load(open(STATE_FILE))
    except Exception: return {}


def mark_asked(): PV.write(STATE_FILE, json.dumps(dict(state(), asked=True)))


def should_ask():
    if not shutil.which("omarchy"): return False        # `o.bind` only exists in Omarchy's Hyprland config
    return not state().get("asked") and not our_block() and not user_has_own_binding() and os.path.exists(bindings_file())


def _lua(s): return s.replace("\\", "\\\\").replace('"', '\\"')


def _backup():
    f = bindings_file()
    if os.path.exists(f):
        os.makedirs(os.path.join(STATE_DIR, "backups"), mode=0o700, exist_ok=True)
        dst = os.path.join(STATE_DIR, "backups", "bindings.lua." + time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(f, dst); os.chmod(dst, 0o600)


def _write(text):
    f = bindings_file()
    try: mode = os.stat(f).st_mode & 0o777
    except OSError: mode = 0o644
    tmp = f + ".deathcorridor.tmp"
    with open(tmp, "w", encoding="utf-8") as fh: fh.write(text)
    os.chmod(tmp, mode); os.replace(tmp, f)           # keeps the user's own permissions on their file


def _reload():
    if not os.environ.get("DEATHCORRIDOR_BINDINGS"):
        try: subprocess.run(["hyprctl", "reload"], capture_output=True, timeout=8)
        except (OSError, subprocess.SubprocessError): pass


def apply(combo):
    """Add (or replace) our block. Returns a one-line message. Raises OSError if the file cannot be written."""
    taken = conflict(combo)
    text = read(); m = our_block(text)
    if m: text = text.replace(m[0], "")
    lines = [BEGIN] + ([f'hl.unbind("{combo}")'] if taken else []) + \
            [f'o.bind("{combo}", "Task manager (deathcorridor)", "{_lua(launcher())}", {{ dont_inhibit = true }})', END]
    _backup()
    _write(text.rstrip("\n") + "\n\n" + "\n".join(lines) + "\n")
    mark_asked(); _reload()
    return f"Done: {combo} opens the task manager" + (f" (it replaced '{taken}')" if taken else "")


def remove():
    text = read(); m = our_block(text)
    if not m: return "There was no shortcut to remove"
    _backup(); _write(text.replace(m[0], "").rstrip("\n") + "\n"); _reload()
    return "Shortcut removed. Your other shortcuts were not touched"
