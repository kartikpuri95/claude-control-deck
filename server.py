#!/usr/bin/env python3
"""Claude Control Deck — local server. Serves index.html, /api/sessions, and tmux send/focus actions.
Binds to 127.0.0.1 only. Run: python3 server.py [port]"""
import json, sys, time, subprocess, os, threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
import collector

ROOT = Path(__file__).parent
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 47823
# *.localhost always resolves to this Mac (RFC 6761), so the name can never point anywhere else
NAME = os.environ.get("DECK_HOST", "deck.localhost")
if not (NAME == "localhost" or NAME.endswith(".localhost")): raise SystemExit("DECK_HOST must end in .localhost")
# Optional config: ~/.config/claude-deck/config.json
#   {"terminal_app": "iTerm", "groups": [{"label": "Work", "path": "~/work"}, {"label": "Side projects", "path": "~/code"}]}
# Without groups, repos are grouped by their parent folder.
def load_config():
    p = Path(os.environ.get("DECK_CONFIG", Path.home() / ".config" / "claude-deck" / "config.json"))
    try: cfg = json.loads(p.read_text())
    except Exception: cfg = {}
    groups = []
    for g in cfg.get("groups") or []:
        if g.get("path"):
            groups.append({"label": g.get("label") or g["path"], "path": os.path.expanduser(g["path"]).rstrip("/") + "/"})
    return {"terminal_app": os.environ.get("DECK_TERMINAL_APP") or cfg.get("terminal_app") or "Terminal", "groups": groups}
CONFIG = load_config()
TERMINAL_APP = CONFIG["terminal_app"]
_cache = {"t": 0, "d": None}; _lock = threading.Lock()

def sessions():
    with _lock:
        if time.time() - _cache["t"] > 2:
            _cache["d"] = dict(collector.collect(), groups=CONFIG["groups"], home=str(Path.home())); _cache["t"] = time.time()
        return _cache["d"]

def find(body):
    sid, pid = body.get("id"), body.get("pid")
    if isinstance(pid, str): pid = int(pid) if pid.isdigit() else None
    xs = sessions()["sessions"]
    if pid: return next((s for s in xs if s["pid"] == pid), None)
    return next((s for s in xs if s["id"] == sid), None)

def tmux(*args):
    r = subprocess.run(["tmux", *args], capture_output=True, text=True, timeout=5)
    if r.returncode: raise RuntimeError(r.stderr.strip() or "tmux failed")
    return r.stdout

import re as _re
_NUM = _re.compile(r"^(\d+)\.\s+")
_RULE = set("─━-╌┄┈ ")
def parse_prompt(screen):
    """Read whatever Claude is asking off the pane: AskUserQuestion, a permission prompt, the folder-trust
    check. They all share a shape: options lined up under a ❯ cursor, above a footer such as
    'Enter to select · Esc to cancel'. Returns None when nothing is being asked."""
    L = [l.replace("│", " ").rstrip() for l in screen.rstrip("\n").split("\n")]
    foot = next((i for i in range(len(L) - 1, max(-1, len(L) - 10), -1)
                 if any(k in L[i] for k in ("Enter to select", "Enter to confirm", "Esc to cancel", "to navigate"))), None)
    if foot is None: return None
    cur = next((i for i in range(foot - 1, max(-1, foot - 40), -1) if L[i].lstrip().startswith("❯")), None)
    if cur is None: return None
    col = L[cur].index("❯") + 1
    while col < len(L[cur]) and L[cur][col] == " ": col += 1          # where option text starts
    rule = lambda l: l.strip() and set(l.strip()) <= _RULE
    is_opt = lambda l: len(l) > col and l[col] != " " and l[:col].strip() in ("", "❯")
    opts, pend, i = [], [], foot - 1
    while i >= 0:
        l = L[i]
        if not l.strip() or rule(l):
            pend = []; i -= 1; continue
        if is_opt(l):
            opts.append(dict(label=l[col:].strip(), on=l[:col].strip() == "❯", desc=" ".join(reversed(pend)))); pend = []
        elif len(l) > col and not l[:col + 1].strip():
            pend.append(l.strip())                                     # description, indented under its option
            if len(pend) > 4: break
        else:
            break
        i -= 1
    if not opts: return None
    opts.reverse()
    for k, o in enumerate(opts):
        o["i"] = k
        m = _NUM.match(o["label"])
        if m: o["n"] = int(m.group(1)); o["label"] = o["label"][m.end():]
        else: o["n"] = k + 1
        o["typed"] = o["label"].rstrip(".").lower() in ("type something", "other")
    # question text: the lines above the options, through dotted separators, stopping at a full rule
    q = []
    while i >= 0 and len(q) < 10:
        l = L[i]
        if l.strip() and set(l.strip()) <= set("─━"): break
        if l.strip() and not rule(l) and not l.strip().startswith("Tip:"): q.append(l.strip())   # skip Claude's tips
        i -= 1
    q.reverse()
    header = None
    if q and q[0][:1] in "☐☒✔□": header = q[0][1:].strip(); q = q[1:]
    labels = " ".join(o["label"] for o in opts)
    return dict(header=header, question="\n".join(q[-8:]), options=opts,
                cursor=next((o["i"] for o in opts if o["on"]), 0),
                multi=any(t in labels for t in ("[ ]", "[✔]", "[x]", "[×]")), footer=L[foot].strip())

def stop_session(s):
    """Graceful /exit via tmux when possible, then SIGTERM; closes the tmux window. Returns True if the process ended."""
    pane = s["tmux"]["pane"] if s["tmux"] else None
    if pane:
        tmux("send-keys", "-t", pane, "-l", "/exit"); time.sleep(0.15); tmux("send-keys", "-t", pane, "Enter")
    for _ in range(30):
        if not collector.alive(s["pid"]): break
        time.sleep(0.2)
    else:
        try: os.kill(s["pid"], 15)
        except Exception: pass
        for _ in range(20):
            if not collector.alive(s["pid"]): break
            time.sleep(0.2)
    if pane:
        try: tmux("kill-window", "-t", pane)
        except Exception: pass
    return not collector.alive(s["pid"])

_git_cache = {}
def _git(cwd, *args, timeout=8):
    r = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, timeout=timeout)
    return r.stdout if r.returncode == 0 else None

def git_info(cwd):
    """Branch, dirty count, ahead/behind for a folder, plus every worktree of its repo."""
    if not cwd or not os.path.isdir(cwd): return {"git": False}
    c = _git_cache.get(cwd)
    if c and time.time() - c[0] < 5: return c[1]
    root = (_git(cwd, "rev-parse", "--show-toplevel") or "").strip()
    if not root:
        out = {"git": False}; _git_cache[cwd] = (time.time(), out); return out
    gc = (_git(cwd, "rev-parse", "--git-common-dir") or "").strip() or os.path.join(root, ".git")
    if not os.path.isabs(gc): gc = os.path.normpath(os.path.join(cwd, gc))
    common = os.path.dirname(gc)                       # the main worktree's folder
    st = (_git(cwd, "status", "--porcelain=v1", "-b") or "").split("\n")
    head = st[0] if st else ""
    ahead = behind = 0
    import re as _re
    m = _re.search(r"ahead (\d+)", head);  ahead = int(m.group(1)) if m else 0
    m = _re.search(r"behind (\d+)", head); behind = int(m.group(1)) if m else 0
    branch = (_git(cwd, "rev-parse", "--abbrev-ref", "HEAD") or "").strip() or "?"
    dirty = len([l for l in st[1:] if l.strip()])
    wts = []
    cur = {}
    for l in (_git(cwd, "worktree", "list", "--porcelain") or "").split("\n"):
        if l.startswith("worktree "): cur = {"path": l[9:]}; wts.append(cur)
        elif l.startswith("branch "): cur["branch"] = l[7:].replace("refs/heads/", "")
        elif l.startswith("HEAD "): cur["head"] = l[5:12]
        elif l.strip() == "detached": cur["branch"] = "(detached)"
        elif l.strip() == "bare": cur["bare"] = True
    for w in wts:
        w["main"] = (w["path"] == common)
        w["exists"] = os.path.isdir(w["path"])
        if w["exists"] and w["path"] != root:
            w["dirty"] = len([l for l in (_git(w["path"], "status", "--porcelain=v1") or "").split("\n") if l.strip()])
        elif w["path"] == root: w["dirty"] = dirty
    out = {"git": True, "root": root, "main": common, "branch": branch, "dirty": dirty, "ahead": ahead, "behind": behind, "worktrees": wts,
           "last": (_git(cwd, "log", "-1", "--format=%h %s · %cr") or "").strip()}
    _git_cache[cwd] = (time.time(), out)
    return out

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache", ".ruff_cache", ".next", ".turbo", "dist", "build", ".DS_Store"}
_status_cache = {}
def git_status_map(cwd):
    c = _status_cache.get(cwd)
    if c and time.time() - c[0] < 5: return c[1]
    m = {}
    out = _git(cwd, "status", "--porcelain=v1", "-z", "--untracked-files=all") or ""
    for rec in out.split("\0"):
        if len(rec) < 4: continue
        xy, path = rec[:2], rec[3:]
        code = "U" if xy == "??" else (xy.strip()[:1] or "M")
        m[path] = code
        parts = path.split("/")
        for i in range(1, len(parts)):          # mark parent folders too
            m.setdefault("/".join(parts[:i]) + "/", "M")
    _status_cache[cwd] = (time.time(), m)
    return m

def list_dir(cwd, rel):
    base = os.path.join(cwd, rel) if rel else cwd
    st = git_status_map(cwd)
    out = []
    try: names = os.listdir(base)
    except Exception: return out
    for n in sorted(names, key=lambda x: x.lower()):
        if n in SKIP_DIRS: continue
        full = os.path.join(base, n); r = (rel + "/" + n) if rel else n
        d = os.path.isdir(full)
        out.append(dict(name=n, dir=d, path=r, status=st.get(r + "/" if d else r, "")))
    out.sort(key=lambda e: (not e["dir"], e["name"].lower()))
    return out

BUILTIN_CMDS = ["help", "clear", "compact", "status", "cost", "model", "resume", "exit", "mcp", "memory", "review",
    "init", "config", "doctor", "permissions", "agents", "hooks", "skills", "fast", "effort", "context", "rewind",
    "vim", "terminal-setup", "bug", "release-notes", "login", "logout", "add-dir", "export", "plan", "diff",
    "ide", "usage", "statusline", "stats", "theme", "privacy-settings", "upgrade"]
_files_cache = {}

def complete(kind, q, cwd):
    import glob as _g
    if kind == "cmd":
        items = [dict(name="/" + c, hint="built-in") for c in BUILTIN_CMDS]
        for base, hint in ((collector.CC, "user"), (Path(cwd) / ".claude", "project")):
            for f in _g.glob(str(base / "commands" / "*.md")): items.append(dict(name="/" + Path(f).stem, hint=hint + " command"))
            for f in _g.glob(str(base / "skills" / "*" / "SKILL.md")): items.append(dict(name="/" + Path(f).parent.name, hint=hint + " skill"))
        seen, out = set(), []
        for it in items:
            if it["name"] in seen: continue
            seen.add(it["name"])
            if q in it["name"].lower(): out.append(it)
        return sorted(out, key=lambda x: (not x["name"][1:].startswith(q), x["name"]))
    if kind == "file" and os.path.isdir(cwd):
        c = _files_cache.get(cwd)
        if not c or time.time() - c[0] > 30:
            r = subprocess.run(["git", "-C", cwd, "ls-files", "--cached", "--others", "--exclude-standard"], capture_output=True, text=True, timeout=10)
            files = r.stdout.split("\n") if r.returncode == 0 else []
            if not files:
                files = [os.path.relpath(p, cwd) for p in _g.glob(cwd + "/**/*", recursive=True) if os.path.isfile(p)][:5000]
            c = (time.time(), [f for f in files if f and not f.startswith(".git/")]); _files_cache[cwd] = c
        hits = [f for f in c[1] if q in f.lower()]
        hits.sort(key=lambda f: (q not in os.path.basename(f).lower(), len(f)))
        return [dict(name="@" + f, hint="") for f in hits]
    return []

class H(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k): super().__init__(*a, directory=str(ROOT), **k)
    def log_message(self, *a): pass
    def end_headers(self):
        if not self.path.startswith("/api/"): self.send_header("Cache-Control", "no-cache")   # always revalidate the page, so a reload picks up fixes
        super().end_headers()
    def _json(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code); self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(b)))
        self.end_headers(); self.wfile.write(b)
    # ---- request guard: this server types into terminals, so only the deck page itself may talk to it ----
    def _allowed(self, write=False):
        ok_hosts = {f"{NAME}:{PORT}", f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if self.headers.get("Host", "") not in ok_hosts:              # blocks DNS-rebinding pages
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{h}" for h in ok_hosts}:  # blocks other websites
            return False
        if self.headers.get("Sec-Fetch-Site") in ("cross-site", "same-site"):
            # only a plain page load may arrive from elsewhere (e.g. the 127.0.0.1 tab forwarding here);
            # it changes nothing. Every /api call and every write must come from the deck itself.
            nav = self.headers.get("Sec-Fetch-Mode") == "navigate" and self.headers.get("Sec-Fetch-Dest") == "document"
            if write or not nav or self.path.startswith("/api/"):
                return False
        if write:
            # a custom header + JSON type forces a CORS preflight, which this server never approves
            if self.headers.get("X-Deck") != "1": return False
            if not (self.headers.get("Content-Type") or "").startswith("application/json"): return False
        return True

    def do_OPTIONS(self):
        self.send_response(403); self.end_headers()

    def do_GET(self):
        if not self._allowed():
            return self._json(403, {"error": "forbidden"})
        if self.path.startswith("/api/sessions"):
            return self._json(200, sessions())
        if self.path.startswith("/api/stats"):
            from urllib.parse import urlparse, parse_qs
            sid = parse_qs(urlparse(self.path).query).get("id", [""])[0]
            s = find({"id": sid})
            if not s: return self._json(404, {"error": "no such session"})
            return self._json(200, {"id": sid, "stats": collector.session_stats(s.get("transcript"))})
        if self.path.startswith("/api/complete"):
            # suggestions for the composer: `/` commands and skills, `@` files in the repo
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            kind, q, cwd = qs.get("kind", [""])[0], qs.get("q", [""])[0].lower(), qs.get("cwd", [""])[0]
            return self._json(200, {"items": complete(kind, q, cwd)[:40]})
        if self.path.startswith("/api/ls"):
            # one level of the session's folder, for the file explorer; git status letters like an editor sidebar
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            cwd, rel = qs.get("cwd", [""])[0], qs.get("path", [""])[0].strip("/")
            if not os.path.isdir(cwd) or ".." in rel.split("/"): return self._json(400, {"error": "bad path"})
            return self._json(200, {"path": rel, "entries": list_dir(cwd, rel)})
        if self.path.startswith("/api/gitdiff"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            cwd, rel = qs.get("cwd", [""])[0], qs.get("path", [""])[0]
            if qs.get("all", [""])[0] and os.path.isdir(cwd):
                # PR-style: every change against HEAD, untracked files shown as fully added
                d = _git(cwd, "diff", "HEAD", "--no-color") or ""
                for l in (_git(cwd, "ls-files", "--others", "--exclude-standard", "--directory", "--no-empty-directory") or "").split("\n"):
                    if not l.strip(): continue
                    if l.endswith("/"):                           # whole untracked folder: one card, not every file in it
                        try: n = sum(len(fs) for _, _, fs in os.walk(os.path.join(cwd, l)))
                        except Exception: n = 0
                        d += f"diff --git a/{l} b/{l}\nnew folder {n}\n"; continue
                    try:
                        body = open(os.path.join(cwd, l), errors="replace").read()
                        if "\0" in body[:4000]: body = "(binary)"
                    except Exception as e: body = f"(cannot read: {e})"
                    lines = body.split("\n")[:1500]
                    d += f"diff --git a/{l} b/{l}\nnew file\n--- /dev/null\n+++ b/{l}\n@@ -0,0 +1,{len(lines)} @@\n" + "".join("+" + x + "\n" for x in lines)
                return self._json(200, {"diff": d[:1_500_000]})
            if not os.path.isdir(cwd) or ".." in rel.split("/"): return self._json(400, {"error": "bad path"})
            tracked = subprocess.run(["git", "-C", cwd, "ls-files", "--error-unmatch", rel], capture_output=True).returncode == 0
            if tracked:
                d = _git(cwd, "diff", "HEAD", "--", rel) or ""
            else:                                                     # untracked: show the whole file as added
                try:
                    body = open(os.path.join(cwd, rel), errors="replace").read()
                    d = f"--- /dev/null\n+++ b/{rel}\n" + "".join("+" + l + "\n" for l in body.split("\n")[:2000])
                except Exception as e: d = f"(cannot read: {e})"
            return self._json(200, {"path": rel, "diff": d[:400_000], "tracked": tracked})
        if self.path.startswith("/api/gitlog"):
            from urllib.parse import urlparse, parse_qs
            cwd = parse_qs(urlparse(self.path).query).get("cwd", [""])[0]
            if not os.path.isdir(cwd): return self._json(400, {"error": "bad path"})
            out = _git(cwd, "log", "-25", "--format=%h%x1f%s%x1f%an%x1f%cr%x1f%D") or ""
            rows = [dict(zip(["h", "s", "a", "t", "d"], l.split("\x1f"))) for l in out.split("\n") if l]
            files = []
            for l in (_git(cwd, "status", "--porcelain=v1") or "").split("\n"):
                if not l.strip(): continue
                path = l[3:].split(" -> ")[-1].strip('"')
                files.append(dict(status=("U" if l[:2] == "??" else l[:2].strip()[:1]), path=path.rstrip("/"), dir=path.endswith("/")))
            return self._json(200, {"log": rows, "files": files})
        if self.path.startswith("/api/git"):
            from urllib.parse import urlparse, parse_qs
            cwd = parse_qs(urlparse(self.path).query).get("cwd", [""])[0]
            return self._json(200, git_info(cwd))
        if self.path.startswith("/api/prompt"):
            from urllib.parse import urlparse, parse_qs
            s = find({"pid": parse_qs(urlparse(self.path).query).get("pid", [""])[0]})
            if not s or not s["alive"] or not s["tmux"]: return self._json(200, {"prompt": None})
            try: screen = tmux("capture-pane", "-p", "-t", s["tmux"]["pane"])
            except Exception: return self._json(200, {"prompt": None})
            return self._json(200, {"prompt": parse_prompt(screen), "status": s["status"], "screen": "\n".join(screen.rstrip().split("\n")[-30:])})
        if self.path.startswith("/api/live"):
            # what the terminal shows right now, for a session that is mid-reply (the transcript
            # only gets the reply once it is complete)
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            s = find({"pid": qs.get("pid", [""])[0], "id": qs.get("id", [""])[0]})
            if not s or not s.get("tmux"): return self._json(200, {"live": ""})
            try:
                raw = tmux("capture-pane", "-p", "-J", "-t", s["tmux"]["pane"], "-S", "-120")
            except Exception as e:
                return self._json(200, {"live": "", "error": str(e)})
            lines = raw.rstrip("\n").split("\n")
            if qs.get("full", [""])[0]:                                 # terminal view: everything, prompt box included
                while lines and not lines[-1].strip(): lines.pop()
                return self._json(200, {"live": "\n".join(lines[-200:])})
            seps = [i for i, l in enumerate(lines) if l.startswith("────")]
            if len(seps) >= 2: lines = lines[:seps[-2]]          # drop the prompt box and status footer
            while lines and not lines[-1].strip(): lines.pop()
            return self._json(200, {"live": "\n".join(lines[-40:])})
        if self.path.startswith("/api/history"):
            from urllib.parse import urlparse, parse_qs
            qs = parse_qs(urlparse(self.path).query)
            sid = qs.get("id", [""])[0]
            s = find({"id": sid})
            if s and s.get("transcript"):
                try:
                    st = os.stat(s["transcript"]); v = f"{st.st_size}:{st.st_mtime_ns}"
                except Exception: v = ""
                if v and qs.get("v", [""])[0] == v: return self._json(200, {"id": sid, "same": True, "v": v})
                if v: return self._json(200, {"id": sid, "v": v, "turns": collector.chat_history(s["transcript"])})
            if not s: return self._json(404, {"error": "no such session"})
            return self._json(200, {"id": sid, "turns": collector.chat_history(s.get("transcript"))})
        if self.path == "/": self.path = "/index.html"
        return super().do_GET()
    def do_POST(self):
        if not self._allowed(write=True):
            return self._json(403, {"error": "forbidden"})
        n = int(self.headers.get("Content-Length") or 0)
        try: body = json.loads(self.rfile.read(n) or b"{}")
        except Exception: return self._json(400, {"error": "bad json"})
        if self.path == "/api/shutdown":
            self._json(200, {"ok": True, "pid": os.getpid()})
            threading.Timer(0.3, lambda: os._exit(0)).start()
            return
        if self.path == "/api/new":
            # fresh claude session in a repo, inside the `claude` tmux session
            cwd = body.get("cwd") or ""
            if not os.path.isdir(cwd): return self._json(400, {"error": "folder does not exist"})
            name = os.path.basename(cwd.rstrip("/"))
            subprocess.run(["tmux", "new-session", "-d", "-s", "claude"], capture_output=True)
            pane = tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "claude:", "-n", name, "-c", cwd, "claude").strip()
            _cache["t"] = 0
            return self._json(200, {"ok": True, "pane": pane, "window": f"claude:{name}"})
        if self.path == "/api/worktree":
            # new worktree next to the repo (<repo>--<branch>) for a new or existing branch, then a claude session in it
            root, branch = body.get("root") or "", (body.get("branch") or "").strip()
            if not os.path.isdir(root) or not branch or "/" in branch.strip("/") and " " in branch: return self._json(400, {"error": "need root and branch"})
            safe = branch.replace("/", "-")
            dest = os.path.join(os.path.dirname(root.rstrip("/")), f"{os.path.basename(root.rstrip('/'))}--{safe}")
            if os.path.exists(dest): return self._json(409, {"error": f"{dest} already exists"})
            exists = subprocess.run(["git", "-C", root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"], capture_output=True).returncode == 0
            args = ["git", "-C", root, "worktree", "add", dest] + ([branch] if exists else ["-b", branch])
            r = subprocess.run(args, capture_output=True, text=True, timeout=30)
            if r.returncode: return self._json(500, {"error": r.stderr.strip() or "git worktree add failed"})
            _git_cache.clear()
            if body.get("start", True):
                subprocess.run(["tmux", "new-session", "-d", "-s", "claude"], capture_output=True)
                pane = tmux("new-window", "-d", "-P", "-F", "#{pane_id}", "-t", "claude:", "-n", os.path.basename(dest), "-c", dest, "claude").strip()
                _cache["t"] = 0
                return self._json(200, {"ok": True, "path": dest, "pane": pane})
            return self._json(200, {"ok": True, "path": dest})
        if self.path == "/api/purge":
            # remove sessions that never got a prompt: stop the process, drop the registry entry,
            # and delete the transcript only when it holds no conversation at all
            pids = [int(p) for p in body.get("pids", []) if str(p).isdigit()]
            mine = os.getpid()
            targets = [x for x in sessions()["sessions"] if x.get("pid") in pids and x["pid"] != mine]
            done, kept = [], []
            for x in targets:
                tp = x.get("transcript")
                if tp and collector.chat_history(tp):
                    kept.append(x["name"]); continue          # it has history after all: never purge that
                try:
                    if x["alive"]: stop_session(x)
                    reg = collector.CC / "sessions" / f"{x['pid']}.json"
                    if reg.exists(): reg.unlink()
                    if tp and os.path.exists(tp): os.remove(tp)
                    done.append(x["name"])
                except Exception:
                    kept.append(x["name"])
            _cache["t"] = 0
            return self._json(200, {"ok": True, "purged": done, "kept": kept})
        if self.path == "/api/stop_many":
            pids = [int(p) for p in body.get("pids", []) if str(p).isdigit()]
            mine = os.getpid()
            targets = [x for x in sessions()["sessions"] if x["alive"] and x["pid"] in pids and x["pid"] != mine]
            results = []
            def run(x):
                try: results.append((x["name"], stop_session(x)))
                except Exception as e: results.append((x["name"], False))
            ts = [threading.Thread(target=run, args=(x,)) for x in targets]
            for t in ts: t.start()
            for t in ts: t.join(timeout=15)
            _cache["t"] = 0
            return self._json(200, {"ok": True, "stopped": [n for n, ok in results if ok], "failed": [n for n, ok in results if not ok]})
        s = find(body)
        if not s: return self._json(404, {"error": "no such session"})
        try:
            if self.path == "/api/revive":
                if s["alive"]: return self._json(409, {"error": "already running — use Jump"})
                subprocess.run(["tmux", "new-session", "-d", "-s", "claude"], capture_output=True)
                tmux("new-window", "-d", "-t", "claude:", "-n", s["name"], "-c", s["cwd"])
                cmd = f"claude --resume {s['id']}" if s.get("transcript") else "claude"   # never-prompted sessions have no transcript
                tmux("send-keys", "-t", f"claude:{s['name']}", cmd, "Enter")
                _cache["t"] = 0
                return self._json(200, {"ok": True, "window": f"claude:{s['name']}"})
            if self.path == "/api/rename":
                import re as _re
                name = _re.sub(r"[^A-Za-z0-9._-]", "", _re.sub(r"\s+", "-", (body.get("name") or "").strip()))[:60].strip("-.")
                if not name: return self._json(400, {"error": "use letters, numbers, - _ ."})
                if s["alive"] and s["tmux"]:
                    # let Claude do it, exactly like typing /rename: it updates its registry and the transcript
                    if s["status"] in ("busy", "shell"): return self._json(409, {"error": "it is working, rename it when it is idle"})
                    pane = s["tmux"]["pane"]
                    tmux("send-keys", "-t", pane, "-l", f"/rename {name}"); time.sleep(0.15)
                    tmux("send-keys", "-t", pane, "Enter")
                    try: tmux("rename-window", "-t", pane, name)
                    except Exception: pass
                elif not s["alive"] and s.get("transcript"):
                    # closed: append the same records /rename writes; Claude picks them up on resume
                    with open(s["transcript"], "a") as f:
                        f.write(json.dumps({"type": "custom-title", "customTitle": name, "sessionId": s["id"]}, separators=(",", ":")) + "\n")
                        f.write(json.dumps({"type": "agent-name", "agentName": name, "sessionId": s["id"]}, separators=(",", ":")) + "\n")
                    collector._closed_cache.clear()
                else:
                    return self._json(409, {"error": "not reachable: running outside tmux"})
                _cache["t"] = 0
                return self._json(200, {"ok": True, "name": name})
            if self.path == "/api/stop":
                if not s["alive"]: return self._json(409, {"error": "already stopped"})
                ok = stop_session(s); _cache["t"] = 0
                return self._json(200, {"ok": True, "stopped": ok})
        except Exception as e:
            return self._json(500, {"error": str(e)})
        if not s["alive"]: return self._json(409, {"error": "process is not running"})
        if not s["tmux"]: return self._json(409, {"error": "session is not inside tmux — restart it with bin/cc"})
        pane = s["tmux"]["pane"]
        try:
            if self.path == "/api/answer":
                # pick option n the way you would: arrow keys to it, type your own text if given, Enter
                pr = parse_prompt(tmux("capture-pane", "-p", "-t", pane))
                if not pr: return self._json(409, {"error": "no question is showing any more"})
                k = int(body.get("i", -1)); text = (body.get("text") or "").strip()
                if not 0 <= k < len(pr["options"]): return self._json(400, {"error": "no such option"})
                if (body.get("label") or pr["options"][k]["label"]) != pr["options"][k]["label"]:
                    return self._json(409, {"error": "the question changed, look again"})
                d = k - pr["cursor"]
                for _ in range(abs(d)): tmux("send-keys", "-t", pane, "Down" if d > 0 else "Up"); time.sleep(0.06)
                time.sleep(0.15)
                if text: tmux("send-keys", "-t", pane, "-l", text); time.sleep(0.25)
                tmux("send-keys", "-t", pane, "Enter")
                return self._json(200, {"ok": True})
            if self.path == "/api/key":
                k = {"up": "Up", "down": "Down", "enter": "Enter", "esc": "Escape", "space": "Space", "tab": "Tab", "btab": "BTab"}.get(body.get("key"))
                if not k: return self._json(400, {"error": "key not allowed"})
                tmux("send-keys", "-t", pane, k)
                return self._json(200, {"ok": True})
            if self.path == "/api/send":
                text = (body.get("text") or "").strip()
                images = body.get("images") or []
                if not text and not images: return self._json(400, {"error": "empty message"})
                # images: write to disk, put on the clipboard, ctrl+v into claude (it shows [Image #n])
                import base64
                img_dir = collector.CC / "deck-images"; img_dir.mkdir(exist_ok=True)
                for i, data in enumerate(images[:10]):
                    b64 = data.split(",", 1)[1] if "," in data else data
                    fp = img_dir / f"{int(time.time()*1000)}-{i}.png"
                    fp.write_bytes(base64.b64decode(b64))
                    subprocess.run(["osascript", "-e", f'set the clipboard to (read (POSIX file "{fp}") as «class PNGf»)'], capture_output=True, timeout=10)
                    tmux("send-keys", "-t", pane, "C-v"); time.sleep(0.8)
                if text: tmux("send-keys", "-t", pane, "-l", text)
                time.sleep(0.15)
                tmux("send-keys", "-t", pane, "Enter")
                return self._json(200, {"ok": True, "pane": pane, "window": f"{s['tmux']['session']}:{s['tmux']['window_name']}"})
            if self.path == "/api/focus":
                tmux("select-window", "-t", pane); tmux("select-pane", "-t", pane)
                try: tmux("switch-client", "-t", s["tmux"]["session"])
                except Exception: pass
                subprocess.run(["osascript", "-e", f'tell application "{TERMINAL_APP}" to activate'], capture_output=True, timeout=5)
                return self._json(200, {"ok": True, "pane": pane})
        except Exception as e:
            return self._json(500, {"error": str(e)})
        return self._json(404, {"error": "unknown action"})

if __name__ == "__main__":
    print(f"Claude Control Deck → http://{NAME}:{PORT}  (loopback only)")
    # the deck used to live on 8766: answer there with one page that carries the browser's saved
    # settings over to the new address. It serves nothing else (no files, no API). DECK_LEGACY_PORT=0 turns it off.
    legacy = int(os.environ.get("DECK_LEGACY_PORT", "8766"))
    if legacy and legacy != PORT:
        from http.server import BaseHTTPRequestHandler
        page = ("<!doctype html><meta charset=utf-8><title>Control Deck moved</title><script>"
                "try{const o={};for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);if(k.startsWith('deck'))o[k]=localStorage.getItem(k)}"
                f"location.replace('http://{NAME}:{PORT}/#import='+encodeURIComponent(JSON.stringify(o)))}}"
                f"catch(e){{location.replace('http://{NAME}:{PORT}/')}}</script>"
                f"<p>The deck moved to <a href='http://{NAME}:{PORT}/'>http://{NAME}:{PORT}</a>.</p>").encode()
        class Moved(BaseHTTPRequestHandler):
            def log_message(self, *a): pass
            def do_GET(self):
                self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-store"); self.send_header("Content-Length", str(len(page)))
                self.end_headers(); self.wfile.write(page)
            def do_POST(self): self.send_response(410); self.end_headers()
        try:
            srv = ThreadingHTTPServer(("127.0.0.1", legacy), Moved)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        except OSError:
            pass
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()
