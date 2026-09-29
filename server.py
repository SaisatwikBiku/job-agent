#!/usr/bin/env python3
"""Phase 3: web control panel for the local agent.

Reuses the prompt, model call and tools from agent.py (same folder).
Runs from /opt/agent as the agentd user (see deploy/agent-web.service); tools run
as the agent user through toolrunner.py when AGENT_USE_TOOLRUNNER=1.
"""
import asyncio
import base64
import gzip
import hashlib
import io
import json
import os
import queue
import re
import subprocess
import threading
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response, StreamingResponse
from pydantic import BaseModel
from pywebpush import WebPushException, webpush

import ollama

import agent as core
import jobs

ALLOWED_LOGIN = os.environ.get("AGENT_ALLOWED_LOGIN", "").strip()
MAX_EVENTS = 300

# Web Push (notifications to the home-screen app)
VAPID_FILE = Path(os.environ.get("AGENT_VAPID_KEY", "/home/agent/vapid_private.pem"))
SUBS_FILE = Path(os.environ.get("AGENT_PUSH_SUBS", "/home/agent/push_subscriptions.json"))
PUSH_SUB = os.environ.get("AGENT_PUSH_SUB", "mailto:agent@example.com")
push_lock = threading.Lock()

# Phase 5: run tools as the separate 'agent' user through sudo (see toolrunner.py)
USE_TOOLRUNNER = os.environ.get("AGENT_USE_TOOLRUNNER") == "1"
TOOLRUNNER = ["sudo", "-n", "-u", "agent", "-H",
              "/opt/agent/venv/bin/python", "/opt/agent/toolrunner.py"]

# Chat mode and memory (plain conversation, no tools; see "chat and memory" below)
CHAT_FILE = Path(os.environ.get("AGENT_CHATS", "/home/agentd/chats.json"))
MEMORY_FILE = Path(os.environ.get("AGENT_MEMORY", "/home/agentd/memory.md"))
CHAT_MODELS = {"better": os.environ.get("AGENT_CHAT_MODEL", "qwen3:8b"), "faster": core.MODEL}
CHAT_HISTORY_CHARS = 12_000  # about 3,000 tokens: the 8B rereads 17 tokens/s when its cache is lost
CHAT_MAX_CHATS = 50
MEMORY_MAX_CHARS = 2_000
FILE_MAX_BYTES = 5_000_000      # per attached file
FILE_MAX_CHARS = 1_000_000      # text kept from a file (tasks save all of it)
CHAT_ATTACH_CHARS = 16_000      # file text put into one chat message, about 4,000 tokens
chat_lock = threading.Lock()
chat_active = threading.Semaphore(1)  # one reply at a time; the CPU can't do two

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
lock = threading.Lock()
state = {"status": "idle", "task": None, "events": [], "pending": None, "seq": 0}
decision = {"id": None, "approve": False, "reason": ""}
decision_ready = threading.Event()
stop_requested = threading.Event()

try:
    core.WORKSPACE.mkdir(parents=True, exist_ok=True)
except OSError:
    pass


class Stopped(Exception):
    pass


# ---------- push notifications ----------

def ensure_vapid_key():
    """Create the server's push signing key on first run; return the public key for browsers."""
    if VAPID_FILE.exists():
        key = serialization.load_pem_private_key(VAPID_FILE.read_bytes(), password=None)
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        VAPID_FILE.write_bytes(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
        VAPID_FILE.chmod(0o600)
    raw = key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


try:
    VAPID_PUBLIC = ensure_vapid_key()
    VAPID_ERROR = ""
except OSError as e:
    VAPID_PUBLIC = ""
    VAPID_ERROR = f"Push notifications are off: can't read or create {VAPID_FILE} ({e.strerror})."


def load_subs():
    try:
        return json.loads(SUBS_FILE.read_text())
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as e:
        print(f"Can't read {SUBS_FILE}: {e}", flush=True)
        return []


def save_subs(subs):
    SUBS_FILE.write_text(json.dumps(subs))
    SUBS_FILE.chmod(0o600)


def _send_push(title, body, tag):
    payload = json.dumps({"title": title, "body": body[:180], "tag": tag})
    with push_lock:
        subs = load_subs()
        keep = []
        for sub in subs:
            try:
                webpush(subscription_info=sub, data=payload,
                        vapid_private_key=str(VAPID_FILE),
                        vapid_claims={"sub": PUSH_SUB}, ttl=3600)
                keep.append(sub)
            except WebPushException as e:
                status = getattr(e.response, "status_code", None)
                if status not in (404, 410):  # 404/410 = subscription gone, drop it
                    keep.append(sub)
            except Exception:
                keep.append(sub)
        if keep != subs:
            save_subs(keep)


def notify(title, body, tag="agent"):
    """Send a push to every subscribed device without blocking the agent loop."""
    if VAPID_PUBLIC:
        threading.Thread(target=_send_push, args=(title, body, tag), daemon=True).start()


def now():
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def add_event(kind, **data):
    with lock:
        state["seq"] += 1
        state["events"].append({"n": state["seq"], "kind": kind, "time": now(), **data})
        del state["events"][:-MAX_EVENTS]


def startup_problems():
    """Files the panel needs but can't use. These used to fail silently."""
    problems = [VAPID_ERROR] if VAPID_ERROR else []
    for path in (core.LOG_FILE, SUBS_FILE, CHAT_FILE, MEMORY_FILE):
        target = path if path.exists() else path.parent
        if not os.access(target, os.W_OK):
            problems.append(f"Can't write {path}. Check that it belongs to the user running the panel.")
    return problems


for problem in startup_problems():  # shown in the journal and at the top of the panel log
    print(problem, flush=True)
    add_event("error", text=problem)


def set_status(status, pending=None):
    with lock:
        state["status"] = status
        state["pending"] = pending
        state["seq"] += 1


def run_tool(tool, arg, content):
    if not USE_TOOLRUNNER:
        return core.TOOLS[tool](arg, content)
    try:
        r = subprocess.run(TOOLRUNNER + [tool],
                           input=json.dumps({"arg": arg, "content": content}),
                           capture_output=True, text=True, timeout=core.CMD_TIMEOUT + 30)
    except subprocess.TimeoutExpired:
        return "Error: tool timed out."
    if r.returncode != 0 and not r.stdout.strip():
        return f"Error running tool: {r.stderr.strip()[:300]}"
    return r.stdout


def wait_for_decision(action):
    action_id = uuid.uuid4().hex[:8]
    decision_ready.clear()
    set_status("waiting", {
        "id": action_id,
        "tool": action.get("tool"),
        "arg": action.get("arg", ""),
        "content": action.get("content", ""),
    })
    notify("Approval needed", f"{action.get('tool')}: {action.get('arg', '')}", tag="approval")
    while not decision_ready.wait(1):
        if stop_requested.is_set():
            raise Stopped
    with lock:
        approve, reason = decision["approve"], decision["reason"]
    set_status("running")
    return approve, reason


def run_task(task):
    system = {"role": "system", "content": core.SYSTEM_PROMPT + memory_block()}
    task_msg = {"role": "user", "content": f"Task: {task}"}
    history = []
    core.log({"event": "task", "task": task, "via": "web"})
    add_event("task", text=task)
    try:
        for step in range(1, core.MAX_STEPS + 1):
            if stop_requested.is_set():
                raise Stopped
            set_status("thinking")
            action, raw, stats = core.next_action([system, task_msg] + history[-core.KEEP_RECENT:])
            history.append({"role": "assistant", "content": raw})
            tool = action.get("tool")
            add_event("thought", step=step, text=action.get("thought", ""), stats=stats)

            if tool == "finish":
                add_event("answer", text=action.get("arg", ""))
                core.log({"event": "finish", "answer": action.get("arg", ""), "via": "web"})
                notify("Task done", action.get("arg", ""), tag="done")
                return

            if tool not in core.TOOLS:
                approved, result = False, f"Unknown tool '{tool}'."
                add_event("error", text=result)
            else:
                auto = tool in core.AUTO_APPROVE
                add_event("action", tool=tool, arg=action.get("arg", ""),
                          content=action.get("content", ""), auto=auto)
                approved, reason = (True, "") if auto else wait_for_decision(action)
                if approved:
                    set_status("running")
                    result = run_tool(tool, action.get("arg", ""), action.get("content", ""))
                    add_event("result", text=result)
                else:
                    result = f"{core.OWNER} rejected this action." + (f" Reason: {reason}" if reason else "")
                    add_event("rejected", text=reason)

            core.log({"event": "action", "tool": tool, "arg": action.get("arg", ""),
                      "approved": approved, "result": result[:500], "via": "web"})
            history.append({"role": "user", "content": f"Result of {tool}:\n{result}"})

        add_event("error", text=f"Stopped after {core.MAX_STEPS} steps without finishing.")
        core.log({"event": "step_limit", "via": "web"})
        notify("Task stopped", f"Hit the {core.MAX_STEPS}-step limit: {task}", tag="done")
    except Stopped:
        add_event("error", text="Task stopped.")
        core.log({"event": "stopped", "via": "web"})
    except Exception as e:  # keep the server alive whatever the agent does
        add_event("error", text=f"Agent error: {e}")
        notify("Agent error", str(e), tag="done")
    finally:
        with lock:
            state["task"] = None
        set_status("idle")


# ---------- chat and memory ----------
#
# Chat is plain conversation with no tools and no network access, so it needs no
# approvals. Memory is a short list of facts about Sai, one per line, added to the
# start of every chat and task. It's kept short because every character is read
# by the model on each uncached request.

CHAT_PROMPT = """You are a helpful assistant for Sai, running privately on his own computer.
- Answer clearly and directly. Lead with the answer, then the details that matter.
- Use short paragraphs. Use a list only when the content is a real list or steps.
- If you are not sure of a fact, say so. Never invent sources, numbers or quotes.
- You have no internet access and no tools here, and your knowledge stops at your training date. For anything current, say you can't check it.
- For code, give complete, working snippets.""".replace("Sai", core.OWNER)

def extract_text(name, data):
    """Text from an attached file: PDF (text layer only), Word .docx, or anything that
    decodes as text. Raises ValueError with a message for Sai when it can't."""
    if data[:5] == b"%PDF-":
        from pypdf import PdfReader
        try:
            pages = [p.extract_text() or "" for p in PdfReader(io.BytesIO(data)).pages]
        except Exception as e:
            raise ValueError(f"Couldn't read this PDF ({e}).")
        text = "\n\n".join(p.strip() for p in pages if p.strip())
        if not text:
            raise ValueError("This PDF has no text layer; it may be a scan. Images aren't supported.")
        return text
    if data[:2] == b"PK":
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                xml = z.read("word/document.xml").decode("utf-8", "replace")
        except (zipfile.BadZipFile, KeyError):
            raise ValueError("Only Word .docx files are supported from zip-based formats.")
        xml = re.sub(r"</w:p>", "\n", xml)
        xml = re.sub(r"<w:tab/>", "\t", xml)
        return re.sub(r"\n{3,}", "\n\n", re.sub(r"<[^>]+>", "", xml)).strip()
    if b"\x00" in data[:8192]:
        raise ValueError("This looks like a binary file (image, audio, archive...). Only text, PDF and .docx work.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("latin-1")


def safe_filename(name):
    base = os.path.basename(name.replace("\\", "/")) or "file"
    base = re.sub(r"[^A-Za-z0-9._-]+", "_", base).strip("._") or "file"
    return base[:80]


def chat_attachment_text(files):
    """The files as a block ahead of Sai's message, cut to CHAT_ATTACH_CHARS in total."""
    blocks, left = [], CHAT_ATTACH_CHARS
    for f in files:
        text = f.text
        cut = ""
        if len(text) > left:
            cut = f"\n[cut: the first {left:,} of {len(text):,} characters]"
            text = text[:left]
        blocks.append(f"Attached file: {f.name}\n<<<\n{text}{cut}\n>>>")
        left -= len(text)
        if left <= 0:
            break
    return "\n\n".join(blocks)


REMEMBER_RE = re.compile(r"^\s*remember(?:\s+that)?\s*[:,]?\s+(.+)$", re.I | re.S)


def load_memory():
    try:
        return MEMORY_FILE.read_text().strip()
    except OSError:
        return ""


def save_memory(text):
    MEMORY_FILE.write_text(text.strip() + "\n" if text.strip() else "")
    MEMORY_FILE.chmod(0o600)


def memory_block():
    mem = load_memory()
    return f"\n\nWhat you know about {core.OWNER} (from memory; use it when relevant):\n{mem}" if mem else ""


def load_chats():
    try:
        return json.loads(CHAT_FILE.read_text())
    except (OSError, ValueError):
        return {}


def save_chats(chats):
    keep = sorted(chats, key=lambda k: chats[k]["updated"], reverse=True)[:CHAT_MAX_CHATS]
    tmp = CHAT_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({k: chats[k] for k in keep}))
    tmp.chmod(0o600)
    os.replace(tmp, CHAT_FILE)


def chat_messages(chat):
    """System prompt, memory and date first (stable, so Ollama's cache covers them),
    then as much recent conversation as fits the budget, oldest dropped first."""
    today = datetime.now().strftime("%A, %B %d, %Y")
    system = CHAT_PROMPT + memory_block() + f"\n\nToday is {today}."
    kept, used = [], 0
    for m in reversed(chat["messages"]):
        used += len(m["content"])
        if kept and used > CHAT_HISTORY_CHARS:
            break
        kept.append({"role": m["role"], "content": m["content"]})
    return [{"role": "system", "content": system}] + kept[::-1]


def write_reply(chat_id, model, out, stop):
    """Run in a thread: stream the model's reply into the queue `out`, stop early when
    `stop` is set, and save the reply (or the part written before a stop or error)."""
    with chat_lock:
        chat = load_chats()[chat_id]
    messages = chat_messages(chat)
    kw = {"think": False} if model.startswith("qwen3:") else {}  # see CLAUDE.md on qwen3 tags
    parts, note = [], ""
    with chat_active:
        stream = None
        try:
            stream = ollama.chat(model=model, messages=messages, stream=True, keep_alive=-1,
                                 options={"temperature": 0.6, "num_predict": 1024}, **kw)
            for part in stream:
                if stop.is_set():
                    note = " (stopped)"
                    break
                text = part.message.content or ""
                if text:
                    parts.append(text)
                    out.put(text)
        except Exception as e:
            note = f"\n\n(error: {e})"
            out.put(note)
        finally:
            if stream is not None and hasattr(stream, "close"):
                stream.close()  # closes the connection, which makes Ollama stop generating
            with chat_lock:
                chats = load_chats()
                if chat_id in chats:
                    chats[chat_id]["messages"].append({"role": "assistant", "content": "".join(parts) + note,
                                                       "model": model, "time": now()})
                    chats[chat_id]["updated"] = time.time()
                    save_chats(chats)
            out.put(None)


async def stream_reply(chat_id, model, request):
    """Pass the reply to the browser as it's written. The model runs in a thread; this
    side checks for a closed connection (Stop, or the page closed) and tells the
    thread to stop, so Ollama doesn't keep writing to nobody."""
    out, stop = queue.Queue(), threading.Event()
    threading.Thread(target=write_reply, args=(chat_id, model, out, stop), daemon=True).start()
    try:
        while True:
            try:
                item = out.get_nowait()
            except queue.Empty:
                if await request.is_disconnected():
                    return
                await asyncio.sleep(0.1)
                continue
            if item is None:
                return
            yield item
    finally:
        stop.set()


# ---------- job search ----------

def panel_busy():
    """A task or a chat reply is using the model; the job search waits for both."""
    if state["status"] != "idle":
        return True
    if chat_active.acquire(blocking=False):
        chat_active.release()
        return False
    return True


def jobs_loop():
    """Run the nightly job search and the morning digest when they're due (see jobs.tick)."""
    last_error = ""
    while True:
        try:
            jobs.tick(notify, panel_busy)
            last_error = ""
        except Exception as e:  # keep the loop alive; report each new error once
            if str(e) != last_error:
                last_error = str(e)
                print(f"job search error: {e}", flush=True)
                notify("Job search error", str(e), tag="jobs")
        time.sleep(60)


threading.Thread(target=jobs_loop, daemon=True).start()

INBOX_EVERY = 300  # seconds between mail checks


def inbox_loop():
    """Check the agent's inbox (see jobs.check_inbox). Separate from jobs_loop, which is
    busy for hours during the nightly run."""
    last_error = ""
    while True:
        try:
            jobs.check_inbox(notify, panel_busy)
            last_error = ""
        except Exception as e:
            if str(e) != last_error:
                last_error = str(e)
                print(f"inbox error: {e}", flush=True)
                notify("Inbox error", str(e), tag="inbox")
        time.sleep(INBOX_EVERY)


threading.Thread(target=inbox_loop, daemon=True).start()


# ---------- HTTP ----------

def check_user(request: Request):
    if ALLOWED_LOGIN and request.headers.get("Tailscale-User-Login", "") != ALLOWED_LOGIN:
        raise HTTPException(403, "Not allowed")


class FileIn(BaseModel):
    name: str
    text: str


class TaskIn(BaseModel):
    task: str
    files: list[FileIn] = []


class DecisionIn(BaseModel):
    id: str
    approve: bool
    reason: str = ""


def fast(request: Request, body, media_type="application/json"):
    """A response the browser can skip downloading: an ETag of the content, so an
    unchanged answer (the Jobs list polled every few seconds) is a 304 with no body,
    and gzip when the browser takes it (the Jobs list shrinks about eightfold)."""
    raw = (body if isinstance(body, str) else json.dumps(body, separators=(",", ":"))).encode()
    tag = '"' + hashlib.sha256(raw).hexdigest()[:20] + '"'
    headers = {"ETag": tag, "Cache-Control": "no-cache", "Vary": "Accept-Encoding"}
    if request.headers.get("if-none-match") == tag:
        return Response(status_code=304, headers=headers)
    if len(raw) > 1024 and "gzip" in request.headers.get("accept-encoding", ""):
        raw = gzip.compress(raw, compresslevel=5)
        headers["Content-Encoding"] = "gzip"
    return Response(raw, media_type=media_type, headers=headers)


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    """The new panel. Until its file is on the server, the classic one."""
    check_user(request)
    if not (UI_DIR / "panel.html").exists():
        return fast(request, PAGE, "text/html; charset=utf-8")
    return ui_file(request, "panel.html")


@app.get("/classic", response_class=HTMLResponse)
def classic(request: Request):
    """The classic panel, kept for what the new one doesn't do yet (approving, settings,
    the assistant). Its API paths are relative, so they resolve to /api/... from here too."""
    check_user(request)
    return fast(request, PAGE, "text/html; charset=utf-8")


# The new panel (served at /; the classic PAGE is at /classic) lives in its own files next
# to this one, so it can be edited as HTML, CSS and JS. They are read on first use and again when they change.
# A file that isn't there yet (the deploy script ships only the files in its FILES list)
# is a 404, never a crash.
UI_DIR = Path(__file__).resolve().parent
UI_FILES = {"panel.html": "text/html; charset=utf-8", "panel.css": "text/css; charset=utf-8",
            "panel.js": "application/javascript; charset=utf-8"}
UI_CACHE = {}


def ui_file(request: Request, name):
    path = UI_DIR / name
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        raise HTTPException(404, "The new panel isn't deployed yet")
    if UI_CACHE.get(name, (None,))[0] != stamp:
        UI_CACHE[name] = (stamp, path.read_text())
    return fast(request, UI_CACHE[name][1], UI_FILES[name])


@app.get("/v2")
def panel_v2(request: Request):
    check_user(request)
    return RedirectResponse("/", status_code=307)  # /v2 was its address while it was built


@app.get("/ui/{name}")
def panel_asset(name: str, request: Request):
    check_user(request)
    if name not in UI_FILES or name == "panel.html":
        raise HTTPException(404, "No such file")
    return ui_file(request, name)


@app.get("/api/state")
def get_state(request: Request, since: int = 0):
    check_user(request)
    with lock:
        return {
            "seq": state["seq"],
            "status": state["status"],
            "task": state["task"],
            "pending": state["pending"],
            "events": [e for e in state["events"] if e["n"] > since],
        }


@app.post("/api/task")
def start_task(body: TaskIn, request: Request):
    """Start a task. Attached files are saved to uploads/ in the agent's workspace (as
    the agent user, through the tool runner) and listed in the task text, so the model
    can read them with its tools."""
    check_user(request)
    task = body.task.strip()
    if not task and not body.files:
        raise HTTPException(400, "Empty task")
    if any(len(f.text) > FILE_MAX_CHARS for f in body.files):
        raise HTTPException(400, f"A file is over {FILE_MAX_CHARS:,} characters")
    with lock:
        if state["status"] != "idle":
            raise HTTPException(409, "A task is already running")
        state["task"] = task or "(files)"
        state["status"] = "thinking"
    saved = []
    for f in body.files:
        path = f"uploads/{safe_filename(f.name)}"
        result = run_tool("write_file", path, f.text)
        if not result.startswith("Wrote"):
            set_status("idle")
            with lock:
                state["task"] = None
            raise HTTPException(500, f"Couldn't save {f.name}: {result[:200]}")
        saved.append(f"{path} ({len(f.text):,} characters)")
    if saved:
        task = (task or "Look at the attached files.") + \
            f"\n\nFiles {core.OWNER} attached, saved in the workspace: " + ", ".join(saved)
        with lock:
            state["task"] = task
    stop_requested.clear()
    threading.Thread(target=run_task, args=(task,), daemon=True).start()
    return {"ok": True}


@app.post("/api/decision")
def decide(body: DecisionIn, request: Request):
    check_user(request)
    with lock:
        pending = state["pending"]
        if not pending or pending["id"] != body.id:
            raise HTTPException(409, "No matching action is waiting")
        decision.update(id=body.id, approve=body.approve, reason=body.reason.strip())
    decision_ready.set()
    return {"ok": True}


@app.post("/api/stop")
def stop(request: Request):
    check_user(request)
    stop_requested.set()
    return {"ok": True}


JOB_STATUSES = ("new", "approved", "applied", "interview", "offer", "rejected", "withdrew", "skipped")


class JobIn(BaseModel):
    id: str
    status: str = ""


JOBS_CACHE = {"key": None, "body": None}


def jobs_cache_key():
    """What the Jobs list is built from: the files' change times, the search's progress
    and the day (rules count days). While none of it changes, the last answer stands."""
    files = [jobs.DB_FILE, jobs.INBOX_FILE, jobs.MAIL_FILE, jobs.JOBS_DIR / "profile.json", jobs.JOBS_DIR / "config.json",
             jobs.RESUME_JSON, jobs.JOBS_DIR / "resume.txt"]
    stamps = tuple(f.stat().st_mtime_ns if f.exists() else 0 for f in files)
    return stamps, json.dumps(jobs.progress, sort_keys=True, default=str), datetime.now(timezone.utc).date().isoformat()


@app.get("/api/jobs")
def jobs_summary(request: Request):
    check_user(request)
    key = jobs_cache_key()
    if JOBS_CACHE["key"] != key:  # polled every few seconds; building it takes about a second
        JOBS_CACHE.update(key=key, body=json.dumps(jobs.summary(), separators=(",", ":")))
    return fast(request, JOBS_CACHE["body"])


@app.get("/api/jobs/detail")
def jobs_detail(id: str, request: Request):
    check_user(request)
    job = jobs.detail(id)
    if not job:
        raise HTTPException(404, "No such job")
    return job


@app.post("/api/jobs/status")
def jobs_status(body: JobIn, request: Request):
    check_user(request)
    if body.status not in JOB_STATUSES:
        raise HTTPException(400, "Status must be one of " + ", ".join(JOB_STATUSES))
    if not jobs.set_status(body.id, body.status):
        raise HTTPException(404, "No such job")
    return {"ok": True}


@app.post("/api/jobs/run")
def jobs_run(request: Request):
    check_user(request)
    if not jobs.configured():
        raise HTTPException(409, "Job search isn't set up yet")
    if jobs.progress["running"]:
        raise HTTPException(409, "A job search is already running")
    threading.Thread(target=jobs.run, args=(panel_busy,), daemon=True).start()
    return {"ok": True}


@app.post("/api/jobs/prepare")
def jobs_prepare(body: JobIn, request: Request):
    check_user(request)
    if not jobs.detail(body.id):
        raise HTTPException(404, "No such job")
    threading.Thread(target=jobs.prepare_now, args=(body.id, panel_busy), daemon=True).start()
    return {"ok": True}


class ApproveIn(BaseModel):
    id: str
    answers: list[dict] = []
    agreed: list[str] = []
    allow: list[str] = []  # hiring rules Sai chose to go ahead despite (Approve anyway)


@app.post("/api/jobs/approve")
def jobs_approve(body: ApproveIn, request: Request):
    """Sai approves one application: his edited answers and the form's consents. The
    autofill script submits approved applications in his browser."""
    check_user(request)
    try:
        missing = jobs.approve(body.id, body.answers, body.agreed[:100], body.allow[:40])
    except ValueError as e:
        raise HTTPException(400, str(e))
    if missing:
        raise HTTPException(400, "Answer these first: " + "; ".join(missing))
    return {"ok": True}


@app.get("/api/jobs/next")
def jobs_next(request: Request):
    check_user(request)
    return jobs.next_approved()


@app.post("/api/jobs/defer")
def jobs_defer(body: JobIn, request: Request):
    check_user(request)
    if not jobs.defer(body.id):
        raise HTTPException(404, "No such job")
    return {"ok": True}


class MailIn(BaseModel):
    address: str
    password: str


@app.get("/api/inbox")
def inbox_get(request: Request):
    check_user(request)
    return fast(request, jobs.inbox_summary())


@app.post("/api/inbox")
def inbox_setup(body: MailIn, request: Request):
    """Connect the agent's mailbox. The login is checked before anything is saved, and
    the password is never sent back."""
    check_user(request)
    if len(body.address) > 200 or len(body.password) > 200:
        raise HTTPException(400, "Too long")
    try:
        jobs.set_mail(body.address, body.password)
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e))
    threading.Thread(target=jobs.check_inbox, args=(notify,), daemon=True).start()
    return {"ok": True}


@app.post("/api/inbox/check")
def inbox_check(request: Request):
    """Check now, in the background: the model may take a while to read new emails."""
    check_user(request)
    threading.Thread(target=jobs.check_inbox, args=(notify, panel_busy), daemon=True).start()
    return {"ok": True}


class DoneIn(BaseModel):
    uid: int
    done: bool = True


@app.post("/api/inbox/done")
def inbox_done(body: DoneIn, request: Request):
    check_user(request)
    if not jobs.mark_done(body.uid, body.done):
        raise HTTPException(404, "No such email")
    return {"ok": True}


@app.delete("/api/inbox")
def inbox_remove(request: Request):
    check_user(request)
    jobs.remove_mail()
    return {"ok": True}


class ProfileIn(BaseModel):
    values: dict[str, str]
    answers: list[dict] = []


@app.get("/api/jobs/profile")
def jobs_profile(request: Request):
    check_user(request)
    return jobs.profile_form()


@app.put("/api/jobs/profile")
def jobs_profile_save(body: ProfileIn, request: Request):
    check_user(request)
    if len(body.values) > 200 or len(body.answers) > jobs.MAX_SAVED_ANSWERS:
        raise HTTPException(400, "Too many fields")
    jobs.save_profile(body.values, body.answers)
    return {"ok": True}


class FillIn(BaseModel):
    url: str
    fields: list[dict]


@app.post("/api/jobs/fill")
def jobs_fill(body: FillIn, request: Request):
    check_user(request)
    if len(body.fields) > 200:
        raise HTTPException(400, "Too many fields")
    return jobs.fill(body.url, body.fields)


@app.get("/api/jobs/history")
def jobs_history(request: Request):
    """Work experience, education, websites and skills for Workday's My Experience page."""
    check_user(request)
    return jobs.work_history()


@app.get("/api/jobs/resume")
def jobs_resume(request: Request, id: str = "", kind: str = "resume"):
    """A document for the autofill script to attach: the job's tailored resume or cover
    letter, or the usual resume.pdf."""
    check_user(request)
    if kind not in ("resume", "cover", "base"):
        raise HTTPException(400, "kind is resume, cover or base")
    data, name = jobs.doc_file(id, kind)
    if data is None:
        raise HTTPException(404, "No such document")
    return {"data": base64.b64encode(data).decode(), "name": name}


@app.get("/api/jobs/doc")
def jobs_doc(id: str, kind: str, request: Request):
    """The same documents as PDFs, to open and read in the panel."""
    check_user(request)
    if kind not in ("resume", "cover"):
        raise HTTPException(400, "kind is resume or cover")
    data, name = jobs.doc_file(id, kind)
    if data is None:
        raise HTTPException(404, "No such document")
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"', "Cache-Control": "no-store"})


class DocsIn(BaseModel):
    id: str
    summary: str = ""
    cover: str = ""


@app.post("/api/jobs/docs")
def jobs_docs_make(body: DocsIn, request: Request):
    """Tailor the resume and draft the cover letter for one job, in the background."""
    check_user(request)
    if not jobs.load_resume():
        raise HTTPException(409, "No resume.json in the jobs folder yet")
    if not jobs.detail(body.id):
        raise HTTPException(404, "No such job")
    threading.Thread(target=jobs.make_docs, args=(body.id, panel_busy), daemon=True).start()
    return {"ok": True}


@app.put("/api/jobs/docs")
def jobs_docs_save(body: DocsIn, request: Request):
    check_user(request)
    if not jobs.save_docs(body.id, body.summary, body.cover):
        raise HTTPException(404, "No documents for this job")
    return {"ok": True}


@app.post("/api/jobs/ready")
def jobs_ready(request: Request):
    """Fill in answers, resumes and cover letters for the review list now."""
    check_user(request)
    if jobs.progress["running"]:
        raise HTTPException(409, "A job search is already running")
    threading.Thread(target=jobs.run_get_ready, args=(panel_busy,), daemon=True).start()
    return {"ok": True}


@app.get("/api/jobs/rules")
def jobs_rules(request: Request):
    check_user(request)
    return {"rules": [{"id": r[0], "title": r[1], "default": r[2], "help": r[3]} for r in jobs.RULES],
            "settings": jobs.rule_settings()}


class RulesIn(BaseModel):
    actions: dict[str, str] = {}
    limit: dict = {}
    limits: dict[str, dict] = {}
    cooldown_days: int = 180
    notes: dict[str, str] = {}


@app.put("/api/jobs/rules")
def jobs_rules_save(body: RulesIn, request: Request):
    check_user(request)
    try:
        jobs.save_rule_settings(body.model_dump())
    except (TypeError, ValueError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


class SubmittingIn(BaseModel):
    id: str
    fields: list[dict] = []
    files: list[dict] = []


@app.post("/api/jobs/submitting")
def jobs_submitting(body: SubmittingIn, request: Request):
    """The autofill script's record of a form right before it's submitted."""
    check_user(request)
    if len(body.fields) > 250 or len(body.files) > 10:
        raise HTTPException(400, "Too many fields")
    if not jobs.save_submission(body.id, body.fields, body.files, "autofill"):
        raise HTTPException(404, "No such job")
    return {"ok": True}


@app.get("/api/applications")
def applications_list(request: Request):
    check_user(request)
    jobs.backfill_submissions()
    return fast(request, {"applications": jobs.applications()})


@app.get("/api/report")
def report_view(request: Request, end: str = ""):
    """This week so far, or a saved weekly report by the day it ended."""
    check_user(request)
    if end:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", end):
            raise HTTPException(400, "end is a date like 2026-09-28")
        report = jobs.saved_report(end)
        if report is None:
            raise HTTPException(404, "No report for that week")
    else:
        report = jobs.weekly_report()
    return fast(request, {"report": report, "saved": jobs.saved_reports()})


@app.get("/api/applications/doc")
def applications_doc(id: str, kind: str, request: Request):
    """The resume or cover letter exactly as it was submitted."""
    check_user(request)
    if kind not in ("resume", "cover"):
        raise HTTPException(400, "kind is resume or cover")
    data = jobs.submission_file(id, kind)
    if data is None:
        raise HTTPException(404, "Not submitted with this application")
    name = next((f["name"] for f in (jobs.detail(id) or {}).get("submission", {}).get("files", []) if f["doc"] == kind), f"{kind}.pdf")
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'inline; filename="{name}"', "Cache-Control": "no-store"})


class WorkdayIn(BaseModel):
    password: str | None = None
    accept_terms: bool | None = None


@app.get("/api/workday")
def workday_get(request: Request):
    """Whether a Workday password is set; the password itself is never sent to the panel."""
    check_user(request)
    return jobs.workday_settings()


@app.put("/api/workday")
def workday_put(body: WorkdayIn, request: Request):
    check_user(request)
    if body.password is not None and len(body.password) > 200:
        raise HTTPException(400, "Too long")
    try:
        jobs.save_workday(body.password, body.accept_terms)
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


WORKDAY_TENANT_RE = re.compile(r"^[a-z0-9-]{1,60}\.wd\d{1,3}$")


@app.get("/api/workday/login")
def workday_login(tenant: str, request: Request):
    """For the autofill script on a Workday sign-in or sign-up page."""
    check_user(request)
    if not WORKDAY_TENANT_RE.match(tenant):
        raise HTTPException(400, "Bad tenant")
    creds = jobs.workday_login(tenant)
    if not creds:
        raise HTTPException(409, "Set a Workday password under Settings, Workday in the panel first")
    return creds


class TenantIn(BaseModel):
    tenant: str


@app.post("/api/workday/account")
def workday_account(body: TenantIn, request: Request):
    check_user(request)
    if not WORKDAY_TENANT_RE.match(body.tenant):
        raise HTTPException(400, "Bad tenant")
    jobs.workday_account(body.tenant)
    return {"ok": True}


@app.post("/api/jobs/stop")
def jobs_stop(request: Request):
    """End the running search at its next step; what's saved so far stays."""
    check_user(request)
    if not jobs.progress["running"]:
        raise HTTPException(409, "No search is running")
    jobs.progress["stop"] = True
    return {"ok": True}


@app.get("/jobs-fill.user.js")
def fill_script(request: Request):
    """The autofill userscript, pointed at whatever address the panel was opened on,
    so the tailnet name never has to be in the repo."""
    check_user(request)
    host = request.headers.get("host", "")
    if not re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?", host):
        raise HTTPException(400, "Bad host")
    local = host.split(":")[0] in ("localhost", "127.0.0.1")
    panel = f"{'http' if local else 'https'}://{host}"
    script = FILL_SCRIPT.replace("__PANEL__", panel).replace("__HOST__", host.split(":")[0])
    return Response(script, media_type="text/javascript", headers={"Cache-Control": "no-cache"})


class ChatIn(BaseModel):
    message: str
    chat_id: str = ""
    model: str = "better"
    files: list[FileIn] = []


class ExtractIn(BaseModel):
    name: str
    data: str  # base64


@app.post("/api/extract")
def extract(body: ExtractIn, request: Request):
    """Turn an attached file into text, so the page can show its size and reading time
    before sending. Nothing is stored."""
    check_user(request)
    try:
        data = base64.b64decode(body.data, validate=True)
    except ValueError:
        raise HTTPException(400, "Bad file data")
    if len(data) > FILE_MAX_BYTES:
        raise HTTPException(400, f"Files are limited to {FILE_MAX_BYTES // 1_000_000} MB")
    try:
        text = extract_text(body.name, data)
    except ValueError as e:
        raise HTTPException(400, str(e))
    if not text.strip():
        raise HTTPException(400, "No text found in this file")
    return {"name": safe_filename(body.name), "text": text[:FILE_MAX_CHARS], "chars": len(text),
            "chat_limit": CHAT_ATTACH_CHARS}


@app.get("/api/chats")
def chats_list(request: Request):
    check_user(request)
    with chat_lock:
        chats = load_chats()
    items = [{"id": k, "title": c["title"], "updated": c["updated"]} for k, c in chats.items()]
    return sorted(items, key=lambda c: c["updated"], reverse=True)


@app.get("/api/chats/{chat_id}")
def chat_get(chat_id: str, request: Request):
    check_user(request)
    with chat_lock:
        chat = load_chats().get(chat_id)
    if not chat:
        raise HTTPException(404, "No such chat")
    return chat


@app.delete("/api/chats/{chat_id}")
def chat_delete(chat_id: str, request: Request):
    check_user(request)
    with chat_lock:
        chats = load_chats()
        chats.pop(chat_id, None)
        save_chats(chats)
    return {"ok": True}


@app.post("/api/chat")
def chat_send(body: ChatIn, request: Request):
    """Add Sai's message and stream the reply as plain text. The chat id comes back
    in the X-Chat-Id header, so the first message of a new chat can create it."""
    check_user(request)
    text = body.message.strip()
    if not text and not body.files:
        raise HTTPException(400, "Empty message")
    if body.files and not text:
        text = "Summarize the attached file." if len(body.files) == 1 else "Summarize the attached files."
    content = (chat_attachment_text(body.files) + "\n\n" + text) if body.files else text
    model = CHAT_MODELS.get(body.model, CHAT_MODELS["better"])
    with chat_lock:
        chats = load_chats()
        chat_id = body.chat_id if body.chat_id in chats else uuid.uuid4().hex[:10]
        chat = chats.setdefault(chat_id, {"title": text[:60], "created": time.time(), "messages": []})
        msg = {"role": "user", "content": content, "time": now()}
        if body.files:  # the page shows the message and file names, not the file text
            msg.update(text=text, files=[{"name": f.name, "chars": len(f.text)} for f in body.files])
        chat["messages"].append(msg)
        chat["updated"] = time.time()
        remembered = not body.files and REMEMBER_RE.match(text)
        if remembered:  # "remember that ..." saves the fact itself; no model call
            fact = " ".join(remembered.group(1).split())
            mem = load_memory()
            if len(mem) + len(fact) + 3 > MEMORY_MAX_CHARS:
                reply = "Memory is full. Remove something under Settings > Memory first."
            else:
                save_memory(f"{mem}\n- {fact}")
                reply = f"Saved to memory: {fact}"
            chat["messages"].append({"role": "assistant", "content": reply, "time": now()})
        save_chats(chats)
    headers = {"X-Chat-Id": chat_id, "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}
    if remembered:
        return Response(reply, media_type="text/plain; charset=utf-8", headers=headers)
    if not chat_active.acquire(blocking=False):
        raise HTTPException(409, "Another reply is still being written")
    chat_active.release()
    return StreamingResponse(stream_reply(chat_id, model, request), media_type="text/plain; charset=utf-8",
                             headers=headers)


class MemoryIn(BaseModel):
    text: str


@app.get("/api/memory")
def memory_get(request: Request):
    check_user(request)
    return {"text": load_memory(), "max": MEMORY_MAX_CHARS}


@app.put("/api/memory")
def memory_put(body: MemoryIn, request: Request):
    check_user(request)
    if len(body.text) > MEMORY_MAX_CHARS:
        raise HTTPException(400, f"Memory is limited to {MEMORY_MAX_CHARS} characters")
    save_memory(body.text)
    return {"ok": True}


class SubscriptionIn(BaseModel):
    endpoint: str
    keys: dict


@app.get("/api/push/key")
def push_key(request: Request):
    check_user(request)
    return {"key": VAPID_PUBLIC}


@app.post("/api/push/subscribe")
def push_subscribe(body: SubscriptionIn, request: Request):
    check_user(request)
    sub = {"endpoint": body.endpoint, "keys": body.keys}
    with push_lock:
        subs = [s for s in load_subs() if s.get("endpoint") != body.endpoint]
        subs.append(sub)
        save_subs(subs)
    return {"ok": True}


@app.post("/api/push/test")
def push_test(request: Request):
    check_user(request)
    notify("Agent", "Notifications are working.", tag="test")
    return {"ok": True}


@app.get("/manifest.json")
def manifest(request: Request):
    check_user(request)
    return Response(MANIFEST, media_type="application/manifest+json")


@app.get("/sw.js")
def service_worker(request: Request):
    check_user(request)
    return Response(SERVICE_WORKER, media_type="application/javascript",
                    headers={"Cache-Control": "no-cache"})


@app.post("/api/clear")
def clear(request: Request):
    check_user(request)
    with lock:
        if state["status"] != "idle":
            raise HTTPException(409, "Stop the task first")
        state["events"].clear()
        state["seq"] += 1
    return {"ok": True}


MANIFEST = json.dumps({
    "name": "Agent",
    "short_name": "Agent",
    "start_url": "/",
    "scope": "/",
    "display": "standalone",
    "background_color": "#0b0b10",
    "theme_color": "#0b0b10",
})

SERVICE_WORKER = """
self.addEventListener("push", event => {
  let d = {};
  try { d = event.data.json(); } catch (e) { d = {title: "Agent", body: event.data ? event.data.text() : ""}; }
  event.waitUntil(self.registration.showNotification(d.title || "Agent", {
    body: d.body || "", tag: d.tag || "agent", data: {url: (d.tag === "jobs" || d.tag === "inbox") ? "/#jobs" : "/"}
  }));
});
self.addEventListener("notificationclick", event => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(clients.matchAll({type: "window", includeUncontrolled: true}).then(list => {
    for (const c of list) {
      if ("focus" in c) { if (url !== "/") c.postMessage({view: "jobs"}); return c.focus(); }
    }
    return clients.openWindow(url);
  }));
});
"""

PAGE = r"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<meta name="apple-mobile-web-app-title" content="Agent">
<meta name="theme-color" content="#0b0b10">
<link rel="manifest" href="manifest.json">
<title>Agent</title>
<style>
:root{
  --bg:#f5f5f9;--surface:#ffffff;--surface2:#f0f0f6;--raise:#ffffffcc;--line:#e3e3ec;--line2:#d4d4e0;
  --fg:#16161d;--muted:#6c6c7e;--faint:#9a9aab;
  --accent:#6a4cff;--accent2:#0ea5c6;--accent-fg:#fff;--accent-soft:#6a4cff14;
  --good:#12a150;--good-soft:#12a15016;--warn:#c97a06;--warn-soft:#c97a0618;--bad:#d9303e;--bad-soft:#d9303e14;
  --code:#f0f0f6;--shadow:0 1px 2px #0000000a,0 8px 24px #00000010;--ring-track:#e6e6ef;
  --r:14px;--r-sm:10px;
}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){
  --bg:#0b0b10;--surface:#13131b;--surface2:#1a1a24;--raise:#17172199;--line:#252532;--line2:#31313f;
  --fg:#ececf4;--muted:#9090a4;--faint:#63637a;
  --accent:#8b73ff;--accent2:#22d3ee;--accent-soft:#8b73ff1f;
  --good:#34d17c;--good-soft:#34d17c1a;--warn:#f3a93c;--warn-soft:#f3a93c1a;--bad:#ff5d6c;--bad-soft:#ff5d6c1a;
  --code:#1d1d28;--shadow:0 1px 2px #0006,0 12px 32px #0007;--ring-track:#262634;
}}
:root[data-theme="dark"]{
  --bg:#0b0b10;--surface:#13131b;--surface2:#1a1a24;--raise:#17172199;--line:#252532;--line2:#31313f;
  --fg:#ececf4;--muted:#9090a4;--faint:#63637a;
  --accent:#8b73ff;--accent2:#22d3ee;--accent-soft:#8b73ff1f;
  --good:#34d17c;--good-soft:#34d17c1a;--warn:#f3a93c;--warn-soft:#f3a93c1a;--bad:#ff5d6c;--bad-soft:#ff5d6c1a;
  --code:#1d1d28;--shadow:0 1px 2px #0006,0 12px 32px #0007;--ring-track:#262634;
}
*{box-sizing:border-box}
html,body{margin:0;height:100%}
body{background:var(--bg);color:var(--fg);font:15px/1.5 -apple-system,BlinkMacSystemFont,"SF Pro Text",Inter,"Segoe UI",system-ui,sans-serif;-webkit-font-smoothing:antialiased;overflow:hidden}
body::before{content:"";position:fixed;inset:-20%;z-index:-1;pointer-events:none;
  background:radial-gradient(40% 35% at 12% 8%,color-mix(in srgb,var(--accent) 22%,transparent),transparent 70%),
             radial-gradient(35% 30% at 92% 96%,color-mix(in srgb,var(--accent2) 16%,transparent),transparent 70%)}
button,input,select,textarea{font:inherit;color:inherit}
button{cursor:pointer;border:0;background:none}
a{color:inherit}
svg{width:18px;height:18px;flex:none;fill:none;stroke:currentColor;stroke-width:1.8;stroke-linecap:round;stroke-linejoin:round}
.sr{position:absolute;width:1px;height:1px;overflow:hidden;clip:rect(0 0 0 0)}

/* ---------- shell ---------- */
#app{display:grid;grid-template-columns:236px minmax(0,1fr);height:100dvh}
#rail{display:flex;flex-direction:column;gap:4px;padding:18px 12px;border-right:1px solid var(--line);background:var(--raise);backdrop-filter:blur(18px)}
.brand{display:flex;align-items:center;gap:10px;padding:4px 10px 18px;font-weight:700;font-size:17px;letter-spacing:-.01em}
.logo{width:28px;height:28px;border-radius:9px;background:linear-gradient(135deg,var(--accent),var(--accent2));display:grid;place-items:center;box-shadow:0 4px 14px color-mix(in srgb,var(--accent) 40%,transparent)}
.logo svg{width:16px;height:16px;stroke:#fff;stroke-width:2.2}
.nav{display:flex;align-items:center;gap:12px;padding:9px 12px;border-radius:10px;color:var(--muted);font-weight:550;text-align:left;width:100%;position:relative;transition:background .15s,color .15s}
.nav:hover{background:var(--surface2);color:var(--fg)}
.nav.on{background:var(--accent-soft);color:var(--fg)}
.nav.on svg{color:var(--accent)}
.badge{margin-left:auto;min-width:20px;height:20px;padding:0 6px;border-radius:10px;background:var(--accent);color:var(--accent-fg);font-size:11.5px;font-weight:700;display:none;align-items:center;justify-content:center;font-variant-numeric:tabular-nums}
.badge.on{display:inline-flex}
.badge.warn{background:var(--warn)}
.rail-foot{margin-top:auto;display:flex;flex-direction:column;gap:8px;padding:10px 4px 0}
.status{display:flex;align-items:center;gap:8px;font-size:12.5px;color:var(--muted);min-width:0}
.status span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.dot{width:8px;height:8px;border-radius:50%;background:var(--faint);flex:none}
.dot.idle{background:var(--good);box-shadow:0 0 0 3px var(--good-soft)}
.dot.busy{background:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);animation:pulse 1.4s infinite}
.dot.wait{background:var(--warn);box-shadow:0 0 0 3px var(--warn-soft);animation:pulse 1.4s infinite}
.dot.off{background:var(--bad)}
@keyframes pulse{50%{opacity:.45}}
#topbar{display:none}
#tabbar{display:none}
main{min-width:0;min-height:0;position:relative}
.view{display:none;flex-direction:column;height:100%;min-height:0}
.view.on{display:flex}
.vhead{display:flex;align-items:center;gap:10px;padding:18px 24px 12px;flex-wrap:wrap}
.vhead h1{font-size:22px;letter-spacing:-.02em;margin:0;font-weight:700}
.vhead .sub{color:var(--muted);font-size:13px;width:100%;margin-top:-4px}
.spacer{flex:1}
.vbody{flex:1;min-height:0;overflow-y:auto;padding:4px 24px 24px;-webkit-overflow-scrolling:touch}

/* ---------- controls ---------- */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;height:36px;padding:0 14px;border-radius:10px;font-weight:600;font-size:14px;white-space:nowrap;transition:transform .08s,background .15s,opacity .15s;text-decoration:none}
.btn:active{transform:scale(.97)}
.btn:disabled{opacity:.5;cursor:default}
.btn.primary{background:linear-gradient(135deg,var(--accent),color-mix(in srgb,var(--accent) 70%,var(--accent2)));color:var(--accent-fg);box-shadow:0 4px 14px color-mix(in srgb,var(--accent) 30%,transparent)}
.btn.good{background:var(--good);color:#fff}
.btn.danger{background:var(--bad-soft);color:var(--bad)}
.btn.ghost{background:var(--surface);border:1px solid var(--line);color:var(--fg)}
.btn.ghost:hover{border-color:var(--line2)}
.btn.icon{width:36px;padding:0}
.btn.sm{height:30px;padding:0 10px;font-size:13px;border-radius:8px}
.btn.block{width:100%}
.seg{display:inline-flex;background:var(--surface2);border:1px solid var(--line);border-radius:11px;padding:3px;gap:2px}
.seg button{height:28px;padding:0 11px;border-radius:8px;font-size:13px;font-weight:600;color:var(--muted);display:inline-flex;align-items:center;gap:6px;white-space:nowrap}
.seg button.on{background:var(--surface);color:var(--fg);box-shadow:var(--shadow)}
.seg .n{font-size:11px;color:var(--faint);font-variant-numeric:tabular-nums}
.seg button.on .n{color:var(--accent)}
.scrollx{overflow-x:auto;scrollbar-width:none;max-width:100%}
.scrollx::-webkit-scrollbar{display:none}
.field{display:block;margin:0 0 14px}
.field>label,.flabel{display:block;font-size:13px;font-weight:600;margin-bottom:6px}
.help{font-size:12.5px;color:var(--muted);margin-top:5px}
.inp,textarea.inp,select.inp{width:100%;background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:10px 12px;font-size:15px;outline:none;transition:border-color .15s,box-shadow .15s}
.inp:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
textarea.inp{min-height:84px;resize:vertical;line-height:1.45}
select.inp{appearance:none;background-image:linear-gradient(45deg,transparent 50%,var(--muted) 50%),linear-gradient(135deg,var(--muted) 50%,transparent 50%);background-position:calc(100% - 17px) 55%,calc(100% - 12px) 55%;background-size:5px 5px;background-repeat:no-repeat;padding-right:32px}
.need .inp{border-color:color-mix(in srgb,var(--warn) 70%,var(--line));background:color-mix(in srgb,var(--warn) 5%,var(--surface))}
.card{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px;box-shadow:var(--shadow)}
.chip{display:inline-flex;align-items:center;gap:5px;height:24px;padding:0 9px;border-radius:12px;font-size:12px;font-weight:600;background:var(--surface2);color:var(--muted);white-space:nowrap}
.chip.good{background:var(--good-soft);color:var(--good)}
.chip.bad{background:var(--bad-soft);color:var(--bad)}
.chip.warn{background:var(--warn-soft);color:var(--warn)}
.chip.acc{background:var(--accent-soft);color:var(--accent)}
.chips{display:flex;flex-wrap:wrap;gap:6px}
.muted{color:var(--muted)}
.small{font-size:13px}
.empty{display:flex;flex-direction:column;align-items:center;justify-content:center;gap:10px;text-align:center;color:var(--muted);padding:56px 20px}
.empty svg{width:40px;height:40px;stroke-width:1.3;color:var(--faint)}
.empty b{color:var(--fg);font-size:16px}
pre{background:var(--code);padding:10px 12px;border-radius:10px;white-space:pre-wrap;word-break:break-word;font:12.5px/1.5 ui-monospace,"SF Mono",Menlo,Consolas,monospace;margin:8px 0 0;max-height:360px;overflow:auto}
code{font:13px ui-monospace,"SF Mono",Menlo,Consolas,monospace;background:var(--code);padding:1px 5px;border-radius:5px}
details>summary{cursor:pointer;list-style:none;display:flex;align-items:center;gap:8px;font-weight:600;font-size:14px;padding:6px 0;user-select:none}
details>summary::-webkit-details-marker{display:none}
details>summary::before{content:"";width:7px;height:7px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(-45deg);transition:transform .15s;margin:0 3px}
details[open]>summary::before{transform:rotate(45deg)}
#toast{position:fixed;left:50%;bottom:28px;transform:translate(-50%,20px);background:var(--fg);color:var(--bg);padding:10px 16px;border-radius:12px;font-size:14px;font-weight:600;opacity:0;pointer-events:none;transition:all .2s;z-index:60;max-width:calc(100vw - 32px)}
#toast.on{opacity:1;transform:translate(-50%,0)}
#toast.bad{background:var(--bad);color:#fff}
#banner{display:none;position:fixed;top:14px;left:50%;transform:translateX(-50%);z-index:50;background:var(--warn);color:#111;border-radius:12px;padding:9px 14px;font-weight:650;font-size:14px;box-shadow:var(--shadow);align-items:center;gap:8px}
#banner.on{display:flex}

/* ---------- chat ---------- */
#v-chat{flex-direction:row}
.chatside{width:260px;border-right:1px solid var(--line);display:flex;flex-direction:column;min-height:0}
.chatside .vhead{padding-bottom:8px}
#chatlist{flex:1;overflow-y:auto;padding:0 10px 12px}
.citem{display:block;width:100%;text-align:left;padding:9px 12px;border-radius:10px;color:var(--muted);font-size:14px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.citem:hover{background:var(--surface2);color:var(--fg)}
.citem.on{background:var(--accent-soft);color:var(--fg);font-weight:600}
.chatmain{flex:1;display:flex;flex-direction:column;min-width:0;min-height:0}
#chatpick{display:none;max-width:48vw}
#msgs{flex:1;overflow-y:auto;padding:8px 24px 16px;display:flex;flex-direction:column;gap:14px}
.msgwrap{width:100%;max-width:780px;margin:0 auto;display:flex;flex-direction:column;gap:14px}
.msg{white-space:pre-wrap;word-break:break-word;padding:11px 15px;border-radius:18px;max-width:86%;line-height:1.55}
.msg.user{align-self:flex-end;background:linear-gradient(135deg,var(--accent),color-mix(in srgb,var(--accent) 75%,var(--accent2)));color:#fff;border-bottom-right-radius:6px}
.msg.assistant{align-self:flex-start;background:var(--surface);border:1px solid var(--line);border-bottom-left-radius:6px;box-shadow:var(--shadow)}
.msg.typing{color:var(--muted)}
.msg.typing::after{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--accent);margin-left:8px;animation:pulse 1s infinite}
.msg.err{color:var(--bad)}
.msg pre{margin:8px 0}
.msg .files{font-size:12px;opacity:.85;margin-top:6px}
.hello{margin:auto;text-align:center;max-width:460px;color:var(--muted);padding:40px 10px}
.hello .logo{width:52px;height:52px;border-radius:16px;margin:0 auto 16px}
.hello .logo svg{width:26px;height:26px}
.hello h2{color:var(--fg);margin:0 0 6px;font-size:22px;letter-spacing:-.02em}
.suggest{display:flex;flex-wrap:wrap;gap:8px;justify-content:center;margin-top:18px}
.suggest button{border:1px solid var(--line);background:var(--surface);border-radius:12px;padding:8px 12px;font-size:13.5px;color:var(--fg)}
.suggest button:hover{border-color:var(--accent)}
.composer{padding:10px 24px calc(14px + env(safe-area-inset-bottom,0px))}
.cbox{max-width:780px;margin:0 auto;background:var(--surface);border:1px solid var(--line);border-radius:18px;padding:8px;box-shadow:var(--shadow);transition:border-color .15s,box-shadow .15s}
.cbox:focus-within{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft),var(--shadow)}
.cfiles{display:none;flex-wrap:wrap;gap:6px;padding:2px 2px 8px}
.cfiles.on{display:flex}
.fchip{display:flex;align-items:center;gap:6px;font-size:12.5px;background:var(--surface2);border-radius:9px;padding:4px 4px 4px 10px;max-width:100%}
.fchip span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.fchip button{width:22px;height:22px;border-radius:6px;color:var(--muted);font-size:16px;line-height:1}
.fchip button:hover{background:var(--line)}
.crow{display:flex;align-items:flex-end;gap:6px}
.crow textarea{flex:1;border:0;outline:0;background:transparent;resize:none;padding:8px 6px;font-size:15.5px;line-height:1.45;max-height:200px;min-height:40px}
.cbtn{width:38px;height:38px;border-radius:12px;display:grid;place-items:center;color:var(--muted);flex:none}
.cbtn:hover{background:var(--surface2);color:var(--fg)}
.cbtn.send{background:var(--accent);color:#fff}
.cbtn.send:hover{background:var(--accent);filter:brightness(1.08)}
.cbtn.send.stop{background:var(--bad)}
.chint{max-width:780px;margin:6px auto 0;font-size:11.5px;color:var(--faint);text-align:center}

/* ---------- jobs ---------- */
.stats{display:flex;gap:10px;flex-wrap:wrap;width:100%}
.stat{flex:1;min-width:110px;background:var(--surface);border:1px solid var(--line);border-radius:12px;padding:10px 14px;box-shadow:var(--shadow);text-align:left;transition:border-color .15s}
.stat:hover{border-color:var(--line2)}
.stat b{display:block;font-size:22px;letter-spacing:-.02em;font-variant-numeric:tabular-nums}
.stat span{font-size:12px;color:var(--muted);font-weight:600}
.stat.hl b{background:linear-gradient(135deg,var(--accent),var(--accent2));-webkit-background-clip:text;background-clip:text;color:transparent}
#japply .short{display:none}
@media (max-width: 860px){#japply .short{display:inline}}
.jbar{display:flex;align-items:center;gap:8px;padding:0 24px 10px;flex-wrap:wrap}
#jmeta{font-size:12.5px;color:var(--muted);padding:0 24px 8px}
.jsplit{flex:1;min-height:0;display:grid;grid-template-columns:minmax(300px,420px) minmax(0,1fr);border-top:1px solid var(--line)}
#alist{overflow-y:auto;padding:12px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:8px}
#adetail{overflow-y:auto;min-width:0;position:relative;display:flex;flex-direction:column}
.funnel{display:flex;height:10px;border-radius:5px;overflow:hidden;background:var(--surface2);width:100%;margin-top:4px}
.reportpart{display:none}
#v-apps.report .alistpart{display:none}
#v-apps.report .reportpart{display:block}
#report{flex:1;min-height:0;overflow-y:auto;padding:4px 24px 32px;border-top:1px solid var(--line)}
#report .stats{margin:14px 0}
.rgrid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,420px),1fr));gap:14px;align-items:start}
.rcard h2{font-size:15px;margin:0 0 4px;letter-spacing:-.01em}
.rcard .sub{margin:0 0 12px}
.rtip{padding:10px 0}
.rtip+.rtip{border-top:1px solid var(--line);padding-top:12px}
.rtip b{display:block;margin-bottom:3px}
.rtip .why{margin-top:4px;font-size:12.5px;color:var(--muted)}
.days{display:grid;grid-template-columns:repeat(7,1fr);gap:8px;align-items:end;height:150px;margin-top:6px}
.days .col{display:flex;flex-direction:column;align-items:center;gap:4px;height:100%;justify-content:flex-end;min-width:0}
.days .stack{width:100%;max-width:38px;display:flex;flex-direction:column-reverse;border-radius:6px 6px 2px 2px;overflow:hidden;background:var(--surface2)}
.days .stack i{display:block;width:100%}
.days .v{font-size:12px;font-weight:700;font-variant-numeric:tabular-nums}
.days .d{font-size:11.5px;color:var(--muted)}
.legend2{display:flex;gap:14px;flex-wrap:wrap;margin-top:10px;font-size:12px;color:var(--muted)}
.legend2 span::before{content:"";display:inline-block;width:9px;height:9px;border-radius:3px;margin-right:5px;background:var(--c);vertical-align:-1px}
.mixblock{margin-top:14px}
.mixblock:first-of-type{margin-top:4px}
.mixblock .t{font-size:12px;font-weight:700;color:var(--muted);text-transform:uppercase;letter-spacing:.05em;margin-bottom:6px}
.mrow{display:grid;grid-template-columns:minmax(0,9.5em) minmax(0,1fr) 3.2em 5.6em;gap:8px;align-items:center;font-size:13px;padding:3px 0}
.mrow .nm{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.mrow .bar{height:8px;border-radius:4px;background:var(--surface2);overflow:hidden}
.mrow .bar i{display:block;height:100%;background:var(--accent)}
.mrow .pc{text-align:right;font-variant-numeric:tabular-nums;font-weight:600}
.mrow .fit{text-align:right;font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
.delta{font-size:11px;margin-left:4px;font-weight:600}
.delta.up{color:var(--good)} .delta.down{color:var(--bad)}
.rtable{width:100%;border-collapse:collapse;font-size:13px;font-variant-numeric:tabular-nums}
.rtable th{text-align:left;font-size:11.5px;color:var(--muted);font-weight:600;padding:4px 6px;border-bottom:1px solid var(--line)}
.rtable td{padding:6px;border-bottom:1px solid var(--line)}
.rtable th:not(:first-child),.rtable td:not(:first-child){text-align:right}
.rtable tr.few td{color:var(--muted)}
.skill{display:flex;justify-content:space-between;gap:10px;font-size:13px;padding:5px 0;border-bottom:1px solid var(--line)}
.skill:last-child{border-bottom:0}
@media (max-width:760px){ #report{padding:4px 14px 28px} .mrow{grid-template-columns:minmax(0,7.5em) minmax(0,1fr) 3em 5em} }
.funnel i{display:block;height:100%}
.legend{display:flex;flex-wrap:wrap;gap:12px;font-size:12px;color:var(--muted);margin-top:8px}
.legend span::before{content:"";display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:5px;background:var(--c)}
.arow{display:block;text-align:left;width:100%;padding:12px 14px;border-radius:12px;border:1px solid transparent;cursor:pointer;transition:background .12s,border-color .12s}
.arow:hover{background:var(--surface2)}
.arow.on{background:var(--surface);border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.arow .co{font-weight:700}
.arow .last{font-size:12.5px;color:var(--muted);margin-top:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.tl{margin:6px 0 0 6px}
.tl .ev{padding-bottom:12px}
.tl .ev .when{font-size:12px;color:var(--muted)}
.qa{padding:10px 0;border-bottom:1px solid var(--line)}
.qa:last-child{border-bottom:0}
.qa .qq{font-size:13px;font-weight:650;color:var(--muted)}
.qa .aa{white-space:pre-wrap;word-break:break-word;font-size:14.5px;margin-top:3px}
.qa .aa.blank{color:var(--faint);font-style:italic}
#jlist{overflow-y:auto;padding:12px;border-right:1px solid var(--line);display:flex;flex-direction:column;gap:8px}
.jrow{display:flex;gap:12px;align-items:flex-start;padding:12px;border-radius:12px;border:1px solid transparent;cursor:pointer;text-align:left;width:100%;transition:background .12s,border-color .12s}
.jrow:hover{background:var(--surface2)}
.jrow.on{background:var(--surface);border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft)}
.ring{--p:0;--c:var(--muted);width:42px;height:42px;border-radius:50%;flex:none;display:grid;place-items:center;font-size:13px;font-weight:750;font-variant-numeric:tabular-nums;
  background:radial-gradient(closest-side,var(--surface) 76%,transparent 78% 100%),conic-gradient(var(--c) calc(var(--p)*1%),var(--ring-track) 0)}
.jrow:hover .ring,.jrow.on .ring{background:radial-gradient(closest-side,var(--surface) 76%,transparent 78% 100%),conic-gradient(var(--c) calc(var(--p)*1%),var(--ring-track) 0)}
.ring.strong{--c:var(--good)}
.ring.good{--c:var(--accent)}
.ring.big{width:58px;height:58px;font-size:17px}
.jinfo{min-width:0;flex:1}
.jt{font-weight:650;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.jm{font-size:13px;color:var(--muted);margin:2px 0 6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
#jdetail{overflow-y:auto;min-width:0;position:relative;display:flex;flex-direction:column}
.jd{padding:22px 26px 0;max-width:860px;width:100%}
/* focused review: one application at a time, full width */
#v-jobs.focus .jbar,#v-jobs.focus #jmeta,#v-jobs.focus #jlist{display:none}
#v-jobs.focus .jsplit{grid-template-columns:minmax(0,1fr)}
#v-jobs.focus .jd,#v-jobs.focus .stickyfoot{max-width:820px;margin-left:auto;margin-right:auto;width:100%}
#v-jobs.focus .jback{display:none}
.fbar{display:flex;align-items:center;gap:12px;margin-bottom:16px}
.fbar .pbar{flex:1;margin:0}
.fbar .fc{font-size:13px;font-weight:650;color:var(--muted);white-space:nowrap;font-variant-numeric:tabular-nums}
.keyhint{font-size:11px;font-weight:700;border:1px solid var(--line);border-radius:5px;padding:0 5px;margin-left:6px;color:var(--muted)}
.jd.enter{animation:cardin .22s ease-out}
@keyframes cardin{from{opacity:0;transform:translateX(18px)}to{opacity:1;transform:none}}
.swipehint{font-size:12px;color:var(--muted);text-align:center;margin:-4px 0 10px}
@media (prefers-reduced-motion:reduce){.jd.enter{animation:none}}
@media (hover:none){.keyhint{display:none}}
.jdh{display:flex;gap:16px;align-items:flex-start}
.jdh h2{margin:0;font-size:21px;line-height:1.25;letter-spacing:-.02em}
.jdh .jm{white-space:normal;margin:4px 0 0;font-size:14px}
.jback{display:none}
.jsum{color:var(--muted);margin:14px 0 10px}
.skills{margin:10px 0 4px}
.actions{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 6px}
.sec{margin:22px 0 6px}
.sech{display:flex;align-items:center;gap:8px;font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin-bottom:10px}
.sech .chip{text-transform:none;letter-spacing:0}
.q{padding:12px 14px;border:1px solid var(--line);border-radius:12px;background:var(--surface);margin-bottom:8px}
.q .ql{font-size:14px;font-weight:600;line-height:1.4}
.q .qk{font-size:12px;color:var(--muted);margin:2px 0 8px}
.q .qa{white-space:pre-wrap;word-break:break-word;font-size:14px;margin-top:6px}
.q .req{color:var(--warn);font-weight:700}
.q.need{border-color:color-mix(in srgb,var(--warn) 45%,var(--line))}
.consent{display:flex;gap:10px;align-items:flex-start;cursor:pointer}
.consent input{width:20px;height:20px;accent-color:var(--accent);margin-top:1px;flex:none}
.stickyfoot{position:sticky;bottom:0;margin-top:auto;padding:12px 26px calc(12px + env(safe-area-inset-bottom,0px));background:linear-gradient(to top,var(--bg) 70%,transparent);display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.stickyfoot .left{font-size:13px;color:var(--muted);flex:1;min-width:140px}
.flag{display:flex;gap:10px;align-items:flex-start;padding:11px 13px;border-radius:12px;border:1px solid var(--line);background:var(--surface);margin-bottom:8px}
.flag .fb{font-size:11px;font-weight:750;text-transform:uppercase;letter-spacing:.05em;padding:2px 7px;border-radius:6px;flex:none;margin-top:1px}
.flag.block{border-color:color-mix(in srgb,var(--bad) 45%,var(--line))}.flag.block .fb{background:var(--bad-soft);color:var(--bad)}
.flag.ask{border-color:color-mix(in srgb,var(--warn) 45%,var(--line))}.flag.ask .fb{background:var(--warn-soft);color:var(--warn)}
.flag.warn .fb{background:var(--surface2);color:var(--muted)}
.flag .ft{font-weight:650;font-size:14px}.flag .fm{font-size:13px;color:var(--muted)}
.rrow{display:grid;grid-template-columns:1fr 190px;gap:12px;align-items:center;padding:12px 0;border-bottom:1px solid var(--line)}
.rrow b{font-size:14px}.rrow .help{margin-top:2px}
.kv{display:grid;grid-template-columns:1fr 90px 90px 36px;gap:8px;margin-bottom:8px;align-items:center}
.kv.two{grid-template-columns:1fr 2fr 36px}
@media (max-width: 860px){.rrow{grid-template-columns:1fr}.kv{grid-template-columns:1fr 64px 64px 36px}}
.docs{display:grid;grid-template-columns:1fr 1fr;gap:10px}
.doc{display:flex;gap:12px;align-items:center;padding:14px;border:1px solid var(--line);border-radius:12px;background:var(--surface);box-shadow:var(--shadow);text-decoration:none;color:var(--fg);transition:border-color .15s,transform .08s}
.doc:hover{border-color:var(--accent)}
.doc:active{transform:scale(.98)}
.doc .dic{width:40px;height:48px;border-radius:8px;background:linear-gradient(160deg,var(--accent-soft),transparent);border:1px solid var(--line);display:grid;place-items:center;color:var(--accent);flex:none}
.doc b{display:block;font-size:14px}
.doc span{font-size:12px;color:var(--muted)}
@media (max-width: 860px){.docs{grid-template-columns:1fr}}
.mail{padding:14px;border:1px solid var(--line);border-radius:12px;background:var(--surface);margin-bottom:8px;box-shadow:var(--shadow)}
.mail .mt{font-weight:650;margin:6px 0 2px}
.mail .code{font:700 26px ui-monospace,"SF Mono",Menlo,monospace;letter-spacing:4px;margin:8px 0}

/* ---------- tasks ---------- */
#events{max-width:860px;margin:0 auto;width:100%}
.ev{position:relative;padding:0 0 14px 22px;border-left:2px solid var(--line);margin-left:6px}
.ev::before{content:"";position:absolute;left:-6px;top:4px;width:10px;height:10px;border-radius:50%;background:var(--line2)}
.ev.task{border-left-color:transparent;padding-top:10px}
.ev.task::before{background:var(--accent);box-shadow:0 0 0 4px var(--accent-soft)}
.ev.task .tt{font-weight:700;font-size:15px}
.ev.answer::before{background:var(--good)}
.ev.err::before{background:var(--bad)}
.ev .thought{color:var(--muted);font-size:13.5px}
.ev .tool{font-size:12.5px;font-weight:650;color:var(--accent)}
.ev .ans{background:var(--surface);border:1px solid var(--line);border-left:3px solid var(--good);border-radius:10px;padding:10px 14px;white-space:pre-wrap;box-shadow:var(--shadow)}
.ev .bad{color:var(--bad);font-size:14px}
#pending{display:none;max-width:860px;margin:0 auto 14px;width:100%;border:1px solid var(--warn);background:color-mix(in srgb,var(--warn) 6%,var(--surface))}
#pending.on{display:block}

/* ---------- you ---------- */
.youwrap{max-width:900px;margin:0 auto;width:100%}
.twrap{max-width:1100px;margin:0 auto;width:100%;display:grid;grid-template-columns:minmax(0,3fr) minmax(0,2fr);gap:16px;align-items:start}
.tcol{display:flex;flex-direction:column;gap:14px;min-width:0}
.tcard{background:var(--surface);border:1px solid var(--line);border-radius:var(--r);padding:16px;box-shadow:var(--shadow)}
.tcard h2{font-size:13px;font-weight:700;text-transform:uppercase;letter-spacing:.06em;color:var(--muted);margin:0 0 10px;display:flex;align-items:center;gap:8px}
.hero{display:flex;align-items:center;gap:16px;flex-wrap:wrap}
.hero .big{font-size:40px;font-weight:750;letter-spacing:-.03em;line-height:1;font-variant-numeric:tabular-nums}
.hero .what{flex:1;min-width:160px}
.hero .what b{display:block;font-size:16px}
.trow{display:flex;align-items:center;gap:10px;padding:10px 0;border-top:1px solid var(--line)}
.trow:first-of-type{border-top:0;padding-top:2px}
.trow .tx{flex:1;min-width:0}
.trow .tx .t1{font-weight:600;overflow-wrap:anywhere}
.trow .tx .t2{font-size:13px;color:var(--muted);margin-top:2px}
.tstatus{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--muted)}
.pbar{height:6px;border-radius:3px;background:var(--surface2);overflow:hidden;margin-top:8px}
.pbar i{display:block;height:100%;background:var(--accent);transition:width .4s}
.kpis{display:grid;grid-template-columns:repeat(2,1fr);gap:8px;margin-bottom:6px}
.kpis div{background:var(--surface2);border-radius:10px;padding:10px}
.kpis b{display:block;font-size:20px;font-variant-numeric:tabular-nums}
.kpis span{font-size:12px;color:var(--muted);font-weight:600}
.code2{font:700 18px/1 ui-monospace,SFMono-Regular,Menlo,monospace;letter-spacing:.12em}
.seg .n:empty{display:none}
@media (max-width:900px){ .twrap{grid-template-columns:1fr} }
.progress{height:8px;border-radius:4px;background:var(--surface2);overflow:hidden;margin:8px 0 4px}
.progress i{display:block;height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));border-radius:4px;transition:width .3s}
.pgrid{display:grid;grid-template-columns:1fr 1fr;gap:0 16px}
.pgrid .wide{grid-column:1/-1}
.psec{scroll-margin-top:12px}
.psec h3{font-size:16px;margin:26px 0 12px;letter-spacing:-.01em;display:flex;align-items:center;gap:8px}
.pfield .from{margin-left:8px;font-size:11px;font-weight:600;color:var(--muted);background:var(--surface2);padding:1px 7px;border-radius:8px}
details.psec{border:1px solid var(--line);border-radius:12px;background:var(--surface);margin-top:10px;padding:0 16px}
details.psec>summary{list-style:none;cursor:pointer;display:flex;align-items:center;gap:8px;padding:14px 0;font-weight:650}
details.psec>summary::-webkit-details-marker{display:none}
details.psec>summary::before{content:"";width:7px;height:7px;border-right:2px solid var(--muted);border-bottom:2px solid var(--muted);transform:rotate(-45deg);transition:transform .15s;margin-right:4px}
details.psec[open]>summary::before{transform:rotate(45deg)}
details.psec>summary .cnt{margin-left:auto;font-size:12px;color:var(--muted);font-weight:600}
details.psec[open]{padding-bottom:10px}
.gaps{border:1px solid var(--warn);background:var(--warn-soft);border-radius:12px;padding:4px 16px 12px;margin-top:4px}
.gaps h3{margin:14px 0 10px!important}
.saved{font-size:12.5px;font-weight:600;color:var(--good)}
.savebar{position:sticky;bottom:0;display:flex;align-items:center;gap:10px;padding:12px 0 calc(12px + env(safe-area-inset-bottom,0px));background:linear-gradient(to top,var(--bg) 75%,transparent);margin-top:10px}
#memtext{min-height:50vh;font:14px/1.55 ui-monospace,"SF Mono",Menlo,Consolas,monospace}

/* ---------- phone ---------- */
@media (max-width: 860px){
  #app{grid-template-columns:1fr;grid-template-rows:auto minmax(0,1fr) auto}
  #rail{display:none}
  #topbar{display:flex;align-items:center;gap:10px;padding:calc(10px + env(safe-area-inset-top,0px)) 16px 8px;border-bottom:1px solid var(--line);background:var(--raise);backdrop-filter:blur(18px)}
  #topbar .brand{padding:0;font-size:16px}
  #topbar .status{margin-left:auto;max-width:40vw}
  #alerts2.on{color:var(--accent);border-color:var(--accent)}
  #tabbar{display:flex;border-top:1px solid var(--line);background:var(--raise);backdrop-filter:blur(18px);padding:6px 4px calc(6px + env(safe-area-inset-bottom,0px))}
  #tabbar .nav{flex-direction:column;gap:3px;padding:6px 2px;font-size:11px;justify-content:center;border-radius:12px}
  #tabbar .nav.on{background:none;color:var(--accent)}
  #tabbar .badge{position:absolute;top:0;left:calc(50% + 6px);margin:0;height:17px;min-width:17px;font-size:10.5px;padding:0 5px}
  .vhead{padding:14px 16px 10px}
  .vhead h1{font-size:20px}
  .vbody{padding:4px 16px 20px}
  .chatside{display:none}
  #chatpick{display:block;flex:1;min-width:0;max-width:none}
  #chattitle{display:none}
  #msgs{padding:8px 14px 12px}
  .msg{max-width:92%}
  .composer{padding:8px 10px 10px}
  .chint{display:none}
  .jbar,#jmeta{padding-left:16px;padding-right:16px}
  #v-jobs .vhead{flex-wrap:nowrap}
  #japply .long{display:none}
  .stats{gap:8px;display:grid;grid-template-columns:repeat(3,minmax(0,1fr))}
  .stat{min-width:0;padding:8px 10px}
  .stat span{display:block;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
  .stat b{font-size:19px}
  .jsplit{grid-template-columns:1fr}
  #jlist{border-right:0;padding:10px}
  #jdetail{position:fixed;inset:0;z-index:40;background:var(--bg);transform:translateX(100%);transition:transform .22s ease;padding-top:env(safe-area-inset-top,0px)}
  #v-jobs.detail #jdetail{transform:none}
  #alist{border-right:0;padding:10px}
  #adetail{position:fixed;inset:0;z-index:40;background:var(--bg);transform:translateX(100%);transition:transform .22s ease;padding-top:env(safe-area-inset-top,0px)}
  #v-apps.detail #adetail{transform:none}
  #tabbar .nav span:not(.badge){font-size:10.5px}
  .jback{display:inline-flex}
  .jd{padding:12px 16px 0}
  .stickyfoot{padding:10px 16px calc(10px + env(safe-area-inset-bottom,0px))}
  .pgrid{grid-template-columns:1fr}
  #banner{top:calc(8px + env(safe-area-inset-top,0px))}
  #toast{bottom:calc(84px + env(safe-area-inset-bottom,0px))}
}
</style></head><body>
<div id="app">
  <aside id="rail">
    <div class="brand"><div class="logo"><svg viewBox="0 0 24 24"><path d="M12 3l2.4 5.6L20 11l-5.6 2.4L12 19l-2.4-5.6L4 11l5.6-2.4z"/></svg></div>Agent</div>
    <div id="navs"></div>
    <div class="rail-foot">
      <button class="btn ghost sm" id="alerts" style="display:none"></button>
      <button class="btn ghost sm" id="theme" title="Switch theme"></button>
      <div class="status"><i class="dot" id="dot"></i><span id="status">connecting</span></div>
    </div>
  </aside>
  <header id="topbar">
    <div class="brand"><div class="logo"><svg viewBox="0 0 24 24"><path d="M12 3l2.4 5.6L20 11l-5.6 2.4L12 19l-2.4-5.6L4 11l5.6-2.4z"/></svg></div>Agent</div>
    <div class="status"><i class="dot" id="dot2"></i><span id="status2">connecting</span></div>
    <button class="btn ghost icon" id="alerts2" style="display:none" aria-label="Alerts" title="Alerts"></button>
  </header>
  <main>
    <!-- today: what needs Sai now, the panel's home -->
    <section id="v-today" class="view">
      <div class="vhead"><div><h1 id="tgreet">Today</h1><div class="sub" id="tdate"></div></div><span class="spacer"></span>
        <div class="tstatus" id="tstatus"></div>
      </div>
      <div class="vbody"><div class="twrap" id="tbody"></div></div>
    </section>

    <!-- chat -->
    <section id="v-chat" class="view">
      <div class="chatside">
        <div class="vhead"><h1>Chats</h1><span class="spacer"></span><button class="btn icon ghost" id="newchat" title="New chat" aria-label="New chat"><svg viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></svg></button></div>
        <div id="chatlist"></div>
      </div>
      <div class="chatmain">
        <div class="vhead">
          <div class="seg asub"><button data-v="chat">Chat</button><button data-v="tasks">Tasks</button></div>
          <select class="inp" id="chatpick" aria-label="Conversation" style="width:auto;height:36px;padding:0 30px 0 10px;font-size:14px"></select>
          <h1 id="chattitle" style="font-size:17px;font-weight:650;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;max-width:50%"></h1>
          <span class="spacer"></span>
          <div class="seg" id="chatmodel"><button data-m="better">8B · smart</button><button data-m="faster">4B · fast</button></div>
          <button class="btn icon ghost" id="chatdel" title="Delete chat" aria-label="Delete chat"><svg viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></svg></button>
        </div>
        <div id="msgs"><div class="msgwrap" id="msgwrap"></div></div>
        <div class="composer">
          <div class="cbox">
            <div class="cfiles" id="c-files"></div>
            <div class="crow">
              <button class="cbtn" id="c-attach" title="Attach files" aria-label="Attach files"><svg viewBox="0 0 24 24"><path d="M21 11.5l-8.6 8.6a5 5 0 01-7-7l8.6-8.6a3.5 3.5 0 015 5l-8.6 8.6a2 2 0 01-2.8-2.8l7.9-7.9"/></svg></button>
              <textarea id="c-input" rows="1" placeholder="Message the agent" enterkeyhint="send"></textarea>
              <button class="cbtn send" id="c-send" title="Send" aria-label="Send"><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>
            </div>
          </div>
          <div class="chint">Runs on your server, offline. Say "remember that ..." to save a fact. Shift+Enter for a new line.</div>
        </div>
      </div>
    </section>

    <!-- jobs -->
    <section id="v-jobs" class="view">
      <div class="vhead">
        <h1>Jobs</h1><span class="spacer"></span>
        <a class="btn primary" id="japply" target="_blank" rel="noopener noreferrer" style="display:none"><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg><span class="long"></span><span class="short"></span></a>
        <button class="btn ghost icon" id="jrun" title="Run the search now" aria-label="Run the search now"><svg viewBox="0 0 24 24"><path d="M20 12a8 8 0 11-2.3-5.7M20 4v5h-5"/></svg></button>
        <a class="btn ghost icon" id="jsetup" href="jobs-fill.user.js" title="Install the autofill script" aria-label="Install the autofill script"><svg viewBox="0 0 24 24"><path d="M12 4v11M7 10l5 5 5-5M5 20h14"/></svg></a>
      </div>
      <div class="jbar"><div class="stats" id="jstats"></div></div>
      <div class="jbar"><div class="scrollx"><div class="seg" id="jfilter"></div></div></div>
      <div id="jmeta"></div>
      <div class="jsplit">
        <div id="jlist"></div>
        <div id="jdetail"></div>
      </div>
    </section>

    <!-- applications -->
    <section id="v-apps" class="view">
      <div class="vhead"><h1>Applications</h1>
        <div class="seg amode"><button data-mode="list">Sent</button><button data-mode="inbox">Inbox<span class="n" data-n="inbox"></span></button><button data-mode="report">Weekly report</button></div>
        <span class="spacer"></span>
        <input class="inp alistpart" id="asearch" placeholder="Search company or role" style="max-width:260px;height:36px;padding:6px 12px;font-size:14px">
        <select class="inp reportpart" id="rweek" aria-label="Week" style="max-width:220px;height:36px;padding:0 10px;font-size:14px;line-height:34px"></select>
      </div>
      <div class="jbar alistpart"><div class="stats" id="astats"></div></div>
      <div class="jbar alistpart" id="afunnel"></div>
      <div class="jbar alistpart"><div class="scrollx"><div class="seg" id="afilter"></div></div></div>
      <div id="report" class="reportpart"></div>
      <div class="jsplit alistpart">
        <div id="alist"></div>
        <div id="adetail"></div>
      </div>
    </section>

    <!-- inbox -->
    <section id="v-inbox" class="view">
      <div class="vhead"><h1>Applications</h1>
        <div class="seg amode"><button data-mode="list">Sent</button><button data-mode="inbox">Inbox<span class="n" data-n="inbox"></span></button><button data-mode="report">Weekly report</button></div>
        <span class="spacer"></span>
        <button class="btn ghost sm" id="icheck" style="display:none">Check now</button>
        <button class="btn danger sm" id="ioff" style="display:none">Disconnect</button>
        <div class="sub" id="imeta"></div>
      </div>
      <div class="jbar" id="ifilterbar" style="display:none"><div class="scrollx"><div class="seg" id="ifilter"></div></div></div>
      <div class="vbody"><div class="youwrap" id="ibody"></div></div>
    </section>

    <!-- tasks -->
    <section id="v-tasks" class="view">
      <div class="vhead"><h1>Assistant</h1><div class="seg asub"><button data-v="chat">Chat</button><button data-v="tasks">Tasks</button></div><span class="spacer"></span>
        <button class="btn danger sm" id="stop">Stop</button>
        <button class="btn ghost sm" id="clear">Clear</button>
        <div class="sub">The agent runs commands in its sandbox. Anything with side effects waits for your approval.</div>
      </div>
      <div class="vbody">
        <div class="card" id="pending">
          <div style="display:flex;align-items:center;gap:8px;font-weight:700"><svg viewBox="0 0 24 24" style="color:var(--warn)"><path d="M12 9v4M12 17h.01M10.3 3.9L2 18a2 2 0 001.7 3h16.6a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg>Approve this action?</div>
          <div id="pbody"></div>
          <input class="inp" id="reason" placeholder="Reason if denying (optional)" style="margin-top:10px">
          <div style="display:flex;gap:8px;margin-top:10px"><button class="btn danger" id="deny" style="flex:1">Deny</button><button class="btn good" id="approve" style="flex:1">Approve</button></div>
        </div>
        <div id="events"></div>
      </div>
      <div class="composer">
        <div class="cbox">
          <div class="cfiles" id="t-files"></div>
          <div class="crow">
            <button class="cbtn" id="t-attach" title="Attach files" aria-label="Attach files"><svg viewBox="0 0 24 24"><path d="M21 11.5l-8.6 8.6a5 5 0 01-7-7l8.6-8.6a3.5 3.5 0 015 5l-8.6 8.6a2 2 0 01-2.8-2.8l7.9-7.9"/></svg></button>
            <textarea id="t-input" rows="1" placeholder="Give the agent a task" enterkeyhint="send"></textarea>
            <button class="cbtn send" id="t-send" title="Run" aria-label="Run"><svg viewBox="0 0 24 24"><path d="M5 12h14M13 6l6 6-6 6"/></svg></button>
          </div>
        </div>
      </div>
    </section>

    <!-- you -->
    <section id="v-you" class="view">
      <div class="vhead"><h1>Settings</h1><span class="spacer"></span>
        <div class="scrollx"><div class="seg" id="yousub"><button data-s="profile">Profile</button><button data-s="rules">Rules</button><button data-s="workday">Workday</button><button data-s="memory">Memory</button><button data-s="app">App</button></div></div>
      </div>
      <div class="vbody">
        <div class="youwrap" id="y-profile">
          <div class="card" style="margin-bottom:6px">
            <div style="display:flex;align-items:baseline;gap:8px"><b>Profile</b><span class="muted small" id="pstat"></span><span class="spacer"></span><span class="saved" id="pdirty"></span></div>
            <div class="progress"><i id="pbar" style="width:0"></i></div>
            <div class="muted small">Everything application forms ask for, built from the questions on real forms. The autofill and the prepared answers use it. Consents are never answered from here.</div>
            <div class="scrollx" style="margin-top:12px"><div class="chips" id="pjump" style="flex-wrap:nowrap"></div></div>
          </div>
          <div id="pform"></div>
          <div class="muted small" style="margin:16px 0 30px">Changes save as you go.</div>
        </div>
        <div class="youwrap" id="y-rules" style="display:none">
          <div class="card" style="margin-bottom:8px"><b>Hiring rules</b>
            <div class="muted small" style="margin-top:4px">Employers reject or hold applications that break their rules: duplicates, too many at one company, reapplying too soon, graduation windows, AI-use policies. The agent checks every application against these before Review, at Approve and again right before submitting. <b>Block</b> keeps it out of Review and Apply, <b>Ask</b> makes you confirm with Approve anyway, <b>Warn</b> shows a note.</div></div>
          <div id="rlist"></div>
          <h3 style="font-size:16px;margin:24px 0 6px">Company caps</h3>
          <div class="muted small" style="margin-bottom:10px">Most companies: at most this many applications in this many days. Add companies with their own caps.</div>
          <div class="kv"><span class="small"><b>Default</b></span><input class="inp" id="rmax" type="number" min="1" aria-label="Most applications"><input class="inp" id="rdays" type="number" min="1" aria-label="Days"><span></span></div>
          <div id="rlimits"></div>
          <button class="btn ghost sm" id="raddlimit">Add a company</button>
          <h3 style="font-size:16px;margin:24px 0 6px">Cooldown after a rejection</h3>
          <div class="kv two"><span class="small">Days to wait after a rejection that followed interviews</span><input class="inp" id="rcool" type="number" min="1" aria-label="Cooldown days"><span></span></div>
          <h3 style="font-size:16px;margin:24px 0 6px">Referrals and agencies</h3>
          <div class="muted small" style="margin-bottom:10px">Companies where someone referred you or a recruiter submitted you. The agent won't apply there directly.</div>
          <div id="rnotes"></div>
          <button class="btn ghost sm" id="raddnote">Add a company</button>
          <div class="savebar"><span class="muted small" id="rdirty" style="flex:1"></span><button class="btn primary" id="rsave">Save rules</button></div>
        </div>
        <div class="youwrap" id="y-workday" style="display:none">
          <div class="card" style="margin-bottom:12px"><b>Workday accounts</b>
            <div class="muted small" style="margin-top:4px">Every company on Workday needs its own account. The autofill creates one the first time it applies there, and signs in after that, with your applications email and this password. Workday usually emails a link to verify a new account; it shows up in Inbox. The password is kept on your server and only given to the autofill on Workday pages.</div>
            <div class="chips" id="wdstate" style="margin-top:10px"></div>
          </div>
          <div class="field"><label>Password for Workday accounts</label>
            <div style="display:flex;gap:8px"><input class="inp" id="wdpw" type="password" autocomplete="new-password" placeholder="At least 12 characters: upper, lower, number, symbol"><button class="btn ghost" id="wdgen" style="flex:none">Generate</button></div>
            <div class="help" id="wdgenhelp">Generate makes a strong one and shows it once so you can save it in your password manager.</div></div>
          <label class="consent card" style="margin-top:6px"><input type="checkbox" id="wdterms"><div><div class="ql">Agree to Workday account terms for me</div><div class="qk">Lets the autofill tick the terms box when it creates an account, and Workday's standard "terms and conditions" box on the voluntary disclosures page. Any other agreement still needs your own tick in Review.</div></div></label>
          <div class="savebar"><span class="muted small" id="wddirty" style="flex:1"></span><button class="btn primary" id="wdsave">Save</button></div>
        </div>
        <div class="youwrap" id="y-app" style="display:none">
          <div class="card" style="margin-bottom:12px"><b>Appearance</b>
            <div class="muted small" style="margin:4px 0 10px">Follows your device unless you pick one.</div>
            <div class="seg" id="themepick"><button data-t="">System</button><button data-t="light">Light</button><button data-t="dark">Dark</button></div></div>
          <div class="card" style="margin-bottom:12px"><b>Alerts</b>
            <div class="muted small" id="appalerts" style="margin:4px 0 10px"></div>
            <button class="btn primary sm" id="appalertson" style="display:none">Turn on alerts</button></div>
          <div class="card"><b>Autofill script</b>
            <div class="muted small" style="margin:4px 0 10px">Fills application forms in your browser and submits the ones you approved. Install it in Tampermonkey or Violentmonkey; it updates itself after that.</div>
            <a class="btn ghost sm" href="jobs-fill.user.js">Install or update</a></div>
        </div>
        <div class="youwrap" id="y-memory" style="display:none">
          <div class="muted small" style="margin-bottom:10px">Facts the assistant knows about you, one per line. They go at the start of every chat and task, so keep them short. In a chat, "remember that ..." adds one.</div>
          <textarea class="inp" id="memtext" spellcheck="false"></textarea>
          <div class="savebar"><span class="muted small" id="memcount" style="flex:1"></span><button class="btn primary" id="memsave">Save memory</button></div>
        </div>
      </div>
    </section>
  </main>
  <nav id="tabbar"></nav>
</div>
<input type="file" id="filepick" multiple hidden>
<div id="banner" role="button" tabindex="0"><svg viewBox="0 0 24 24"><path d="M12 9v4M12 17h.01M10.3 3.9L2 18a2 2 0 001.7 3h16.6a2 2 0 001.7-3L13.7 3.9a2 2 0 00-3.4 0z"/></svg><span>The agent is waiting for your approval</span></div>
<div id="toast"></div>
<script>
// Everything on this page is built with DOM calls and textContent, never innerHTML:
// model output and email text are untrusted.
const $ = id => document.getElementById(id);
function el(tag, cls, text){ const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined && text !== null) e.textContent = text; return e; }
function pre(text){ return el("pre", null, text); }
const SVGNS = "http://www.w3.org/2000/svg";
const ICONS = {
  chat: "M21 12a8 8 0 01-11.6 7.1L4 20l1-4.6A8 8 0 1121 12z",
  jobs: "M4 8h16v11H4zM9 8V5h6v3M4 13h16",
  inbox: "M4 13l2.5-8h11L20 13v6H4zM4 13h5l1 2h4l1-2h5",
  tasks: "M5 7l2 2 4-4M5 17l2 2 4-4M14 7h6M14 17h6",
  you: "M12 12a4 4 0 100-8 4 4 0 000 8zM4 21a8 8 0 0116 0",
  apps: "M9 4h6v3H9zM7 5H5v16h14V5h-2M9 12l2 2 4-4M9 17h6",
  back: "M15 18l-6-6 6-6", send: "M5 12h14M13 6l6 6-6 6", ext: "M14 4h6v6M20 4l-9 9M18 14v5H5V6h5", copy: "M8 8h11v11H8zM5 16V5h11",
  check: "M5 12l5 5 9-10", x: "M6 6l12 12M18 6L6 18", mail: "M3 6h18v12H3zM3 7l9 6 9-6",
  sparkle: "M12 3l2.4 5.6L20 11l-5.6 2.4L12 19l-2.4-5.6L4 11l5.6-2.4z", sun: "M12 17a5 5 0 100-10 5 5 0 000 10zM12 1v2M12 21v2M4.2 4.2l1.4 1.4M18.4 18.4l1.4 1.4M1 12h2M21 12h2M4.2 19.8l1.4-1.4M18.4 5.6l1.4-1.4",
  moon: "M21 12.8A9 9 0 1111.2 3a7 7 0 009.8 9.8z", doc: "M7 3h7l5 5v13H7zM14 3v5h5M10 13h6M10 17h6", stop: "M7 7h10v10H7z",
  refresh: "M20 12a8 8 0 11-2.3-5.7M20 4v5h-5", bell: "M6 8a6 6 0 1112 0c0 7 3 9 3 9H3s3-2 3-9M10 21h4",
  today: "M4 6h16v14H4zM4 10h16M8 3v4M16 3v4M8 14h3v3H8z", assist: "M12 3l2.4 5.6L20 11l-5.6 2.4L12 19l-2.4-5.6L4 11l5.6-2.4z",
  settings: "M12 15a3 3 0 100-6 3 3 0 000 6zM19 12a7 7 0 00-.1-1.2l2-1.6-2-3.4-2.4 1a7 7 0 00-2-1.2L14 3h-4l-.5 2.6a7 7 0 00-2 1.2l-2.4-1-2 3.4 2 1.6a7 7 0 000 2.4l-2 1.6 2 3.4 2.4-1a7 7 0 002 1.2L10 21h4l.5-2.6a7 7 0 002-1.2l2.4 1 2-3.4-2-1.6c.1-.4.1-.8.1-1.2z",
};
function icon(name){ const s = document.createElementNS(SVGNS, "svg"); s.setAttribute("viewBox", "0 0 24 24"); const p = document.createElementNS(SVGNS, "path"); p.setAttribute("d", ICONS[name]); s.append(p); return s; }
let toastTimer;
function toast(msg, bad){ const t = $("toast"); t.textContent = msg; t.className = "on" + (bad ? " bad" : ""); clearTimeout(toastTimer); toastTimer = setTimeout(() => { t.className = ""; }, bad ? 5000 : 2600); }
// The panel's main lists are kept in this browser, so on opening it shows the last known
// state at once and swaps in the live one a moment later
const CACHED = ["api/jobs", "api/inbox", "api/applications", "api/report"];
const warmed = new Set(), RELOAD = {};
async function api(path, opts){
  const cacheable = !opts && CACHED.includes(path);
  if (cacheable && !warmed.has(path)) {
    warmed.add(path);
    let hit = null;
    try { hit = JSON.parse(localStorage.getItem("cache:" + path) || "null"); } catch (e) {}
    if (hit) { setTimeout(() => RELOAD[path] && RELOAD[path](), 0); return hit; }
  }
  const r = await fetch(path, opts);
  if (!r.ok) { const t = await r.json().catch(() => ({})); throw new Error(t.detail || ("Error " + r.status)); }
  const data = await r.json().catch(() => ({}));
  if (cacheable) {
    const tag = r.headers.get("etag") || "";
    if (!tag || tag !== store("etag:" + path)) { try { localStorage.setItem("cache:" + path, JSON.stringify(data)); store("etag:" + path, tag); } catch (e) {} }
  }
  return data;
}
const send = (path, method, body) => api(path, {method, headers: {"Content-Type": "application/json"}, body: JSON.stringify(body || {})});
function when(iso){ return iso ? new Date(iso).toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"}) : ""; }
const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
function store(k, v){ try { if (v === undefined) return localStorage.getItem(k); localStorage.setItem(k, v); } catch (e) { return null; } }

// ---------- navigation ----------
// Five places in the navigation; Applications and Assistant each hold two screens
const PLACES = [["today", "Today", "today"], ["jobs", "Jobs", "jobs"], ["apps", "Applications", "apps", "Applied"], ["assist", "Assistant", "assist"], ["you", "Settings", "settings"]];
const SCREENS = ["today", "jobs", "apps", "inbox", "chat", "tasks", "you"];
const PLACE_OF = {today: "today", jobs: "jobs", apps: "apps", inbox: "apps", chat: "assist", tasks: "assist", you: "you"};
const OLD = {log: "tasks", memory: "you", profile: "you", settings: "you"};
let view = "";
for (const holder of [$("navs"), $("tabbar")]) {
  for (const [p, label, ic, short] of PLACES) {
    const b = el("button", "nav");
    b.dataset.view = p;
    b.append(icon(ic), el("span", null, holder.id === "tabbar" && short ? short : label), el("span", "badge"));
    b.onclick = () => showView(p === "apps" ? (store("screen:apps") || "apps") : p === "assist" ? (store("screen:assist") || "chat") : p);
    holder.append(b);
  }
}
function badge(v, n, warn){
  for (const b of document.querySelectorAll('.nav[data-view="' + (PLACE_OF[v] || v) + '"] .badge, .seg [data-n="' + v + '"]')) {
    b.textContent = n ? (n > 99 ? "99+" : String(n)) : "";
    b.classList.toggle("on", n > 0);
    b.classList.toggle("warn", !!warn);
  }
}
function showView(v){
  v = OLD[v] || v;
  if (v === "assist") v = store("screen:assist") || "chat";
  if (v === "report") { store("amode", "report"); v = "apps"; }
  if (!SCREENS.includes(v)) v = "today";
  view = v;
  const place = PLACE_OF[v];
  if (place === "apps" || place === "assist") store("screen:" + place, v);
  for (const name of SCREENS) $("v-" + name).classList.toggle("on", name === v);
  for (const b of document.querySelectorAll(".nav")) b.classList.toggle("on", b.dataset.view === place);
  for (const b of document.querySelectorAll(".asub button")) b.classList.toggle("on", b.dataset.v === v);
  for (const b of document.querySelectorAll(".amode button")) b.classList.toggle("on", v === "inbox" ? b.dataset.mode === "inbox" : b.dataset.mode === (store("amode") || "list"));
  history.replaceState(null, "", "#" + v);
  store("view", v);
  if (v === "today") { loadJobs(); loadInbox(); loadTodayReport(); renderToday(); }
  if (v === "jobs") loadJobs(true);
  if (v === "inbox") { inboxSig = ""; loadInbox(); }
  if (v === "apps") setAppsMode(store("amode") === "report" ? "report" : "list");
  if (v === "you") showYou(store("yousub") || "profile");
  if (v === "chat" && !chatLoaded) { chatLoaded = true; loadChatList(); openChat(chatId); }
  if (v === "tasks") { const b = $("v-tasks").querySelector(".vbody"); b.scrollTop = b.scrollHeight; }
}

// ---------- theme and alerts ----------
function applyTheme(t){
  if (t) document.documentElement.dataset.theme = t; else delete document.documentElement.dataset.theme;
  const dark = t ? t === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  const b = $("theme"); b.replaceChildren(icon(dark ? "sun" : "moon"), el("span", null, dark ? "Light mode" : "Dark mode"));
}
$("theme").onclick = () => {
  const dark = document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  const t = dark ? "light" : "dark"; store("theme", t); applyTheme(t);
};
applyTheme(store("theme"));
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => applyTheme(store("theme")));
function b64ToBytes(s){
  const pad = "=".repeat((4 - s.length % 4) % 4);
  const raw = atob((s + pad).replace(/-/g, "+").replace(/_/g, "/"));
  return Uint8Array.from(raw, c => c.charCodeAt(0));
}
// Alerts: the sidebar button on a laptop, the bell in the top bar on a phone, and a
// prompt in Inbox while they're off. iPhones only allow them in the home-screen app.
let alertsOn = false, alertsState = "checking";
const standalone = () => matchMedia("(display-mode: standalone)").matches || navigator.standalone === true;
function alertsHint(){
  if (alertsState === "on" || alertsState === "checking") return "";
  if (/iPhone|iPad/.test(navigator.userAgent) && !standalone()) return "On iPhone, alerts work only in the home-screen app: tap Share, then Add to Home Screen, open Agent from there and turn alerts on.";
  if (alertsState === "unsupported") return "This browser can't show alerts from the panel.";
  if (alertsState === "denied") return "Alerts are blocked for this site. Allow notifications for it in the browser or phone settings, then reload.";
  return "Turn on alerts to hear about assessments, interviews and offers the moment they arrive.";
}
function paintApp(){
  const t = store("theme") || "";
  for (const b of $("themepick").children) { b.classList.toggle("on", b.dataset.t === t); b.onclick = () => { store("theme", b.dataset.t); applyTheme(b.dataset.t); paintApp(); }; }
  $("appalerts").textContent = alertsOn ? "On. Assessments, interviews, codes and the morning list come to this device." : (alertsHint() || "Off on this device.");
  $("appalertson").style.display = alertsOn ? "none" : "";
  $("appalertson").onclick = turnOnAlerts;
}
function paintAlerts(){
  if (view === "you") paintApp();
  const b = $("alerts"), b2 = $("alerts2");
  b.replaceChildren(icon("bell"), el("span", null, alertsOn ? "Alerts on" : "Turn on alerts"));
  b2.replaceChildren(icon("bell")); b2.classList.toggle("on", alertsOn);
  b2.title = alertsOn ? "Alerts are on" : "Turn on alerts";
  b.style.display = b2.style.display = "";
  if (view === "inbox") { inboxSig = ""; loadInbox(); }
}
async function turnOnAlerts(){
  if (alertsState === "unsupported" || (alertsState !== "on" && /iPhone|iPad/.test(navigator.userAgent) && !standalone())) return toast(alertsHint(), true);
  if (alertsOn) return send("api/push/test", "POST").then(() => toast("Alerts are on. A test alert is on its way."), e => toast(e.message, true));
  $("alerts").click();
}
$("alerts2").onclick = turnOnAlerts;
async function setupAlerts(){
  if (!("serviceWorker" in navigator) || !("PushManager" in window) || !("Notification" in window)) {
    alertsState = "unsupported"; paintAlerts(); return;
  }
  const reg = await navigator.serviceWorker.register("sw.js");
  const btn = $("alerts");
  const existing = await reg.pushManager.getSubscription();
  alertsOn = !!existing && Notification.permission === "granted";
  alertsState = alertsOn ? "on" : Notification.permission === "denied" ? "denied" : "off";
  const label = on => { alertsOn = on; if (on) alertsState = "on"; paintAlerts(); };
  label(alertsOn);
  if (existing) send("api/push/subscribe", "POST", existing.toJSON()).catch(() => {});
  btn.onclick = async () => {
    try {
      const perm = await Notification.requestPermission();
      if (perm !== "granted") { alertsState = "denied"; paintAlerts(); return toast(alertsHint(), true); }
      const {key} = await api("api/push/key");
      const sub = (await reg.pushManager.getSubscription()) ||
        await reg.pushManager.subscribe({userVisibleOnly: true, applicationServerKey: b64ToBytes(key)});
      await send("api/push/subscribe", "POST", sub.toJSON());
      await send("api/push/test", "POST");
      label(true); toast("Alerts are on");
    } catch (e) { toast("Could not turn on alerts: " + e.message, true); }
  };
}

// ---------- tasks (the agent's log) ----------
let last = 0, pendingId = null;
function render(ev){
  const box = el("div", "ev");
  if (ev.kind === "task") { box.classList.add("task"); box.append(el("div", "tt", ev.text)); }
  else if (ev.kind === "thought") box.append(el("div", "thought", "Step " + ev.step + " · " + ev.stats + " — " + ev.text));
  else if (ev.kind === "action") {
    box.append(el("div", "tool", ev.tool + (ev.auto ? " · auto-approved, read-only" : "")), pre(ev.arg));
    if (ev.content) box.append(pre(ev.content));
  }
  else if (ev.kind === "result") { const d = el("details"); d.append(el("summary", null, "Result"), pre(ev.text)); box.append(d); }
  else if (ev.kind === "rejected") { box.classList.add("err"); box.append(el("div", "bad", "Denied" + (ev.text ? ": " + ev.text : ""))); }
  else if (ev.kind === "answer") { box.classList.add("answer"); box.append(el("div", "ans", ev.text)); }
  else { box.classList.add("err"); box.append(el("div", "bad", ev.text)); }
  $("events").append(box);
}
function setStatus(text, cls){
  for (const [d, s] of [["dot", "status"], ["dot2", "status2"]]) { $(s).textContent = text; $(d).className = "dot " + cls; }
}
async function poll(){
  try {
    const r = await fetch("api/state?since=" + last);
    if (!r.ok) return setStatus("error " + r.status, "off");
    const s = await r.json();
    if (s.seq < last) { last = 0; $("events").replaceChildren(); return poll(); }
    const body = $("v-tasks").querySelector(".vbody");
    const nearBottom = body.scrollHeight - body.scrollTop - body.clientHeight < 80;
    if (!last && !s.events.length && !$("events").childNodes.length) {
      const e = el("div", "empty"); e.append(icon("tasks"), el("b", null, "No tasks yet"), el("div", null, "Ask the agent to do something on the server: check disk space, organize files, summarize a log."));
      $("events").append(e);
    }
    if (s.events.length) { const e = $("events").querySelector(".empty"); if (e) e.remove(); }
    for (const e of s.events) { render(e); last = Math.max(last, e.n); }
    const p = s.pending;
    setStatus(p ? "waiting for approval" : s.status + (s.task ? " · " + s.task : ""), p ? "wait" : s.status === "idle" ? "idle" : "busy");
    if (p && p.id !== pendingId) {
      pendingId = p.id;
      const b = $("pbody");
      b.replaceChildren(el("div", "tool small", p.tool), pre(p.arg));
      if (p.content) b.append(pre(p.content));
      $("reason").value = "";
      $("pending").classList.add("on");
    }
    if (!p) { pendingId = null; $("pending").classList.remove("on"); }
    $("banner").classList.toggle("on", !!p && view !== "tasks");
    badge("tasks", p ? 1 : 0, true);
    if (s.events.length && nearBottom) body.scrollTop = body.scrollHeight;
  } catch (e) { setStatus("offline", "off"); }
}
$("banner").onclick = () => showView("tasks");
$("approve").onclick = () => { if (pendingId) { const id = pendingId; pendingId = "sent"; send("api/decision", "POST", {id, approve: true}).catch(e => toast(e.message, true)).then(poll); } };
$("deny").onclick = () => { if (pendingId) { const id = pendingId; pendingId = "sent"; send("api/decision", "POST", {id, approve: false, reason: $("reason").value}).catch(e => toast(e.message, true)).then(poll); } };
$("stop").onclick = () => send("api/stop", "POST").catch(e => toast(e.message, true)).then(poll);
$("clear").onclick = () => send("api/clear", "POST").then(() => { last = 0; $("events").replaceChildren(); poll(); }, e => toast(e.message, true));

// ---------- composers and attachments ----------
// Files are turned into text on the server first, so the page can show how long the
// model will take to read them. Chat puts the text in the message (first 16,000
// characters); Tasks saves each file into the agent's workspace.
const READ_RATE = {better: 17, faster: 35};  // tokens per second, measured on the server
const composers = {c: {files: []}, t: {files: []}};
let pickFor = "c";
function readTime(chars){
  const s = Math.round(chars / 4 / READ_RATE[chatModel]);
  return s < 60 ? "~" + Math.max(s, 1) + " s to read" : "~" + Math.round(s / 60) + " min to read";
}
function renderFiles(k){
  const box = $(k + "-files"), list = composers[k].files;
  box.replaceChildren();
  let left = 16000;
  for (const f of list) {
    let label = f.name;
    if (f.reading) label += " · reading...";
    else if (k === "c") {
      const used = Math.min(f.chars, Math.max(left, 0));
      left -= used;
      label += " · " + f.chars.toLocaleString() + " chars" + (used < f.chars ? ", first " + used.toLocaleString() + " used" : "") + " · " + readTime(used);
    } else label += " · " + f.chars.toLocaleString() + " chars, saved to the workspace";
    const chip = el("div", "fchip"); chip.title = label;
    const x = el("button", null, "×"); x.setAttribute("aria-label", "Remove " + f.name);
    x.onclick = () => { composers[k].files = list.filter(a => a !== f); renderFiles(k); };
    chip.append(el("span", null, label), x);
    box.append(chip);
  }
  box.classList.toggle("on", list.length > 0);
}
async function toBase64(file){
  const bytes = new Uint8Array(await file.arrayBuffer());
  let s = "";
  for (let i = 0; i < bytes.length; i += 0x8000) s += String.fromCharCode.apply(null, bytes.subarray(i, i + 0x8000));
  return btoa(s);
}
async function addFile(k, file){
  if (file.size > 5e6) return toast(file.name + " is over 5 MB.", true);
  const f = {name: file.name, reading: true, chars: 0, text: ""};
  composers[k].files.push(f);
  renderFiles(k);
  try {
    const d = await send("api/extract", "POST", {name: file.name, data: await toBase64(file)});
    Object.assign(f, {name: d.name, text: d.text, chars: d.chars, reading: false});
  } catch (e) {
    composers[k].files = composers[k].files.filter(a => a !== f);
    toast(file.name + ": " + e.message, true);
  }
  renderFiles(k);
}
$("filepick").onchange = async () => {
  const picked = [...$("filepick").files];
  $("filepick").value = "";
  for (const file of picked) await addFile(pickFor, file);
};
function grow(t){ t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 200) + "px"; }
for (const k of ["c", "t"]) {
  const input = $(k + "-input");
  $(k + "-attach").onclick = () => { pickFor = k; $("filepick").click(); };
  input.addEventListener("input", () => grow(input));
  input.addEventListener("keydown", e => { if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); $(k + "-send").click(); } });
  $(k + "-send").onclick = () => {
    if (k === "c" && chatAbort) { chatAbort.abort(); return; }
    const text = input.value.trim(), list = composers[k].files;
    if (!text && !list.length) return;
    if (list.some(f => f.reading)) return toast("Still reading a file.");
    const files = list.map(f => ({name: f.name, text: f.text}));
    input.value = ""; grow(input);
    composers[k].files = []; renderFiles(k);
    if (k === "c") sendChat(text, files);
    else send("api/task", "POST", {task: text, files}).then(poll, e => toast(e.message, true));
  };
}

// ---------- chat ----------
let chatId = store("chat") || "", chatAbort = null, chatLoaded = false, chatModel = store("chatmodel") || "better", chats = [];
function setModel(m){ chatModel = m; store("chatmodel", m); for (const b of $("chatmodel").children) b.classList.toggle("on", b.dataset.m === m); renderFiles("c"); }
for (const b of $("chatmodel").children) b.onclick = () => setModel(b.dataset.m);
setModel(chatModel);
const msgBox = () => $("msgs");
function nearEnd(box){ return box.scrollHeight - box.scrollTop - box.clientHeight < 120; }
// Light formatting for replies: code blocks, **bold**, `code` and # headings, built
// from text nodes and elements.
function inline(parent, line){
  for (const piece of line.split(/(\*\*[^*\n]+\*\*|`[^`\n]+`)/)) {
    if (/^\*\*[^*]+\*\*$/.test(piece)) { const b = el("strong"); inline(b, piece.slice(2, -2)); parent.append(b); }
    else if (/^`[^`]+`$/.test(piece)) parent.append(el("code", null, piece.slice(1, -1)));
    else if (piece) parent.append(document.createTextNode(piece));
  }
}
function rich(box, text){
  box.replaceChildren();
  const fence = /```[^\n]*\n?([\s\S]*?)(?:```|$)/g;
  let at = 0, m;
  const prose = t => t.split("\n").forEach((line, i) => {
    if (i) box.append(document.createTextNode("\n"));
    const h = line.match(/^#{1,6}\s+(.*)$/);
    if (h) box.append(el("strong", null, h[1])); else inline(box, line);
  });
  while ((m = fence.exec(text))) {
    prose(text.slice(at, m.index));
    box.append(pre(m[1].replace(/\n$/, "")));
    at = fence.lastIndex;
  }
  prose(text.slice(at));
}
function addMsg(role, text, files){
  const d = el("div", "msg " + role);
  if (role === "assistant") rich(d, text); else d.textContent = text;
  if (files && files.length) d.append(el("div", "files", "\u{1F4CE} " + files.map(f => f.name).join(", ")));
  $("msgwrap").append(d);
  return d;
}
function hello(){
  const h = el("div", "hello");
  const logo = el("div", "logo"); logo.append(icon("sparkle"));
  h.append(logo, el("h2", null, "What can I help with?"), el("div", null, "Replies are written on your own server, with no internet access."));
  const sg = el("div", "suggest");
  for (const s of ["Write a follow-up email after an interview", "Explain a Python error I paste", "Make a study plan for system design", "Rewrite my resume bullet to sound stronger"]) {
    const b = el("button", null, s);
    b.onclick = () => { const i = $("c-input"); i.value = s; grow(i); i.focus(); };
    sg.append(b);
  }
  h.append(sg);
  $("msgwrap").append(h);
}
function renderChatList(){
  const list = $("chatlist"), pick = $("chatpick");
  list.replaceChildren(); pick.replaceChildren();
  const o = el("option", null, "New chat"); o.value = ""; pick.append(o);
  if (!chats.length) list.append(el("div", "muted small", "No conversations yet."));
  for (const c of chats) {
    const b = el("button", "citem" + (c.id === chatId ? " on" : ""), c.title);
    b.onclick = () => { if (!chatAbort) openChat(c.id); };
    list.append(b);
    const op = el("option", null, c.title); op.value = c.id; pick.append(op);
  }
  pick.value = chatId;
}
async function loadChatList(){
  try { chats = await api("api/chats"); } catch (e) { chats = []; }
  if (chatId && !chats.some(c => c.id === chatId)) chatId = "";
  renderChatList();
}
async function openChat(id){
  chatId = id;
  store("chat", id);
  $("msgwrap").replaceChildren();
  $("chatdel").style.display = id ? "" : "none";
  const c = chats.find(x => x.id === id);
  $("chattitle").textContent = c ? c.title : "New chat";
  renderChatList();
  if (!id) return hello();
  try {
    const data = await api("api/chats/" + encodeURIComponent(id));
    for (const m of data.messages) addMsg(m.role, m.text || m.content, m.files);
  } catch (e) { return openChat(""); }
  msgBox().scrollTop = msgBox().scrollHeight;
}
async function sendChat(text, files){
  if (chatAbort) return;
  if (!chatId) $("msgwrap").replaceChildren();
  files = files || [];
  addMsg("user", text || (files.length > 1 ? "Summarize the attached files." : "Summarize the attached file."), files);
  const out = addMsg("assistant", "Thinking. After a pause or a model switch, the first reply can take a minute.");
  out.classList.add("typing");
  const box = msgBox();
  box.scrollTop = box.scrollHeight;
  chatAbort = new AbortController();
  const sendBtn = $("c-send");
  sendBtn.classList.add("stop"); sendBtn.replaceChildren(icon("x")); sendBtn.title = "Stop";
  let got = "";
  try {
    const r = await fetch("api/chat", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({message: text, chat_id: chatId, model: chatModel, files}), signal: chatAbort.signal});
    if (!r.ok) {
      const t = await r.json().catch(() => ({}));
      out.textContent = t.detail || ("Error " + r.status);
      out.classList.remove("typing"); out.classList.add("err");
      return;
    }
    const id = r.headers.get("X-Chat-Id") || "";
    const isNew = id !== chatId;
    chatId = id;
    store("chat", id);
    const reader = r.body.getReader(), dec = new TextDecoder();
    for (;;) {
      const {done, value} = await reader.read();
      if (done) break;
      const follow = nearEnd(box);
      got += dec.decode(value, {stream: true});
      rich(out, got);
      out.classList.remove("typing");
      if (follow) box.scrollTop = box.scrollHeight;
    }
    if (isNew) { await loadChatList(); const c = chats.find(x => x.id === chatId); $("chattitle").textContent = c ? c.title : ""; $("chatdel").style.display = ""; }
  } catch (e) {
    out.classList.remove("typing");
    rich(out, got + (e.name === "AbortError" ? " (stopped)" : "\n(connection lost)"));
  } finally {
    chatAbort = null;
    sendBtn.classList.remove("stop"); sendBtn.replaceChildren(icon("send")); sendBtn.title = "Send";
  }
}
$("newchat").onclick = () => { if (!chatAbort) { openChat(""); $("c-input").focus(); } };
$("chatpick").onchange = () => { if (!chatAbort) openChat($("chatpick").value); };
$("chatdel").onclick = async () => {
  if (!chatId || !confirm("Delete this conversation?")) return;
  await fetch("api/chats/" + encodeURIComponent(chatId), {method: "DELETE"});
  await loadChatList();
  openChat("");
};

// ---------- jobs ----------
const FILTERS = [["review", "Review"], ["approved", "Approved"], ["new", "New"], ["agency", "Agencies"], ["applied", "Applied"], ["interview", "Interviewing"], ["offer", "Offers"], ["rejected", "Rejected"], ["withdrew", "Withdrew"], ["skipped", "Skipped"], ["all", "All"]];
let S = null, jfilter = store("jfilter") || "review", jsel = null, listSig = "", applySig = "";
function counts(s){
  const c = {review: (s.review || []).length, all: s.jobs.length,
             agency: (s.agency_ready || []).length};
  for (const j of s.jobs) c[j.status] = (c[j.status] || 0) + 1;
  return c;
}
function jobsMeta(s){
  const p = s.progress, lr = s.last_run;
  if (p.running) return "Searching now: " + p.step + (p.total ? " (" + p.done + "/" + p.total + ")" : "");
  if (!lr) return "The search hasn't run yet.";
  const n = x => (x || 0).toLocaleString();
  let t = "Last search " + when(lr.finished || lr.started) + ": " + n(lr.boards) + " companies, " + n(lr.new) + " new postings, " + n(lr.scored) + " scored";
  if (lr.backlog) t += ", " + n(lr.backlog) + " left for the next run";
  const names = {teksystems: "TEKsystems", apex: "Apex Systems", randstad: "Randstad", insightglobal: "Insight Global"};
  const down = (lr.board_errors || []).filter(e => e.startsWith("agency/")).map(e => {
    const [who, ...why] = e.slice(7).split(": ");
    return (names[who] || who) + " (" + why.join(": ") + ")";
  });
  return t + "." + (down.length ? " Couldn't read " + down.join("; ") + "." : "");
}
function scoreRing(score, s, big){
  const r = el("div", "ring" + (score >= s.strong ? " strong" : score >= s.good ? " good" : "") + (big ? " big" : ""), String(score));
  r.style.setProperty("--p", Math.max(0, Math.min(100, score || 0)));
  return r;
}
function statusChip(st){
  const map = {approved: ["acc", "Approved"], applied: ["good", "Applied"], interview: ["good", "Interviewing"], offer: ["good", "Offer"], rejected: ["bad", "Rejected"], withdrew: ["", "Withdrew"], skipped: ["", "Skipped"]};
  const m = map[st]; return m ? el("span", "chip " + m[0], m[1]) : null;
}
function listJobs(){
  if (jfilter === "agency") return (S.agency_ready || []).map(id => S.jobs.find(j => j.id === id)).filter(Boolean);
  if (jfilter === "review") return (S.review || []).map(id => S.jobs.find(j => j.id === id)).filter(Boolean);
  return S.jobs.filter(j => jfilter === "all" || j.status === jfilter);
}
async function loadJobs(force){
  let s;
  try { s = await api("api/jobs"); } catch (e) { return; }
  S = s;
  const c = counts(s);
  badge("jobs", c.review);
  if (view === "today") renderToday();
  if (view !== "jobs") return;
  $("jmeta").textContent = jobsMeta(s);
  const jr = $("jrun");
  jr.disabled = !s.configured;
  jr.dataset.mode = s.progress.running ? "stop" : "run";
  jr.replaceChildren(icon(s.progress.running ? "stop" : "refresh"));
  jr.title = s.progress.running ? "Stop the search" : "Run the search now";
  jr.classList.toggle("danger", s.progress.running); jr.classList.toggle("ghost", !s.progress.running);
  // stats
  const st = $("jstats"); st.replaceChildren();
  for (const [k, label, hl] of [["review", "To review", true], ["approved", "Approved"], ["applied", "Applied"], ["interview", "Interviews"], ["offer", "Offers"]]) {
    const b = el("button", "stat" + (hl ? " hl" : ""));
    b.append(el("b", null, String(c[k] || 0)), el("span", null, label));
    b.onclick = () => setFilter(k);
    st.append(b);
  }
  // filters
  const f = $("jfilter"); f.replaceChildren();
  for (const [k, label] of FILTERS) {
    const b = el("button", k === jfilter ? "on" : "");
    b.append(document.createTextNode(label));
    if (c[k]) b.append(el("span", "n", String(c[k])));
    b.onclick = () => setFilter(k);
    f.append(b);
  }
  updateApply(c.approved || 0);
  const list = listJobs();
  const sig = jfilter + s.not_ready + (s.agency_not_ready || 0) + s.progress.running + JSON.stringify(list.map(j => [j.id, j.status, j.prepared, j.docs, j.score, (j.flags || []).map(f => f.rule + f.action)]));
  if (sig !== listSig || force) { listSig = sig; renderList(list, s); }
  if (!s.configured) $("jdetail").replaceChildren(emptyState("jobs", "Job search isn't set up", "Put config.json and resume.txt in the jobs folder on the server."));
  else if (!jsel && !document.querySelector("#jdetail .jd")) $("jdetail").replaceChildren(emptyState("jobs", list.length ? "Pick a job" : "Nothing here", list.length ? "Its answers, the posting and the actions open here." : (jfilter === "review" ? "New applications are prepared overnight." : "No jobs with this status.")));
}
function emptyState(ic, title, text){ const e = el("div", "empty"); e.append(icon(ic), el("b", null, title), el("div", null, text)); return e; }
function setFilter(k){ jfilter = k; store("jfilter", k); jsel = null; listSig = ""; $("jdetail").replaceChildren(); $("v-jobs").classList.remove("detail"); loadJobs(true); }
function renderList(list, s){
  const box = $("jlist"); box.replaceChildren();
  if (jfilter === "review" && s.not_ready) {
    const c = el("div", "card"); c.style.cssText = "padding:12px 14px;margin-bottom:4px";
    const t = el("div", "small");
    t.append(el("b", null, s.not_ready + (s.not_ready === 1 ? " more match is" : " more matches are") + " getting ready. "),
             document.createTextNode("They join this list once their answers, resume and cover letter are written, about 4 minutes each. The nightly run does this before your morning alert."));
    c.append(t);
    if (!s.progress.running) {
      const b = el("button", "btn ghost sm"); b.append(icon("sparkle"), el("span", null, "Get them ready now"));
      b.style.marginTop = "10px";
      b.onclick = () => send("api/jobs/ready", "POST").then(() => { toast("Getting them ready. The Stop button ends it."); listSig = ""; setTimeout(() => loadJobs(true), 600); }, e => toast(e.message, true));
      c.append(b);
    }
    box.append(c);
  }
  if (jfilter === "review" && list.length) {
    const go = el("button", "btn primary"); go.append(el("span", null, "Review one by one"), icon("send"));
    go.onclick = startReview; go.style.alignSelf = "flex-start";
    box.append(go, el("div", "muted small", "Each one has its answers, tailored resume and cover letter. Check them, fill what's missing, tick the statements you agree to, approve. Approved ones are submitted from Apply to approved."));
  }
  if (jfilter === "agency" && s.agency_not_ready) {
    const c = el("div", "card"); c.style.cssText = "padding:12px 14px;margin-bottom:4px";
    const t = el("div", "small");
    t.append(el("b", null, s.agency_not_ready + (s.agency_not_ready === 1 ? " more agency job is" : " more agency jobs are") + " getting ready. "),
             document.createTextNode("They show up here once their tailored resume and cover letter are written. The nightly run does it after the Review list."));
    c.append(t);
    if (!s.progress.running) {
      const b = el("button", "btn ghost sm"); b.append(icon("sparkle"), el("span", null, "Get them ready now"));
      b.style.marginTop = "10px";
      b.onclick = () => send("api/jobs/ready", "POST").then(() => { toast("Getting them ready, Review first. The Stop button ends it."); listSig = ""; setTimeout(() => loadJobs(true), 600); }, e => toast(e.message, true));
      c.append(b);
    }
    box.append(c);
  }
  if (jfilter === "agency" && list.length) box.append(el("div", "muted small", "Contract and contract-to-hire roles from TEKsystems, Apex Systems, Randstad and Insight Global, each with a tailored resume and cover letter. Open one, download the PDFs, apply on the agency's site with the prepared answers, then tap Mark applied."));
  if (!list.length) box.append(emptyState(jfilter === "review" ? "check" : "jobs", jfilter === "review" ? "All caught up" : "Nothing here", jfilter === "review" ? "New applications are prepared overnight." : "No jobs with this status yet."));
  for (const j of list) {
    const r = el("button", "jrow" + (j.id === jsel ? " on" : ""));
    r.dataset.id = j.id;
    const info = el("div", "jinfo");
    info.append(el("div", "jt", j.title), el("div", "jm", [j.company, j.location].filter(Boolean).join(" · ")));
    const chips = el("div", "chips");
    const sc = statusChip(j.status); if (sc && jfilter !== j.status) chips.append(sc);
    if (j.prepared && j.status === "new") chips.append(el("span", "chip acc", "Answers ready"));
    if (j.docs && j.status === "new") chips.append(el("span", "chip acc", "Resume + letter"));
    const f0 = (j.flags || [])[0];
    if (f0) chips.prepend(el("span", "chip " + (f0.action === "block" ? "bad" : f0.action === "ask" ? "warn" : ""), (f0.action === "block" ? "Blocked: " : f0.action === "ask" ? "Check: " : "") + f0.title));
    if (j.no_sponsorship) chips.append(el("span", "chip bad", "No sponsorship"));
    else if (j.sponsors) chips.append(el("span", "chip good", "Sponsors"));
    if (j.agency) chips.append(el("span", "chip", "via " + j.agency + (j.employment ? " \u00b7 " + j.employment.toLowerCase().replace("_", " ") : "")));
    if (j.level) chips.append(el("span", "chip", j.level));
    info.append(chips);
    r.append(scoreRing(j.score, s), info);
    r.onclick = () => selectJob(j.id);
    box.append(r);
  }
}
async function selectJob(id){
  jsel = id;
  for (const r of document.querySelectorAll(".jrow")) r.classList.toggle("on", r.dataset.id === id);
  $("v-jobs").classList.add("detail");
  const d = $("jdetail");
  let j = takeDetail(id);
  if (!j) {
    d.replaceChildren(emptyState("jobs", "Loading...", ""));
    try { j = await api("api/jobs/detail?id=" + encodeURIComponent(id)); } catch (e) { d.replaceChildren(emptyState("x", "Couldn't load this job", e.message)); return; }
  }
  if (jsel !== id) return;
  renderDetail(j);
  d.scrollTop = 0;
  if (focusMode) prefetchNext(id);
}
// Focused review: the next application's details are fetched while Sai reads this one,
// so it appears the moment he decides
const detailCache = new Map();
function takeDetail(id){
  const hit = detailCache.get(id);
  detailCache.delete(id);
  return hit && Date.now() - hit.at < 120000 ? hit.j : null;
}
function prefetchNext(id){
  const list = listJobs().map(j => j.id), next = list[list.indexOf(id) + 1];
  if (next && !detailCache.has(next)) api("api/jobs/detail?id=" + encodeURIComponent(next)).then(j => detailCache.set(next, {j, at: Date.now()}), () => {});
}
let focusMode = false, focusDone = 0;
function setFocus(on){
  focusMode = on;
  $("v-jobs").classList.toggle("focus", on);
  if (!on) { focusDone = 0; listSig = ""; loadJobs(true); }
}
function closeDetail(){ jsel = null; $("v-jobs").classList.remove("detail"); for (const r of document.querySelectorAll(".jrow")) r.classList.remove("on"); if (focusMode) setFocus(false); }
async function setStatus2(id, status, msg){
  try { await send("api/jobs/status", "POST", {id, status}); toast(msg); } catch (e) { return toast(e.message, true); }
  advance(id, status);
}
// After Approve or Skip: take the job out of this list here, open the next one right away,
// and let the list catch up in the background, so going through the morning list is one
// tap per job
function advance(id, status){
  const list = listJobs().map(j => j.id), at = list.indexOf(id);
  const next = list.filter(x => x !== id)[Math.max(0, at)] || null;
  const j = S.jobs.find(x => x.id === id);
  if (j) j.status = status;
  S.review = (S.review || []).filter(x => x !== id);
  S.agency_ready = (S.agency_ready || []).filter(x => x !== id);
  listSig = ""; applySig = "";
  if (next) selectJob(next);
  else if (focusMode) { toast("Review list done: " + focusDone + " decided."); closeDetail(); showView("today"); }
  else { jsel = null; $("jdetail").replaceChildren(); $("v-jobs").classList.remove("detail"); toast("That's the last one in this list."); }
  loadJobs(true);
}
function copyBtn(text){
  const b = el("button", "btn ghost sm"); b.append(icon("copy"), el("span", null, "Copy"));
  b.onclick = () => navigator.clipboard.writeText(text).then(() => toast("Copied"));
  return b;
}
function answerInput(a){
  let input;
  const opts = a.options || [];
  if (opts.length && opts.length <= 40) {
    input = el("select", "inp");
    for (const o of [""].concat(opts)) { const op = el("option", null, o || "Choose..."); op.value = o; input.append(op); }
    if (a.kind !== "you" && a.a && !opts.includes(a.a)) { const op = el("option", null, a.a); op.value = a.a; input.append(op); }
  } else input = el(a.kind === "draft" || (a.a || "").length > 70 ? "textarea" : "input", "inp");
  const orig = a.kind === "you" ? "" : (a.a || "");
  input.value = orig;
  if (a.kind === "you" && input.tagName !== "SELECT") input.placeholder = /Application profile/.test(a.a || "") ? a.a : "Your answer";
  return {input, orig};
}
const KINDTXT = {fact: "From your profile", draft: "Drafted by the local model. Check every claim.", you: "Needs your answer", legal: "Consent", file: "File", eeo: "Voluntary"};
function renderDetail(j){
  const d = $("jdetail"); d.replaceChildren();
  const wrap = el("div", "jd");
  const back = el("button", "btn ghost sm jback"); back.append(icon("back"), el("span", null, "Jobs")); back.onclick = closeDetail;
  back.style.marginBottom = "12px";
  wrap.append(back);
  if (focusMode) {
    const left = (S && S.review || []).length, bar = el("div", "pbar"), i = el("i"), fb = el("div", "fbar");
    i.style.width = (focusDone / Math.max(1, focusDone + left) * 100) + "%"; bar.append(i);
    const x = el("button", "btn ghost sm"); x.append(icon("x"), el("span", null, "List"), el("span", "keyhint", "Esc")); x.onclick = closeDetail;
    fb.append(el("span", "fc", focusDone + " done \u00b7 " + left + " left"), bar, x);
    wrap.append(fb);
    if (matchMedia("(hover:none)").matches && !store("swipehint")) { wrap.append(el("div", "swipehint", "Swipe right to approve, left to skip")); store("swipehint", "1"); }
    wrap.classList.add("enter");
  }
  const h = el("div", "jdh");
  const ht = el("div"); ht.style.flex = "1"; ht.style.minWidth = "0";
  ht.append(el("h2", null, j.title), el("div", "jm", [j.company, j.location, j.level, j.years != null ? j.years + "+ years" : ""].filter(Boolean).join(" · ")));
  h.append(ht, scoreRing(j.score, S || {strong: 72, good: 60}, true));
  wrap.append(h);
  const flags = el("div", "chips"); flags.style.marginTop = "10px";
  const sc = statusChip(j.status); if (sc) flags.append(sc);
  if (j.no_sponsorship) flags.append(el("span", "chip bad", "Won't sponsor"));
  else if (j.sponsors) flags.append(el("span", "chip good", "Sponsors visas"));
  if (flags.childNodes.length) wrap.append(flags);
  if (j.summary) wrap.append(el("div", "jsum", j.summary));
  const has = j.has_skills || [], miss = j.missing_skills || [];
  if (has.length + miss.length) {
    const sk = el("div", "chips skills");
    for (const x of has) { const c = el("span", "chip good"); c.append(icon("check"), document.createTextNode(x)); sk.append(c); }
    for (const x of miss) sk.append(el("span", "chip bad", x));
    wrap.append(el("div", "muted small", "Has " + has.length + " of " + (has.length + miss.length) + " required skills"), sk);
  }
  // actions
  const act = el("div", "actions");
  if (j.apply_url && j.apply_url.startsWith("https://")) {
    const a = el("a", "btn ghost"); a.href = j.apply_url; a.target = "_blank"; a.rel = "noopener noreferrer";
    a.append(icon("ext"), el("span", null, "Open application")); act.append(a);
  }
  const STATUS_ACTIONS = {
    new: [["skipped", "Skip", "Skipped"], ["applied", "Mark applied", "Marked applied"]],
    approved: [["new", "Back to review", "Moved back to review"], ["applied", "Mark applied", "Marked applied"]],
    applied: [["interview", "Interviewing", "Nice! Marked interviewing"], ["rejected", "Rejected", "Marked rejected"], ["withdrew", "Withdrew", "Marked withdrawn"], ["new", "Back to new", "Moved back"]],
    interview: [["offer", "Got an offer", "Congratulations!"], ["rejected", "Rejected", "Marked rejected"], ["withdrew", "Withdrew or declined", "Marked withdrawn"], ["applied", "Back to applied", "Moved back"]],
    offer: [["withdrew", "Declined", "Marked declined"], ["interview", "Back to interviewing", "Moved back"]],
    rejected: [["new", "Back to new", "Moved back"]], withdrew: [["new", "Back to new", "Moved back"]], skipped: [["new", "Back to new", "Moved back"]],
  };
  const reviewable = j.answers && j.status === "new" && j.auto_apply;
  for (const [st, label, msg] of STATUS_ACTIONS[j.status] || []) {
    if (reviewable && st === "skipped") continue;  // Skip sits next to Approve
    const b = el("button", "btn ghost", label); b.onclick = () => setStatus2(j.id, st, msg); act.append(b);
  }
  wrap.append(act);
  if (j.flags && j.flags.length) {
    const sec = el("div", "sec"); sec.append(el("div", "sech", "Hiring rules"));
    for (const f of j.flags) {
      const c = el("div", "flag " + f.action);
      const t = el("div"); t.append(el("div", "ft", f.title), el("div", "fm", f.msg));
      c.append(el("span", "fb", f.action === "block" ? "Blocked" : f.action === "ask" ? "Confirm" : "Note"), t);
      sec.append(c);
    }
    if (j.flags.some(f => f.action === "block")) sec.append(el("div", "muted small", "Blocked jobs stay out of Review and Apply. If a rule is wrong here, change it under Settings › Rules."));
    wrap.append(sec);
  }
  // emails
  if (j.emails && j.emails.length) {
    const sec = el("div", "sec"); sec.append(el("div", "sech", "Emails"));
    for (const m of j.emails) sec.append(mailCard(m, false));
    wrap.append(sec);
  }
  if (j.auto_apply || j.agency || j.docs) { const box = el("div", "sec"); wrap.append(box); renderDocs(box, j); }
  d.append(wrap);
  if (reviewable) reviewForm(j, wrap, d);
  else {
    if (j.answers) readOnlyAnswers(j, wrap);
    else {
      const sec = el("div", "sec"); sec.append(el("div", "sech", "Answers"));
      const b = el("button", "btn primary"); b.append(icon("sparkle"), el("span", null, "Prepare answers"));
      b.onclick = async () => { b.disabled = true; b.lastChild.textContent = "Preparing, a few minutes..."; try { await send("api/jobs/prepare", "POST", {id: j.id}); toast("Preparing answers in the background"); } catch (e) { toast(e.message, true); } };
      sec.append(el("div", "muted small", "No answers prepared for this job yet."), b);
      sec.lastChild.style.marginTop = "10px";
      wrap.append(sec);
    }
    posting(j, wrap);
  }
}
// The tailored resume and cover letter: open the PDFs, edit the summary and the letter.
function docTile(j, kind, title, sub){
  const a = el("a", "doc");
  a.href = "api/jobs/doc?id=" + encodeURIComponent(j.id) + "&kind=" + kind + "&t=" + Date.now();
  a.target = "_blank"; a.rel = "noopener";
  const ic = el("div", "dic"); ic.append(icon("doc"));
  const t = el("div"); t.append(el("b", null, title), el("span", null, sub));
  const go = icon("ext"); go.style.marginLeft = "auto"; go.style.color = "var(--muted)";
  a.append(ic, t, go);
  return a;
}
function renderDocs(box, j){
  box.replaceChildren();
  const hd = el("div", "sech", "Resume and cover letter");
  box.append(hd);
  const dc = j.docs;
  if (j.ai_restricted) { const c = el("div", "chip warn", "This form has an AI-use policy. The autofill will attach your usual resume, no cover letter, and leave drafted answers for you, unless you Approve anyway."); c.style.cssText = "height:auto;padding:6px 10px;white-space:normal;margin-bottom:10px"; box.append(c); }
  if (!dc) {
    box.append(el("div", "muted small", "No tailored documents yet. Without them the autofill attaches your usual resume and no cover letter."));
    const b = el("button", "btn ghost"); b.append(icon("sparkle"), el("span", null, "Make resume and cover letter"));
    b.style.marginTop = "10px";
    b.onclick = () => makeDocs(j, box, b);
    box.append(b);
    return;
  }
  const tiles = el("div", "docs");
  tiles.append(docTile(j, "resume", "Tailored resume", "PDF · projects and skills ordered for this job"),
               docTile(j, "cover", "Cover letter", "PDF · drafted by the local model"));
  box.append(tiles);
  for (const w of dc.warnings || []) { const c = el("div", "chip warn", w); c.style.cssText = "margin-top:8px;height:auto;padding:4px 10px;white-space:normal"; box.append(c); }
  const dt = el("details"); dt.style.marginTop = "10px";
  dt.append(el("summary", null, "Edit the summary and the cover letter"));
  const sum = el("textarea", "inp"); sum.value = dc.summary || ""; sum.style.minHeight = "96px";
  const cov = el("textarea", "inp"); cov.value = dc.cover || ""; cov.style.minHeight = "280px";
  const f1 = el("div", "field"); f1.append(el("label", null, "Resume summary"), sum, el("div", "help", "Your bullets stay as they are; only this summary and the order of projects and skills change per job."));
  const f2 = el("div", "field"); f2.append(el("label", null, "Cover letter"), cov, el("div", "help", "Check every claim. Blank lines start new paragraphs."));
  const row = el("div", "actions");
  const save = el("button", "btn primary", "Save changes");
  save.onclick = async () => {
    try { await send("api/jobs/docs", "PUT", {id: j.id, summary: sum.value, cover: cov.value}); } catch (e) { return toast(e.message, true); }
    j.docs.summary = sum.value; j.docs.cover = cov.value;
    toast("Saved. The PDFs now use your changes.");
    renderDocs(box, j);
  };
  const remake = el("button", "btn ghost", "Make them again");
  remake.onclick = () => { if (confirm("Make a new summary and cover letter? Your edits here are replaced.")) makeDocs(j, box, remake); };
  row.append(save, remake);
  f1.style.marginTop = "12px";
  dt.append(f1, f2, row);
  box.append(dt);
}
async function makeDocs(j, box, btn){
  try { await send("api/jobs/docs", "POST", {id: j.id}); } catch (e) { return toast(e.message, true); }
  btn.disabled = true;
  btn.lastChild.textContent = "Writing, about 2 minutes...";
  const was = j.docs && j.docs.made;
  for (let t = 0; t < 40; t++) {  // up to 10 minutes: the model may be busy with a chat
    await new Promise(r => setTimeout(r, 15000));
    if (jsel !== j.id || !box.isConnected) return;
    let fresh;
    try { fresh = await api("api/jobs/detail?id=" + encodeURIComponent(j.id)); } catch (e) { continue; }
    if (fresh.docs && fresh.docs.made !== was) { j.docs = fresh.docs; toast("Resume and cover letter ready"); return renderDocs(box, j); }
  }
  btn.disabled = false; btn.lastChild.textContent = "Still working. Check back later";
}
function posting(j, wrap){
  const dt = el("details", "sec"); dt.append(el("summary", null, "Posting text"), pre(j.description || "(none)"));
  const gap = el("div"); gap.style.height = "28px";
  wrap.append(dt, gap);
}
function readOnlyAnswers(j, wrap){
  const groups = [["you", "Needs you"], ["legal", "Consents"], ["draft", "Drafts"], ["fact", "From your profile"], ["eeo", "Voluntary"], ["file", "Files"]];
  for (const [k, title] of groups) {
    const items = j.answers.filter(a => a.kind === k);
    if (!items.length) continue;
    const sec = el("div", "sec");
    const hd = el("div", "sech", title); hd.append(el("span", "chip", String(items.length)));
    sec.append(hd);
    for (const a of items) {
      const q = el("div", "q");
      const ql = el("div", "ql"); if (a.required) ql.append(el("span", "req", "* ")); ql.append(document.createTextNode(a.q));
      q.append(ql);
      if (a.a) q.append(el("div", "qa" + (k === "fact" || k === "draft" ? "" : " muted"), a.a));
      if ((k === "fact" || k === "draft") && a.a) { const c = copyBtn(a.a); c.style.marginTop = "8px"; q.append(c); }
      sec.append(q);
    }
    wrap.append(sec);
  }
  if (j.note) wrap.append(el("div", "muted small", j.note));
}
function reviewForm(j, wrap, d){
  const rows = [], consents = [];
  const byKind = k => j.answers.filter(a => a.kind === k);
  const need = byKind("you").sort((a, b) => (b.required ? 1 : 0) - (a.required ? 1 : 0));
  const left = el("span", "left");
  const refresh = () => {
    if ((j.flags || []).some(f => f.action === "block")) { left.textContent = "Blocked by a hiring rule"; left.style.color = "var(--bad)"; return; }
    const miss = rows.filter(r => r.need && !r.input.value.trim()).length + consents.filter(c => c.required && !c.box.checked).length;
    left.textContent = miss ? miss + " required " + (miss === 1 ? "item" : "items") + " left" : "Ready to approve";
    left.style.color = miss ? "var(--warn)" : "var(--good)";
  };
  const qbox = (a, withInput) => {
    const q = el("div", "q");
    const ql = el("div", "ql"); if (a.required) ql.append(el("span", "req", "* ")); ql.append(document.createTextNode(a.q));
    q.append(ql, el("div", "qk", KINDTXT[a.kind] || a.kind));
    if (withInput) {
      const {input, orig} = answerInput(a);
      q.append(input);
      const r = {q: a.q, input, orig, need: a.required && a.kind === "you"};
      if (r.need) q.classList.add("need");
      input.addEventListener("input", () => { if (r.need) q.classList.toggle("need", !input.value.trim()); refresh(); });
      input.addEventListener("change", () => { if (r.need) q.classList.toggle("need", !input.value.trim()); refresh(); });
      rows.push(r);
    }
    return q;
  };
  const section = (title, count, extra) => {
    const sec = el("div", "sec");
    const hd = el("div", "sech", title); hd.append(el("span", "chip" + (extra || ""), String(count)));
    sec.append(hd); wrap.append(sec); return sec;
  };
  if (need.length) { const sec = section("Needs you", need.length, " warn"); for (const a of need) sec.append(qbox(a, true)); }
  const legal = byKind("legal");
  if (legal.length) {
    const sec = section("Statements you agree to", legal.length);
    if (legal.length > 1) {
      const all = el("button", "btn ghost sm", "Agree to all " + legal.length);
      all.onclick = () => { for (const c of consents) c.box.checked = true; refresh(); };
      all.style.marginLeft = "auto"; sec.firstChild.append(all);
    }
    for (const a of legal) {
      const q = el("div", "q");
      const lab = el("label", "consent");
      const box = el("input"); box.type = "checkbox";
      const txt = el("div");
      const ql = el("div", "ql"); if (a.required) ql.append(el("span", "req", "* ")); ql.append(document.createTextNode(a.q));
      txt.append(ql, el("div", "qk", a.required ? "Required to apply. Only tick it if it's true for you." : "Optional. Left blank unless you tick it."));
      lab.append(box, txt); q.append(lab); sec.append(q);
      box.onchange = refresh;
      consents.push({q: a.q, box, required: a.required});
    }
  }
  const drafts = byKind("draft");
  if (drafts.length) { const sec = section("Drafts", drafts.length, " acc"); for (const a of drafts) sec.append(qbox(a, true)); }
  const facts = byKind("fact");
  if (facts.length) {
    const dt = el("details", "sec");
    const sm = el("summary", null, "From your profile"); sm.append(el("span", "chip good", String(facts.length)));
    dt.append(sm);
    for (const a of facts) dt.append(qbox(a, true));
    wrap.append(dt);
  }
  const other = j.answers.filter(a => a.kind === "file" || a.kind === "eeo");
  if (other.length) {
    const dt = el("details", "sec");
    dt.append(el("summary", null, "Resume and voluntary questions"));
    for (const a of other) {
      const q = el("div", "q");
      q.append(el("div", "ql", a.q), el("div", "qa muted", a.kind === "file" ? "Your resume PDF is attached automatically." : "Voluntary. Left blank; set it in Settings › Application profile to share it."));
      dt.append(q);
    }
    wrap.append(dt);
  }
  if (j.note) wrap.append(el("div", "muted small", j.note));
  posting(j, wrap);
  // sticky approve bar
  const foot = el("div", "stickyfoot");
  const skip = el("button", "btn ghost"); skip.append(el("span", null, "Skip"), el("span", "keyhint", "S"));
  skip.id = "fskip";
  skip.onclick = () => { if (focusMode) focusDone++; setStatus2(j.id, "skipped", "Skipped"); };
  const asks = (j.flags || []).filter(f => f.action === "ask"), blocks = (j.flags || []).filter(f => f.action === "block");
  const ok = el("button", "btn primary"); ok.append(icon("check"), el("span", null, asks.length ? "Approve anyway" : "Approve"), el("span", "keyhint", "A"));
  ok.id = "fapprove";
  if (blocks.length) { ok.disabled = true; ok.title = blocks.map(f => f.msg).join(" "); }
  ok.onclick = async () => {
    const answers = rows.filter(r => r.input.value.trim() && r.input.value.trim() !== r.orig).map(r => ({q: r.q, a: r.input.value.trim()}));
    const agreed = consents.filter(c => c.box.checked).map(c => c.q);
    if (asks.length && !confirm("Go ahead despite these hiring rules?\n\n" + asks.map(f => "\u2022 " + f.title + ": " + f.msg).join("\n"))) return;
    ok.disabled = true;
    try { await send("api/jobs/approve", "POST", {id: j.id, answers, agreed, allow: asks.map(f => f.rule)}); }
    catch (e) { ok.disabled = false; return toast(e.message, true); }
    toast("Approved. It's in Apply to approved.");
    if (focusMode) focusDone++;
    advance(j.id, "approved");
  };
  foot.append(left, skip, ok);
  d.append(foot);
  refresh();
}
async function updateApply(n){
  const a = $("japply");
  if (!n) { a.style.display = "none"; applySig = ""; return; }
  if (applySig === String(n) && a.href) return;
  try {
    const r = await api("api/jobs/next");
    if (!r.job || !r.job.apply_url.startsWith("https://")) { a.style.display = "none"; return; }
    a.href = r.job.apply_url + "#agent-auto";
    a.querySelector(".long").textContent = "Apply to approved (" + r.left + ")";
    a.querySelector(".short").textContent = "Apply (" + r.left + ")";
    a.style.display = "";
    applySig = String(n);
  } catch (e) {}
}
$("jrun").onclick = () => {
  if ($("jrun").dataset.mode === "stop") {
    if (!confirm("Stop the search? What it found so far is kept; the rest waits for the next run.")) return;
    return send("api/jobs/stop", "POST").then(() => { toast("Stopping after the current step"); setTimeout(() => loadJobs(), 800); }, e => toast(e.message, true));
  }
  send("api/jobs/run", "POST").then(() => { toast("Search started"); setTimeout(() => loadJobs(), 600); }, e => toast(e.message, true));
};

// ---------- applications ----------
const APP_FILTERS = [["all", "All"], ["approved", "Waiting to send"], ["applied", "Applied"], ["interview", "Interviewing"], ["offer", "Offers"], ["rejected", "Rejected"], ["withdrew", "Withdrew"]];
const APP_COLORS = {applied: "var(--accent)", interview: "var(--good)", offer: "#e0b400", rejected: "var(--bad)", withdrew: "var(--faint)", approved: "var(--accent2)"};
let APPS = [], afilter = "all", asel = null, appsSig = "", amode = "list", rweek = "";
function setAppsMode(m){
  amode = m;
  $("v-apps").classList.toggle("report", m === "report");
  for (const b of document.querySelectorAll(".amode button")) b.classList.toggle("on", b.dataset.mode === m);
  store("amode", m);
  if (m === "report") loadReport(); else loadApps();
}
async function loadReport(){
  let d;
  try { d = await api("api/report" + (rweek ? "?end=" + encodeURIComponent(rweek) : "")); } catch (e) { return toast(e.message, true); }
  const sel = $("rweek"); sel.replaceChildren();
  const short = iso => new Date(iso + "T12:00:00").toLocaleDateString([], {month: "short", day: "numeric"});
  const cur = el("option", null, "Last 7 days"); cur.value = ""; sel.append(cur);
  for (const w of [...d.saved].reverse()) { const o = el("option", null, short(w.start) + " \u2013 " + short(w.end)); o.value = w.end; sel.append(o); }
  sel.value = rweek;
  sel.onchange = () => { rweek = sel.value; loadReport(); };
  renderReport(d.report);
}
function card(title, sub){
  const c = el("div", "card rcard"); c.append(el("h2", null, title));
  if (sub) c.append(el("div", "sub muted small", sub));
  return c;
}
function renderReport(r){
  const box = $("report"); box.replaceChildren();
  const f = r.found, fw = r.funnel.week, fa = r.funnel.all;
  const short = iso => new Date(iso + "T12:00:00").toLocaleDateString([], {weekday: "short"});
  const range = new Date(r.start + "T12:00:00").toLocaleDateString([], {month: "short", day: "numeric"}) + " \u2013 " + new Date(r.end + "T12:00:00").toLocaleDateString([], {month: "short", day: "numeric"});
  box.append(el("div", "muted small", range + ". Counted from your jobs and inbox; nothing here is written by the model."));
  const st = el("div", "stats");
  const vs = (a, b) => b ? " \u00b7 " + (a >= b ? "+" : "") + Math.round((a - b) / b * 100) + "%" : "";
  for (const [v, label] of [[f.total, "Jobs found" + vs(f.total, f.prev_total)], [f.good, "Good matches"],
      [fw.applied, "Applied"], [fw.replied, "Replies"], [fw.interview, "Interviews"]]) {
    const b = el("div", "stat"); b.append(el("b", null, String(v)), el("span", null, label)); st.append(b);
  }
  box.append(st);
  const grid = el("div", "rgrid"); box.append(grid);

  const tips = card("Suggestions", "Based on the numbers below. Each one says what it rests on.");
  if (!r.suggestions.length) tips.append(el("div", "muted small", "Nothing to change this week."));
  for (const s of r.suggestions) { const t = el("div", "rtip"); t.append(el("b", null, s.title), el("div", "small", s.detail), el("div", "why", s.evidence)); tips.append(t); }
  grid.append(tips);

  const days = card("Jobs found each day", "Openings that passed your filters and were scored.");
  const chart = el("div", "days"), top = Math.max(1, ...f.days.map(d => d.found));
  for (const d of f.days) {
    const col = el("div", "col"); col.title = d.found + " found, " + d.good + " good, " + d.strong + " strong" + (d.fetched != null ? ", " + d.fetched + " postings read" : "");
    const stack = el("div", "stack"); stack.style.height = Math.max(2, d.found / top * 100) + "%";
    for (const [n, c] of [[d.strong, "var(--good)"], [d.good - d.strong, "var(--accent)"], [d.found - d.good, "var(--line)"]]) {
      if (!n) continue; const i = el("i"); i.style.height = (n / d.found * 100) + "%"; i.style.background = c; stack.append(i);
    }
    col.append(el("span", "v", String(d.found)), stack, el("span", "d", short(d.day)));
    chart.append(col);
  }
  const leg = el("div", "legend2");
  for (const [l, c] of [["Strong", "var(--good)"], ["Good", "var(--accent)"], ["Below good", "var(--line)"]]) { const s = el("span", null, l); s.style.setProperty("--c", c); leg.append(s); }
  days.append(chart, leg);
  const down = f.days.filter(d => d.agency_errors.length);
  for (const d of down) days.append(el("div", "small", short(d.day) + ": couldn't read " + d.agency_errors.map(e => e.slice(7)).join("; ")));
  const filt = Object.entries(f.filtered);
  if (filt.length) days.append(el("div", "muted small", "Left out by your filters: " + filt.map(([k, v]) => v + " " + k).join(", ") + "."));
  grid.append(days);

  const kinds = card("What kind of jobs it finds", "Share of this week's jobs, the change from last week, and how many of each fit you (scored good).");
  for (const [key, rows] of Object.entries(r.mix)) {
    if (!rows.length) continue;
    const b = el("div", "mixblock"); b.append(el("div", "t", r.mix_labels[key]));
    for (const m of rows.slice(0, 6)) {
      const row = el("div", "mrow"), bar = el("div", "bar"), i = el("i");
      i.style.width = m.share + "%"; bar.append(i);
      const pc = el("span", "pc", m.share + "%");
      if (m.prev_share != null && m.share !== m.prev_share) pc.append(el("span", "delta " + (m.share > m.prev_share ? "up" : "down"), (m.share > m.prev_share ? "\u25b2" : "\u25bc") + Math.abs(m.share - m.prev_share)));
      const nm = el("span", "nm", m.name); nm.title = m.name + ": " + m.count + " jobs";
      row.append(nm, bar, pc, el("span", "fit", m.good_rate + "% fit"));
      b.append(row);
    }
    kinds.append(b);
  }
  grid.append(kinds);

  const sk = card("Skills", "What this week's jobs ask for, and the missing skills that would lift the most jobs over your good line (last 30 days).");
  const asked = el("div", "mixblock"); asked.append(el("div", "t", "Most asked"));
  for (const s of r.skills.asked.slice(0, 10)) {
    const row = el("div", "skill"), right = el("span", "chips");
    right.append(el("span", "muted", s.share + "% of jobs"), el("span", "chip " + (s.have ? "good" : "warn"), s.have ? "On your resume" : "Missing"));
    row.append(el("span", null, s.skill), right); asked.append(row);
  }
  const gaps = el("div", "mixblock"); gaps.append(el("div", "t", "Gaps worth closing"));
  for (const g of r.skills.gaps.slice(0, 8)) {
    const row = el("div", "skill"); row.append(el("span", null, g.skill), el("span", "muted", (g.lift ? "+" + g.lift + " good matches \u00b7 " : "") + "asked in " + g.asked));
    gaps.append(row);
  }
  gaps.append(el("div", "muted small", "Only add a skill to your resume if you've really used it."));
  sk.append(asked, gaps);
  grid.append(sk);

  const ap = card("How your applications are doing", "All applications sent so far. Groups under " + r.min_group + " applications are greyed out: too few to compare.");
  const tbl = el("table", "rtable"), head = el("tr");
  for (const h of ["", "All time", "This week"]) head.append(el("th", null, h));
  tbl.append(head);
  const pct = (a, b) => b ? " (" + Math.round(a / b * 100) + "%)" : "";
  for (const [k, label] of [["applied", "Sent"], ["replied", "Replied"], ["interview", "Assessment or interview"], ["rejected", "Rejected"], ["offer", "Offer"], ["quiet", "No reply in 14+ days"]]) {
    const tr = el("tr"); tr.append(el("td", null, label), el("td", null, fa[k] + (k === "applied" ? "" : pct(fa[k], fa.applied))), el("td", null, fw[k] + (k === "applied" ? "" : pct(fw[k], fw.applied)))); tbl.append(tr);
  }
  if (fa.median_wait != null) { const tr = el("tr"); tr.append(el("td", null, "Typical days to first reply"), el("td", null, String(fa.median_wait)), el("td", null, fw.median_wait != null ? String(fw.median_wait) : "\u2013")); tbl.append(tr); }
  ap.append(tbl);
  for (const [key, label] of [["score", "By match strength"], ["level", "By level"], ["source", "By where it's posted"], ["role", "By kind of role"]]) {
    const rows = r.funnel.by[key]; if (!rows.length) continue;
    const b = el("div", "mixblock"); b.append(el("div", "t", label));
    const t = el("table", "rtable"), h = el("tr");
    for (const x of ["", "Sent", "Replied", "Interview"]) h.append(el("th", null, x));
    t.append(h);
    for (const g of rows) { const tr = el("tr", g.small ? "few" : ""); tr.append(el("td", null, g.name), el("td", null, String(g.n)), el("td", null, g.reply_rate + "%"), el("td", null, g.interview_rate + "%")); t.append(tr); }
    b.append(t); ap.append(b);
  }
  grid.append(ap);
}
function daysAgo(iso){ return iso ? (Date.now() - new Date(iso).getTime()) / 864e5 : 1e9; }
async function loadApps(){
  let d;
  try { d = await api("api/applications"); } catch (e) { return toast(e.message, true); }
  APPS = d.applications;
  const sent = APPS.filter(a => a.status !== "approved");
  const n = k => APPS.filter(a => a.status === k).length;
  const replied = sent.filter(a => a.replied).length;
  const st = $("astats"); st.replaceChildren();
  for (const [v, label, hl, f] of [[sent.length, "Applied", true, "all"], [n("interview"), "Interviewing", false, "interview"], [n("offer"), "Offers", false, "offer"],
      [n("rejected"), "Rejected", false, "rejected"], [sent.length ? Math.round(replied / sent.length * 100) + "%" : "\u2013", "Heard back", false, null],
      [sent.filter(a => daysAgo(a.applied_at) <= 7).length, "This week", false, null]]) {
    const b = el("button", "stat" + (hl ? " hl" : ""));
    b.append(el("b", null, String(v)), el("span", null, label));
    if (f) b.onclick = () => { afilter = f; appsSig = ""; renderApps(); };
    st.append(b);
  }
  const fu = $("afunnel"); fu.replaceChildren();
  if (APPS.length) {
    const wrap = el("div"); wrap.style.width = "100%";
    const bar = el("div", "funnel"), leg = el("div", "legend");
    for (const k of ["approved", "applied", "interview", "offer", "rejected", "withdrew"]) {
      const c = n(k); if (!c) continue;
      const seg = el("i"); seg.style.width = (c / APPS.length * 100) + "%"; seg.style.background = APP_COLORS[k]; bar.append(seg);
      const l = el("span", null, (APP_FILTERS.find(f => f[0] === k) || [0, k])[1] + " " + c); l.style.setProperty("--c", APP_COLORS[k]); leg.append(l);
    }
    wrap.append(bar, leg); fu.append(wrap);
  }
  renderApps();
}
function renderApps(){
  const q = norm($("asearch").value);
  const f = $("afilter"); f.replaceChildren();
  for (const [k, label] of APP_FILTERS) {
    const c = k === "all" ? APPS.length : APPS.filter(a => a.status === k).length;
    if (k !== "all" && !c) continue;
    const b = el("button", k === afilter ? "on" : ""); b.append(document.createTextNode(label), el("span", "n", String(c)));
    b.onclick = () => { afilter = k; renderApps(); };
    f.append(b);
  }
  const list = APPS.filter(a => (afilter === "all" || a.status === afilter) && (!q || norm(a.company + " " + a.title).includes(q)));
  const box = $("alist"); box.replaceChildren();
  if (!list.length) box.append(emptyState("apps", APPS.length ? "No matches" : "No applications yet", APPS.length ? "Try another filter or search." : "Jobs you approve or mark applied show up here."));
  for (const a of list) {
    const r = el("button", "arow" + (a.id === asel ? " on" : ""));
    r.dataset.id = a.id;
    const top = el("div", "chips"); top.style.justifyContent = "space-between"; top.style.flexWrap = "nowrap";
    const co = el("span", "co", a.company); co.style.overflow = "hidden"; co.style.textOverflow = "ellipsis"; co.style.whiteSpace = "nowrap";
    top.append(co, statusChip(a.status) || el("span", "chip acc", "Waiting to send"));
    r.append(top, el("div", "jm", a.title), el("div", "muted small", [a.applied_at ? "Applied " + when(a.applied_at) : "Approved " + when(a.approved_at), a.agency ? "via " + a.agency : ""].filter(Boolean).join(" \u00b7 ")));
    if (a.last_email) {
      const k = MAIL_KIND[a.last_email.kind] || ["", a.last_email.kind];
      const l = el("div", "last"); l.append(el("span", "chip " + k[0], k[1])); l.append(document.createTextNode(" " + a.last_email.summary));
      r.append(l);
    }
    r.onclick = () => openApp(a.id);
    box.append(r);
  }
  if (!asel && window.innerWidth > 860 && !document.querySelector("#adetail .jd")) $("adetail").replaceChildren(emptyState("apps", "Pick an application", "Its timeline and the full application as submitted, with the resume and cover letter, open here."));
}
$("asearch").oninput = () => renderApps();
// Jobs on a laptop: j / arrow down for the next job, k / arrow up for the previous, Esc to close
document.addEventListener("keydown", e => {
  if (view !== "jobs" || !S || e.metaKey || e.ctrlKey || e.altKey || e.target.closest("input, textarea, select, [contenteditable]")) return;
  if (e.key === "Escape" && jsel) { closeDetail(); return; }
  if (focusMode && (e.key === "a" || e.key === "s")) {
    const b = $(e.key === "a" ? "fapprove" : "fskip");
    if (b && !b.disabled) { e.preventDefault(); b.click(); }
    return;
  }
  const step = {j: 1, ArrowDown: 1, k: -1, ArrowUp: -1}[e.key];
  if (!step) return;
  const list = listJobs().map(j => j.id);
  if (!list.length) return;
  const at = list.indexOf(jsel);
  const next = list[at < 0 ? 0 : Math.min(list.length - 1, Math.max(0, at + step))];
  if (next && next !== jsel) { e.preventDefault(); selectJob(next); document.querySelector('.jrow[data-id="' + CSS.escape(next) + '"]')?.scrollIntoView({block: "nearest"}); }
});
for (const b of document.querySelectorAll(".amode button")) b.onclick = () => {
  if (b.dataset.mode === "inbox") return showView("inbox");
  store("amode", b.dataset.mode);
  if (view !== "apps") showView("apps"); else setAppsMode(b.dataset.mode);
};
for (const b of document.querySelectorAll(".asub button")) b.onclick = () => showView(b.dataset.v);
function closeApp(){ asel = null; $("v-apps").classList.remove("detail"); for (const r of document.querySelectorAll(".arow")) r.classList.remove("on"); }
async function openApp(id){
  asel = id;
  for (const r of document.querySelectorAll(".arow")) r.classList.toggle("on", r.dataset.id === id);
  $("v-apps").classList.add("detail");
  const d = $("adetail");
  d.replaceChildren(emptyState("apps", "Loading...", ""));
  let j;
  try { j = await api("api/jobs/detail?id=" + encodeURIComponent(id)); } catch (e) { return d.replaceChildren(emptyState("x", "Couldn't load it", e.message)); }
  if (asel !== id) return;
  const w = el("div", "jd");
  const back = el("button", "btn ghost sm jback"); back.append(icon("back"), el("span", null, "Applications")); back.onclick = closeApp; back.style.marginBottom = "12px";
  const h = el("div", "jdh"); const ht = el("div"); ht.style.flex = "1"; ht.style.minWidth = "0";
  ht.append(el("h2", null, j.company), el("div", "jm", [j.title, j.location].filter(Boolean).join(" \u00b7 ")));
  h.append(ht, statusChip(j.status) || el("span", "chip acc", "Waiting to send"));
  w.append(back, h);
  const act = el("div", "actions");
  const posting = el("a", "btn ghost"); posting.href = j.url; posting.target = "_blank"; posting.rel = "noopener noreferrer"; posting.append(icon("ext"), el("span", null, "Posting"));
  if (/^https:\/\//.test(j.url || "")) act.append(posting);
  const NEXT = {approved: [], applied: [["interview", "Interviewing"], ["rejected", "Rejected"], ["withdrew", "Withdrew"]],
    interview: [["offer", "Got an offer"], ["rejected", "Rejected"], ["withdrew", "Withdrew"]], offer: [["withdrew", "Declined"]], rejected: [], withdrew: []};
  for (const [st, label] of NEXT[j.status] || []) {
    const b = el("button", "btn ghost", label);
    b.onclick = async () => { try { await send("api/jobs/status", "POST", {id: j.id, status: st}); toast("Updated"); } catch (e) { return toast(e.message, true); } await loadApps(); openApp(j.id); };
    act.append(b);
  }
  w.append(act);
  // timeline
  const tl = el("div", "sec"); tl.append(el("div", "sech", "Timeline"));
  const evs = [];
  if (j.approved_at) evs.push([j.approved_at, "Approved in the panel", ""]);
  const sub = j.submission;
  if (sub) evs.push([sub.at, sub.exact ? "Submitted by the autofill" : "Applied", sub.exact ? "" : "Marked applied"]);
  for (const m of j.emails || []) { const k = MAIL_KIND[m.kind] || ["", m.kind]; evs.push([m.date, k[1], m.summary || m.subject]); }
  if (j.status_at && ["interview", "offer", "rejected", "withdrew"].includes(j.status)) evs.push([j.status_at, "Status: " + (statusChip(j.status) || {}).textContent, ""]);
  evs.sort((a, b) => (a[0] || "").localeCompare(b[0] || ""));
  const tlb = el("div", "tl");
  for (const [at, what, extra] of evs) {
    const e = el("div", "ev"); e.append(el("div", "when", when(at)), el("b", null, what));
    if (extra) e.append(el("div", "muted small", extra));
    tlb.append(e);
  }
  if (!evs.length) tlb.append(el("div", "muted small", "Nothing yet."));
  tl.append(tlb); w.append(tl);
  // the application as submitted
  const sa = el("div", "sec"); sa.append(el("div", "sech", "Submitted application"));
  if (!sub) sa.append(el("div", "muted small", j.status === "approved" ? "Not sent yet. It's waiting in Apply to approved." : "No record of this application."));
  else {
    const note = el("div", "chip " + (sub.exact ? "good" : "warn"), sub.exact ? "Exact copy, recorded right before Submit on " + when(sub.at) : "Rebuilt from the prepared answers on " + when(sub.at) + ". What was sent may differ.");
    note.style.cssText = "height:auto;padding:5px 10px;white-space:normal;margin-bottom:12px";
    sa.append(note);
    if (sub.files && sub.files.length) {
      const tiles = el("div", "docs");
      for (const f of sub.files) {
        const a = el("a", "doc"); a.href = "api/applications/doc?id=" + encodeURIComponent(j.id) + "&kind=" + f.doc; a.target = "_blank"; a.rel = "noopener";
        const ic = el("div", "dic"); ic.append(icon("doc"));
        const t = el("div"); t.append(el("b", null, f.doc === "cover" ? "Cover letter" : (f.tailored ? "Tailored resume" : "Resume")), el("span", null, f.name));
        a.append(ic, t); tiles.append(a);
      }
      sa.append(tiles);
    } else sa.append(el("div", "muted small", "No documents on record."));
    const card = el("div", "card"); card.style.marginTop = "12px";
    for (const x of sub.fields || []) {
      const row = el("div", "qa");
      row.append(el("div", "qq", x.q), el("div", "aa" + (x.a ? "" : " blank"), x.a || "(left blank)"));
      card.append(row);
    }
    if (!(sub.fields || []).length) card.append(el("div", "muted small", "No fields on record."));
    sa.append(card);
    if (sub.consents && sub.consents.length) {
      const cs = el("div", "sec"); cs.append(el("div", "sech", "Statements you agreed to"));
      for (const c of sub.consents) { const q = el("div", "q"); q.append(el("div", "ql", c)); cs.append(q); }
      sa.append(cs);
    }
  }
  w.append(sa);
  const gap = el("div"); gap.style.height = "28px"; w.append(gap);
  d.replaceChildren(w);
  d.scrollTop = 0;
}

// ---------- today ----------
// The home screen: what needs Sai now, in the order it matters: emails that need him
// (assessments, interviews, codes), the review list, approved applications to send,
// agency jobs, then how the search and the week are going
let INBOX = null, TREPORT = null, treportAt = 0, todaySig = "";
async function loadTodayReport(force){
  if (!force && TREPORT && Date.now() - treportAt < 600000) return;
  try { TREPORT = (await api("api/report")).report; treportAt = Date.now(); } catch (e) { return; }
  if (view === "today") renderToday();
}
function greeting(){ const h = new Date().getHours(); return h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening"; }
function trow(title, sub, ...actions){
  const r = el("div", "trow"), tx = el("div", "tx");
  tx.append(el("div", "t1", title));
  if (sub) tx.append(el("div", "t2", sub));
  r.append(tx, ...actions.filter(Boolean));
  return r;
}
function startReview(){
  jfilter = "review"; store("jfilter", "review"); listSig = "";
  showView("jobs");
  const first = (S && S.review || [])[0];
  if (first) { setFocus(true); focusDone = 0; selectJob(first); }
}
// Swipe on the phone while reviewing: right approves, left skips (the same checks as the buttons)
(() => {
  let x0 = 0, y0 = 0, ok = false;
  const d = $("jdetail");
  d.addEventListener("touchstart", e => {
    ok = focusMode && e.touches.length === 1 && !e.target.closest("input, textarea, select, details, pre, a, button, label");
    x0 = e.touches[0].clientX; y0 = e.touches[0].clientY;
  }, {passive: true});
  d.addEventListener("touchend", e => {
    if (!ok) return;
    const t = e.changedTouches[0], dx = t.clientX - x0, dy = t.clientY - y0;
    if (Math.abs(dx) < 110 || Math.abs(dy) > 60) return;
    const b = $(dx > 0 ? "fapprove" : "fskip");
    if (b && !b.disabled) b.click();
  });
})();
function renderToday(){
  const s = S, inbox = INBOX, r = TREPORT;
  const sig = JSON.stringify([s && [s.review, s.approved, s.agency_ready, s.not_ready, s.progress, s.last_run && s.last_run.finished],
                              inbox && inbox.messages && inbox.messages.filter(m => m.open || m.code).map(m => [m.uid, m.open, m.code]), r && r.made, alertsOn]);
  $("tgreet").textContent = greeting();
  $("tdate").textContent = new Date().toLocaleDateString([], {weekday: "long", month: "long", day: "numeric"});
  if (sig === todaySig) return;
  todaySig = sig;
  const box = $("tbody"); box.replaceChildren();
  const left = el("div", "tcol"), right = el("div", "tcol");
  box.append(left, right);
  if (!s) { left.append(emptyState("today", "Loading", "")); return; }

  // search status in the header
  const st = $("tstatus"); st.replaceChildren();
  if (s.progress.running) st.append(el("i", "dot on"), el("span", null, "Searching: " + s.progress.step + (s.progress.total ? " (" + s.progress.done + "/" + s.progress.total + ")" : "")));
  else if (s.last_run) { const t = el("span", null, "Last search " + when(s.last_run.finished || s.last_run.started)); t.title = jobsMeta(s); st.append(t); }

  // 1. emails that need him
  const needs = inbox && inbox.messages ? inbox.messages.filter(m => m.open) : [];
  const codes = inbox && inbox.messages ? inbox.messages.filter(m => m.code && daysAgo(m.date) < 1 / 12) : [];
  if (needs.length || codes.length) {
    const c = el("div", "tcard"), h = el("h2", null, "Needs you"); h.append(el("span", "chip warn", String(needs.length + codes.length)));
    c.append(h);
    for (const m of codes) {
      const code = el("span", "code2", m.code);
      c.append(trow((m.company || m.ai_company || m.from) + " code", when(m.date), code, copyBtn(m.code)));
    }
    for (const m of needs) {
      const k = MAIL_KIND[m.kind] || ["", m.kind];
      const bits = [k[1], m.due ? "due " + m.due : "", m.when].filter(Boolean).join(" \u00b7 ");
      const done = el("button", "btn ghost sm", "Done");
      done.onclick = () => send("api/inbox/done", "POST", {uid: m.uid, done: true}).then(() => { m.open = false; todaySig = ""; renderToday(); loadInbox(); }, e => toast(e.message, true));
      c.append(trow((m.company || m.ai_company || m.from) + (m.title ? " \u00b7 " + m.title : ""), bits + (m.summary ? ". " + m.summary : ""), gmailLink(m), done));
    }
    left.append(c);
  }

  // 2. the review list
  const review = (s.review || []).length;
  const rc = el("div", "tcard"), hero = el("div", "hero");
  rc.append(el("h2", null, "Review"));
  if (review) {
    const go = el("button", "btn primary"); go.append(el("span", null, "Start reviewing"), icon("send"));
    go.onclick = startReview;
    const what = el("div", "what"); what.append(el("b", null, review === 1 ? "application ready" : "applications ready"), el("span", "muted small", "Answers, tailored resume and cover letter done. About a minute each."));
    hero.append(el("div", "big", String(review)), what, go);
  } else {
    const what = el("div", "what"); what.append(el("b", null, "All caught up"), el("span", "muted small", s.not_ready ? s.not_ready + " more getting ready." : "New ones are prepared overnight."));
    hero.append(icon("check"), what);
    if (s.not_ready && !s.progress.running) {
      const b = el("button", "btn ghost sm"); b.append(icon("sparkle"), el("span", null, "Get them ready now"));
      b.onclick = () => send("api/jobs/ready", "POST").then(() => { toast("Getting them ready."); setTimeout(() => loadJobs(true), 600); }, e => toast(e.message, true));
      hero.append(b);
    }
  }
  rc.append(hero);
  left.append(rc);

  // 3. approved, waiting to be sent from the browser
  if (s.approved) {
    const c = el("div", "tcard"), a = el("a", "btn primary sm"); a.append(el("span", null, "Apply to approved"), icon("ext"));
    a.target = "_blank"; a.rel = "noopener noreferrer"; a.style.display = "none";
    api("api/jobs/next").then(n => { if (n.job && n.job.apply_url.startsWith("https://")) { a.href = n.job.apply_url + "#agent-auto"; a.style.display = ""; } }, () => {});
    c.append(el("h2", null, "Ready to send"), trow(s.approved + (s.approved === 1 ? " approved application" : " approved applications"), "The autofill opens each one, fills it and submits it after a 3-second countdown.", a));
    left.append(c);
  }

  // 4. agency jobs to apply to by hand
  const agency = (s.agency_ready || []).length;
  if (agency) {
    const c = el("div", "tcard"), b = el("button", "btn ghost sm", "Open");
    b.onclick = () => { jfilter = "agency"; store("jfilter", "agency"); listSig = ""; showView("jobs"); };
    c.append(el("h2", null, "Staffing agencies"), trow(agency + " ready to apply by hand", "Resume and cover letter ready; you apply on the agency's site.", b));
    left.append(c);
  }

  // right column: search progress and the week
  if (s.progress.running) {
    const c = el("div", "tcard"), bar = el("div", "pbar"), i = el("i");
    i.style.width = (s.progress.total ? s.progress.done / s.progress.total * 100 : 5) + "%"; bar.append(i);
    c.append(el("h2", null, "Searching now"), el("div", "small", s.progress.step), bar);
    right.append(c);
  }
  if (r) {
    const f = r.found, fw = r.funnel.week, c = el("div", "tcard"), k = el("div", "kpis");
    for (const [v, label] of [[f.total, "Jobs found"], [f.good, "Good matches"], [fw.applied, "Applied"], [fw.replied, "Replies"]]) { const d = el("div"); d.append(el("b", null, String(v)), el("span", null, label)); k.append(d); }
    c.append(el("h2", null, "Last 7 days"), k);
    const tips = r.suggestions.filter(x => !/^Success-rate advice/.test(x.title)).slice(0, 2);
    for (const t of tips) c.append(trow(t.title, t.evidence));
    const more = el("button", "btn ghost sm", "Weekly report"); more.style.marginTop = "10px";
    more.onclick = () => showView("report");
    c.append(more);
    right.append(c);
  }
  if (!alertsOn && alertsState !== "checking") {
    const c = el("div", "tcard"), b = el("button", "btn primary sm", "Turn on alerts");
    b.onclick = turnOnAlerts;
    c.append(el("h2", null, "Alerts"), trow("Alerts are off on this device", alertsHint() || "Get assessments, interviews and codes as they arrive.", alertsState === "off" ? b : null));
    right.append(c);
  }
}

// ---------- inbox ----------
const MAIL_KIND = {assessment: ["warn", "Assessment"], assessment_done: ["good", "Assessment submitted"], interview: ["good", "Interview request"], scheduled: ["good", "Interview scheduled"],
  offer: ["good", "Offer"], action: ["warn", "Action needed"], outreach: ["acc", "Recruiter"], rejection: ["bad", "Rejection"],
  confirmation: ["acc", "Application received"], verification: ["warn", "Verification"], other: ["", "Other"]};
let inboxGmail = false, inboxAddr = "";
let inboxSig = "", ifilter = "all";
function gmailLink(m){
  // the email itself in Gmail, found by its Message-ID; nothing else from the email becomes a URL
  if (!inboxGmail || !m.msgid) return null;
  const a = el("a", "btn ghost sm"); a.append(icon("ext"), el("span", null, "Open in Gmail"));
  a.href = "https://mail.google.com/mail/?authuser=" + encodeURIComponent(inboxAddr) + "#search/" + encodeURIComponent("rfc822msgid:" + m.msgid);
  a.target = "_blank"; a.rel = "noopener noreferrer";
  return a;
}
function mailCard(m, withJob){
  const c = el("div", "mail");
  const top = el("div", "chips");
  const k = MAIL_KIND[m.kind] || ["", m.kind];
  top.append(el("span", "chip " + k[0], k[1]));
  if (withJob && (m.company || m.ai_company)) top.append(el("span", "chip", m.company || m.ai_company));
  if (m.due) top.append(el("span", "chip warn", "Due " + m.due));
  if (m.when) top.append(el("span", "chip good", m.when));
  c.append(top, el("div", "mt", m.subject || "(no subject)"), el("div", "muted small", [m.from, when(m.date), withJob && m.title ? m.title : ""].filter(Boolean).join(" · ")));
  if (m.summary) { const sm = el("div", "small", m.summary); sm.style.marginTop = "6px"; c.append(sm); }
  const row = el("div", "chips"); row.style.marginTop = "10px";
  const g = gmailLink(m); if (g) row.append(g);
  if (m.open || m.done) {
    const b = el("button", "btn sm " + (m.open ? "good" : "ghost"), m.open ? "Done" : "Put back on Needs you");
    b.onclick = () => send("api/inbox/done", "POST", {uid: m.uid, done: !!m.open}).then(() => { inboxSig = ""; loadInbox(); }, e => toast(e.message, true));
    row.append(b);
  }
  if (row.childNodes.length) c.append(row);
  if (m.code) {
    c.append(el("div", "code", m.code));
    const b = copyBtn(m.code); b.lastChild.textContent = "Copy code"; c.append(b);
  }
  if (m.snippet) { const d = el("details"); d.style.marginTop = "6px"; d.append(el("summary", null, "Email text"), pre(m.snippet)); c.append(d); }
  return c;
}
function inboxSetup(box){
  const c = el("div", "card");
  const h = el("div"); h.style.cssText = "display:flex;align-items:center;gap:10px;margin-bottom:8px";
  const lg = el("div", "logo"); lg.append(icon("mail")); h.append(lg, el("b", null, "Connect the agent's inbox"));
  c.append(h, el("div", "muted small", "An address just for applications. Every 5 minutes the agent reads new mail without marking it read: confirmations mark jobs Applied, interview requests and rejections move them along, and verification codes show up here and as alerts. Links in emails are never opened. Use an app password: Gmail › Google Account › Security › App passwords. Your profile's email becomes this address."));
  const addr = el("input", "inp"); addr.type = "email"; addr.placeholder = "you.applications@gmail.com"; addr.autocomplete = "off";
  const pw = el("input", "inp"); pw.type = "password"; pw.placeholder = "App password"; pw.autocomplete = "new-password";
  const f1 = el("div", "field"); f1.append(el("label", null, "Address"), addr);
  const f2 = el("div", "field"); f2.append(el("label", null, "App password"), pw);
  f1.style.marginTop = "14px";
  const b = el("button", "btn primary", "Connect");
  b.onclick = async () => {
    b.disabled = true; b.textContent = "Checking the login...";
    try { await send("api/inbox", "POST", {address: addr.value, password: pw.value}); pw.value = ""; toast("Inbox connected"); inboxSig = ""; loadInbox(); }
    catch (e) { toast(e.message, true); }
    b.disabled = false; b.textContent = "Connect";
  };
  c.append(f1, f2, b);
  box.append(c);
}
async function loadInbox(){
  let s;
  try { s = await api("api/inbox"); } catch (e) { return; }
  const needs = (s.messages || []).filter(m => m.open);
  badge("inbox", needs.length, needs.some(m => m.kind === "assessment" || m.kind === "action"));
  badge("today", needs.length, true);
  inboxGmail = !!s.gmail; inboxAddr = s.address || "";
  INBOX = s;
  if (view === "today") renderToday();
  if (view !== "inbox") return;
  const sig = JSON.stringify(s) + ifilter + alertsState;
  if (sig === inboxSig) return;
  inboxSig = sig;
  const box = $("ibody"); box.replaceChildren();
  $("icheck").style.display = $("ioff").style.display = s.configured ? "" : "none";
  $("ifilterbar").style.display = s.configured ? "" : "none";
  if (!s.configured) { $("imeta").textContent = ""; return inboxSetup(box); }
  $("imeta").textContent = s.address + " · checked " + (when(s.checked) || "not yet");
  if (s.error) box.append(el("div", "chip bad", s.error));
  const hint = alertsHint();
  if (hint) {
    const c = el("div", "card"); c.style.cssText = "display:flex;gap:12px;align-items:center;margin-bottom:12px;padding:12px 14px";
    const t = el("div", "small"); t.style.flex = "1"; t.append(el("b", null, "Alerts are off. "), document.createTextNode(hint));
    c.append(icon("bell"), t);
    if (alertsState === "off" && !(/iPhone|iPad/.test(navigator.userAgent) && !standalone())) { const b = el("button", "btn primary sm", "Turn on"); b.onclick = turnOnAlerts; c.append(b); }
    box.append(c);
  }
  if (needs.length) {
    const h = el("div", "sech", "Needs you"); h.append(el("span", "chip warn", String(needs.length)));
    box.append(h);
    for (const m of needs) box.append(mailCard(m, true));
    const all = el("div", "sech", "All email"); all.style.marginTop = "22px"; box.append(all);
  }
  const f = $("ifilter"); f.replaceChildren();
  const cnt = {all: s.messages.length};
  const tab = k => k === "assessment_done" ? "assessment" : k;  // submitted ones sit with their assessments
  for (const m of s.messages) cnt[tab(m.kind)] = (cnt[tab(m.kind)] || 0) + 1;
  for (const [k, label] of [["all", "All"], ["assessment", "Assessments"], ["interview", "Interviews"], ["scheduled", "Scheduled"], ["offer", "Offers"], ["action", "Action needed"], ["outreach", "Recruiters"], ["verification", "Codes"], ["confirmation", "Received"], ["rejection", "Rejections"], ["other", "Other"]]) {
    const b = el("button", k === ifilter ? "on" : ""); b.append(document.createTextNode(label));
    if (cnt[k]) b.append(el("span", "n", String(cnt[k])));
    b.onclick = () => { ifilter = k; inboxSig = ""; loadInbox(); };
    f.append(b);
  }
  const list = s.messages.filter(m => ifilter === "all" || tab(m.kind) === ifilter);
  if (!list.length) box.append(emptyState("inbox", "No emails here", "Emails about your applications show up here."));
  for (const m of list) box.append(mailCard(m, true));
}
$("icheck").onclick = async () => {
  $("icheck").disabled = true;
  try { await send("api/inbox/check", "POST"); toast("Checking. New emails show up here as the model reads them."); } catch (e) { toast(e.message, true); }
  setTimeout(() => { $("icheck").disabled = false; inboxSig = ""; loadInbox(); }, 4000);
};
$("ioff").onclick = async () => {
  if (!confirm("Disconnect the inbox? The saved login and the email list are deleted from the server.")) return;
  await fetch("api/inbox", {method: "DELETE"}); inboxSig = ""; loadInbox();
};

// ---------- you: profile and memory ----------
function showYou(sub){
  store("yousub", sub);
  for (const b of $("yousub").children) b.classList.toggle("on", b.dataset.s === sub);
  $("y-profile").style.display = sub === "profile" ? "" : "none";
  $("y-memory").style.display = sub === "memory" ? "" : "none";
  $("y-rules").style.display = sub === "rules" ? "" : "none";
  $("y-workday").style.display = sub === "workday" ? "" : "none";
  $("y-app").style.display = sub === "app" ? "" : "none";
  if (sub === "profile") loadProfile(); else if (sub === "rules") loadRules(); else if (sub === "workday") loadWorkday(); else if (sub === "app") paintApp(); else loadMemory();
}
for (const b of $("yousub").children) b.onclick = () => showYou(b.dataset.s);
let profileDirty = false;
function pInput(value, choices, multi){
  let input;
  if (choices) {
    input = el("select", "inp");
    const opts = [""].concat(choices);
    if (value && !choices.includes(value)) opts.push(value);
    for (const c of opts) { const o = el("option", null, c || "Choose..."); o.value = c; input.append(o); }
  } else if (multi) input = el("textarea", "inp");
  else { input = el("input", "inp"); input.autocomplete = "off"; }
  input.value = value || "";
  return input;
}
function pField(label, input, help, wide, from){
  const f = el("div", "field pfield" + (wide ? " wide" : ""));
  const l = el("label", null, label);
  if (from) l.append(el("span", "from", from));
  f.append(l, input);
  if (help) f.append(el("div", "help", help));
  input.addEventListener("input", () => { profileDirty = true; $("pdirty").textContent = ""; progress(); saveSoon(1200); });
  input.addEventListener("change", () => { profileDirty = true; progress(); saveSoon(0); });
  return f;
}
// The profile saves itself: a moment after typing stops, or when a field is left
let saveTimer = null, saving = Promise.resolve();
function saveSoon(ms){
  clearTimeout(saveTimer);
  saveTimer = setTimeout(() => { saving = saving.then(saveProfile); }, ms);
}
async function saveProfile(){
  const values = {}, answers = [];
  for (const i of $("pform").querySelectorAll("[data-key]")) values[i.dataset.key] = i.value.trim();
  for (const i of $("pform").querySelectorAll("[data-q]")) if (i.value.trim()) answers.push({q: i.dataset.q, a: i.value.trim()});
  $("pdirty").textContent = "Saving...";
  try { await send("api/jobs/profile", "PUT", {values, answers}); }
  catch (e) { $("pdirty").textContent = ""; return toast("Couldn't save: " + e.message, true); }
  profileDirty = false;
  $("pdirty").textContent = "Saved \u2713";
}
function progress(){
  const all = [...$("pform").querySelectorAll("[data-key]")];
  const filled = all.filter(i => i.value.trim()).length;
  $("pbar").style.width = (all.length ? Math.round(filled / all.length * 100) : 0) + "%";
  $("pstat").textContent = filled + " of " + all.length + " filled";
}
async function loadProfile(){
  if (profileDirty) return;
  let p;
  try { p = await api("api/jobs/profile"); } catch (e) { return; }
  const box = $("pform"), jump = $("pjump");
  box.replaceChildren(); jump.replaceChildren();
  const sections = p.form.map(s => [s.section, s.fields]);
  // what's still empty comes first, in one place; each field appears once
  const gaps = el("div", "gaps"), ggrid = el("div", "pgrid");
  let gapCount = 0, n = 0;
  for (const [name, fields] of sections) for (const f of fields) if (!String(p.profile[f.key] || "").trim()) {
    const input = pInput(p.profile[f.key], f.choices, false);
    input.dataset.key = f.key;
    ggrid.append(pField(f.label, input, f.help, (f.help || "").length > 70, name));
    gapCount++;
  }
  if (gapCount) { const h = el("h3", null, "Still empty"); h.append(el("span", "chip warn", String(gapCount))); const note = el("div", "muted small", "Forms ask for these. Fill what applies; leave the rest."); note.style.marginBottom = "14px"; gaps.append(h, note, ggrid); box.append(gaps); }
  for (const [name, fields] of sections) {
    const filled = fields.filter(f => String(p.profile[f.key] || "").trim());
    if (!filled.length) continue;
    const sec = el("details", "psec"); sec.id = "ps" + (n++);
    const sm = el("summary", null, name); sm.append(el("span", "cnt", filled.length + " of " + fields.length));
    sec.append(sm);
    const grid = el("div", "pgrid");
    for (const f of filled) {
      const input = pInput(p.profile[f.key], f.choices, false);
      input.dataset.key = f.key;
      grid.append(pField(f.label, input, f.help, (f.help || "").length > 70));
    }
    sec.append(grid); box.append(sec);
    const j = el("button", "chip", name); j.onclick = () => { sec.open = true; sec.scrollIntoView({behavior: "smooth", block: "start"}); }; jump.append(j);
  }
  const extra = [["Questions it still can't answer", p.unanswered, "q"], ["Your saved answers", p.answers, "a"]];
  for (const [name, items, kind] of extra) {
    const sec = el("div", "psec"); sec.id = "ps" + (n++);
    const h = el("h3", null, name); h.append(el("span", "chip" + (kind === "q" && items.length ? " warn" : ""), String(items.length)));
    sec.append(h);
    if (kind === "q") sec.append(el("div", "muted small", items.length ? "From the " + p.prepared_jobs + " jobs with prepared answers, most common first. Answer once and every form that asks gets it. Leave empty to skip." : "None right now. Questions show up here as jobs get prepared."));
    if (kind === "a" && !items.length) sec.append(el("div", "muted small", "None yet. Clear one to remove it."));
    const grid = el("div"); grid.style.marginTop = "12px";
    for (const u of items) {
      if (kind === "q") {
        const input = pInput("", u.options.length && u.options.length <= 12 ? u.options : null, !u.options.length);
        input.dataset.q = u.q;
        const where = u.jobs + (u.jobs === 1 ? " job" : " jobs") + ": " + u.companies.join(", ");
        grid.append(pField(u.q, input, where + (u.options.length > 12 ? ". Choices include " + u.options.slice(0, 6).join(", ") : ""), true));
      } else {
        const input = pInput(u.a, null, true); input.dataset.q = u.q;
        grid.append(pField(u.q, input, "", true));
      }
    }
    sec.append(grid); box.append(sec);
    const j = el("button", "chip" + (kind === "q" && items.length ? " warn" : ""), kind === "q" ? "Open questions" : "Saved answers");
    j.onclick = () => sec.scrollIntoView({behavior: "smooth", block: "start"}); jump.append(j);
  }
  $("pdirty").textContent = "";
  progress();
}
window.addEventListener("pagehide", () => { if (profileDirty) { clearTimeout(saveTimer); saveProfile(); } });
// ---------- you: hiring rules ----------
const ACTION_TXT = {block: "Block", ask: "Ask", warn: "Warn", off: "Off"};
function kvRow(box, cells){
  const row = el("div", "kv" + (cells.length === 2 ? " two" : ""));
  const inputs = cells.map(([v, ph, type]) => { const i = el("input", "inp"); i.value = v ?? ""; i.placeholder = ph; if (type) { i.type = type; i.min = "1"; } i.oninput = () => { $("rdirty").textContent = "Unsaved changes"; }; return i; });
  const x = el("button", "btn ghost icon sm", "×"); x.setAttribute("aria-label", "Remove"); x.onclick = () => { row.remove(); $("rdirty").textContent = "Unsaved changes"; };
  row.append(...inputs, x); box.append(row);
}
async function loadRules(){
  let d;
  try { d = await api("api/jobs/rules"); } catch (e) { return toast(e.message, true); }
  const st = d.settings, list = $("rlist"); list.replaceChildren();
  for (const r of d.rules) {
    const row = el("div", "rrow");
    const t = el("div"); t.append(el("b", null, r.title), el("div", "help", r.help));
    const sel = el("select", "inp"); sel.dataset.rule = r.id;
    for (const a of ["block", "ask", "warn", "off"]) { const o = el("option", null, ACTION_TXT[a] + (a === r.default ? " (default)" : "")); o.value = a; sel.append(o); }
    sel.value = st.actions[r.id] || r.default;
    sel.onchange = () => { $("rdirty").textContent = "Unsaved changes"; };
    row.append(t, sel); list.append(row);
  }
  $("rmax").value = st.limit.max; $("rdays").value = st.limit.days; $("rcool").value = st.cooldown_days;
  for (const i of ["rmax", "rdays", "rcool"]) $(i).oninput = () => { $("rdirty").textContent = "Unsaved changes"; };
  $("rlimits").replaceChildren();
  for (const [k, v] of Object.entries(st.limits)) kvRow($("rlimits"), [[k, "Company"], [v.max, "Max", "number"], [v.days, "Days", "number"]]);
  $("rnotes").replaceChildren();
  for (const [k, v] of Object.entries(st.notes)) kvRow($("rnotes"), [[k, "Company"], [v, "Referred by Jane Doe / submitted by an agency"]]);
  $("rdirty").textContent = "";
}
$("raddlimit").onclick = () => kvRow($("rlimits"), [["", "Company"], [3, "Max", "number"], [30, "Days", "number"]]);
$("raddnote").onclick = () => kvRow($("rnotes"), [["", "Company"], ["", "Referred by Jane Doe / submitted by an agency"]]);
$("rsave").onclick = async () => {
  const actions = {}, limits = {}, notes = {};
  for (const s of $("rlist").querySelectorAll("select")) actions[s.dataset.rule] = s.value;
  for (const r of $("rlimits").children) { const [c, m, dd] = r.querySelectorAll("input"); if (c.value.trim()) limits[c.value.trim()] = {max: +m.value || 3, days: +dd.value || 30}; }
  for (const r of $("rnotes").children) { const [c, n] = r.querySelectorAll("input"); if (c.value.trim() && n.value.trim()) notes[c.value.trim()] = n.value.trim(); }
  try { await send("api/jobs/rules", "PUT", {actions, limits, notes, limit: {max: +$("rmax").value || 3, days: +$("rdays").value || 30}, cooldown_days: +$("rcool").value || 180}); }
  catch (e) { return toast(e.message, true); }
  toast("Rules saved"); loadRules(); listSig = "";
};

// ---------- you: Workday ----------
async function loadWorkday(){
  let w;
  try { w = await api("api/workday"); } catch (e) { return toast(e.message, true); }
  const st = $("wdstate"); st.replaceChildren(
    el("span", "chip " + (w.has_password ? "good" : "warn"), w.has_password ? "Password set" : "No password yet"),
    el("span", "chip", w.accounts.length + (w.accounts.length === 1 ? " account" : " accounts") + " created"));
  $("wdterms").checked = w.accept_terms;
  $("wdpw").value = ""; $("wddirty").textContent = "";
}
$("wdgen").onclick = () => {
  const sets = ["ABCDEFGHJKLMNPQRSTUVWXYZ", "abcdefghijkmnopqrstuvwxyz", "23456789", "!@#$%^*-_=+"];
  const all = sets.join(""), rnd = n => { const a = new Uint32Array(1); crypto.getRandomValues(a); return a[0] % n; };
  let pw = sets.map(x => x[rnd(x.length)]);
  while (pw.length < 18) pw.push(all[rnd(all.length)]);
  for (let i = pw.length - 1; i > 0; i--) { const j = rnd(i + 1); [pw[i], pw[j]] = [pw[j], pw[i]]; }
  pw = pw.join("");
  $("wdpw").value = pw; $("wdpw").type = "text";
  $("wdgenhelp").textContent = "Save this in your password manager now; it isn't shown again after you save: " + pw;
  $("wddirty").textContent = "Unsaved changes";
};
$("wdpw").oninput = $("wdterms").onchange = () => { $("wddirty").textContent = "Unsaved changes"; };
$("wdsave").onclick = async () => {
  const body = {accept_terms: $("wdterms").checked};
  if ($("wdpw").value) body.password = $("wdpw").value;
  try { await send("api/workday", "PUT", body); } catch (e) { return toast(e.message, true); }
  $("wdpw").type = "password"; $("wdgenhelp").textContent = "Generate makes a strong one and shows it once so you can save it in your password manager.";
  toast("Saved"); loadWorkday();
};

let memMax = 2000;
function memCount(){ const n = $("memtext").value.length; $("memcount").textContent = n + " / " + memMax + " characters"; }
async function loadMemory(){
  try { const m = await api("api/memory"); $("memtext").value = m.text; memMax = m.max; } catch (e) {}
  memCount();
}
$("memtext").oninput = memCount;
$("memsave").onclick = async () => {
  try { await send("api/memory", "PUT", {text: $("memtext").value}); toast("Memory saved"); } catch (e) { toast(e.message, true); }
};

// ---------- start ----------
if ("serviceWorker" in navigator) navigator.serviceWorker.addEventListener("message", e => { if (e.data && e.data.view) showView(e.data.view); });
window.addEventListener("hashchange", () => { const v = location.hash.slice(1); if (v && (OLD[v] || v) !== view) showView(v); });
setInterval(() => { if (view === "jobs" || view === "today") loadJobs(); }, 5000);
setInterval(() => { if (view !== "jobs" && view !== "today") loadJobs(); loadInbox(); }, 60000);
setInterval(() => { if (view === "inbox" || view === "today") loadInbox(); }, 15000);
setInterval(() => { if (view === "today") loadTodayReport(); }, 600000);
Object.assign(RELOAD, {"api/jobs": () => { listSig = ""; loadJobs(true); }, "api/inbox": () => { inboxSig = ""; loadInbox(); },
                       "api/applications": () => { if (view === "apps") loadApps(); }, "api/report": () => loadTodayReport(true)});
let startView = location.hash.slice(1) || "today";  // the panel opens on Today
showView(startView);
setInterval(poll, 1500);
poll();
loadJobs();
loadInbox();
setupAlerts();
</script>
</body></html>
"""

# Installed in Sai's browser (Userscripts on iPhone Safari, Tampermonkey or Userscripts on
# the Mac). On any form it fills what it can and leaves the rest to Sai. Applications Sai
# approved in the panel it also submits, but only when opened from "Apply to approved"
# (#agent-auto): it ticks the consents he agreed to, checks every required field is
# filled, clicks Submit after a countdown he can stop, waits for the confirmation and
# opens the next one. CAPTCHA challenges stay with Sai. Page text is only ever set with
# textContent.
FILL_SCRIPT = r"""// ==UserScript==
// @name         Agent application autofill
// @namespace    local-agent
// @version      15
// @description  Fills job application forms from the agent panel, and submits the ones you approved there.
// @match        https://job-boards.greenhouse.io/*
// @match        https://boards.greenhouse.io/*
// @match        https://jobs.lever.co/*
// @match        https://jobs.ashbyhq.com/*
// @match        https://*.myworkdayjobs.com/*
// @grant        GM.xmlHttpRequest
// @grant        GM_xmlhttpRequest
// @connect      __HOST__
// @updateURL    __PANEL__/jobs-fill.user.js
// @downloadURL  __PANEL__/jobs-fill.user.js
// ==/UserScript==
(function () {
  "use strict";
  const PANEL = "__PANEL__";
  const KIND = {legal: "Read and answer yourself", eeo: "Voluntary, your choice", you: "Answer yourself",
                file: "Attach the file yourself", draft: "No draft for this one, answer yourself"};
  const COLORS = {filled: "#2f9e62", review: "#2f6fd6", you: "#e08a1e"};
  const AGREE_RE = /^(yes|i agree|agree|i acknowledge|acknowledge|i confirm|confirm|i have read|i accept|accept|i understand|i certify|i consent|consent)/i;
  const SUCCESS_RE = /thank(s| you) for (applying|your application|submitting)|application (has been |was )?(submitted|received)|we('ve| have) received your application|successfully (submitted|applied)/i;
  const AUTO_KEY = "agent-auto", SENT_KEY = "agent-submitted";
  const store = {  // this tab only; the next job's link carries #agent-auto across sites
    get: k => { try { return sessionStorage.getItem(k); } catch (e) { return null; } },
    set: (k, v) => { try { sessionStorage.setItem(k, v); } catch (e) {} },
    del: k => { try { sessionStorage.removeItem(k); } catch (e) {} },
  };
  if (location.hash.includes("agent-auto")) store.set(AUTO_KEY, "1");
  let stopped = false;
  const gmx = (typeof GM !== "undefined" && GM.xmlHttpRequest) ? GM.xmlHttpRequest.bind(GM)
            : (typeof GM_xmlhttpRequest !== "undefined" ? GM_xmlhttpRequest : null);
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const clean = t => (t || "").replace(/[*✱]/g, "").replace(/\s+/g, " ").trim();
  // Forms spell some answers differently: "United States" is "US" on Stripe's form.
  const ALIASES = {"united states": "us", "united states of america": "us", "usa": "us", "u s": "us", "u s a": "us",
                   "united kingdom": "uk", "great britain": "uk"};
  const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
  const alias = s => ALIASES[norm(s)] || norm(s);

  function api(method, path, body) {
    return new Promise((resolve, reject) => {
      if (!gmx) return reject(new Error("This userscript manager can't make cross-site requests."));
      gmx({
        method, url: PANEL + path, timeout: 20000,
        headers: {"Content-Type": "application/json"},
        data: body ? JSON.stringify(body) : undefined,
        onload: r => (r.status >= 200 && r.status < 300)
          ? resolve(JSON.parse(r.responseText)) : reject(new Error("The panel answered " + r.status)),
        onerror: () => reject(new Error("Can't reach the panel. Is Tailscale on?")),
        ontimeout: () => reject(new Error("The panel didn't answer in time.")),
      });
    });
  }

  const byIds = ids => clean((ids || "").split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(n => n.innerText).join(" "));

  // The question text for a field: the nearest label-like element around it that
  // holds no inputs of its own (so a "No" radio label never names the question).
  function questionLabel(el) {
    for (let p = el.parentElement, k = 0; p && k < 7; p = p.parentElement, k++) {
      const t = byIds(p.getAttribute("aria-labelledby"));
      if (t) return t;
      const l = [...p.children].find(c => !c.contains(el) && !c.querySelector("input, select, textarea")
        && c.matches("legend, label, .application-label, [class*=label], [class*=Label], [class*=heading], [class*=title]")
        && clean(c.innerText));
      if (l) return clean(l.innerText);
    }
    return "";
  }

  function labelFor(el) {
    if (el.labels && el.labels.length && clean(el.labels[0].innerText)) return clean(el.labels[0].innerText);
    const t = byIds(el.getAttribute("aria-labelledby")) || clean(el.getAttribute("aria-label"));
    return t || questionLabel(el) || clean(el.placeholder || el.name || "");
  }

  // Upload inputs are usually labeled by their button ("Attach"); use the question's label.
  function fileLabel(el) {
    const own = labelFor(el);
    if (!/^(attach|upload|browse|choose file|select file)?$/i.test(own)) return own;
    return questionLabel(el) || (el.id || el.name || "").replace(/[_-]+/g, " ");
  }

  // Ashby puts an "Autofill from resume" upload above the form. A file there makes Ashby
  // refill the form from the resume after we've filled it, wiping our answers, so it's
  // skipped whenever the form has another upload for the resume itself.
  function autofillUpload(el) {
    if (document.querySelectorAll("input[type=file]").length < 2) return false;
    for (let p = el.parentElement, k = 0; p && k < 5; p = p.parentElement, k++) {
      if (p.querySelectorAll("input[type=file]").length > 1) break;  // reached the whole form
      if (/autofill/i.test((p.innerText || "").slice(0, 300))) return true;
    }
    return false;
  }

  // Fields for robots only (Workday's "beecatcher": "This input is for robots only, do
  // not fill"). Filling one marks the application as a bot's, so they're never touched.
  function trapField(el) {
    const ids = [el.getAttribute("data-automation-id"), el.name, el.id].join(" ");
    if (/beecatcher|honeypot|\bhp[_-]|bot.?field/i.test(ids)) return true;
    const lab = (el.getAttribute("aria-label") || "") + " " + (el.labels && el.labels[0] ? el.labels[0].innerText : "");
    if (/robots|do not fill|don.t fill|leave (this|it)? ?(field )?(blank|empty)/i.test(lab)) return true;
    if (!["file", "radio", "checkbox", "hidden"].includes((el.type || "").toLowerCase()) && el.offsetParent) {
      const r = el.getBoundingClientRect();
      if (r.width * r.height < 4) return true;  // a 1-pixel box nobody can see or type in
    }
    return false;
  }

  function collect() {
    const fields = [], els = [], radios = new Set();
    for (const el of document.querySelectorAll("input, textarea, select")) {
      const type = (el.type || "").toLowerCase();
      if (trapField(el)) continue;
      if (WD && el.closest('[data-agent-history], [data-automation-id="activeListContainer"]')) continue;  // work history (wdExperience()) and open search results
      if (el.disabled || ["hidden", "submit", "button", "image", "reset", "password", "search"].includes(type)) continue;
      if (type === "checkbox") {
        // single boxes only (consents, opt-ins); groups of choices are left to Sai
        if (el.name && [...document.querySelectorAll("input[type=checkbox]")].filter(c => c.name === el.name).length > 1) continue;
        const own = labelFor(el), q = questionLabel(el);
        fields.push({label: own.length < 30 && q && q !== own ? q + " " + own : own, type: "checkbox", options: []});
        els.push(el);
        continue;
      }
      if (type === "file") {
        if (autofillUpload(el)) continue;  // the form's own resume parser, not the resume field
        fields.push({label: fileLabel(el), type: "file", options: [], name: el.id || el.name || ""}); els.push(el); continue;
      }
      if (el.getAttribute("aria-hidden") === "true" || el.tabIndex < 0) continue;  // validation helpers
      if (type === "radio") {
        if (!el.name || radios.has(el.name)) continue;
        radios.add(el.name);
        const group = [...document.querySelectorAll("input[type=radio]")].filter(r => r.name === el.name);
        fields.push({label: questionLabel(el), type: "radio", options: group.map(r => labelFor(r))});
        els.push(group);
        continue;
      }
      if (!el.offsetParent) continue;  // not shown
      const combo = el.getAttribute("role") === "combobox";
      fields.push({
        label: labelFor(el),
        type: el.tagName === "TEXTAREA" ? "textarea" : el.tagName === "SELECT" ? "select" : combo ? "combobox" : "text",
        options: el.tagName === "SELECT" ? [...el.options].map(o => o.text.trim()) : [],
      });
      els.push(el);
    }
    if (WD) {  // Workday's dropdowns are buttons that open a list
      for (const b of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
        if (!b.offsetParent || b.closest("[data-agent-history]")) continue;
        fields.push({label: wdLabel(b), type: "wdselect", options: []});
        els.push(b);
      }
    }
    return {fields, els};
  }

  function setValue(el, v) {
    const proto = el.tagName === "TEXTAREA" ? HTMLTextAreaElement.prototype
                : el.tagName === "SELECT" ? HTMLSelectElement.prototype : HTMLInputElement.prototype;
    Object.getOwnPropertyDescriptor(proto, "value").set.call(el, v);  // works with React forms
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
  }

  function pick(options, value) {
    if (!norm(value)) return -1;
    for (const f of [norm, alias]) {  // as written first, then "United States" as "US"
      const v = f(value);
      let i = options.findIndex(o => f(o) === v);
      if (i < 0) i = options.findIndex(o => { const n = f(o); return n && (v.startsWith(n + " ") || n.startsWith(v + " ")); });
      if (i >= 0) return i;
    }
    return -1;
  }

  // Option index for a dropdown answer. Location searches also accept the first word
  // ("Albany, NY" takes "Albany, New York, United States"); other dropdowns, like
  // schools, need a real match or are left for Sai.
  function pickLoose(labels, value, loose) {
    let i = pick(labels, value);
    const first = norm(value).split(" ")[0];
    if (i < 0 && loose) i = labels.findIndex(l => first && norm(l).startsWith(first));
    return i;
  }

  // react-select dropdowns (Greenhouse, Ashby) ignore scripted typing, and userscripts
  // usually run in an isolated world that can't see page components. This helper runs
  // in the page instead: it finds the component, starts its search and selects the
  // option. The two sides talk through attributes on the input.
  function pageHelper() {
    if (document.documentElement.hasAttribute("data-agent-helper")) return;
    document.documentElement.setAttribute("data-agent-helper", "1");
    const ALIASES = {"united states": "us", "united states of america": "us", "usa": "us", "u s": "us", "u s a": "us",
                     "united kingdom": "uk", "great britain": "uk"};
    const norm = s => (s || "").toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
    const alias = s => ALIASES[norm(s)] || norm(s);
    const find = (raw, value, loose) => {  // as written first, then "United States" as "US"
      for (const f of [norm, alias]) {
        const labels = raw.map(f), v = f(value), first = v.split(" ")[0];
        let i = labels.findIndex(l => l === v);
        if (i < 0) i = labels.findIndex(l => l && (v.startsWith(l + " ") || l.startsWith(v + " ")));
        if (i < 0 && loose) i = labels.findIndex(l => first && l.startsWith(first));
        if (i >= 0) return i;
      }
      return -1;
    };
    const comp = el => {
      const fk = Object.keys(el).find(k => k.startsWith("__reactFiber"));
      for (let f = fk && el[fk], k = 0; f && k < 40; k++, f = f.return)
        if (f.stateNode && typeof f.stateNode.selectOption === "function") return f.stateNode;
      return null;
    };
    document.addEventListener("agent-fill-combo", async e => {
      const el = e.target, value = el.getAttribute("data-agent-fill") || "";
      const loose = el.getAttribute("data-agent-loose") === "1";
      let inst = comp(el);
      if (!inst) return el.setAttribute("data-agent-fill-result", "none");
      const labelOf = o => String((inst.props.getOptionLabel ? inst.props.getOptionLabel(o) : o.label) || "");
      if (typeof inst.props.loadOptions === "function") {
        // search-as-you-type lists (Greenhouse school, degree, discipline) load options
        // only when opened; ask the list's own loader, with the full answer and its first part
        for (const q of [...new Set([value, value.split(/,| - /)[0].trim()])]) {
          try {
            const r = await inst.props.loadOptions(q, [], {page: 1});
            const opts = (r && r.options) || [];
            const i = find(opts.map(labelOf), value, loose);
            if (i >= 0) { comp(el).selectOption(opts[i]); return el.setAttribute("data-agent-fill-result", "ok"); }
          } catch (err) {}
        }
      }
      if (inst.props.onInputChange) inst.props.onInputChange(value.split(",")[0], {action: "input-change", prevInputValue: ""});
      for (let t = 0; t < 12; t++) {  // options can load from the network
        await new Promise(r => setTimeout(r, 250));
        inst = comp(el);
        const opts = inst.props.options || [];
        const i = find(opts.map(labelOf), value, loose);
        if (i >= 0) { inst.selectOption(opts[i]); return el.setAttribute("data-agent-fill-result", "ok"); }
      }
      el.setAttribute("data-agent-fill-result", "no");
    }, true);
  }

  function injectHelper() {
    if (document.documentElement.hasAttribute("data-agent-helper")) return;
    const s = document.createElement("script");
    s.textContent = "(" + pageHelper.toString() + ")();";
    document.documentElement.append(s);  // blocked on sites whose policy forbids inline scripts
    s.remove();
  }

  async function fillCombo(el, value, loose) {
    injectHelper();
    if (document.documentElement.hasAttribute("data-agent-helper")) {
      el.removeAttribute("data-agent-fill-result");
      el.setAttribute("data-agent-fill", value);
      el.setAttribute("data-agent-loose", loose ? "1" : "0");
      el.dispatchEvent(new CustomEvent("agent-fill-combo", {bubbles: true}));
      for (let t = 0; t < 20; t++) {
        await sleep(250);
        const r = el.getAttribute("data-agent-fill-result");
        if (r === "ok") return true;
        if (r === "no") return false;
        if (r === "none") break;  // not a react-select box: type into it instead
      }
    }
    el.focus();
    setValue(el, value);
    for (let t = 0; t < 12; t++) {
      await sleep(250);
      // only this box's own list: the page can have others, like the phone country list
      const list = document.getElementById(el.getAttribute("aria-controls") || el.getAttribute("aria-owns") || "");
      const opts = list ? [...list.querySelectorAll("[role=option]")] : [];
      const i = pickLoose(opts.map(o => o.innerText), value, loose);
      if (i >= 0) {
        for (const ev of ["mousedown", "mouseup", "click"]) opts[i].dispatchEvent(new MouseEvent(ev, {bubbles: true}));
        return true;
      }
    }
    el.blur();
    return false;
  }

  // ---------- Workday ----------
  const WD = /\.myworkdayjobs\.com$/.test(location.hostname);
  const wdq = id => document.querySelector('[data-automation-id="' + id + '"]');
  const wdTenant = () => location.hostname.split(".myworkdayjobs.com")[0];
  function wdLabel(el) {
    const box = el.closest('[data-automation-id^="formField-"]');
    const l = box && box.querySelector("label, legend");
    if (l && clean(l.innerText)) return clean(l.innerText);
    return clean((el.getAttribute("aria-label") || "").replace(/select one|required/gi, ""));
  }
  async function wdSelect(btn, value) {
    const cur = clean(btn.innerText);
    if (cur && !/^select one$/i.test(cur) && pick([cur], value) === 0) return true;
    btn.click();
    for (let t = 0; t < 16; t++) {
      await sleep(250);
      const opts = [...document.querySelectorAll('[role="listbox"] [role="option"], [data-automation-id="promptOption"]')].filter(o => o.offsetParent);
      if (!opts.length) continue;
      const i = pick(opts.map(o => clean(o.innerText)), value);
      if (i >= 0) {
        for (const ev of ["mousedown", "mouseup", "click"]) opts[i].dispatchEvent(new MouseEvent(ev, {bubbles: true}));
        await sleep(300);
        return true;
      }
      break;
    }
    document.activeElement && document.activeElement.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
    return false;
  }
  // Workday's search boxes ("How Did You Hear About Us?", skills, schools, fields of
  // study). The search runs only on a full Enter (keydown, keypress and keyup); an
  // exact single match is then chosen by Workday itself, otherwise a result is chosen
  // by ticking its box. "No Items." comes back as a result too and is skipped.
  // Strict (schools, fields of study, skills, certifications): an option counts when it
  // matches as written, or has every distinctive word of the name, with SUNY and CUNY
  // spelled out, so "University at Albany, SUNY" takes "State University of New York at
  // Albany" but never "Albany State University". Two equally good options are left for Sai.
  const WD_STOP = new Set(["university", "of", "at", "the", "college", "institute", "school", "and", "in", "for", "a"]);
  const WD_ABBR = {suny: "state university of new york", cuny: "city university of new york"};
  const expand = s => norm(s).split(" ").map(w => WD_ABBR[w] || w).join(" ");
  function wdAccept(labels, value, strict) {
    if (!strict) return pickLoose(labels, value, true);
    let i = pick(labels, value);
    if (i < 0) i = pick(labels, value.split(",")[0]);
    if (i >= 0) return i;
    const need = [...new Set(expand(value).split(" ").filter(w => w && !WD_STOP.has(w)))];
    if (!need.length) return -1;
    const hits = labels.map((l, k) => [expand(l).split(" "), k]).filter(([ws]) => need.every(w => ws.includes(w)))
      .sort((a, b) => a[0].length - b[0].length);
    if (!hits.length || (hits.length > 1 && hits[0][0].length === hits[1][0].length)) return -1;
    return hits[0][1];
  }
  function wdQueries(value, strict) {
    if (!strict) return [value.split(",")[0].trim()];
    const words = norm(value).split(" ");
    const need = expand(value).split(" ").filter(w => w && !WD_STOP.has(w));
    return [...new Set([value, value.split(",")[0].trim(), ...words.filter(w => WD_ABBR[w]).map(w => WD_ABBR[w]),
                        need.sort((a, b) => b.length - a.length)[0] || ""])].filter(q => q.length > 1);
  }
  const wdEnter = el => ["keydown", "keypress", "keyup"].forEach(t => el.dispatchEvent(new KeyboardEvent(t,
    {key: "Enter", code: "Enter", keyCode: 13, which: 13, bubbles: true, cancelable: true})));
  async function wdMulti(el, value, strict) {
    const wrap = el.closest('[data-automation-id^="formField-"]') || el.parentElement;
    const chosen = () => [...wrap.querySelectorAll('[data-automation-id="selectedItem"]')];
    if (!strict && chosen().length) return true;
    for (const q of wdQueries(value, strict)) {
      const before = chosen().length;
      el.focus();
      setValue(el, q);
      await sleep(150);
      wdEnter(el);
      for (let t = 0; t < 16; t++) {
        await sleep(300);
        const now = chosen();
        if (now.length > before) {  // Workday chose its single exact match itself
          const last = now[now.length - 1];
          if (wdAccept([clean(last.innerText)], value, strict) === 0) return true;
          const del = last.querySelector('[data-automation-id="DELETE_charm"]') || last.parentElement.querySelector('[data-automation-id="DELETE_charm"]');
          if (del) del.click();  // not the one we meant
          await sleep(300);
          break;
        }
        const opts = [...document.querySelectorAll('[data-automation-id="promptOption"]')]
          .filter(o => o.offsetParent && !/^no items\.?$/i.test(clean(o.innerText)));
        const i = wdAccept(opts.map(o => clean(o.innerText)), value, strict);
        if (i >= 0) {
          const leaf = opts[i].closest('[data-automation-id="promptLeafNode"]');
          const box = leaf && leaf.querySelector('input[data-automation-id="checkboxPanel"], input[type=checkbox], input[type=radio]');
          (box || leaf || opts[i]).click();
          for (let k = 0; k < 8 && chosen().length <= before; k++) await sleep(250);
          if (chosen().length > before) { setValue(el, ""); return true; }
          break;
        }
        if (t > 4 && document.querySelector('[data-automation-id="promptOption"]')) break;  // results are in and none fit
      }
      setValue(el, "");
      el.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", code: "Escape", keyCode: 27, bubbles: true}));
      el.blur();  // closes the results list
      await sleep(200);
    }
    return false;
  }
  // A Workday list button, trying each wording in turn ("Master of Science", "Master's
  // Degree", "Masters"...), then the shortest option with the word ("master") in it.
  async function wdSelectAny(btn, values, word) {
    const cur = clean(btn.innerText);
    if (cur && !/^select one$/i.test(cur)) return true;  // chosen already, never changed
    btn.click();
    for (let t = 0; t < 16; t++) {
      await sleep(250);
      const opts = [...document.querySelectorAll('[role="listbox"] [role="option"], [data-automation-id="promptOption"]')].filter(o => o.offsetParent);
      if (!opts.length) continue;
      const labels = opts.map(o => clean(o.innerText));
      let i = -1;
      for (const v of values) { i = pick(labels, v); if (i >= 0) break; }
      if (i < 0 && word) {
        const re = new RegExp("\\b" + word, "i");
        const hits = labels.map((l, k) => [l, k]).filter(([l]) => re.test(l)).sort((a, b) => a[0].length - b[0].length);
        if (hits.length) i = hits[0][1];
      }
      if (i >= 0) {
        for (const ev of ["mousedown", "mouseup", "click"]) opts[i].dispatchEvent(new MouseEvent(ev, {bubbles: true}));
        await sleep(300);
        return true;
      }
      break;
    }
    document.activeElement && document.activeElement.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", bubbles: true}));
    return false;
  }

  // ---------- Workday: My Experience from the resume ----------
  // Workday's "Autofill with Resume" parser gets titles, dates and schools wrong, so jobs
  // open with Apply Manually and the script adds each job, degree, certification,
  // website and skill from resume.json (GET /api/jobs/history). Workday doesn't wrap
  // sections or entries in their own elements: a section is an h4 ("Work Experience"),
  // an entry an h5 ("Work Experience 1"), and an entry's fields are the formField-*
  // elements between its heading and the next one. Fields are matched by their
  // formField id, or else their label, and only empty ones are filled. Every entry
  // field is tagged data-agent-history so run() doesn't put profile answers in them.
  let wdHist = null;
  const HEAD = "h1, h2, h3, h4, h5, h6";
  const level = h => +h.tagName[1];
  const follows = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING);  // b comes after a
  const wdHeads = () => [...document.querySelectorAll(HEAD)].filter(visible);
  const wdSectionHead = name => wdHeads().find(h => norm(h.innerText) === norm(name));
  const wdEntryHeads = name => {
    const re = new RegExp("^" + norm(name).replace(/s$/, "") + "s? \\d+$");
    return wdHeads().filter(h => re.test(norm(h.innerText)));
  };
  // elements matching sel after a heading and before the next heading of its level or above
  function wdUnder(head, sel) {
    const heads = wdHeads(), i = heads.indexOf(head);
    const end = heads.slice(i + 1).find(h => level(h) <= level(head));
    return [...document.querySelectorAll(sel)].filter(e => visible(e) && follows(head, e) && (!end || follows(e, end)));
  }
  const wdAdd = head => wdUnder(head, "button").filter(b => /^add( another)?$/i.test(clean(b.innerText))
    || b.getAttribute("data-automation-id") === "add-button").pop();
  const WD_KEYS = {jobTitle: "title", companyName: "company", location: "location", currentlyWorkHere: "current",
                   startDate: "from", endDate: "to", roleDescription: "description", school: "school", degree: "degree",
                   fieldOfStudy: "field", gradeAverage: "gpa", firstYearAttended: "from", lastYearAttended: "to",
                   url: "url", certification: "name"};
  const WD_LABELS = [[/job title|position/i, "title"], [/company|employer/i, "company"], [/currently work/i, "current"],
    [/location/i, "location"], [/^from|start|first year/i, "from"], [/^to\b|end date|last year|graduat/i, "to"],
    [/description|responsibilit/i, "description"], [/school|university|college|institution/i, "school"],
    [/degree/i, "degree"], [/field of study|major|discipline/i, "field"], [/gpa|overall result/i, "gpa"],
    [/url|website/i, "url"], [/^certification( name)?$/i, "name"]];
  const wdFieldLabel = w => clean((w.querySelector("label, legend") || {}).innerText || "");
  function wdKey(w) {
    const id = (w.getAttribute("data-automation-id") || "").replace(/^formField-/, "");
    if (WD_KEYS[id]) return WD_KEYS[id];
    const hit = WD_LABELS.find(([re]) => re.test(wdFieldLabel(w)));
    return hit ? hit[1] : "";
  }
  const wdChosen = w => !!w.querySelector('[data-automation-id="selectedItem"]');
  function wdEmpty(fields) {
    return fields.every(w => [...w.querySelectorAll("input:not([type=checkbox]):not([type=file]), textarea")].every(i => !i.value.trim())
      && !wdChosen(w) && [...w.querySelectorAll('button[aria-haspopup="listbox"]')].every(b => /^select one$/i.test(clean(b.innerText))));
  }
  // Workday's date boxes show typed or pasted values but save them as empty ("The field
  // From is required"). Picking the month in the box's own calendar (the date icon, a
  // year spinner and month tiles) goes through Workday's date-picked handler and saves.
  // A date already there that differs from the resume is Sai's and stays.
  const MONTH_TILES = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"];
  async function wdDate(month, year, v) {
    if (month && month.value && year.value && (+month.value !== +v.month || +year.value !== +v.year)) return true;
    const field = year.closest('[data-automation-id^="formField-"]');
    const icon = field && field.querySelector('[data-automation-id="dateIcon"]');
    const picker = () => document.querySelector('[data-automation-id="monthPicker"]');
    if (icon && month && v.month) {
      icon.click();
      for (let t = 0; t < 12 && !picker(); t++) await sleep(150);
      const shown = () => +clean((document.querySelector('[data-automation-id="monthPickerSpinnerLabel"]') || {}).innerText || "0");
      for (let k = 0; k < 60 && picker() && shown() && shown() !== +v.year; k++) {
        const b = document.querySelector('[data-automation-id="' + (shown() > +v.year ? "monthPickerLeftSpinner" : "monthPickerRightSpinner") + '"]');
        if (!b) break;
        b.click();
        await sleep(120);
      }
      if (picker() && shown() === +v.year) {
        const tile = [...document.querySelectorAll('[data-automation-id="monthPickerTileLabel"]')]
          .find(e => norm(e.innerText).slice(0, 3) === MONTH_TILES[v.month - 1]);
        if (tile) { tile.click(); await sleep(400); }
      }
      if (picker()) document.activeElement.dispatchEvent(new KeyboardEvent("keydown", {key: "Escape", code: "Escape", keyCode: 27, bubbles: true}));
      if (+month.value === +v.month && +year.value === +v.year) return true;
    }
    // no calendar (year-only fields): paste the date into the box
    const first = month || year;
    first.focus();
    await sleep(100);
    const dt = new DataTransfer();
    dt.setData("text/plain", (month && v.month ? String(v.month).padStart(2, "0") + "/" : "") + v.year);
    first.dispatchEvent(new ClipboardEvent("paste", {clipboardData: dt, bubbles: true, cancelable: true}));
    await sleep(300);
    year.blur();
    return String(year.value) === String(v.year);
  }
  async function wdFillEntry(head, item) {
    const left = [];
    for (const w of wdUnder(head, '[data-automation-id^="formField-"]')) {
      w.setAttribute("data-agent-history", "1");
      if (!visible(w)) continue;  // "To" goes away once "I currently work here" is ticked
      const key = wdKey(w), v = item[key];
      if (!key || v === "" || v == null || v === false) continue;
      const month = w.querySelector('[data-automation-id="dateSectionMonth-input"]');
      const year = w.querySelector('[data-automation-id="dateSectionYear-input"]');
      const btn = w.querySelector('button[aria-haspopup="listbox"]');
      const multi = w.querySelector('[data-automation-id="multiselectInputContainer"] input');
      const box = w.querySelector('input[type="checkbox"]');
      const text = w.querySelector("textarea, input[type=text], input:not([type])");
      let ok = true;
      if (year) ok = await wdDate(month, year, v); else if (btn) ok = await wdSelectAny(btn, v.pick || [String(v)], v.word);
      else if (multi) ok = wdChosen(w) || await wdMulti(multi, String(v), true);
      else if (box) { if (v === true && !box.checked) box.click(); }
      else if (text) { if (!text.value.trim()) setValue(text, String(v)); }
      else ok = false;
      if (!ok) left.push(wdFieldLabel(w) + ": " + (v.pick ? v.pick[0] : String(v)));
      await sleep(200);
    }
    return left;
  }
  // sections filled from the resume; an entry left completely empty in the optional
  // ones (a blank "Certifications 1" with a required field) is deleted so it can't block the page
  const WD_SECTIONS = [["Work Experience", "experience", false], ["Education", "education", false],
                       ["Certifications", "certifications", true], ["Languages", "", true], ["Websites", "websites", true]];
  const wdExperiencePage = () => !!(wdq("applyFlowMyExpPage") || wdSectionHead("Work Experience") || wdSectionHead("Education"));
  let notes = [];  // what was left out on purpose, shown apart from what Sai still has to fill
  async function wdExperience() {
    if (!wdHist) wdHist = await api("GET", "/api/jobs/history");
    const left = [];
    notes = [];
    for (const [name, list, optional] of WD_SECTIONS) {
      const items = list ? wdHist[list] || [] : [];
      const sec = wdSectionHead(name);
      if (!sec) continue;
      for (let k = wdEntryHeads(name).length; k < items.length; k++) {
        const add = wdAdd(sec);
        if (!add) break;
        add.click();
        for (let t = 0; t < 12 && wdEntryHeads(name).length <= k; t++) await sleep(250);
      }
      let heads = wdEntryHeads(name);
      if (heads.length < items.length) left.push(name + ": add " + (items.length - heads.length) + " more yourself");
      const drop = new Set();  // optional entries the company's list can't take (a certification it doesn't list)
      for (let k = 0; k < heads.length; k++) {
        if (k >= items.length) { wdUnder(heads[k], '[data-automation-id^="formField-"]').forEach(w => w.setAttribute("data-agent-history", "1")); continue; }
        const miss = await wdFillEntry(heads[k], items[k]);
        if (optional && miss.length) { drop.add(heads[k]); notes.push(name + ": " + Object.values(items[k])[0] + " isn't in " + wdTenant().split(".")[0] + "'s list, so it's left out"); }
        else for (const l of miss) left.push(name + " " + (k + 1) + ", " + l);
      }
      if (optional) {  // newest first, so the numbering of the others doesn't matter
        for (const h of wdEntryHeads(name).reverse()) {
          if (!drop.has(h) && !wdEmpty(wdUnder(h, '[data-automation-id^="formField-"]'))) continue;
          const del = wdUnder(h, "button").find(b => /^delete$/i.test(clean(b.innerText)));
          if (del) { del.click(); await sleep(600); }
          else if (drop.has(h)) left.push(name + ": delete the entry for " + clean(h.innerText) + " yourself");
        }
      }
    }
    const sk = wdq("formField-skills") || [...document.querySelectorAll('[data-automation-id^="formField-"]')].find(w => visible(w) && /skill/i.test(wdFieldLabel(w)));
    const input = sk && sk.querySelector("input");
    if (input && (wdHist.skills || []).length) {
      sk.setAttribute("data-agent-history", "1");
      const have = () => [...sk.querySelectorAll('[data-automation-id="selectedItem"]')].map(s => norm(s.innerText));
      for (const s of wdHist.skills) if (!have().includes(norm(s))) await wdMulti(input, s, true);  // skills not in the list stay out
    }
    return left;
  }
  // The My Experience step, then the rest of the page from the profile and prepared answers
  async function wdFillPage() {
    let left = [];
    if (wdExperiencePage()) {
      say("Adding your work history and education from your resume...");
      try { left = await wdExperience(); } catch (e) { left = ["Work history: " + e.message]; }
    }
    const res = await run();
    if (left.length) body.append(row(node("div", "Fill these in My Experience yourself:", "font-weight:600")),
      ...left.slice(0, 8).map(q => row(node("div", q, "color:#e08a1e"))));
    if (notes.length) body.append(...notes.map(q => row(node("div", q, "color:#9b9992"))));
    return res;
  }
  const wdNext = () => wdq("pageFooterNextButton") || wdq("bottom-navigation-next-button");
  // Self Identify's "Date" is today's date, pasted into Workday's date box as MM/DD/YYYY
  // (typed values show but save as empty)
  async function wdToday() {
    const d = new Date(), pad = n => String(n).padStart(2, "0");
    for (const wrap of document.querySelectorAll('[data-automation-id="dateInputWrapper"]')) {
      const field = wrap.closest('[data-automation-id^="formField-"]');
      const lab = field && field.querySelector("label, legend");
      const inputs = [...wrap.querySelectorAll('input[data-automation-id^="dateSection"]')];
      if (!lab || !/^date\b|today|signed/i.test(clean(lab.innerText)) || !inputs.length || inputs.some(i => i.value)) continue;
      inputs[0].focus();
      const dt = new DataTransfer();
      dt.setData("text/plain", pad(d.getMonth() + 1) + "/" + pad(d.getDate()) + "/" + d.getFullYear());
      inputs[0].dispatchEvent(new ClipboardEvent("paste", {clipboardData: dt, bubbles: true, cancelable: true}));
      await sleep(300);
      inputs[inputs.length - 1].blur();
    }
  }
  // Workday's own "terms and conditions" box (voluntary disclosures), only with Sai's yes
  function wdTerms() {
    for (const box of document.querySelectorAll('input[type="checkbox"]')) {
      const lab = clean((box.labels && box.labels[0] ? box.labels[0].innerText : "") + " " + questionLabel(box));
      if (!box.checked && /terms and conditions/i.test(lab) && box.offsetParent) box.click();
    }
  }
  function wdMissing() {
    const out = missingRequired();
    for (const b of document.querySelectorAll('button[aria-haspopup="listbox"]')) {
      if (!b.offsetParent) continue;
      const req = /required/i.test(b.getAttribute("aria-label") || "") || /\*/.test((b.closest('[data-automation-id^="formField-"]') || {}).innerText || "");
      if (req && /^select one$/i.test(clean(b.innerText))) out.push(wdLabel(b));
    }
    return [...new Set(out)];
  }
  let wdLast = "", wdBusy = false, wdCreds = null;
  function wdPageKey() {
    const ids = ["applyManually", "autofillWithResume", "signInSubmitButton", "createAccountSubmitButton"].filter(wdq).join(",");
    const step = wdq("progressBarActiveStep"), next = wdNext();
    return [ids, step ? clean(step.innerText) : "", next ? clean(next.innerText) : "", location.pathname].join("|");
  }
  async function wdLogin(auto) {
    if (!auto) return say("Sign in or create your account, or open this job from Apply to approved and the autofill does it.");
    let c;
    try { c = wdCreds = await api("GET", "/api/workday/login?tenant=" + encodeURIComponent(wdTenant())); }
    catch (e) { return say(e.message); }
    const signup = !!wdq("createAccountSubmitButton");
    if (signup && c.has_account && wdq("signInLink")) { wdq("signInLink").click(); wdLast = ""; return; }
    if (!signup && !c.has_account && wdq("createAccountLink")) { wdq("createAccountLink").click(); wdLast = ""; return; }
    setValue(wdq("email"), c.email);
    setValue(wdq("password"), c.password);
    if (signup && wdq("verifyPassword")) setValue(wdq("verifyPassword"), c.password);
    if (signup) {
      const box = wdq("createAccountCheckbox");
      if (box && !box.checked) {
        if (!c.accept_terms) return say("Tick the terms box to create the account, then tap Create Account. (Or allow it under Settings, Workday in the panel.)");
        box.click();
      }
    }
    await sleep(600);
    const filter = [...document.querySelectorAll('[data-automation-id="click_filter"]')].find(e => e.offsetParent);
    (filter || wdq(signup ? "createAccountSubmitButton" : "signInSubmitButton")).click();
    say(signup ? "Creating your Workday account for " + wdTenant().split(".")[0] + "..." : "Signing in...");
    await sleep(3500);
    const err = wdq("errorMessage") || [...document.querySelectorAll('[role="alert"]')].find(e => clean(e.innerText));
    const text = err ? clean(err.innerText) : "";
    if (signup && /already|exists|in use/i.test(text)) {  // an account made before, by hand
      api("POST", "/api/workday/account", {tenant: wdTenant()}).catch(() => {});
      if (wdq("signInLink")) wdq("signInLink").click();
    } else if (text) {
      body.replaceChildren(row(node("div", "Workday says: " + text)), stopButton());
    } else if (signup) {
      api("POST", "/api/workday/account", {tenant: wdTenant()}).catch(() => {});
      if (/verif/i.test(document.body.innerText.slice(0, 3000))) say("Workday sent a verification email. Open it under Inbox in the panel, tap its link, then come back here.");
    }
    wdLast = "";
  }
  async function wdHandle() {
    const auto = !!store.get(AUTO_KEY);
    if (wdq("applyManually") || wdq("autofillWithResume")) {
      // Apply Manually: the script fills My Experience from the resume, which Workday's parser gets wrong
      const b = wdq("applyManually") || wdq("autofillWithResume");
      if (auto) { say("Starting with " + clean(b.innerText) + "..."); b.click(); }
      else say("Tap Apply Manually to start. The autofill adds your work history and education itself.");
      return;
    }
    if (wdq("signInSubmitButton") || wdq("createAccountSubmitButton")) return wdLogin(auto);
    if (!wdNext()) return;  // not a form page yet
    await wdToday();
    const res = await wdFillPage();
    if (!res || !res.job) return;
    if (document.querySelector('[data-automation-id="file-upload-input-ref"]')) await sleep(4000);  // Workday reads the resume
    if (!auto) return;
    if (!res.approved) return body.append(row(node("div", "Not approved in the panel, so it won't be submitted. Review it in the Jobs tab.", "color:#e08a1e")));
    if (!wdCreds) { try { wdCreds = await api("GET", "/api/workday/login?tenant=" + encodeURIComponent(wdTenant())); } catch (e) {} }
    if (wdCreds && wdCreds.accept_terms) wdTerms();
    await sleep(1200);
    const missing = wdMissing();
    if (missing.length) {
      body.replaceChildren(row(node("div", "Fill these, then tap Save and Continue (it carries on from the next page):", "font-weight:600")),
        ...missing.slice(0, 8).map(q => row(node("div", q, "color:#e08a1e"))), laterButton(res.job), stopButton());
      return;
    }
    const next = wdNext();
    if (/submit/i.test(clean(next.innerText))) {  // the Review page
      for (let s = 3; s > 0 && !stopped; s--) {
        body.replaceChildren(row(node("div", "Submitting " + res.job.company + ": " + res.job.title + " in " + s + "...", "font-weight:600")), stopButton("Stop"));
        await sleep(1000);
      }
      if (stopped) return;
      store.set(SENT_KEY, JSON.stringify({id: res.job.id, company: res.job.company, title: res.job.title, at: Date.now()}));
      await snapshot(res);
      next.click();
      body.replaceChildren(row(node("div", "Submitted, waiting for " + res.job.company + " to confirm...")), stopButton());
      return watch(res.job);
    }
    say("Page done: " + clean((wdq("progressBarActiveStep") || next).innerText) + ". Going on...");
    next.click();
  }
  // After Submit (the script's or Sai's own tap): Workday leaves the application flow for
  // the candidate home or shows a confirmation, without loading a new page
  // (seen on two ticks in a row, since the flow can blink out while a step loads)
  let wdGone = 0;
  function wdSubmitted() {
    const text = document.body.innerText.slice(0, 5000);
    if (SUCCESS_RE.test(text) || /application submitted|submitted successfully|you('ve| have) (successfully )?applied/i.test(text)) return true;
    wdGone = wdq("applyFlowPage") ? 0 : wdGone + 1;
    return wdGone >= 2;
  }
  let wdDone = false;
  document.addEventListener("click", async e => {
    if (!WD) return;
    const b = e.target.closest && e.target.closest("button");
    if (!b || b !== wdNext() || !/submit/i.test(clean(b.innerText))) return;
    let res = lastRes;
    if (!res || !res.job) { try { res = await api("POST", "/api/jobs/fill", {url: location.href, fields: []}); } catch (err) { return; } }
    if (!res.job) return;
    store.set(SENT_KEY, JSON.stringify({id: res.job.id, company: res.job.company, title: res.job.title, at: Date.now()}));
    snapshot(res);
  }, true);
  async function wdTick() {
    if (stopped || wdBusy) return;
    let sent = null;
    try { sent = JSON.parse(store.get(SENT_KEY) || "null"); } catch (e) {}
    if (sent && !wdDone && Date.now() - sent.at < 30 * 60 * 1000 && Date.now() - sent.at > 2000 && wdSubmitted()) {
      wdDone = true;
      if (body) minimize(false);
      return finished(sent);
    }
    const key = wdPageKey();
    if (key === wdLast) return;
    wdBusy = true; wdLast = key;
    try { await wdHandle(); } catch (e) { say("Workday: " + e.message); } finally { wdBusy = false; }
  }

  async function put(el, f, v, kind) {
    if (f.type === "wdselect") return wdSelect(el, kind === "consent" ? "Yes" : v);
    if (WD && el.closest && el.closest('[data-automation-id="multiselectInputContainer"]')) return wdMulti(el, v);
    if (kind === "consent") {  // only sent for applications Sai approved
      if (f.type === "checkbox") { if (!el.checked) el.click(); return true; }
      const i = f.options.findIndex(o => AGREE_RE.test(clean(o)));
      if (i < 0) return false;
      if (f.type === "radio") { el[i].click(); return true; }
      if (f.type === "select") { setValue(el, el.options[i].value); return true; }
      if (f.type === "combobox") return fillCombo(el, f.options[i], false);
      return false;
    }
    if (f.type === "checkbox") { if (/^yes$/i.test(v) && !el.checked) el.click(); return true; }
    if (f.type === "radio") { const i = pick(f.options, v); if (i < 0) return false; el[i].click(); return true; }
    if (f.type === "select") { const i = pick(f.options, v); if (i < 0) return false; setValue(el, el.options[i].value); return true; }
    if (f.type === "combobox") return fillCombo(el, v, /location|city/i.test(f.label));
    if (!el.value || !el.value.trim()) setValue(el, v);  // never overwrite what's there
    return true;
  }

  function mark(el, state) {
    // outline what's visible: the question around radios, the box around dropdowns and uploads
    const target = Array.isArray(el) ? (el[0].closest("fieldset, [role=radiogroup], li") || el[0].parentElement)
      : el.getAttribute("role") === "combobox" ? (el.closest("[class*=control]") || el)
      : el.type === "file" ? (el.closest("[class*=upload], [class*=Upload]") || el.parentElement) : el;
    if (!target || !target.style) return;
    target.style.outline = "3px solid " + COLORS[state];
    target.style.outlineOffset = "2px";
  }

  // The job's tailored resume or cover letter from the panel, or the usual resume
  async function attachDoc(el, kind, jobId) {
    const {data, name} = await api("GET", "/api/jobs/resume?kind=" + kind + (jobId ? "&id=" + encodeURIComponent(jobId) : ""));
    const bytes = Uint8Array.from(atob(data), c => c.charCodeAt(0));
    const dt = new DataTransfer();
    dt.items.add(new File([bytes], name, {type: "application/pdf"}));
    el.files = dt.files;
    el.dispatchEvent(new Event("input", {bubbles: true}));
    el.dispatchEvent(new Event("change", {bubbles: true}));
  }

  // ---------- floating panel ----------

  let box, body, pill;
  function node(tag, text, style) {
    const e = document.createElement(tag);
    if (text !== undefined) e.textContent = text;
    if (style) e.style.cssText = style;
    return e;
  }
  const BTN = "font:600 14px -apple-system,system-ui,sans-serif;border:0;border-radius:8px;padding:9px 12px;cursor:pointer;";
  const SHADOW = "z-index:2147483647;box-shadow:0 6px 24px rgba(0,0,0,.35);font:14px/1.4 -apple-system,system-ui,sans-serif;";

  // Minimized, the panel is a small round button in the corner, so it never
  // covers the form. The choice is remembered for the site.
  function minimize(on) {
    box.style.display = on ? "none" : "block";
    pill.style.display = on ? "block" : "none";
    try { localStorage.setItem("agent-fill-min", on ? "1" : "0"); } catch (e) {}
  }

  function ui() {
    box = node("div", undefined, SHADOW + "position:fixed;right:12px;bottom:12px;width:min(340px,calc(100vw - 24px));background:#1f1e1c;color:#ebeae4;border-radius:12px;padding:10px");
    const row = node("div", undefined, "display:flex;gap:8px");
    const go = node("button", "Fill from agent", BTN + "background:#57a37a;color:#fff;flex:1");
    go.onclick = () => { go.disabled = true; (WD ? wdFillPage() : run()).finally(() => { go.disabled = false; go.textContent = "Fill again"; }); };
    const min = node("button", "\u2013", BTN + "background:#33322e;color:#ebeae4;width:40px");
    min.title = "Minimize";
    min.setAttribute("aria-label", "Minimize");
    min.onclick = () => minimize(true);
    row.append(go, min);
    body = node("div", undefined, "max-height:45vh;overflow:auto");
    box.append(row, body);
    pill = node("button", "Agent", SHADOW + BTN + "position:fixed;right:12px;bottom:12px;background:#57a37a;color:#fff;border-radius:22px;padding:10px 14px;display:none");
    pill.setAttribute("aria-label", "Show the autofill panel");
    pill.onclick = () => minimize(false);
    // in a shadow root, so the page's styles can't squash or restyle the panel
    const host = document.createElement("div");
    host.style.cssText = "all:initial";
    const root = host.attachShadow({mode: "open"});
    const reset = document.createElement("style");
    reset.textContent = "*{box-sizing:border-box;line-height:1.4;letter-spacing:normal;text-transform:none;text-align:left}div{display:block;position:static;height:auto;width:auto;white-space:normal;word-break:break-word}button{text-align:center}";
    root.append(reset, box, pill);
    document.body.append(host);
    let saved = "0";
    try { saved = localStorage.getItem("agent-fill-min") || "0"; } catch (e) {}
    minimize(saved === "1");
  }
  function say(text) { body.replaceChildren(node("div", text, "margin-top:8px")); }

  async function run() {
    say("Reading the form...");
    const {fields, els} = collect();
    if (!fields.length && !WD) return say("No form fields on this page.");  // Workday's Review page has none but still needs the job
    let res;
    try { res = await api("POST", "/api/jobs/fill", {url: location.href, fields}); }
    catch (e) { return say(e.message); }
    const todo = [], counts = {filled: 0, review: 0, you: 0};
    // the resume first: some forms fill fields from it and would overwrite ours
    for (const a of res.answers) {
      if (a.kind !== "file" || fields[a.i].type !== "file") continue;
      try { await attachDoc(els[a.i], a.doc || "resume", res.job && res.job.id); mark(els[a.i], "filled"); counts.filled++; }
      catch (e) { todo.push([fields[a.i].label, (a.doc === "cover" ? "Cover letter: " : "Resume: ") + e.message]); mark(els[a.i], "you"); counts.you++; }
      await sleep(1500);
    }
    for (const a of res.answers) {
      const el = els[a.i], f = fields[a.i];
      if (a.kind === "file" && f.type === "file") continue;
      let state = "you";
      if (a.value && f.type !== "file") {
        if (await put(el, f, a.value, a.kind)) state = a.kind === "draft" ? "review" : "filled";
        else todo.push([f.label, "Couldn't choose an option for: " + a.value]);
      } else if (a.value) {
        todo.push([f.label, "Upload this as a file or paste it: ", a.value]);
      } else if (f.label) {
        todo.push([f.label, KIND[a.kind] || "Answer yourself"]);
      }
      mark(el, state);
      counts[state]++;
    }
    const out = [node("div", (res.job ? res.job.company + ": " + res.job.title : "Not a job from the agent; filled from your profile.") , "margin-top:8px;font-weight:600"),
                 node("div", counts.filled + " filled (green), " + counts.review + " drafts to review (blue), " + counts.you + " for you (orange). Check everything, solve the CAPTCHA and submit yourself.", "margin-top:4px")];
    for (const [q, what, text] of todo) {
      const row = node("div", undefined, "margin-top:8px;border-top:1px solid #33322e;padding-top:6px");
      row.append(node("div", q, "font-weight:600"), node("div", what, "color:#9b9992"));
      if (text) {
        const copy = node("button", "Copy text", BTN + "background:#33322e;color:#ebeae4;margin-top:4px");
        copy.onclick = () => navigator.clipboard.writeText(text).then(() => { copy.textContent = "Copied"; });
        row.append(copy);
      }
      out.push(row);
    }
    res.fields = fields;
    lastRes = res;
    watchSubmit(res);
    if (res.job) {
      const done = node("button", "Mark applied in the panel", BTN + "background:#2f6fd6;color:#fff;margin-top:10px;width:100%");
      done.onclick = () => snapshot(res).then(() => api("POST", "/api/jobs/status", {id: res.job.id, status: "applied"}))
        .then(() => { done.textContent = "Marked applied"; done.disabled = true; }, e => { done.textContent = e.message; });
      out.push(done);
    }
    body.replaceChildren(...out);
    return res;
  }

  // ---------- the record of what was submitted ----------
  // Right before the form is submitted (by the script or by Sai's own tap), read every
  // field's value as it stands and tell the panel, with the documents that were attached.
  let lastSnap = 0;
  function fieldValue(el, f) {
    if (f.type === "radio") { const r = el.find(x => x.checked); return r ? labelFor(r) : ""; }
    if (f.type === "checkbox") return el.checked ? "Ticked" : "";
    if (f.type === "file") return el.files && el.files[0] ? el.files[0].name : "";
    if (f.type === "select") return el.selectedIndex >= 0 ? el.options[el.selectedIndex].text.trim() : "";
    if (f.type === "combobox") {
      const box = el.closest("[class*=__control], [class*=-control], [class*=Control]");
      const v = box && box.querySelector("[class*=single-value], [class*=singleValue], [class*=multi-value], [class*=multiValue]");
      return v ? clean(v.innerText) : el.value;
    }
    return el.value || "";
  }
  async function snapshot(res) {
    if (!res || !res.job || Date.now() - lastSnap < 3000) return;  // the script's click and the listener
    lastSnap = Date.now();
    const {fields, els} = collect();
    const out = fields.map((f, i) => ({q: f.label, a: fieldValue(els[i], f)}));
    const files = (res.answers || []).filter(a => a.kind === "file" && a.doc).map(a => ({q: (res.fields || [])[a.i] ? res.fields[a.i].label : "", doc: a.doc}));
    try { await api("POST", "/api/jobs/submitting", {id: res.job.id, fields: out, files}); } catch (e) {}
  }
  function watchSubmit(res) {
    if (!res || !res.job || watchSubmit.on) return;
    watchSubmit.on = true;
    document.addEventListener("submit", () => { snapshot(res); }, true);
    document.addEventListener("click", e => {
      const b = e.target.closest && e.target.closest("button, input[type=submit]");
      if (b && b === submitButton()) snapshot(res);
    }, true);
  }

  // ---------- submitting approved applications ----------

  let lastRes = null;
  const visible = e => !!(e && e.offsetParent !== null && e.getClientRects().length);

  // Required fields still empty, by their question text
  function missingRequired() {
    const out = [], seen = new Set();
    for (const el of document.querySelectorAll("input, textarea, select")) {
      const req = el.required || el.getAttribute("aria-required") === "true";
      if (!req || el.disabled || el.type === "hidden") continue;
      if (el.getAttribute("aria-hidden") === "true" && el.tabIndex < 0) continue;  // react-select's own validation input
      let empty;
      if (el.type === "radio") {
        if (seen.has(el.name)) continue;
        seen.add(el.name);
        empty = ![...document.querySelectorAll("input[type=radio]")].some(r => r.name === el.name && r.checked);
      } else if (el.type === "checkbox") empty = !el.checked;
      else if (el.type === "file") empty = !(el.files && el.files.length) && !visible(el.closest("[class*=upload], [class*=Upload]")?.querySelector("[class*=filename], [class*=file-name], [class*=FileName]"));
      else if (el.getAttribute("role") === "combobox") {
        // react-select shows the choice next to the input, inside the control
        const box = el.closest("[class*=__control], [class*=-control], [class*=Control]");
        empty = !(box && box.querySelector("[class*=single-value], [class*=singleValue], [class*=multi-value], [class*=multiValue]"));
      } else {
        if (!visible(el)) continue;
        empty = !String(el.value || "").trim();
      }
      if (empty) out.push(labelFor(el) || questionLabel(el) || el.name || "A required field");
    }
    return out;
  }

  function submitButton() {
    return [...document.querySelectorAll("button, input[type=submit]")].find(b => visible(b) && !b.disabled
      && /^(submit( your)?( application)?|apply|send application)$/i.test(clean(b.innerText || b.value)));
  }

  const challengeOpen = () => [...document.querySelectorAll("iframe")].some(f =>
    /recaptcha.*bframe|hcaptcha.*challenge|challenge/i.test(f.src + " " + (f.title || "")) && visible(f) && f.offsetHeight > 100);
  const succeeded = () => SUCCESS_RE.test(document.body.innerText) || /\/(thanks|confirmation|thank_you|success)\b/i.test(location.pathname);

  function row(...nodes) { const d = node("div", undefined, "margin-top:8px"); d.append(...nodes); return d; }
  function stopButton(label) {
    const b = node("button", label || "Stop applying", BTN + "background:#a3392b;color:#fff;margin-top:8px;width:100%");
    b.onclick = () => { stopped = true; store.del(AUTO_KEY); store.del(SENT_KEY); say("Stopped. Approved applications stay approved; open Apply to approved in the panel to go on."); };
    return b;
  }

  // After Submit: wait for the confirmation (this page or the next one), then move on.
  async function watch(job) {
    for (let t = 0; t < 240 && !stopped; t++) {  // 2 minutes, then a CAPTCHA may still be open
      if (succeeded()) return finished(job);
      if (challengeOpen()) body.replaceChildren(row(node("div", "Solve the CAPTCHA for " + job.company + ". It goes on by itself after that.")), stopButton());
      await sleep(500);
    }
    if (stopped) return;
    const again = node("button", "It went through", BTN + "background:#2f6fd6;color:#fff;margin-top:8px;width:100%");
    again.onclick = () => finished(job);
    body.replaceChildren(row(node("div", "No confirmation from " + job.company + " yet. Fix what the form points out and tap its Submit button, or confirm it went through.")), again, laterButton(job), stopButton());
    for (let t = 0; t < 1200 && !stopped; t++) {  // keep watching while Sai fixes things
      if (succeeded()) return finished(job);
      await sleep(500);
    }
  }

  function laterButton(job) {
    const b = node("button", "Skip for now, next one", BTN + "background:#33322e;color:#ebeae4;margin-top:8px;width:100%");
    b.onclick = async () => { stopped = true; store.del(SENT_KEY); await api("POST", "/api/jobs/defer", {id: job.id}).catch(() => {}); goNext(); };
    return b;
  }

  async function finished(job) {
    if (finished.id === job.id) return;  // the Workday tick and the confirmation watch can both see it
    finished.id = job.id;
    store.del(SENT_KEY);
    try { await api("POST", "/api/jobs/status", {id: job.id, status: "applied"}); } catch (e) {}
    say("Submitted: " + job.company + ", " + job.title + ".");
    if (store.get(AUTO_KEY)) { await sleep(2500); if (!stopped) goNext(); }
  }

  async function goNext() {
    let n;
    try { n = await api("GET", "/api/jobs/next"); } catch (e) { return say(e.message); }
    if (!n.job) { store.del(AUTO_KEY); return say("All approved applications are done."); }
    if (!/^https:\/\//.test(n.job.apply_url)) return say("The next job has no application link.");
    say("Next: " + n.job.company + ", " + n.job.title + " (" + n.left + " left).");
    location.href = n.job.apply_url + "#agent-auto";
  }

  async function autoApply() {
    stopped = false;
    const res = await run();
    if (!res || !res.job) return;
    if (!res.approved) return body.append(row(node("div", "Not approved in the panel, so it won't be submitted. Review it in the Jobs tab of the panel.", "color:#e08a1e")));
    await sleep(1200);  // let the form settle after the last choices
    const missing = missingRequired();
    if (missing.length) {
      body.replaceChildren(row(node("div", "Fill these, then tap the form's Submit button:", "font-weight:600")),
        ...missing.slice(0, 8).map(q => row(node("div", q, "color:#e08a1e"))), laterButton(res.job), stopButton());
      store.set(SENT_KEY, JSON.stringify({id: res.job.id, company: res.job.company, title: res.job.title, at: Date.now()}));
      return watch(res.job);
    }
    const btn = submitButton();
    if (!btn) {
      body.replaceChildren(row(node("div", "Couldn't find the Submit button. Tap it yourself.")), laterButton(res.job), stopButton());
      store.set(SENT_KEY, JSON.stringify({id: res.job.id, company: res.job.company, title: res.job.title, at: Date.now()}));
      return watch(res.job);
    }
    for (let s = 3; s > 0 && !stopped; s--) {
      body.replaceChildren(row(node("div", "Submitting " + res.job.company + ": " + res.job.title + " in " + s + "...", "font-weight:600")), stopButton("Stop"));
      await sleep(1000);
    }
    if (stopped) return;
    store.set(SENT_KEY, JSON.stringify({id: res.job.id, company: res.job.company, title: res.job.title, at: Date.now()}));
    await snapshot(res);
    btn.click();
    body.replaceChildren(row(node("div", "Submitted, waiting for " + res.job.company + " to confirm...")), stopButton());
    watch(res.job);
  }

  // Workday is one page app across its steps: a timer handles each step as it appears.
  if (WD) {
    const start = setInterval(() => {
      if (!document.body) return;
      clearInterval(start);
      ui();
      if (store.get(AUTO_KEY)) minimize(false);
      setInterval(wdTick, 1500);
    }, 300);
  }
  // A page loaded after Submit (the confirmation) picks up the job it was waiting for.
  let sent = null;
  try { sent = JSON.parse(store.get(SENT_KEY) || "null"); } catch (e) {}
  if (WD) {
    // handled above
  } else if (sent && Date.now() - sent.at < 30 * 60 * 1000 && !document.querySelector("input[type=file]")) {
    ui(); minimize(false);
    say("Checking whether " + sent.company + " confirmed...");
    watch(sent);
  } else {
    // Application forms often render after load; wait for one before showing the button.
    let tries = 0;
    const timer = setInterval(() => {
      if (++tries > 40) return clearInterval(timer);
      if (document.querySelector("input[type=email], input[name*=email i], input[type=file]")) {
        clearInterval(timer);
        ui();
        if (store.get(AUTO_KEY)) { minimize(false); autoApply(); }
      }
    }, 500);
  }
  window.__agentFill = {collect, put, mark, attachDoc, run, missingRequired, submitButton};  // for testing from the console
})();
"""
