"""deathcorridor_watch: flag a program to follow it over time, from "something looks off" to "it is fixed".

A flagged program is matched by its executable. While it is flagged we record what happens to it (started, exited,
crashed, not responding, working hard, memory growing) in ~/.local/state/deathcorridor/watch/<name>.jsonl.
Status of a flagged program:
  watching   nothing to do (or an open incident, shown with attention)
  fix-sent   the case was handed to the AI; we now check it behaves
  resolved   it ran VERIFY_S seconds, healthy, after the fix was sent
"""
import json, os, time
import deathcorridor_private as PV

STATE_DIR = os.path.expanduser("~/.local/state/deathcorridor")
FILE = os.path.join(STATE_DIR, "watched.json")
LOG_DIR = os.path.join(STATE_DIR, "watch")
VERIFY_S = 600                     # healthy for this long after the fix = resolved
SAMPLE_S = 60                      # trend sample interval
MEM_STEP_KB = 1024 * 1024          # note memory each time it grows by 1 GB over its previous high-water mark
CRASH_WINDOW = 30                  # an exit within this many seconds of a recorded crash counts as that crash


def _slug(exe): return "".join(c if c.isalnum() or c in "-_." else "_" for c in os.path.basename(exe))[:60]


def fmt_kb(kb): return f"{kb / 1048576:.1f}G" if kb >= 1048576 else f"{kb / 1024:.0f}M"


def span(s): return f"{int(s)}s" if s < 90 else f"{int(s / 60)}min" if s < 5400 else f"{s / 3600:.1f}h" if s < 172800 else f"{s / 86400:.1f} days"


class Watch:
    def __init__(self):
        self.d = {}                # exe -> dict(name, since, status, fix_ts, ok_since, incident, alive, samples, peak_rss, last_sample, ...)
        try: self.d = json.load(open(FILE))
        except Exception: pass
        for e, w in self.d.items():
            w.setdefault("samples", []); w.setdefault("alive", False); w.setdefault("peak_rss", 0); w.setdefault("last_sample", 0)
            w.setdefault("health", None); w.setdefault("incident", None); w.setdefault("ok_since", None)
        self.pids = set()          # live pids of every flagged program (for the ◉ marker)
        self.exe_cache = {}        # pid -> exe, only for pids we looked at

    # ------------------------------------------------------------ persistence
    def _save(self):
        keep = {e: {k: v for k, v in w.items() if k not in ("samples", "pids")} for e, w in self.d.items()}
        PV.write(FILE, json.dumps(keep))

    def _log(self, exe, kind, text):
        w = self.d[exe]
        ev = dict(ts=time.time(), kind=kind, text=text)
        PV.append(os.path.join(LOG_DIR, _slug(exe) + ".jsonl"), json.dumps(ev) + "\n")
        if kind in ("crashed", "stuck", "busy", "exited", "memory", "back"): w["incident"] = dict(kind=kind, text=text, ts=ev["ts"])
        return ev

    def events(self, exe, n=40):
        try: lines = open(os.path.join(LOG_DIR, _slug(exe) + ".jsonl")).read().splitlines()
        except OSError: return []
        out = []
        for l in lines[-n:]:
            try: out.append(json.loads(l))
            except ValueError: pass
        return out

    # ------------------------------------------------------------ identity
    def exe_of(self, pids):
        for p in pids:
            try: return os.readlink(f"/proc/{p}/exe").replace(" (deleted)", "")
            except OSError: pass
        return None

    def has(self, exe): return bool(exe) and exe in self.d

    def toggle(self, exe, name):
        """Flag / unflag. Returns True when now flagged."""
        if exe in self.d:
            del self.d[exe]; self._save(); return False
        self.d[exe] = dict(name=name, since=time.time(), status="watching", fix_ts=None, ok_since=None, incident=None, alive=False,
                           samples=[], peak_rss=0, last_sample=0, health=None, resolved_ts=None)
        self._log(exe, "flagged", "Flagged by the user to follow it")
        self._save(); return True

    # ------------------------------------------------------------ the 1 s loop
    def update(self, procs, cpu_of, health_of, crashes, now=None):
        """procs: pid -> dict(uid, rss, ...). cpu_of(pid) -> % of one core. health_of(pids) -> ("stuck"|"busy", secs) | None.
        crashes: list of crash groups from RP.crash_groups(). Returns a list of messages worth telling the user (e.g. "fixed")."""
        if not self.d: self.pids = set(); return []
        now = now or time.time(); told = []
        by_exe = {}
        for pid, p in procs.items():
            if p["uid"] != os.getuid(): continue
            exe = self.exe_cache.get(pid)
            if exe is None:
                try: exe = os.readlink(f"/proc/{pid}/exe").replace(" (deleted)", "")
                except OSError: exe = ""
                self.exe_cache[pid] = exe
            if exe in self.d: by_exe.setdefault(exe, []).append(pid)
        for pid in [p for p in self.exe_cache if p not in procs]: del self.exe_cache[pid]
        self.pids = {p for v in by_exe.values() for p in v}
        for exe, w in self.d.items():
            pids = by_exe.get(exe, [])
            w["pids"] = pids
            alive = bool(pids)
            rss = sum(procs[p]["rss"] for p in pids)
            if alive and not w["alive"]:
                self._log(exe, "started", f"Started ({len(pids)} process{'es' if len(pids) != 1 else ''})")
                w["ok_since"] = now
            if not alive and w["alive"]:
                crash = next((g for g in crashes if g["exe"] == exe and now - g["last"] < CRASH_WINDOW + 60), None)
                if crash:
                    e = crash["entries"][-1]
                    self._log(exe, "crashed", f"Crashed ({e['sig']})")
                else:
                    self._log(exe, "exited", "Exited" + (" while a fix was being checked" if w["status"] == "fix-sent" else ""))
                w["ok_since"] = None; w["health"] = None
            w["alive"] = alive
            if alive:
                hl = health_of(pids)
                kind = hl[0] if hl else None
                if kind != w["health"]:
                    if kind: self._log(exe, kind, "Not responding" if kind == "stuck" else "Using a full CPU core"); w["ok_since"] = None
                    elif w["health"]: self._log(exe, "recovered", "Recovered"); w["ok_since"] = now
                    w["health"] = kind
                if rss >= w["peak_rss"] + MEM_STEP_KB and w["peak_rss"]:
                    self._log(exe, "memory", f"Memory grew to {fmt_kb(rss)}")
                w["peak_rss"] = max(w["peak_rss"], rss)
                if w["peak_rss"] == rss and not w["last_sample"]: w["peak_rss"] = rss
                if now - w["last_sample"] >= SAMPLE_S:
                    w["last_sample"] = now
                    w["samples"].append((int(now), round(sum(cpu_of(p) for p in pids)), rss, len(pids))); del w["samples"][:-180]
            # fix verification
            if w["status"] == "fix-sent":
                if w["incident"] and w["incident"]["ts"] > w["fix_ts"]:
                    w["status"] = "watching"; self._log(exe, "back", "Problem came back after the fix was sent")
                elif alive and w["ok_since"] and now - max(w["ok_since"], w["fix_ts"]) >= VERIFY_S:
                    w["status"] = "resolved"; w["resolved_ts"] = now; w["incident"] = None
                    self._log(exe, "resolved", f"Ran {span(VERIFY_S)} without problems after the fix")
                    told.append(f"✓ {w['name']} looks fixed: {span(VERIFY_S)} without problems")
        if told or int(now) % 30 == 0: self._save()
        return told

    # ------------------------------------------------------------ what the UI shows
    def rows(self, now=None):
        """One JAIL row per flagged program."""
        now = now or time.time(); out = []
        for exe, w in self.d.items():
            inc = w.get("incident")
            if w["status"] == "resolved":
                what, att, det = f"✓ Fixed: stable for {span(VERIFY_S)}", False, f"{w['name']} ran {span(VERIFY_S)} with no problems after the fix. Press d (or w) to stop following it."
            elif w["status"] == "fix-sent":
                left = VERIFY_S - (now - max(w["ok_since"] or now, w["fix_ts"])) if w["alive"] and w["ok_since"] else VERIFY_S
                what, att, det = f"Fix sent: checking ({span(max(0, left))} left)" if w["alive"] else "Fix sent: waiting for it to run", False, \
                    f"The AI got the case for {w['name']}. I keep watching it: if it stays healthy for {span(VERIFY_S)} I mark it fixed; if it fails again it comes back here."
            elif inc:
                n = sum(1 for e in self.events(exe, 200) if e["ts"] >= w["since"] and e["kind"] in ("crashed", "stuck", "exited", "busy"))
                what, att = ("Earlier: " if inc["kind"] in ("stuck", "busy") and not w.get("health") else "") + f"{inc['text']} {_ago(now - inc['ts'])}" + (f" · {n} problem{'s' if n != 1 else ''} since flagged" if n > 1 else ""), True
                det = f"You flagged {w['name']} {_ago(now - w['since'])} and since then: {inc['text'].lower()}. Press Enter and the AI gets the whole timeline and fixes it, asking you first."
            else:
                what, att, det = f"Following it · {span(now - w['since'])}, no problems", False, \
                    f"{w['name']} is flagged. I record when it starts, exits, crashes or hangs, and its CPU and memory over time. Press Enter to send that to the AI anyway, or w to stop following it."
            out.append(dict(key=("w", exe), name=w["name"], what=what, live=bool(w["alive"]), stuck=w.get("health") == "stuck", count=0,
                            last=(inc["ts"] if inc else w["since"]), pids=list(w.get("pids", [])), exe=exe, attention=att, detail=det, note="", watched=True))
        return out

    def mark_fix_sent(self, exe):
        w = self.d.get(exe)
        if not w: return
        w.update(status="fix-sent", fix_ts=time.time(), ok_since=time.time() if w["alive"] else None, incident=None)
        self._log(exe, "fix-sent", "Case sent to the AI"); self._save()

    def tracking_text(self, exe):
        """Markdown block for the AI: what happened since the user flagged the program."""
        w = self.d.get(exe)
        if not w: return ""
        t0 = w["since"]
        evs = [e for e in self.events(exe, 60)]
        L = [f"- The user flagged this program {_ago(time.time() - t0)} to follow it; status now: {w['status']}.",
             f"- Running now: {'yes' if w['alive'] else 'no'}"]
        if w["samples"]:
            s = w["samples"]
            L += [f"- Last {span(s[-1][0] - s[0][0])} of samples (every {SAMPLE_S}s): CPU {min(x[1] for x in s)}–{max(x[1] for x in s)}% of a core, RAM {fmt_kb(min(x[2] for x in s))}–{fmt_kb(max(x[2] for x in s))} (now {fmt_kb(s[-1][2])})"]
        L += ["", "Timeline:"] + [f"  {time.strftime('%m-%d %H:%M:%S', time.localtime(e['ts']))}  {e['text']}" for e in evs]
        return "\n".join(L)


def _ago(s): return f"{span(max(0, s))} ago"
