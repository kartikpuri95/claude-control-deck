#!/usr/bin/env python3
"""Collect every local Claude Code session: registry, liveness, last prompt, transcript tail, tmux pane."""
import json, os, glob, subprocess, time, re, calendar
from pathlib import Path

HOME = Path.home()
CC = HOME / ".claude"
TAIL_BYTES = 2_000_000

def sh(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return ""

def alive(pid):
    try:
        os.kill(pid, 0); return True
    except Exception:
        return False

def proc_table():
    """pid -> (ppid, tty) for every process, from a single `ps`."""
    t = {}
    for line in sh(["ps", "-axo", "pid=,ppid=,tty="]).splitlines():
        p = line.split()
        if len(p) >= 2 and p[0].isdigit() and p[1].isdigit():
            tty = p[2] if len(p) > 2 and p[2] != "??" else None
            t[int(p[0])] = (int(p[1]), tty)
    return t

def tty_of(pid, table=None):
    if table is not None: return (table.get(pid) or (None, None))[1]
    out = sh(["ps", "-o", "tty=", "-p", str(pid)]).strip()
    return out if out and out != "??" else None

def ppid_of(pid, table=None):
    if table is not None: return (table.get(pid) or (None, None))[0]
    out = sh(["ps", "-o", "ppid=", "-p", str(pid)]).strip()
    return int(out) if out.isdigit() else None

def tmux_panes():
    out = sh(["tmux", "list-panes", "-a", "-F",
              "#{pane_pid}|#{pane_id}|#{session_name}|#{window_index}|#{window_name}|#{pane_index}"])
    panes = {}
    for line in out.splitlines():
        p = line.split("|")
        if len(p) == 6 and p[0].isdigit():
            panes[int(p[0])] = dict(pane=p[1], session=p[2], window=int(p[3]), window_name=p[4], index=int(p[5]))
    return panes

def pane_for(pid, panes, table=None, hint=None):
    """Walk the parent chain to a tmux pane. `hint` is the registry's own `tmux` field ("sess:@win.%pane")."""
    if hint and "%" in hint:
        pid_hint = hint.rsplit(".", 1)[-1]
        for p in panes.values():
            if p["pane"] == pid_hint: return p
    seen = 0
    while pid and seen < 12:
        if pid in panes:
            return panes[pid]
        pid = ppid_of(pid, table); seen += 1
    return None

def transcript_path(cwd, sid):
    d = CC / "projects" / ("-" + re.sub(r"[/_.]", "-", cwd.strip("/")))
    f = d / f"{sid}.jsonl"
    return f if f.exists() else None

def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for x in content:
            if x.get("type") == "text" and x.get("text", "").strip():
                return x["text"]
    return ""

_tail_cache = {}
def tail_transcript(path, now_ms):
    info = dict(last_assistant=None, last_tool=None, last_user=None, buckets=[0]*24, turns=0)
    if not path:
        return info
    try:
        st = os.stat(path); k = (st.st_size, int(st.st_mtime))
        c = _tail_cache.get(str(path))
        if c and c[0] == k and now_ms - c[1] < 60_000:
            return c[2]
    except Exception:
        pass
    info = _tail_transcript(path, now_ms)
    try: _tail_cache[str(path)] = (k, now_ms, info)
    except Exception: pass
    return info

def _tail_transcript(path, now_ms):
    info = dict(last_assistant=None, last_tool=None, last_user=None, buckets=[0]*24, turns=0)
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            f.seek(max(0, size - TAIL_BYTES))
            raw = f.read().decode("utf-8", "ignore")
    except Exception:
        return info
    for line in raw.split("\n"):
        if not line.startswith("{"):
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        t = o.get("type"); ts = o.get("timestamp")
        if ts:
            try:
                ms = calendar.timegm(time.strptime(ts[:19], "%Y-%m-%dT%H:%M:%S")) * 1000
                age_h = (now_ms - ms) / 3600e3
                if 0 <= age_h < 24 and t in ("assistant", "user"):
                    info["buckets"][23 - int(age_h)] += 1
            except Exception:
                pass
        m = o.get("message") or {}
        c = m.get("content")
        if t == "assistant" and isinstance(c, list):
            for x in c:
                if x.get("type") == "tool_use":
                    info["last_tool"] = dict(name=x.get("name"), ts=ts,
                                             arg=str((x.get("input") or {}).get("command") or (x.get("input") or {}).get("file_path") or "")[:80])
                elif x.get("type") == "text" and x["text"].strip():
                    info["last_assistant"] = dict(text=x["text"].strip()[:400], ts=ts)
                    info["turns"] += 1
        elif t == "user" and isinstance(c, (str, list)):
            if isinstance(c, list) and c and c[0].get("type") == "tool_result":
                continue
            txt = text_of(c).strip()
            if txt and not txt.startswith("<"):
                info["last_user"] = dict(text=txt[:300], ts=ts)
    return info

def history_last():
    """Per session: last prompt, plus project/first/count (used to list closed sessions). Cached on the file's mtime/size."""
    hp = CC / "history.jsonl"
    try:
        st = hp.stat(); k = (st.st_size, st.st_mtime_ns)
    except Exception:
        k = None
    c = _hist_cache.get("h")
    if c and c[0] == k: return c[1]
    last = {}
    try:
        with open(hp) as f:
            for line in f:
                try:
                    h = json.loads(line)
                    e = last.setdefault(h["sessionId"], dict(first=h["timestamp"], n=0, project=h.get("project")))
                    e.update(ts=h["timestamp"], text=h["display"][:300]); e["n"] += 1
                except Exception:
                    pass
    except Exception:
        pass
    _hist_cache["h"] = (k, last)
    return last
_hist_cache = {}

CLOSED_DAYS = 60
CLOSED_PER_REPO = 5

_closed_cache = {}
def closed_sessions(hist, live_ids, now_ms):
    k = (id(hist), tuple(sorted(live_ids)))
    c = _closed_cache.get("c")
    if c and c[0] == k and now_ms - c[1] < 20_000: return c[2]
    res = _closed_sessions(hist, live_ids, now_ms)
    _closed_cache["c"] = (k, now_ms, res)
    return res

def _closed_sessions(hist, live_ids, now_ms):
    out = []
    for sid, h in hist.items():
        if sid in live_ids or not h.get("project"): continue
        if now_ms - h["ts"] > CLOSED_DAYS * 86400e3: continue
        cwd = h["project"]
        if not os.path.isdir(cwd): continue        # repo deleted — nothing to revive into
        tp = transcript_path(cwd, sid)
        if not tp: continue
        out.append(dict(id=sid, pid=None, name=f"{os.path.basename(cwd.rstrip('/'))}-{sid[:4]}", cwd=cwd,
                        repo=os.path.basename(cwd.rstrip("/")), status="closed", started=h["first"], upd=h["ts"],
                        version=None, alive=False, tty=None, tmux=None, last=dict(ts=h["ts"], text=h["text"]),
                        last_assistant=None, last_tool=None, buckets=[0]*24, transcript=str(tp), prompts=h["n"]))
    out.sort(key=lambda s: -s["upd"])
    per = {}; kept = []
    for s in out:
        per[s["cwd"]] = per.get(s["cwd"], 0) + 1
        if per[s["cwd"]] <= CLOSED_PER_REPO: kept.append(s)
    return kept

_stats_cache = {}

def session_stats(path):
    """Totals for a transcript: tokens by direction, context size now, cost checkpoint, tools, model, title."""
    if not path or not Path(path).exists():
        return None
    st = Path(path).stat(); k = (path, st.st_size)
    if k in _stats_cache: return _stats_cache[k]
    r = dict(replies=0, prompts=0, tool_calls=0, tools={}, models={}, input=0, output=0, cache_read=0, cache_write=0,
             thinking=0, context=None, cost=None, title=None, first=None, last=None, max_context=0)
    with open(path, "rb") as f:
        for raw in f:
            if not raw.startswith(b"{"): continue
            if b'"type":"assistant"' in raw:
                try: o = json.loads(raw)
                except Exception: continue
                m = o.get("message") or {}; u = m.get("usage") or {}
                r["replies"] += 1
                if m.get("model"): r["models"][m["model"]] = r["models"].get(m["model"], 0) + 1
                for x in (m.get("content") or []):
                    if isinstance(x, dict) and x.get("type") == "tool_use":
                        r["tool_calls"] += 1; n = x.get("name", "?"); r["tools"][n] = r["tools"].get(n, 0) + 1
                if u:
                    r["input"] += u.get("input_tokens", 0); r["output"] += u.get("output_tokens", 0)
                    r["cache_read"] += u.get("cache_read_input_tokens", 0); r["cache_write"] += u.get("cache_creation_input_tokens", 0)
                    r["thinking"] += (u.get("output_tokens_details") or {}).get("thinking_tokens", 0)
                    ctx = u.get("input_tokens", 0) + u.get("cache_read_input_tokens", 0) + u.get("cache_creation_input_tokens", 0)
                    r["context"] = ctx; r["max_context"] = max(r["max_context"], ctx)
                ts = o.get("timestamp")
                if ts: r["first"] = r["first"] or ts; r["last"] = ts
            elif b'"type":"user"' in raw and b'"tool_result"' not in raw[:400]:
                r["prompts"] += 1
            elif b'"type":"cost-state"' in raw:
                try:
                    o = json.loads(raw)
                    r["cost"] = dict(usd=o.get("totalCostUSD"), api_ms=o.get("totalAPIDuration"), tool_ms=o.get("totalToolDuration"),
                                     lines_added=o.get("totalLinesAdded"), lines_removed=o.get("totalLinesRemoved"))
                except Exception: pass
            elif b'"type":"ai-title"' in raw:
                try: r["title"] = json.loads(raw).get("aiTitle")
                except Exception: pass
    r["tools"] = dict(sorted(r["tools"].items(), key=lambda kv: -kv[1])[:8])
    r["size_bytes"] = st.st_size
    if len(_stats_cache) > 64: _stats_cache.clear()
    _stats_cache[k] = r
    return r

def chat_history(path, limit=80, max_bytes=4_000_000):
    """Last `limit` user/assistant text turns of a transcript as [{role, ts, text}]."""
    turns = []
    if not path or not Path(path).exists():
        return turns
    size = Path(path).stat().st_size
    with open(path, "rb") as f:
        f.seek(max(0, size - max_bytes)); raw = f.read().decode("utf-8", "ignore")
    for line in raw.split("\n"):
        if not line.startswith("{"): continue
        try: o = json.loads(line)
        except Exception: continue
        t = o.get("type")
        if t == "attachment":
            # a message sent while the session was busy: queued and absorbed mid-turn,
            # it never becomes a `user` entry, only this attachment
            a = o.get("attachment") or {}
            pr = a.get("prompt") if a.get("type") == "queued_command" else ""
            if isinstance(pr, list):
                pr = "\n\n".join(x.get("text", "") for x in pr if isinstance(x, dict) and x.get("type") == "text")
            txt = pr.strip() if isinstance(pr, str) else ""
            if txt and not txt.startswith("<") and not any(x["role"] == "user" and x.get("text") == txt for x in turns[-3:]):
                turns.append(dict(role="user", ts=o.get("timestamp"), text=txt, queued=True))
            continue
        if t not in ("user", "assistant"): continue
        m = o.get("message") or {}; c = m.get("content")
        if isinstance(c, list):
            if c and c[0].get("type") == "tool_result": continue
            parts = [x["text"] for x in c if x.get("type") == "text" and x.get("text", "").strip()]
            tools = [x.get("name") for x in c if x.get("type") == "tool_use"]
            txt = "\n\n".join(parts)
            if not txt and tools and t == "assistant":
                if turns and turns[-1]["role"] == "tool":
                    turns[-1]["n"] += len(tools); turns[-1]["tools"].update(tools); turns[-1]["ts"] = o.get("timestamp")
                else:
                    turns.append(dict(role="tool", ts=o.get("timestamp"), n=len(tools), tools=set(tools)))
                continue
        else:
            txt = c if isinstance(c, str) else ""
        txt = txt.strip()
        if not txt or (t == "user" and (txt.startswith("<") or txt.startswith("[Image: source:"))): continue
        if t == "user" and txt.startswith("[Request interrupted"): continue
        if t == "user" and any(x["role"] == "user" and x.get("queued") and x["text"] == txt for x in turns[-3:]): continue
        turns.append(dict(role=t, ts=o.get("timestamp"), text=txt))
    for t in turns:
        if t["role"] == "tool":
            t["text"] = f"{t['n']} tool call{'s' if t['n']>1 else ''} · " + ", ".join(sorted(t.pop("tools"))); t.pop("n")
    text_idx = [i for i, t in enumerate(turns) if t["role"] != "tool"]
    start = text_idx[-limit] if len(text_idx) > limit else 0
    return turns[start:]

def collect():
    now_ms = int(time.time() * 1000)
    panes = tmux_panes()
    hist = history_last()
    table = proc_table()
    out = []
    for f in glob.glob(str(CC / "sessions" / "*.json")):
        try:
            s = json.load(open(f))
        except Exception:
            continue
        pid = s.get("pid"); sid = s.get("sessionId"); cwd = s.get("cwd", "")
        if not pid or not sid:
            continue
        is_alive = alive(pid)
        tp = transcript_path(cwd, sid)
        tail = tail_transcript(tp, now_ms) if is_alive else dict(last_assistant=None, last_tool=None, last_user=None, buckets=[0]*24, turns=0)
        out.append(dict(
            id=sid, pid=pid, name=s.get("name"), cwd=cwd, repo=os.path.basename(cwd.rstrip("/")),
            status=s.get("status"), started=s.get("startedAt"), upd=s.get("updatedAt") or s.get("startedAt"),
            version=s.get("version"), alive=is_alive,
            tty=tty_of(pid, table) if is_alive else None,
            tmux=pane_for(pid, panes, table, s.get("tmux")) if is_alive else None,
            last=hist.get(sid) or (tail["last_user"] and dict(ts=None, text=tail["last_user"]["text"])),
            last_assistant=tail["last_assistant"], last_tool=tail["last_tool"],
            buckets=tail["buckets"], transcript=str(tp) if tp else None,
        ))
    live_ids = {s["id"] for s in out if s["alive"]}
    out = [s for s in out if s["alive"] or s["id"] not in live_ids]
    known = {s["id"] for s in out}
    out += [c for c in closed_sessions(hist, known, now_ms)]
    out.sort(key=lambda s: -(s["upd"] or 0))
    return dict(generated=now_ms, host=os.uname().nodename, server_pid=os.getpid(), tmux_sessions=sorted({p["session"] for p in panes.values()}), sessions=out)

if __name__ == "__main__":
    print(json.dumps(collect(), indent=1))
