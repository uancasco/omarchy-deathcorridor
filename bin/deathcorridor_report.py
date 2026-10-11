"""deathcorridor_report: read-only evidence gathering and report building for deathcorridor.

Nothing here changes the system. It reads what Linux already keeps (coredumpctl, the journal, /proc) and
writes one markdown "case file" with machine-identifying details redacted, ready to paste to an AI or open in `claude`.
"""
import datetime, getpass, json, os, platform, re, shutil, signal, socket, subprocess, time
import deathcorridor_private as PV

HOME, USER, HOST = os.path.expanduser("~"), getpass.getuser(), socket.gethostname()
STATE_DIR = os.path.expanduser("~/.local/state/deathcorridor")
REPORT_DIR = os.path.join(STATE_DIR, "reports")
DISMISS_FILE = os.path.join(STATE_DIR, "dismissed.json")
MAX_LINES = 60                                       # cap per section: a report an AI can read in one go


def sh(*a, timeout=8):
    try: return subprocess.run(a, capture_output=True, timeout=timeout).stdout.decode("utf-8", "replace")
    except Exception: return ""


def sig_name(n):
    try: return signal.Signals(n).name
    except Exception: return f"SIG{n}"


# plain-language reading of a crash signal: (short for the list, longer for the detail strip)
SIGNALS = {
    "SIGSEGV": ("memory access error", "it touched memory it was not allowed to use (a classic bug: segfault)"),
    "SIGABRT": ("aborted itself", "it detected an internal error and stopped on purpose"),
    "SIGILL": ("invalid instruction", "it tried to run an instruction the CPU rejected (corrupted code, wrong build, or a hardware/compat problem)"),
    "SIGTRAP": ("internal check failed", "it hit a built-in breakpoint or assertion, meaning its own code decided something was wrong"),
    "SIGBUS": ("memory or disk access error", "it read memory or a mapped file that was not there (often a disk or filesystem problem)"),
    "SIGFPE": ("math error", "it did an invalid arithmetic operation, such as dividing by zero"),
    "SIGKILL": ("killed", "something force-killed it, often the kernel when the system ran out of memory"),
}


def explain(sig): return SIGNALS.get(sig, (f"crashed ({sig})", f"it was stopped by signal {sig}"))


def ago(ts):
    s = max(0, time.time() - ts)
    return (f"{int(s)}s" if s < 90 else f"{int(s / 60)}min" if s < 5400 else f"{s / 3600:.0f}h" if s < 172800 else f"{s / 86400:.0f}d") + " ago"


def span(secs):
    return f"{int(secs)}s" if secs < 90 else f"{int(secs / 60)}min" if secs < 5400 else f"{secs / 3600:.0f}h" if secs < 172800 else f"{secs / 86400:.0f} days"


def when(ts): return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- crashes (systemd-coredump keeps this for free)

def load_dismissed():
    try: return json.load(open(DISMISS_FILE))
    except Exception: return {}


def dismiss(exe, ts):
    """Hide a crash group until it crashes again (a newer crash than `ts` brings it back)."""
    d = load_dismissed(); d[exe] = ts
    PV.write(DISMISS_FILE, json.dumps(d))


def crash_groups(days=30):
    """Crashes of the last `days` days grouped by executable, newest first. Programs that are no longer installed, and
    groups the user dismissed (with nothing newer since), are left out: nothing to act on."""
    try: rows = json.loads(sh("coredumpctl", "list", "--json=short", "--since", f"-{days} days", "--no-pager") or "[]")
    except ValueError: return []
    dis, g = load_dismissed(), {}
    for r in rows:
        exe = r.get("exe") or "unknown"
        a = g.setdefault(exe, dict(exe=exe, name=os.path.basename(exe), entries=[], installed=os.path.exists(exe)))
        a["entries"].append(dict(ts=r["time"] / 1e6, pid=r["pid"], sig=sig_name(r["sig"]) if r.get("sig") else "?", core=r.get("corefile", "")))
    out = []
    for a in g.values():
        a["entries"].sort(key=lambda e: e["ts"])
        a.update(count=len(a["entries"]), first=a["entries"][0]["ts"], last=a["entries"][-1]["ts"], sigs=sorted({e["sig"] for e in a["entries"]}))
        if not a["installed"] or a["last"] <= dis.get(a["exe"], 0): continue
        out.append(a)
    return sorted(out, key=lambda a: a["last"], reverse=True)


# ---------------------------------------------------------------- redaction

SECRET = re.compile(r"(?i)((?:token|secret|passw(?:or)?d|passwd|api[-_]?key|apikey|authorization|bearer|cookie|credential)[\"'=: ]+)([^\s\"']+)")
LONG = re.compile(r"\b[A-Za-z0-9]{32,}\b")


def redact(text):
    """-> (text, number of items masked). Masks secrets and long opaque ids, and swaps home path / user / host for placeholders."""
    n = 0
    def hide(m):
        nonlocal n; n += 1
        return m.group(1) + "«redacted»" if m.lastindex else "«redacted»"
    text = SECRET.sub(hide, text)
    text = LONG.sub(hide, text)
    text = text.replace(HOME, "~")
    text = re.sub(rf"\b{re.escape(USER)}\b", "<user>", text)
    if HOST: text = re.sub(rf"\b{re.escape(HOST)}\b", "<host>", text)
    return text, n


# ---------------------------------------------------------------- evidence

def clip(text, n=MAX_LINES):
    lines = [l for l in text.splitlines() if l.strip()]
    return "\n".join(lines[:n] + ([f"... ({len(lines) - n} more lines cut)"] if len(lines) > n else [])) or "(nothing found)"


def block(text): return "```\n" + text + "\n```"


def cpu_model():
    try:
        for l in open("/proc/cpuinfo"):
            if l.startswith("model name"): return l.split(":", 1)[1].strip()
    except OSError: pass
    return f"{os.cpu_count()} threads"


def system_facts():
    osr = {}
    try:
        for l in open("/etc/os-release"):
            k, _, v = l.partition("=")
            osr[k] = v.strip().strip('"')
    except OSError: pass
    gpu = sh("nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader").strip()
    mi = {}
    for l in open("/proc/meminfo"):
        k, _, v = l.partition(":")
        mi[k] = int(v.split()[0]) // 1024 if v.split() else 0
    return (f"{osr.get('PRETTY_NAME', platform.platform())}, kernel {platform.release()}, {cpu_model()} ({os.cpu_count()} threads), "
            f"{mi.get('MemTotal', 0)} MB RAM ({mi.get('MemAvailable', 0)} MB free), GPU {gpu or 'none detected'}, "
            f"{os.environ.get('XDG_CURRENT_DESKTOP', '?')} on {os.environ.get('XDG_SESSION_TYPE', '?')}")


def package_of(exe):
    if not (exe and os.path.exists(exe)): return None
    own = sh("pacman", "-Qo", "-q", exe).strip()
    return sh("pacman", "-Q", own).strip() if own else ""


def cmdline(pid):
    try: return open(f"/proc/{pid}/cmdline", "rb").read().replace(b"\0", b" ").decode("utf-8", "replace").strip()
    except OSError: return ""


TRAP = re.compile(r"in (\S+?)\[([0-9a-f]+),")
KIND = re.compile(r"(trap \w+|segfault at \S+|general protection fault)")


def crash_sites(entries):
    """Where in the code each crash happened, from the kernel's trap/segfault line: [(ts, kind, module, offset)].
    The same module+offset across crashes means one deterministic bug; different ones mean something random (memory, hardware)."""
    sites = []
    for e in entries[-6:]:
        out = sh("journalctl", "_TRANSPORT=kernel", "--no-pager", "-q", "-o", "short-iso", "-S", when(e["ts"] - 240), "-U", when(e["ts"] + 120), "-g", rf"\[{e['pid']}\]")
        for l in out.splitlines():
            m = TRAP.search(l)
            if m:
                k = KIND.search(l)
                sites.append((e["ts"], k.group(1).split(" at ")[0] if k else "crash", m.group(1), m.group(2)))
                break
    return sites


def kernel_events(around, window=1800):
    """Segfault / OOM / GPU / disk anomalies in the kernel log of the last 7 days, each tagged with how far it is from the incidents."""
    out = sh("journalctl", "_TRANSPORT=kernel", "--no-pager", "-q", "-o", "short-iso", "-S", "-7days", "-g",
             "out of memory|oom-kill|Xid|NVRM: (?:GPU|Xid)|amdgpu.*(timeout|hang)|blocked for more than|hung task|I/O error|EXT4-fs error|BTRFS.*error|nvme.*(timeout|reset)")
    rows = []
    for l in out.splitlines():
        try: t = datetime.datetime.fromisoformat(l.split()[0]).timestamp()
        except Exception: continue
        if around:
            d = min(around, key=lambda a: abs(a - t)); d = t - d
            rel = f"{span(abs(d))} {'after' if d > 0 else 'before'} an incident"
            if abs(d) > 1800: continue                      # only what is close enough to be a suspect
        else: rel = ago(t)
        rows.append(f"{l[:230]}   <-- {rel}")
    return rows


def journal_around(ts, needles, pad=(180, 60)):
    out = sh("journalctl", "--no-pager", "-q", "-o", "short-iso", "-S", when(ts - pad[0]), "-U", when(ts + pad[1]))
    keys = [n.lower() for n in needles if n]
    return clip("\n".join(l for l in out.splitlines() if any(k in l.lower() for k in keys)), 30)


def journal_recent(comm, minutes=30):
    out = sh("journalctl", "--no-pager", "-q", "-o", "short-iso", "-S", f"-{minutes}min", "-p", "warning")
    return clip("\n".join(l for l in out.splitlines() if comm.lower()[:15] in l.lower()), 30)


def crash_detail(pid):
    """-> (readable excerpt, systemd unit, [(module path, mtime)] most-seen first)."""
    txt = sh("coredumpctl", "info", str(pid), "--no-pager")
    keep, on, unit = [], False, ""
    for l in txt.splitlines():
        st = l.strip()
        if st.startswith("User Unit:"): unit = re.sub(r"\\x([0-9a-f]{2})", lambda m: chr(int(m.group(1), 16)), st.split(":", 1)[1].strip())      # systemd escapes "-" as \x2d
        if st.startswith(("Signal:", "Timestamp:", "Command Line:", "Storage:")) and not on: keep.append(st)
        if "Message:" in l: on = True
        if on: keep.append(l.rstrip())
    counts = {}
    for m in re.findall(r"(/\S+?) \+ 0x", txt): counts[m] = counts.get(m, 0) + 1
    mods = []
    for m, _ in sorted(counts.items(), key=lambda kv: -kv[1])[:3]:
        try: mods.append((m, os.path.getmtime(m)))
        except OSError: pass
    return clip("\n".join(keep), 40), unit, mods


def fault_instruction(pid):
    """The exact instruction that faulted, read from the core with gdb (read-only). ud2 / int3 mean the program stopped itself on purpose."""
    if not shutil.which("gdb"): return None
    out = sh("coredumpctl", "debug", str(pid), "--debugger=gdb", '--debugger-arguments=-batch -ex "x/3i $pc"', timeout=40)
    rows = [l.strip() for l in out.splitlines() if re.match(r"\s*(=>)?\s*0x[0-9a-f]+:", l)]
    if not rows: return None
    first = rows[0].replace("=>", "").strip()
    ins = first.split("\t", 1)[-1].strip()
    why = {"ud2": "ud2 is a deliberate trap: the program's own assertion/CHECK failed or it aborted on purpose. It is NOT the CPU rejecting an instruction it does not support.",
           "int3": "int3 is a breakpoint trap: a software assertion or debug-style abort inside the program."}.get(ins.split()[0] if ins else "", "")
    return "\n".join(rows[:3]) + ("\n" + why if why else "")


RELEVANT = re.compile(r"^(linux|linux-omarchy|linux-zen|linux-lts|nvidia.*|mesa|lib32-mesa|glibc|systemd|hyprland|xorg-xwayland|electron\d*|gcc-libs)$")


def recent_changes(since, program_words):
    """Package changes from pacman.log since `since` that could plausibly matter (kernel, GPU stack, libc, the program itself)."""
    rx = re.compile(r"^\[(\S+)\] \[ALPM\] (upgraded|installed|removed|downgraded) (\S+) \((.*)\)")
    rel, other = [], 0
    try: lines = open("/var/log/pacman.log", errors="replace").read().splitlines()
    except OSError: return None
    for l in lines[-6000:]:
        m = rx.match(l)
        if not m: continue
        try: t = datetime.datetime.strptime(m.group(1), "%Y-%m-%dT%H:%M:%S%z").timestamp()
        except ValueError: continue
        if t < since: continue
        pkg = m.group(3)
        if (RELEVANT.match(pkg) or any(w and w in pkg.lower() for w in program_words)) and m.group(2) != "removed": rel.append(f"{m.group(1)[:16]}  {m.group(2)} {pkg} ({m.group(4)})")
        else: other += 1
    return rel[-14:], other


def pressure():
    out = []
    for r in ("cpu", "memory", "io"):
        try: out.append(f"{r}: " + open(f"/proc/pressure/{r}").read().splitlines()[0].replace("some ", ""))
        except OSError: pass
    return "; ".join(out)


def live_state(pids):
    out = []
    for p in pids[:5]:
        try:
            st = dict(l.split(":", 1) for l in open(f"/proc/{p}/status").read().splitlines() if ":" in l)
            wchan = open(f"/proc/{p}/wchan").read().strip() or "-"
            fds = len(os.listdir(f"/proc/{p}/fd"))
            age = sh("ps", "-o", "etimes=", "-p", str(p)).strip()
            out.append(f"pid {p} ({st.get('Name', '?').strip()}): state {st.get('State', '?').strip()}, {st.get('Threads', '?').strip()} threads, RSS {st.get('VmRSS', '?').strip()}, "
                       f"{fds} open files, kernel wait point: {wchan}, running {age}s")
        except OSError: pass
    return "\n".join(out)


def thread_hotspots(pid):
    """Which threads burn CPU (a spinning UI thread is the usual signature of a hang), sampled over 2 seconds."""
    out = sh("top", "-H", "-b", "-n", "2", "-d", "1", "-p", str(pid), timeout=6)
    last = out.split("top - ")[-1].splitlines()
    rows = [l for l in last if re.match(r"\s*\d+\s", l)]
    return "\n".join(["TID  CPU%  STATE  THREAD NAME"] + [f"{l.split()[0]}  {l.split()[8]}  {l.split()[7]}  {' '.join(l.split()[11:])}" for l in rows[:6]]) if rows else ""


# ---------------------------------------------------------------- the case file

def build_report(subject):
    """subject: dict(name, pids=[...], exe=None, note=""). -> (short case file in markdown, number of masked items). Only what helps fix it."""
    pids = [p for p in subject.get("pids", []) if os.path.exists(f"/proc/{p}")]
    exe = subject.get("exe")
    if not exe:
        for p in pids:
            try: exe = os.readlink(f"/proc/{p}/exe"); break
            except OSError: pass
    group = next((g for g in crash_groups_all(30) if exe and g["exe"] == exe), None)
    name = subject.get("name") or (os.path.basename(exe) if exe else "program")
    comm = os.path.basename(exe) if exe else name
    s = [f"# Case: {name}", ""]
    if exe:
        pkg = package_of(exe)
        s += [f"- Program: `{exe}`, " + (f"package {pkg}" if pkg else "not from a package (vendor installer / AppImage / manual)" if os.path.exists(exe) else "NOT installed anymore")]
    s += [f"- Machine: {system_facts()}"]
    if subject.get("note"): s += [f"- Now: {subject['note']}"]
    detail = unit = ""
    if group:
        e0, e1 = group["entries"][0], group["entries"][-1]
        gaps = [b["ts"] - a["ts"] for a, b in zip(group["entries"], group["entries"][1:])]
        s += [f"- Crashes: {group['count']} in 30 days ({', '.join(group['sigs'])}), last {ago(e1['ts'])}" + (f", about every {span(sum(gaps) / len(gaps))}" if gaps else "") + (" (RECURRING)" if group["count"] >= 2 else " (single event)")]
        detail, unit, mods = crash_detail(e1["pid"])
        sites = crash_sites(group["entries"])
        if sites:
            mods_ = {(m, o) for _, _, m, o in sites}
            s += [f"- Crash site: " + (f"identical every time, `{next(iter(mods_))[0]}` offset 0x{next(iter(mods_))[1]} (a deterministic bug)" if len(sites) >= 2 and len(mods_) == 1
                                        else "; ".join(f"`{m}`+0x{o}" for _, _, m, o in sites[-3:]) + (" (different places: suspect memory/hardware)" if len(mods_) > 1 else ""))]
        fault = fault_instruction(e1["pid"]) if e1.get("core") == "present" else None
        if fault: s += ["- Faulting instruction: " + fault.splitlines()[0].replace("=>", "").strip().split("\t")[-1] + (" (" + fault.splitlines()[-1].split(":")[0] + ")" if fault.count("\n") >= 3 else "")]
        if mods: s += ["- Stack is in: " + ", ".join(f"`{m}` (file dated {when(t)[:10]})" for m, t in mods)]
        if unit: s += [f"- Started by: `{unit}`" + (" (autostart at login)" if "autostart" in unit else "")]
    elif not pids: s += ["- Not running and no recorded crash: if it fails to start, the journal below is the clue."]
    pr = pressure()
    if re.search(r"avg\d+=(?:[1-9]\d|\d\.[1-9]\d?)\b", pr or ""): s += [f"- System pressure (high): {pr}"]      # only worth mentioning when something is stalled
    s += [""]

    if subject.get("tracking"): s += ["## Followed by deathcorridor (the user flagged this program to track the problem)", block(subject["tracking"]), ""]
    if group:
        stack = "\n".join(detail.splitlines()[:16])
        s += ["## Crash (latest)", block(stack), "", "## Journal around it", block(journal_around(group["last"], [comm, name, "coredump"])[:1800]), ""]
    elif pids:
        s += ["## Process", block(f"command: {cmdline(pids[0])[:300]}\n" + live_state(pids))]
        hot = thread_hotspots(pids[0])
        if hot: s += ["", "## Busiest threads (2 s sample)", block(hot)]
        warn = journal_recent(comm)
        if "(nothing found)" not in warn: s += ["", "## Recent warnings", block(warn)]
        s += [""]
    since = (group["first"] if group else time.time()) - 21 * 86400
    ch = recent_changes(since, [w.lower() for w in {comm, name.split()[0]} if len(w) > 3])
    if ch and ch[0]: s += ["## Relevant updates in the 3 weeks before", block("\n".join(ch[0][-5:])), ""]
    ker = kernel_events([e["ts"] for e in group["entries"]] if group else [])
    if ker: s += ["## Kernel warnings near it", block(clip("\n".join(ker), 6)), ""]
    return redact("\n".join(s))


def crash_groups_all(days=30):
    """Like crash_groups but keeps uninstalled / dismissed ones: the report for a given program still wants its full history."""
    try: rows = json.loads(sh("coredumpctl", "list", "--json=short", "--since", f"-{days} days", "--no-pager") or "[]")
    except ValueError: return []
    g = {}
    for r in rows:
        exe = r.get("exe") or "unknown"
        a = g.setdefault(exe, dict(exe=exe, name=os.path.basename(exe), entries=[]))
        a["entries"].append(dict(ts=r["time"] / 1e6, pid=r["pid"], sig=sig_name(r["sig"]) if r.get("sig") else "?", core=r.get("corefile", "")))
    for a in g.values():
        a["entries"].sort(key=lambda e: e["ts"])
        a.update(count=len(a["entries"]), first=a["entries"][0]["ts"], last=a["entries"][-1]["ts"], sigs=sorted({e["sig"] for e in a["entries"]}))
    return list(g.values())


BASE_PROMPT = ("Fix this problem on my Omarchy machine (Arch Linux, Hyprland). Below is the case file collected by my task manager. "
               "Reply in my language (Spanish), short and plain, I am not a Linux expert. Do this:\n"
               "1. Say in one or two lines what is wrong.\n"
               "2. If there is a safe fix, say in ONE sentence what you will do and ask me 'ok?'. When I say yes, do it.\n"
               "3. Then put everything back to normal: restart the program or service that failed, check that it is really running, and tell me 'listo'. "
               "Leave nothing half-done (no stopped processes, moved files or half-changed settings). If you change something, tell me in one line how to undo it.\n"
               "4. If it was a one-off with nothing to fix, say so and stop.\n"
               "Stay in this window for any back-and-forth; do not touch the task manager. Never delete my data or change anything unrelated without asking.")


def prompt_for(tool):
    return BASE_PROMPT + (" For crashes you may use the diagnose-crash skill." if tool == "claude" else "") + "\n\n"


# ---------------------------------------------------------------- which AI fixes things

# read-only commands claude may run without asking, so the only question is the 'ok?' before a real change
READ_ONLY = ["Bash(coredumpctl:*)", "Bash(journalctl:*)", "Bash(pacman -Q:*)", "Bash(pacman -Si:*)", "Bash(checkupdates)", "Bash(systemctl status:*)",
             "Bash(systemctl --user status:*)", "Bash(systemctl is-active:*)", "Bash(systemctl --user is-active:*)", "Bash(ps:*)", "Bash(lscpu)",
             "Bash(free:*)", "Bash(df:*)", "Bash(ls:*)", "Bash(cat:*)", "Bash(uname:*)", "Bash(nvidia-smi:*)"]
# {tool: shell script run in a new terminal; $0 is the file with the prompt + case}
PRESETS = {"claude": 'claude "$(cat "$0")" --allowedTools ' + " ".join(f'"{t}"' for t in READ_ONLY),
           "codex": 'codex "$(cat "$0")"',
           "gemini": 'gemini -i "$(cat "$0")"',
           "opencode": 'opencode --prompt "$(cat "$0")"'}
AUTO_ORDER = ["claude", "codex", "gemini", "opencode"]                          # "auto" = the first one installed
CONFIG = os.path.expanduser("~/.config/deathcorridor/ai.conf")
CONFIG_TEMPLATE = """# deathcorridor: which AI looks into a problem when you press "Fix it".
# ai = auto  -> the first one found: claude, codex, gemini, opencode
# ai = claude | codex | gemini | opencode
ai = auto

# Anything else (a script, another CLI, a local model runner): write the command here. {file} is the file with the instructions and the case.
# command = my-ai --prompt "$(cat {file})"
"""


def read_config():
    cfg = {}
    try:
        for l in open(CONFIG):
            l = l.strip()
            if l and not l.startswith("#") and "=" in l:
                k, _, v = l.partition("=")
                cfg[k.strip().lower()] = v.strip()
    except OSError: pass
    return cfg


def ensure_config():
    if not os.path.exists(CONFIG):
        try:
            PV.write(CONFIG, CONFIG_TEMPLATE)
        except OSError: pass


LABELS = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini", "opencode": "OpenCode", "custom": "Custom command"}


def installed():
    """The AIs this machine has, in a stable order."""
    return [t for t in ("claude", "codex", "gemini", "opencode") if shutil.which(PRESETS[t].split()[0])]


def chosen():
    """The AI the user picked (None if they never did)."""
    cfg = read_config()
    if cfg.get("command"): return "custom"
    ai = cfg.get("ai", "auto").lower()
    return None if ai == "auto" else ai


def set_ai(tool):
    """Remember the choice (keeps a custom `command` line if the user wrote one)."""
    cmd = read_config().get("command")
    try:
        PV.write(CONFIG, CONFIG_TEMPLATE.replace("\nai = auto\n", f"\nai = {tool}\n") + (f"command = {cmd}\n" if cmd else ""))
    except OSError: pass


def pick_ai():
    """-> (tool name, shell script) or (None, reason). Honors ~/.config/deathcorridor/ai.conf; otherwise the first installed AI."""
    cfg = read_config()
    cmd = cfg.get("command")
    if cmd: return "custom", (cmd.replace("{file}", '"$0"') if "{file}" in cmd else cmd + ' "$(cat "$0")"')
    ai = cfg.get("ai", "auto").lower()
    cands = AUTO_ORDER if ai == "auto" else [ai]
    for t in cands:
        script = PRESETS.get(t)
        if script and shutil.which(script.split()[0]): return t, script
    return None, (f"'{ai}' is not installed" if ai != "auto" else "no AI found (claude, codex, gemini, opencode)")


def save(name, text):
    path = os.path.join(REPORT_DIR, re.sub(r"[^A-Za-z0-9._-]+", "_", name)[:40] + "-" + time.strftime("%Y%m%d-%H%M%S") + ".md")
    PV.write(path, text)
    return path
