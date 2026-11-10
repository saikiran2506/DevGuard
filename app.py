import os
import re
import secrets
import threading
import time
from collections import OrderedDict, deque

from flask import Flask, Response, jsonify, render_template_string, request
from flask_socketio import SocketIO, emit, join_room, leave_room
from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    generate_latest,
)

app = Flask(__name__)
app.config["SECRET_KEY"] = (
    os.environ.get("FLASK_SECRET_KEY") or os.urandom(32).hex()
)
APP_COLOR = os.environ.get("APP_COLOR", "unknown")

socketio = SocketIO(app, async_mode="threading")

# ---------------------------------------------------------------------------
# Shared state. One gunicorn worker means one copy per container.
# ---------------------------------------------------------------------------
DEFAULT_ROOM = "main"
MAX_SEGMENTS = 15000      # freehand segments kept per workspace
MAX_ITEMS = 1000          # shapes, text and notes kept per workspace
MAX_ROOMS = 200
MAX_USERS_PER_ROOM = 50
ROOM_TTL_SECONDS = 3600   # empty workspaces are forgotten after one hour
RESTORE_LIMIT = 4000

ROOM_PATTERN = re.compile(r"[A-Za-z0-9_-]{3,32}")
SID_PATTERN = re.compile(r"[A-Za-z0-9]{6,32}")
COLOR_PATTERN = re.compile(r"#[0-9a-fA-F]{6}")
CONTROL_CHARS = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")
NAME_CHARS = re.compile(r"[^\w .'-]", re.UNICODE)

SHAPE_TYPES = ("line", "arrow", "rect", "ellipse")
ITEM_TYPES = SHAPE_TYPES + ("text", "note")
USER_COLORS = (
    "#ef4444", "#3b82f6", "#22c55e", "#f59e0b", "#a855f7",
    "#06b6d4", "#ec4899", "#84cc16", "#f97316", "#14b8a6",
)

state_lock = threading.RLock()
rooms = {}      # room id -> {"strokes", "items", "members", "touched"}
sessions = {}   # socket id -> participant data

active_connections = Gauge(
    "devguard_active_connections",
    "Number of currently connected drawing clients",
)
draw_events = Counter(
    "devguard_draw_events_total",
    "Total drawing events received",
)
clear_events = Counter(
    "devguard_clear_events_total",
    "Total canvas clear events received",
)
undo_events = Counter(
    "devguard_undo_events_total",
    "Total undo events received",
)
item_events = Counter(
    "devguard_item_events_total",
    "Total shape, text and note events received",
)
stored_segments = Gauge(
    "devguard_stored_segments",
    "Number of freehand segments kept for late joiners",
)
active_rooms = Gauge(
    "devguard_active_rooms",
    "Number of workspaces with at least one connected client",
)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
def clean_number(value, low, high):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not low <= number <= high:  # also rejects NaN
        return None
    return round(number, 4)


def clean_color(value):
    if isinstance(value, str) and COLOR_PATTERN.fullmatch(value):
        return value
    return None


def clean_id(value):
    if isinstance(value, str) and SID_PATTERN.fullmatch(value):
        return value
    return None


def clean_text(value, limit=500):
    return CONTROL_CHARS.sub("", value)[:limit]


def clean_name(value):
    if not isinstance(value, str):
        value = ""
    value = NAME_CHARS.sub("", value).strip()[:24]
    return value or "Guest-" + secrets.token_hex(2)


def clean_segment(data):
    """Validate one freehand segment. Returns a clean dict or None."""
    if not isinstance(data, dict):
        return None
    segment = {}
    for key in ("x0", "y0", "x1", "y1"):
        number = clean_number(data.get(key), 0, 1)
        if number is None:
            return None
        segment[key] = number
    width = clean_number(data.get("width"), 1, 20)
    color = clean_color(data.get("color"))
    if width is None or color is None:
        return None
    segment["width"] = width
    segment["color"] = color
    if "sid" in data and data["sid"] is not None:
        stroke_id = clean_id(data["sid"])
        if stroke_id is None:
            return None
        segment["sid"] = stroke_id
    if data.get("erase") is True:
        segment["erase"] = True
    return segment


def clean_item(data):
    """Validate a shape, text or note. Returns a clean dict or None."""
    if not isinstance(data, dict):
        return None
    item_id = clean_id(data.get("id"))
    kind = data.get("type")
    color = clean_color(data.get("color"))
    if item_id is None or kind not in ITEM_TYPES or color is None:
        return None
    item = {"id": item_id, "type": kind, "color": color}

    if kind in SHAPE_TYPES:
        for key in ("x0", "y0", "x1", "y1"):
            number = clean_number(data.get(key), 0, 1)
            if number is None:
                return None
            item[key] = number
        width = clean_number(data.get("width"), 1, 20)
        if width is None:
            return None
        item["width"] = width
        return item

    for key in ("x", "y"):
        number = clean_number(data.get(key), 0, 1)
        if number is None:
            return None
        item[key] = number
    text = data.get("text")
    if not isinstance(text, str):
        return None
    item["text"] = clean_text(text)
    if kind == "text":
        size = clean_number(data.get("size"), 8, 160)
        if size is None or not item["text"].strip():
            return None
        item["size"] = size
    return item


# ---------------------------------------------------------------------------
# Workspace helpers (call with state_lock held)
# ---------------------------------------------------------------------------
def new_room():
    return {
        "strokes": deque(maxlen=MAX_SEGMENTS),
        "items": OrderedDict(),
        "members": set(),
        "touched": time.time(),
    }


def get_room(room_id):
    room = rooms.get(room_id)
    if room is not None:
        return room
    if len(rooms) >= MAX_ROOMS:
        cutoff = time.time() - ROOM_TTL_SECONDS
        stale = [
            rid for rid, r in rooms.items()
            if rid != DEFAULT_ROOM and not r["members"] and r["touched"] < cutoff
        ]
        for rid in stale:
            del rooms[rid]
        if len(rooms) >= MAX_ROOMS:
            return None
    room = new_room()
    rooms[room_id] = room
    return room


def participants_of(room):
    people = []
    for member in room["members"]:
        user = sessions.get(member)
        if user and user["explicit"]:
            people.append(
                {"id": user["id"], "name": user["name"], "color": user["color"]}
            )
    return sorted(people, key=lambda p: p["name"].lower())


def pick_color(room):
    used = {
        sessions[m]["color"]
        for m in room["members"]
        if m in sessions and sessions[m]["explicit"]
    }
    for color in USER_COLORS:
        if color not in used:
            return color
    return secrets.choice(USER_COLORS)


def refresh_gauges():
    stored_segments.set(sum(len(r["strokes"]) for r in rooms.values()))
    active_rooms.set(sum(1 for r in rooms.values() if r["members"]))


def detach(sid):
    """Remove a connection from its workspace. Returns what to announce."""
    user = sessions.get(sid)
    if user is None:
        return None
    room = rooms.get(user["room"])
    info = {"room": user["room"], "explicit": user["explicit"],
            "id": user["id"], "name": user["name"]}
    if room is not None:
        room["members"].discard(sid)
        room["touched"] = time.time()
        info["participants"] = participants_of(room)
    else:
        info["participants"] = []
    return info


def announce_leave(info):
    if not info or not info["explicit"]:
        return
    socketio.emit(
        "notice",
        {"kind": "leave", "name": info["name"], "count": len(info["participants"])},
        to=info["room"],
    )
    socketio.emit("participants", {"participants": info["participants"]}, to=info["room"])
    socketio.emit("cursor_gone", {"id": info["id"]}, to=info["room"])


def current_room(sid):
    """Return (room id, room) for a connection, or (None, None)."""
    user = sessions.get(sid)
    if user is None:
        return None, None
    room = rooms.get(user["room"])
    if room is None:
        return None, None
    return user["room"], room


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------
def asset_version():
    try:
        path = os.path.join(app.static_folder, "studio.js")
        return str(int(os.path.getmtime(path)))
    except OSError:
        return "1"


ASSET_VERSION = asset_version()

PAGE = r"""<!DOCTYPE html>
<html lang="en" data-theme="light">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DevGuard Studio</title>
<link rel="stylesheet" href="{{ url_for('static', filename='studio.css') }}?v={{ asset_version }}">
</head>
<body>
<svg width="0" height="0" style="position:absolute" aria-hidden="true">
  <defs>
    <symbol id="i-select" viewBox="0 0 24 24"><path d="M5 3l14 8-6 2-3 6z"/></symbol>
    <symbol id="i-hand" viewBox="0 0 24 24"><path d="M12 3v18M3 12h18M12 3l-3 3M12 3l3 3M12 21l-3-3M12 21l3-3M3 12l3-3M3 12l3 3M21 12l-3-3M21 12l-3 3"/></symbol>
    <symbol id="i-pen" viewBox="0 0 24 24"><path d="M4 20l1-4L16 5a2.1 2.1 0 013 3L8 19z"/><path d="M14 7l3 3"/></symbol>
    <symbol id="i-eraser" viewBox="0 0 24 24"><path d="M4 15l9-9 7 7-7 7H8z"/><path d="M9 10l7 7"/></symbol>
    <symbol id="i-line" viewBox="0 0 24 24"><path d="M5 19L19 5"/></symbol>
    <symbol id="i-arrow" viewBox="0 0 24 24"><path d="M5 19L19 5M10 5h9v9"/></symbol>
    <symbol id="i-rect" viewBox="0 0 24 24"><rect x="4" y="6" width="16" height="12" rx="1"/></symbol>
    <symbol id="i-ellipse" viewBox="0 0 24 24"><ellipse cx="12" cy="12" rx="9" ry="6"/></symbol>
    <symbol id="i-text" viewBox="0 0 24 24"><path d="M5 6V4h14v2M12 4v16M9 20h6"/></symbol>
    <symbol id="i-note" viewBox="0 0 24 24"><path d="M5 4h14v11l-5 5H5z"/><path d="M14 20v-5h5"/></symbol>
    <symbol id="i-undo" viewBox="0 0 24 24"><path d="M9 14L4 9l5-5"/><path d="M4 9h10a6 6 0 010 12h-3"/></symbol>
    <symbol id="i-redo" viewBox="0 0 24 24"><path d="M15 14l5-5-5-5"/><path d="M20 9H10a6 6 0 000 12h3"/></symbol>
    <symbol id="i-zoomin" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-5-5M11 8v6M8 11h6"/></symbol>
    <symbol id="i-zoomout" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M21 21l-5-5M8 11h6"/></symbol>
    <symbol id="i-fit" viewBox="0 0 24 24"><path d="M4 9V4h5M20 9V4h-5M4 15v5h5M20 15v5h-5"/></symbol>
    <symbol id="i-trash" viewBox="0 0 24 24"><path d="M4 7h16M9 7V4h6v3M6 7l1 13h10l1-13"/></symbol>
    <symbol id="i-download" viewBox="0 0 24 24"><path d="M12 4v11M7 11l5 5 5-5M5 20h14"/></symbol>
    <symbol id="i-link" viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2"/><path d="M16 8V6a2 2 0 00-2-2H6a2 2 0 00-2 2v8a2 2 0 002 2h2"/></symbol>
    <symbol id="i-moon" viewBox="0 0 24 24"><path d="M20 14A8 8 0 1110 4a7 7 0 0010 10z"/></symbol>
    <symbol id="i-plus" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14"/></symbol>
    <symbol id="i-users" viewBox="0 0 24 24"><circle cx="9" cy="8" r="3"/><path d="M3 20a6 6 0 0112 0M17 5a3 3 0 010 6M21 20a5 5 0 00-4-5"/></symbol>
  </defs>
</svg>

<header class="topbar">
  <div class="brand">
    <div class="logo">&#10033;</div>
    <div><h1>DevGuard Studio</h1><p>Real-time collaborative whiteboard</p></div>
  </div>
  <div class="workspace-chip" id="workspaceChip" hidden>
    <span class="chip-label">Workspace</span>
    <strong id="roomId">-</strong>
    <button class="icon-btn" id="copyLink" type="button" title="Copy workspace link" aria-label="Copy workspace link"><svg class="ic"><use href="#i-link"/></svg></button>
    <button class="btn small" id="switchRoom" type="button">New / Join</button>
  </div>
  <div class="top-right">
    <div class="avatars" id="avatars" aria-label="Participants"></div>
    <span class="pill"><svg class="ic"><use href="#i-users"/></svg><span id="userCount">0</span> online</span>
    <span class="pill"><span class="dot" id="statusDot"></span><span id="status">Connecting...</span></span>
    <span class="pill badge c-{{ color|lower|replace(' ', '-') }}" title="Deployment colour serving this page">Server: {{ color }}</span>
    <button class="icon-btn" id="theme" type="button" title="Toggle dark mode" aria-label="Toggle dark mode"><svg class="ic"><use href="#i-moon"/></svg></button>
  </div>
</header>

<div class="layout">
  <aside class="tools" id="tools" aria-label="Drawing tools">
    <button class="tool" data-tool="select" title="Select and move (V)"><svg class="ic"><use href="#i-select"/></svg><span>Select</span></button>
    <button class="tool" data-tool="hand" title="Pan (H or hold Space)"><svg class="ic"><use href="#i-hand"/></svg><span>Pan</span></button>
    <button class="tool active" data-tool="pen" title="Pen (P)"><svg class="ic"><use href="#i-pen"/></svg><span>Pen</span></button>
    <button class="tool" data-tool="eraser" title="Eraser (E)"><svg class="ic"><use href="#i-eraser"/></svg><span>Eraser</span></button>
    <button class="tool" data-tool="line" title="Line (L)"><svg class="ic"><use href="#i-line"/></svg><span>Line</span></button>
    <button class="tool" data-tool="arrow" title="Arrow (A)"><svg class="ic"><use href="#i-arrow"/></svg><span>Arrow</span></button>
    <button class="tool" data-tool="rect" title="Rectangle (R)"><svg class="ic"><use href="#i-rect"/></svg><span>Rectangle</span></button>
    <button class="tool" data-tool="ellipse" title="Circle / ellipse (O)"><svg class="ic"><use href="#i-ellipse"/></svg><span>Ellipse</span></button>
    <button class="tool" data-tool="text" title="Text (T)"><svg class="ic"><use href="#i-text"/></svg><span>Text</span></button>
    <button class="tool" data-tool="note" title="Sticky note (N)"><svg class="ic"><use href="#i-note"/></svg><span>Note</span></button>
  </aside>

  <main class="main">
    <div class="options" id="options">
      <div class="group" id="colorGroup">
        <span class="label">Color</span>
        <div class="swatches" id="swatches"></div>
        <input id="color" type="color" value="#3b82f6" aria-label="Custom color">
      </div>
      <div class="group" id="sizeGroup">
        <span class="label" id="sizeLabel">Size</span>
        <input id="brush" type="range" min="1" max="20" value="4" aria-label="Brush size">
        <span id="sizeValue" class="value">4px</span>
      </div>
      <div class="group" id="fontGroup" hidden>
        <span class="label">Font</span>
        <select id="fontSize" aria-label="Font size">
          <option value="16">Small</option><option value="24" selected>Medium</option>
          <option value="36">Large</option><option value="56">Huge</option>
        </select>
      </div>
      <div class="group" id="noteGroup" hidden>
        <span class="label">Note</span>
        <div class="swatches" id="noteSwatches"></div>
      </div>
      <div class="group">
        <button class="btn icon" id="undo" type="button" title="Undo (Ctrl+Z)" disabled><svg class="ic"><use href="#i-undo"/></svg></button>
        <button class="btn icon" id="redo" type="button" title="Redo (Ctrl+Y)" disabled><svg class="ic"><use href="#i-redo"/></svg></button>
        <button class="btn icon" id="deleteItem" type="button" title="Delete selected (Del)" disabled><svg class="ic"><use href="#i-trash"/></svg></button>
      </div>
      <div class="group">
        <button class="btn icon" id="zoomOut" type="button" title="Zoom out (-)"><svg class="ic"><use href="#i-zoomout"/></svg></button>
        <span class="value" id="zoomValue">100%</span>
        <button class="btn icon" id="zoomIn" type="button" title="Zoom in (+)"><svg class="ic"><use href="#i-zoomin"/></svg></button>
        <button class="btn icon" id="fitView" type="button" title="Fit to screen (0)"><svg class="ic"><use href="#i-fit"/></svg></button>
      </div>
      <div class="group">
        <select id="background" aria-label="Canvas background">
          <option value="white">White</option><option value="grid">Dot grid</option><option value="dark">Dark</option>
        </select>
      </div>
      <div class="group push">
        <button class="btn" id="clear" type="button">Clear</button>
        <button class="btn primary" id="download" type="button"><svg class="ic"><use href="#i-download"/></svg>PNG</button>
      </div>
    </div>

    <div class="stage" id="stage">
      <canvas id="board" aria-label="Collaborative whiteboard"></canvas>
      <div id="cursors" class="cursors"></div>
      <textarea id="editor" class="editor" hidden aria-label="Edit text"></textarea>
      <div class="hint" id="hint">Draw with the pen, or pick a shape, text or sticky note from the left.</div>
    </div>
  </main>

  <aside class="people" id="people">
    <h2>Participants <span class="count" id="peopleCount">0</span></h2>
    <ul id="peopleList"></ul>
    <p class="muted">Share the workspace link so others can join and draw with you.</p>
  </aside>
</div>

<footer class="footer">DevGuard &middot; Collaborative drawing workspace &middot; served by {{ color }}</footer>
<div class="toasts" id="toasts" aria-live="polite"></div>

<div class="modal" id="modal" hidden>
  <div class="dialog" role="dialog" aria-modal="true" aria-labelledby="modalTitle">
    <div class="logo big">&#10033;</div>
    <h2 id="modalTitle">Welcome to DevGuard Studio</h2>
    <p class="muted" id="modalText">Create a private workspace or join one with a workspace ID.</p>
    <label class="field">Your name
      <input id="nameInput" type="text" maxlength="24" placeholder="e.g. Asha" autocomplete="nickname">
    </label>
    <div class="actions">
      <button class="btn primary wide" id="createBtn" type="button"><svg class="ic"><use href="#i-plus"/></svg>Create workspace</button>
    </div>
    <div class="or"><span>or join an existing one</span></div>
    <div class="joinrow">
      <input id="roomInput" type="text" maxlength="32" placeholder="Workspace ID" aria-label="Workspace ID">
      <button class="btn" id="joinBtn" type="button">Join</button>
    </div>
    <p class="error" id="modalError" hidden></p>
    <button class="link" id="publicBtn" type="button">Use the shared public canvas</button>
  </div>
</div>

<script src="{{ url_for('static', filename='socket.io.min.js') }}"></script>
<script src="{{ url_for('static', filename='studio.js') }}?v={{ asset_version }}"></script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE, color=APP_COLOR, asset_version=ASSET_VERSION)


@app.route("/health")
def health():
    return jsonify(status="healthy", color=APP_COLOR), 200


@app.route("/metrics")
def metrics():
    return Response(generate_latest(), content_type=CONTENT_TYPE_LATEST)


# ---------------------------------------------------------------------------
# Socket.IO events
# ---------------------------------------------------------------------------
@socketio.on("connect")
def on_connect(auth=None):
    sid = request.sid
    active_connections.inc()
    with state_lock:
        sessions[sid] = {
            "id": secrets.token_hex(3),
            "name": "Guest-" + secrets.token_hex(2),
            "color": USER_COLORS[0],
            "room": DEFAULT_ROOM,
            "explicit": False,
            "cursor_at": 0.0,
        }
        get_room(DEFAULT_ROOM)["members"].add(sid)
        refresh_gauges()
    join_room(DEFAULT_ROOM)


@socketio.on("disconnect")
def on_disconnect(reason=None):
    sid = request.sid
    active_connections.dec()
    with state_lock:
        info = detach(sid)
        sessions.pop(sid, None)
        refresh_gauges()
    announce_leave(info)


@socketio.on("join")
def on_join(data):
    sid = request.sid
    data = data if isinstance(data, dict) else {}
    room_id = secrets.token_urlsafe(6) if data.get("create") else data.get("room", DEFAULT_ROOM)
    if not isinstance(room_id, str) or not ROOM_PATTERN.fullmatch(room_id):
        emit("join_error", {"message": "Workspace IDs use 3 to 32 letters, numbers, - or _."})
        return
    name = clean_name(data.get("name"))

    with state_lock:
        user = sessions.get(sid)
        if user is None:
            return
        room = get_room(room_id)
        if room is None:
            emit("join_error", {"message": "The server has too many workspaces right now."})
            return
        same_room = user["room"] == room_id and user["explicit"]
        full = sum(1 for m in room["members"] if sessions.get(m, {}).get("explicit"))
        if not same_room and full >= MAX_USERS_PER_ROOM:
            emit("join_error", {"message": "This workspace is full."})
            return

        previous = None if same_room else detach(sid)
        old_room = user["room"]
        room["members"].add(sid)
        room["touched"] = time.time()
        user["name"] = name
        if not same_room:
            user["color"] = pick_color(room)
        user["room"] = room_id
        user["explicit"] = True
        people = participants_of(room)
        payload = {
            "room": room_id,
            "you": {"id": user["id"], "name": user["name"], "color": user["color"]},
            "segments": list(room["strokes"]),
            "items": list(room["items"].values()),
            "participants": people,
        }
        refresh_gauges()

    if not same_room:
        if old_room != room_id:
            leave_room(old_room)
        join_room(room_id)
        announce_leave(previous)
    emit("joined", payload)
    socketio.emit("participants", {"participants": people}, to=room_id)
    if not same_room:
        emit(
            "notice",
            {"kind": "join", "name": name, "count": len(people)},
            to=room_id,
            include_self=False,
        )


@socketio.on("draw")
def on_draw(data):
    segment = clean_segment(data)
    if segment is None:
        return
    with state_lock:
        room_id, room = current_room(request.sid)
        if room is None:
            return
        room["strokes"].append(segment)
        room["touched"] = time.time()
        refresh_gauges()
    draw_events.inc()
    emit("draw", segment, to=room_id, include_self=False)


@socketio.on("restore")
def on_restore(data):
    raw = data.get("segments") if isinstance(data, dict) else None
    if not isinstance(raw, list):
        return
    cleaned = [s for s in (clean_segment(x) for x in raw[:RESTORE_LIMIT]) if s]
    if not cleaned:
        return
    with state_lock:
        room_id, room = current_room(request.sid)
        if room is None:
            return
        room["strokes"].extend(cleaned)
        refresh_gauges()
    emit("segments", {"segments": cleaned}, to=room_id, include_self=False)


@socketio.on("undo")
def on_undo(data):
    stroke_id = clean_id(data.get("sid")) if isinstance(data, dict) else None
    if stroke_id is None:
        return
    with state_lock:
        room_id, room = current_room(request.sid)
        if room is None:
            return
        kept = [s for s in room["strokes"] if s.get("sid") != stroke_id]
        removed = len(room["strokes"]) - len(kept)
        if removed:
            room["strokes"].clear()
            room["strokes"].extend(kept)
        refresh_gauges()
    if removed:
        undo_events.inc()
        socketio.emit("undo", {"sid": stroke_id}, to=room_id)


@socketio.on("item")
def on_item(data):
    if not isinstance(data, dict):
        return
    op = data.get("op")
    with state_lock:
        room_id, room = current_room(request.sid)
        if room is None:
            return
        if op == "upsert":
            item = clean_item(data.get("item"))
            if item is None:
                return
            if item["id"] not in room["items"] and len(room["items"]) >= MAX_ITEMS:
                return
            room["items"][item["id"]] = item
            message = {"op": "upsert", "item": item}
        elif op == "delete":
            item_id = clean_id(data.get("id"))
            if item_id is None or room["items"].pop(item_id, None) is None:
                return
            message = {"op": "delete", "id": item_id}
        else:
            return
        room["touched"] = time.time()
    item_events.inc()
    emit("item", message, to=room_id, include_self=False)


@socketio.on("cursor")
def on_cursor(data):
    if not isinstance(data, dict):
        return
    now = time.monotonic()
    with state_lock:
        user = sessions.get(request.sid)
        if user is None or not user["explicit"] or now - user["cursor_at"] < 0.02:
            return
        x = clean_number(data.get("x"), 0, 1)
        y = clean_number(data.get("y"), 0, 1)
        if x is None or y is None:
            return
        user["cursor_at"] = now
        room_id = user["room"]
        message = {"id": user["id"], "name": user["name"], "color": user["color"], "x": x, "y": y}
    emit("cursor", message, to=room_id, include_self=False)


@socketio.on("clear")
def on_clear(data=None):
    with state_lock:
        room_id, room = current_room(request.sid)
        if room is None:
            return
        room["strokes"].clear()
        room["items"].clear()
        refresh_gauges()
    clear_events.inc()
    socketio.emit("clear", to=room_id)


if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=False,
        allow_unsafe_werkzeug=True,
    )
