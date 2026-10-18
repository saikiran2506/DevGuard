
import os
import re

from flask import Flask, render_template_string, jsonify, Response
from flask_socketio import SocketIO
from prometheus_client import (
    Counter,
    Gauge,
    generate_latest,
    CONTENT_TYPE_LATEST,
)

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get(
    "FLASK_SECRET_KEY", "devguard-local-development-only"
)

socketio = SocketIO(app, async_mode="threading")

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

PAGE = """
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>DevGuard Studio</title>
<style>
:root {
    --bg: #f4f6fb;
    --ink: #182238;
    --muted: #7b8498;
    --blue: #536dfe;
    --border: #e5e9f2;
}
* { box-sizing: border-box; }
body {
    margin: 0;
    background: var(--bg);
    color: var(--ink);
    font-family: "Segoe UI", Arial, sans-serif;
}
header {
    background: #182238;
    color: white;
    padding: 18px 5%;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 15px;
    flex-wrap: wrap;
}
.brand { display: flex; align-items: center; gap: 12px; }
.logo {
    width: 42px;
    height: 42px;
    border-radius: 13px;
    display: grid;
    place-items: center;
    background: linear-gradient(135deg, #7890ff, #9b6cff);
    font-size: 24px;
}
.brand h1 { margin: 0; font-size: 20px; }
.brand p { margin: 4px 0 0; color: #b7c1d8; font-size: 12px; }
.status {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 9px 13px;
    border: 1px solid #39455e;
    border-radius: 25px;
    font-size: 13px;
    color: #e5eaff;
}
.dot { width: 8px; height: 8px; border-radius: 50%; background: #f3bb4c; }
.dot.online { background: #42d69a; }
main { max-width: 1200px; margin: auto; padding: 30px 20px; }
.heading {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 15px;
    flex-wrap: wrap;
    margin-bottom: 22px;
}
.heading h2 { margin: 0 0 8px; font-size: clamp(24px, 4vw, 32px); }
.heading p { margin: 0; color: var(--muted); line-height: 1.5; }
.tag {
    background: #e8edff;
    color: #4058d9;
    padding: 9px 13px;
    border-radius: 20px;
    font-size: 11px;
    font-weight: 800;
    letter-spacing: .5px;
}
.panel {
    background: white;
    border: 1px solid var(--border);
    border-radius: 18px;
    overflow: hidden;
    box-shadow: 0 10px 35px #25375c0b;
}
.toolbar {
    display: flex;
    align-items: center;
    gap: 12px;
    flex-wrap: wrap;
    padding: 16px;
    border-bottom: 1px solid var(--border);
}
.group {
    display: flex;
    align-items: center;
    gap: 9px;
    padding-right: 13px;
    border-right: 1px solid var(--border);
}
.group:last-child { border-right: 0; }
.label {
    font-size: 11px;
    font-weight: 800;
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: .5px;
}
input[type=color] {
    width: 36px;
    height: 36px;
    padding: 2px;
    border: 1px solid var(--border);
    border-radius: 9px;
    background: white;
    cursor: pointer;
}
input[type=range] { width: 90px; accent-color: var(--blue); }
#sizeValue { font-size: 12px; color: var(--muted); min-width: 25px; }
button {
    padding: 10px 13px;
    border: 1px solid var(--border);
    background: white;
    border-radius: 10px;
    color: var(--ink);
    font-size: 13px;
    font-weight: 650;
    cursor: pointer;
    transition: background .15s, border-color .15s;
}
button:hover { background: #f3f5ff; border-color: #bbc6ff; }
button.selected { background: #edf0ff; border-color: #bdc8ff; color: #3f58dc; }
button.primary { background: var(--blue); color: white; border-color: var(--blue); }
button.primary:hover { background: #3f57e6; }
.canvas-area { background: #fafbfe; padding: 16px; }
.canvas-info {
    display: flex;
    justify-content: space-between;
    align-items: center;
    flex-wrap: wrap;
    gap: 10px;
    padding: 0 2px 12px;
    color: var(--muted);
    font-size: 12px;
}
canvas {
    display: block;
    width: 100%;
    height: 500px;
    background: white;
    border: 1px solid #dfe5f0;
    border-radius: 12px;
    touch-action: none;
    cursor: crosshair;
}
.footer {
    display: flex;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 10px;
    padding: 15px 18px;
    border-top: 1px solid var(--border);
    color: var(--muted);
    font-size: 12px;
}
footer { text-align: center; padding: 5px 15px 25px; color: #9098aa; font-size: 12px; }
@media(max-width: 650px) {
    main { padding: 22px 11px; }
    .toolbar { gap: 10px; padding: 12px; }
    .group { padding-right: 8px; gap: 6px; }
    .canvas-area { padding: 9px; }
    canvas { height: 380px; }
    button { padding: 9px 10px; }
}
</style>
</head>
<body>
<header>
    <div class="brand">
        <div class="logo">✳</div>
        <div>
            <h1>DevGuard Studio</h1>
            <p>Draw together. Build ideas together.</p>
        </div>
    </div>
    <div class="status">
        <span id="statusDot" class="dot"></span>
        <span id="status">Connecting...</span>
    </div>
</header>

<main>
    <div class="heading">
        <div>
            <h2>Your collaborative canvas</h2>
            <p>A shared space for sketches, ideas and creativity.</p>
        </div>
        <span class="tag">● LIVE WORKSPACE</span>
    </div>

    <section class="panel">
        <div class="toolbar">
            <div class="group">
                <span class="label">Color</span>
                <input id="color" type="color" value="#536dfe" aria-label="Brush color">
            </div>
            <div class="group">
                <span class="label">Size</span>
                <input id="brush" type="range" min="1" max="20" value="4" aria-label="Brush size">
                <span id="sizeValue">4px</span>
            </div>
            <div class="group">
                <button id="pen" class="selected" type="button">✎ Pen</button>
                <button id="eraser" type="button">▱ Eraser</button>
            </div>
            <div class="group">
                <button id="clear" type="button">Clear canvas</button>
            </div>
            <button id="download" class="primary" type="button">↓ Download PNG</button>
        </div>

        <div class="canvas-area">
            <div class="canvas-info">
                <span>✦ &nbsp;Changes appear live in connected tabs</span>
                <span id="activity">Ready to draw</span>
            </div>
            <canvas id="canvas" aria-label="Collaborative drawing canvas"></canvas>
        </div>

        <div class="footer">
            <span>Tip: Open this page in two tabs to collaborate.</span>
            <span>Connection: <strong id="connectionText">Waiting...</strong></span>
        </div>
    </section>
</main>

<footer>DevGuard · Collaborative drawing workspace</footer>

<script src="{{ url_for('static', filename='socket.io.min.js') }}"></script>
<script>
const canvas = document.getElementById("canvas");
const ctx = canvas.getContext("2d");
const colorInput = document.getElementById("color");
const brushInput = document.getElementById("brush");
const sizeValue = document.getElementById("sizeValue");
const activity = document.getElementById("activity");

let socket = null;
let drawing = false;
let previous = null;
let tool = "pen";

function setStatus(connected, message) {
    document.getElementById("status").textContent = message;
    document.getElementById("connectionText").textContent =
        connected ? "Connected" : "Not connected";
    document.getElementById("statusDot").classList.toggle("online", connected);
}

function resizeCanvas() {
    const old = document.createElement("canvas");
    old.width = canvas.width;
    old.height = canvas.height;

    if (old.width && old.height) {
        old.getContext("2d").drawImage(canvas, 0, 0);
    }

    const rect = canvas.getBoundingClientRect();
    canvas.width = Math.round(rect.width);
    canvas.height = Math.round(rect.height);
    ctx.lineCap = "round";
    ctx.lineJoin = "round";

    if (old.width && old.height) {
        ctx.drawImage(old, 0, 0, old.width, old.height,
                      0, 0, canvas.width, canvas.height);
    }
}

function getPoint(event) {
    const rect = canvas.getBoundingClientRect();
    return {
        x: (event.clientX - rect.left) / rect.width,
        y: (event.clientY - rect.top) / rect.height
    };
}

function paint(data) {
    ctx.beginPath();
    ctx.moveTo(data.x0 * canvas.width, data.y0 * canvas.height);
    ctx.lineTo(data.x1 * canvas.width, data.y1 * canvas.height);
    ctx.strokeStyle = data.color;
    ctx.lineWidth = data.width;
    ctx.lineCap = "round";
    ctx.lineJoin = "round";
    ctx.stroke();
}

function startDrawingApp() {
    socket = io({
        reconnection: true,
        reconnectionAttempts: Infinity,
        reconnectionDelay: 1000,
        timeout: 10000
    });

    socket.on("connect", () => {
        setStatus(true, "Connected");
        activity.textContent = "Ready to draw";
    });

    socket.on("disconnect", () => {
        setStatus(false, "Disconnected — reconnecting...");
    });

    socket.on("connect_error", error => {
        setStatus(false, "Connection error");
        activity.textContent = error.message || "Connection failed";
    });

    socket.on("draw", paint);

    socket.on("clear", () => {
        ctx.clearRect(0, 0, canvas.width, canvas.height);
        activity.textContent = "Canvas cleared";
    });

    canvas.addEventListener("pointerdown", event => {
        if (!socket.connected) {
            activity.textContent = "Waiting for server connection";
            return;
        }

        drawing = true;
        previous = getPoint(event);
        canvas.setPointerCapture(event.pointerId);
    });

    canvas.addEventListener("pointermove", event => {
        if (!drawing || !previous || !socket.connected) return;

        const current = getPoint(event);
        const data = {
            x0: previous.x,
            y0: previous.y,
            x1: current.x,
            y1: current.y,
            color: tool === "eraser" ? "#ffffff" : colorInput.value,
            width: Number(brushInput.value)
        };

        paint(data);
        socket.emit("draw", data);
        previous = current;
        activity.textContent = "Drawing...";
    });

    function stopDrawing() {
        drawing = false;
        previous = null;
    }

    canvas.addEventListener("pointerup", stopDrawing);
    canvas.addEventListener("pointercancel", stopDrawing);
    canvas.addEventListener("lostpointercapture", stopDrawing);
}

brushInput.addEventListener("input", () => {
    sizeValue.textContent = brushInput.value + "px";
});

document.getElementById("pen").addEventListener("click", () => {
    tool = "pen";
    document.getElementById("pen").classList.add("selected");
    document.getElementById("eraser").classList.remove("selected");
    canvas.style.cursor = "crosshair";
});

document.getElementById("eraser").addEventListener("click", () => {
    tool = "eraser";
    document.getElementById("eraser").classList.add("selected");
    document.getElementById("pen").classList.remove("selected");
    canvas.style.cursor = "cell";
});

document.getElementById("clear").addEventListener("click", () => {
    if (socket && socket.connected &&
        confirm("Clear the canvas for everyone?")) {
        socket.emit("clear");
    } else if (!socket || !socket.connected) {
        activity.textContent = "Connect to the server before clearing";
    }
});

document.getElementById("download").addEventListener("click", () => {
    const link = document.createElement("a");
    link.download = "devguard-drawing.png";
    link.href = canvas.toDataURL("image/png");
    link.click();
});

resizeCanvas();
window.addEventListener("resize", resizeCanvas);

if (window.io) {
    startDrawingApp();
} else {
    setStatus(false, "Socket.IO library unavailable");
    activity.textContent = "Check internet access and refresh the page";
}
</script>
</body>
</html>
"""


@app.route("/")
def index():
    return render_template_string(PAGE)


@app.route("/health")
def health():
    return jsonify(status="healthy"), 200


@app.route("/metrics")
def metrics():
    return Response(
        generate_latest(),
        content_type=CONTENT_TYPE_LATEST,
    )


@socketio.on("connect")
def on_connect(auth=None):
    active_connections.inc()


@socketio.on("disconnect")
def on_disconnect(reason=None):
    active_connections.dec()


@socketio.on("draw")
def on_draw(data):
    if not isinstance(data, dict):
        return

    required = ("x0", "y0", "x1", "y1", "color", "width")
    if any(key not in data for key in required):
        return

    try:
        for key in ("x0", "y0", "x1", "y1", "width"):
            data[key] = float(data[key])

        if any(
            not 0 <= data[key] <= 1
            for key in ("x0", "y0", "x1", "y1")
        ):
            return

        if not 1 <= data["width"] <= 20:
            return

        if (
            not isinstance(data["color"], str)
            or not re.fullmatch(r"#[0-9a-fA-F]{6}", data["color"])
        ):
            return

    except (TypeError, ValueError):
        return

    draw_events.inc()
    socketio.emit("draw", data, include_self=False)


@socketio.on("clear")
def on_clear():
    clear_events.inc()
    socketio.emit("clear")


if __name__ == "__main__":
    socketio.run(
        app,
        host="0.0.0.0",
        port=5000,
        debug=False,
    )
