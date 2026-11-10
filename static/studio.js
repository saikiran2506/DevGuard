/* DevGuard Studio: real-time collaborative whiteboard (client).
 *
 * World model: the board is BOARD_W x BOARD_H world units. Everything sent over the
 * network uses normalised 0..1 coordinates, so zoom and pan never change what other
 * people receive. Freehand strokes (pen and eraser) are sent as the original "draw"
 * segments; shapes, text and sticky notes are sent as "item" objects.
 */
(function () {
    "use strict";

    var BOARD_W = 1600;
    var BOARD_H = 1000;
    var UNIT = BOARD_W / 800;
    var NOTE_W = 260;
    var NOTE_H = 190;
    var MIN_ZOOM = 0.25;
    var MAX_ZOOM = 4;
    var MAX_LOCAL_SEGMENTS = 20000;
    var SWATCHES = ["#111827", "#ef4444", "#f97316", "#eab308", "#22c55e", "#06b6d4", "#3b82f6", "#a855f7", "#ffffff"];
    var NOTE_COLORS = ["#fde68a", "#fbcfe8", "#bbf7d0", "#bfdbfe", "#fed7aa"];
    var SHAPES = ["line", "arrow", "rect", "ellipse"];
    var ROOM_RE = /^[A-Za-z0-9_-]{3,32}$/;
    var HEX_RE = /^#[0-9a-fA-F]{6}$/;

    function byId(id) { return document.getElementById(id); }

    var board = byId("board");
    var ctx = board.getContext("2d");
    var layer = document.createElement("canvas");
    var layerCtx = layer.getContext("2d");
    var measureCtx = document.createElement("canvas").getContext("2d");
    var stage = byId("stage");
    var editor = byId("editor");
    var cursorBox = byId("cursors");

    var S = {
        tool: "pen", color: "#3b82f6", size: 4, fontSize: 24, noteColor: NOTE_COLORS[0],
        background: "white", view: { scale: 1, ox: 0, oy: 0 }, fit: true, w: 0, h: 0,
        freehand: [], items: new Map(), selection: null, undo: [], redo: [],
        draft: null, drag: null, editing: null, space: false,
        socket: null, joined: false, room: null, me: null, people: [], desired: null,
        stageColor: "#dfe5f1", lastCursorAt: 0, renderQueued: false
    };

    /* ---------------------------------------------------------------- utils */
    function clamp(v, lo, hi) { return Math.min(hi, Math.max(lo, v)); }
    function round4(v) { return Math.round(v * 10000) / 10000; }
    function clone(o) { return JSON.parse(JSON.stringify(o)); }
    function dpr() { return window.devicePixelRatio || 1; }
    function isShape(it) { return SHAPES.indexOf(it.type) !== -1; }

    function newId() {
        var chars = "abcdefghijklmnopqrstuvwxyz0123456789";
        var out = "";
        var buf = null;
        if (window.crypto && window.crypto.getRandomValues) {
            buf = new Uint8Array(10);
            window.crypto.getRandomValues(buf);
        }
        for (var i = 0; i < 10; i++) {
            var n = buf ? buf[i] : Math.floor(Math.random() * 256);
            out += chars.charAt(n % chars.length);
        }
        return out;
    }

    function initials(name) {
        var parts = String(name || "?").trim().split(/\s+/);
        var text = parts[0].charAt(0) + (parts.length > 1 ? parts[1].charAt(0) : "");
        return text.toUpperCase();
    }

    function send(name, data) {
        if (S.socket && S.socket.connected && S.joined) { S.socket.emit(name, data); }
    }

    function toast(message, kind) {
        var box = byId("toasts");
        var el = document.createElement("div");
        el.className = "toast " + (kind || "");
        el.textContent = message;
        box.appendChild(el);
        while (box.children.length > 4) { box.removeChild(box.firstChild); }
        window.setTimeout(function () { el.classList.add("out"); }, 3200);
        window.setTimeout(function () { if (el.parentNode) { el.parentNode.removeChild(el); } }, 3600);
    }

    /* ------------------------------------------------------------ transforms */
    function applyView(c, scale, ox, oy, d) { c.setTransform(d * scale, 0, 0, d * scale, d * ox, d * oy); }

    function toWorld(e) {
        var r = board.getBoundingClientRect();
        var sx = e.clientX - r.left;
        var sy = e.clientY - r.top;
        return { x: (sx - S.view.ox) / S.view.scale, y: (sy - S.view.oy) / S.view.scale, sx: sx, sy: sy };
    }

    function normalise(p) {
        return { x: round4(clamp(p.x / BOARD_W, 0, 1)), y: round4(clamp(p.y / BOARD_H, 0, 1)) };
    }

    function resizeCanvas() {
        var r = stage.getBoundingClientRect();
        S.w = Math.max(1, r.width);
        S.h = Math.max(1, r.height);
        var d = dpr();
        board.width = Math.round(S.w * d);
        board.height = Math.round(S.h * d);
        layer.width = board.width;
        layer.height = board.height;
        if (S.fit) { fitView(); }
        requestRender();
    }

    function fitView() {
        commitEditor();
        var scale = clamp(Math.min((S.w - 48) / BOARD_W, (S.h - 48) / BOARD_H), MIN_ZOOM, MAX_ZOOM);
        S.view.scale = scale;
        S.view.ox = (S.w - BOARD_W * scale) / 2;
        S.view.oy = (S.h - BOARD_H * scale) / 2;
        S.fit = true;
        requestRender();
    }

    function zoomAt(factor, cx, cy) {
        commitEditor();
        var old = S.view.scale;
        var next = clamp(old * factor, MIN_ZOOM, MAX_ZOOM);
        if (next === old) { return; }
        var wx = (cx - S.view.ox) / old;
        var wy = (cy - S.view.oy) / old;
        S.view.scale = next;
        S.view.ox = cx - wx * next;
        S.view.oy = cy - wy * next;
        S.fit = false;
        requestRender();
    }

    function panBy(dx, dy) {
        S.view.ox += dx;
        S.view.oy += dy;
        S.fit = false;
        requestRender();
    }

    /* -------------------------------------------------------------- painting */
    function roundRectPath(c, x, y, w, h, r) {
        c.beginPath();
        c.moveTo(x + r, y);
        c.arcTo(x + w, y, x + w, y + h, r);
        c.arcTo(x + w, y + h, x, y + h, r);
        c.arcTo(x, y + h, x, y, r);
        c.arcTo(x, y, x + w, y, r);
        c.closePath();
    }

    function paintBackground(c, kind) {
        c.fillStyle = kind === "dark" ? "#1b2233" : "#ffffff";
        c.fillRect(0, 0, BOARD_W, BOARD_H);
        if (kind === "grid" || kind === "dark") {
            c.fillStyle = kind === "dark" ? "#3a4560" : "#c3cbd9";
            for (var x = 40; x < BOARD_W; x += 40) {
                for (var y = 40; y < BOARD_H; y += 40) { c.fillRect(x - 1.5, y - 1.5, 3, 3); }
            }
        }
    }

    function sameRun(a, b) {
        return a.sid === b.sid && a.color === b.color && a.width === b.width &&
            Boolean(a.erase) === Boolean(b.erase) && a.x1 === b.x0 && a.y1 === b.y0;
    }

    function paintFreehand(c) {
        var segs = S.freehand;
        var i = 0;
        while (i < segs.length) {
            var j = i + 1;
            while (j < segs.length && sameRun(segs[j - 1], segs[j])) { j++; }
            var s = segs[i];
            c.globalCompositeOperation = s.erase ? "destination-out" : "source-over";
            c.strokeStyle = s.erase ? "#000000" : s.color;
            c.lineWidth = s.width * UNIT;
            c.lineCap = "round";
            c.lineJoin = "round";
            c.beginPath();
            c.moveTo(s.x0 * BOARD_W, s.y0 * BOARD_H);
            for (var k = i; k < j; k++) { c.lineTo(segs[k].x1 * BOARD_W, segs[k].y1 * BOARD_H); }
            c.stroke();
            i = j;
        }
        c.globalCompositeOperation = "source-over";
    }

    function fontFor(size) { return "600 " + size + "px 'Segoe UI', Roboto, Arial, sans-serif"; }

    function splitWord(c, word, maxW) {
        var parts = [];
        var chunk = "";
        for (var i = 0; i < word.length; i++) {
            if (chunk && c.measureText(chunk + word.charAt(i)).width > maxW) { parts.push(chunk); chunk = ""; }
            chunk += word.charAt(i);
        }
        if (chunk) { parts.push(chunk); }
        return parts;
    }

    function wrapLines(c, text, maxW) {
        var out = [];
        text.split("\n").forEach(function (paragraph) {
            var line = "";
            paragraph.split(" ").forEach(function (word) {
                var pieces = c.measureText(word).width > maxW ? splitWord(c, word, maxW) : [word];
                pieces.forEach(function (piece) {
                    var test = line ? line + " " + piece : piece;
                    if (line && c.measureText(test).width > maxW) { out.push(line); line = piece; } else { line = test; }
                });
            });
            out.push(line);
        });
        return out;
    }

    function noteLines(c, it) {
        c.font = fontFor(30);
        var lines = wrapLines(c, it.text || "", NOTE_W - 36);
        var max = Math.floor((NOTE_H - 36) / 38);
        if (lines.length > max) {
            lines = lines.slice(0, max);
            lines[max - 1] = lines[max - 1].replace(/.?.?$/, "") + "...";
        }
        return lines;
    }

    function textMetrics(it) {
        var fs = it.size * 2;
        measureCtx.font = fontFor(fs);
        var lines = String(it.text || "").split("\n");
        var width = 0;
        lines.forEach(function (ln) { width = Math.max(width, measureCtx.measureText(ln).width); });
        return { fs: fs, lines: lines, w: width, h: lines.length * fs * 1.25 };
    }

    function paintArrowHead(c, it) {
        var x0 = it.x0 * BOARD_W, y0 = it.y0 * BOARD_H, x1 = it.x1 * BOARD_W, y1 = it.y1 * BOARD_H;
        var angle = Math.atan2(y1 - y0, x1 - x0);
        var len = Math.max(30, it.width * UNIT * 4.5);
        c.beginPath();
        c.moveTo(x1 - len * Math.cos(angle - Math.PI / 6), y1 - len * Math.sin(angle - Math.PI / 6));
        c.lineTo(x1, y1);
        c.lineTo(x1 - len * Math.cos(angle + Math.PI / 6), y1 - len * Math.sin(angle + Math.PI / 6));
        c.stroke();
    }

    function paintItem(c, it) {
        c.save();
        c.strokeStyle = it.color;
        c.fillStyle = it.color;
        c.lineWidth = (it.width || 4) * UNIT;
        c.lineCap = "round";
        c.lineJoin = "round";
        if (isShape(it)) {
            var x0 = it.x0 * BOARD_W, y0 = it.y0 * BOARD_H, x1 = it.x1 * BOARD_W, y1 = it.y1 * BOARD_H;
            if (it.type === "rect") {
                c.strokeRect(Math.min(x0, x1), Math.min(y0, y1), Math.abs(x1 - x0), Math.abs(y1 - y0));
            } else if (it.type === "ellipse") {
                c.beginPath();
                c.ellipse((x0 + x1) / 2, (y0 + y1) / 2, Math.max(1, Math.abs(x1 - x0) / 2), Math.max(1, Math.abs(y1 - y0) / 2), 0, 0, Math.PI * 2);
                c.stroke();
            } else {
                c.beginPath();
                c.moveTo(x0, y0);
                c.lineTo(x1, y1);
                c.stroke();
                if (it.type === "arrow") { paintArrowHead(c, it); }
            }
        } else if (it.type === "text") {
            var m = textMetrics(it);
            c.font = fontFor(m.fs);
            c.textBaseline = "top";
            m.lines.forEach(function (ln, i) { c.fillText(ln, it.x * BOARD_W, it.y * BOARD_H + i * m.fs * 1.25); });
        } else if (it.type === "note") {
            var nx = it.x * BOARD_W, ny = it.y * BOARD_H;
            c.shadowColor = "rgba(0,0,0,0.22)";
            c.shadowBlur = 14;
            c.shadowOffsetY = 5;
            roundRectPath(c, nx, ny, NOTE_W, NOTE_H, 12);
            c.fill();
            c.shadowColor = "transparent";
            c.fillStyle = "#1f2937";
            c.textBaseline = "top";
            noteLines(c, it).forEach(function (ln, i) { c.fillText(ln, nx + 18, ny + 18 + i * 38); });
        }
        c.restore();
    }

    function paintBoard(target, targetEl, view, d, scratchEl) {
        var scratch = scratchEl.getContext("2d");
        scratch.setTransform(1, 0, 0, 1, 0, 0);
        scratch.clearRect(0, 0, scratchEl.width, scratchEl.height);

        target.save();
        applyView(target, view.scale, view.ox, view.oy, d);
        paintBackground(target, S.background);
        target.restore();

        scratch.save();
        applyView(scratch, view.scale, view.ox, view.oy, d);
        scratch.beginPath();
        scratch.rect(0, 0, BOARD_W, BOARD_H);
        scratch.clip();
        paintFreehand(scratch);
        scratch.restore();
        target.setTransform(1, 0, 0, 1, 0, 0);
        target.drawImage(scratchEl, 0, 0);

        target.save();
        applyView(target, view.scale, view.ox, view.oy, d);
        target.beginPath();
        target.rect(0, 0, BOARD_W, BOARD_H);
        target.clip();
        S.items.forEach(function (it) {
            if (!S.editing || S.editing.id !== it.id) { paintItem(target, it); }
        });
        target.restore();
    }

    function itemBounds(it) {
        if (isShape(it)) {
            var pad = (it.width * UNIT) / 2 + 8;
            var x0 = Math.min(it.x0, it.x1) * BOARD_W, x1 = Math.max(it.x0, it.x1) * BOARD_W;
            var y0 = Math.min(it.y0, it.y1) * BOARD_H, y1 = Math.max(it.y0, it.y1) * BOARD_H;
            return { x: x0 - pad, y: y0 - pad, w: x1 - x0 + 2 * pad, h: y1 - y0 + 2 * pad };
        }
        if (it.type === "note") { return { x: it.x * BOARD_W, y: it.y * BOARD_H, w: NOTE_W, h: NOTE_H }; }
        var m = textMetrics(it);
        return { x: it.x * BOARD_W - 4, y: it.y * BOARD_H - 4, w: m.w + 8, h: m.h + 8 };
    }

    function render() {
        S.renderQueued = false;
        if (!S.w) { return; }
        var d = dpr();
        ctx.setTransform(1, 0, 0, 1, 0, 0);
        ctx.fillStyle = S.stageColor;
        ctx.fillRect(0, 0, board.width, board.height);

        ctx.save();
        applyView(ctx, S.view.scale, S.view.ox, S.view.oy, d);
        ctx.shadowColor = "rgba(0,0,0,0.28)";
        ctx.shadowBlur = 18;
        ctx.fillStyle = "#ffffff";
        ctx.fillRect(0, 0, BOARD_W, BOARD_H);
        ctx.restore();

        paintBoard(ctx, board, S.view, d, layer);

        ctx.save();
        applyView(ctx, S.view.scale, S.view.ox, S.view.oy, d);
        if (S.draft) { paintItem(ctx, S.draft); }
        var selected = S.selection && S.items.get(S.selection);
        if (selected) {
            var b = itemBounds(selected);
            ctx.setLineDash([10 / S.view.scale, 7 / S.view.scale]);
            ctx.lineWidth = 2 / S.view.scale;
            ctx.strokeStyle = "#4f46e5";
            ctx.strokeRect(b.x, b.y, b.w, b.h);
        }
        ctx.restore();

        placeAllCursors();
        byId("zoomValue").textContent = Math.round(S.view.scale * 100) + "%";
    }

    function requestRender() {
        if (S.renderQueued) { return; }
        S.renderQueued = true;
        if (window.requestAnimationFrame) { window.requestAnimationFrame(render); } else { window.setTimeout(render, 16); }
    }

    /* ------------------------------------------------------------ hit testing */
    function distToSegment(px, py, x0, y0, x1, y1) {
        var dx = x1 - x0, dy = y1 - y0;
        var len2 = dx * dx + dy * dy;
        var t = len2 ? clamp(((px - x0) * dx + (py - y0) * dy) / len2, 0, 1) : 0;
        return Math.hypot(px - (x0 + t * dx), py - (y0 + t * dy));
    }

    function hitItem(it, wx, wy) {
        var tol = Math.max(10, 12 / S.view.scale);
        if (it.type === "line" || it.type === "arrow") {
            return distToSegment(wx, wy, it.x0 * BOARD_W, it.y0 * BOARD_H, it.x1 * BOARD_W, it.y1 * BOARD_H) <= tol + (it.width * UNIT) / 2;
        }
        if (it.type === "ellipse") {
            var b = itemBounds(it);
            var rx = Math.max(1, b.w / 2), ry = Math.max(1, b.h / 2);
            var nx = (wx - (b.x + rx)) / rx, ny = (wy - (b.y + ry)) / ry;
            return nx * nx + ny * ny <= 1.02;
        }
        var bb = itemBounds(it);
        return wx >= bb.x && wx <= bb.x + bb.w && wy >= bb.y && wy <= bb.y + bb.h;
    }

    function hitTest(wx, wy) {
        var all = Array.from(S.items.values());
        for (var i = all.length - 1; i >= 0; i--) { if (hitItem(all[i], wx, wy)) { return all[i]; } }
        return null;
    }

    /* ---------------------------------------------------------- undo and redo */
    function updateButtons() {
        byId("undo").disabled = S.undo.length === 0;
        byId("redo").disabled = S.redo.length === 0;
        byId("deleteItem").disabled = !S.selection;
    }

    function pushAction(action) {
        S.undo.push(action);
        if (S.undo.length > 100) { S.undo.shift(); }
        S.redo.length = 0;
        updateButtons();
    }

    function putItem(it) {
        S.items.set(it.id, clone(it));
        send("item", { op: "upsert", item: it });
    }

    function removeItem(id) {
        S.items.delete(id);
        if (S.selection === id) { S.selection = null; }
        send("item", { op: "delete", id: id });
    }

    function removeStroke(sid) {
        S.freehand = S.freehand.filter(function (s) { return s.sid !== sid; });
    }

    function restoreSegments(segs) {
        for (var i = 0; i < segs.length; i += 2000) { send("restore", { segments: segs.slice(i, i + 2000) }); }
    }

    function revert(a) {
        if (a.t === "stroke") { removeStroke(a.sid); send("undo", { sid: a.sid }); }
        else if (a.t === "add") { removeItem(a.item.id); }
        else if (a.t === "edit") { putItem(a.before); }
        else if (a.t === "del") { putItem(a.item); }
    }

    function reapply(a) {
        if (a.t === "stroke") { a.segs.forEach(function (s) { S.freehand.push(s); }); restoreSegments(a.segs); }
        else if (a.t === "add") { putItem(a.item); }
        else if (a.t === "edit") { putItem(a.after); }
        else if (a.t === "del") { removeItem(a.item.id); }
    }

    function doUndo() {
        commitEditor();
        var a = S.undo.pop();
        if (!a) { return; }
        revert(a);
        S.redo.push(a);
        updateButtons();
        requestRender();
    }

    function doRedo() {
        commitEditor();
        var a = S.redo.pop();
        if (!a) { return; }
        reapply(a);
        S.undo.push(a);
        updateButtons();
        requestRender();
    }

    function deleteSelected() {
        var it = S.selection && S.items.get(S.selection);
        if (!it) { return; }
        var before = clone(it);
        removeItem(it.id);
        pushAction({ t: "del", item: before });
        updateButtons();
        requestRender();
    }

    function clearAll() {
        commitEditor();
        if (!window.confirm("Clear the whole canvas for everyone in this workspace?")) { return; }
        S.freehand = [];
        S.items.clear();
        S.selection = null;
        S.undo = [];
        S.redo = [];
        send("clear");
        updateButtons();
        requestRender();
    }

    /* -------------------------------------------------------- text and notes */
    function openEditor(it, isNew) {
        commitEditor();
        var b = itemBounds(it);
        var left = S.view.ox + b.x * S.view.scale;
        var top = S.view.oy + b.y * S.view.scale;
        S.editing = { id: it.id, isNew: isNew, type: it.type, before: isNew ? null : clone(it), draft: isNew ? it : null };
        editor.hidden = false;
        editor.className = "editor" + (it.type === "note" ? " note" : "");
        editor.value = it.text || "";
        editor.style.left = left + "px";
        editor.style.top = top + "px";
        if (it.type === "note") {
            editor.style.width = NOTE_W * S.view.scale + "px";
            editor.style.height = NOTE_H * S.view.scale + "px";
            editor.style.fontSize = 30 * S.view.scale + "px";
            editor.style.background = it.color;
            editor.style.color = "#1f2937";
        } else {
            editor.style.width = Math.max(200, b.w * S.view.scale + 40) + "px";
            editor.style.height = Math.max(52, b.h * S.view.scale + 18) + "px";
            editor.style.fontSize = it.size * 2 * S.view.scale + "px";
            editor.style.background = "rgba(255,255,255,0.92)";
            editor.style.color = it.color === "#ffffff" ? "#111827" : it.color;
        }
        requestRender();
        editor.focus();
    }

    function commitEditor(cancel) {
        var ed = S.editing;
        if (!ed) { return; }
        S.editing = null;
        editor.hidden = true;
        var text = cancel ? null : editor.value.slice(0, 500);
        if (text !== null) { finishEdit(ed, text); }
        requestRender();
    }

    function finishEdit(ed, text) {
        if (ed.isNew) {
            if (ed.type === "text" && !text.trim()) { return; }
            var created = ed.draft;
            created.text = text;
            putItem(created);
            pushAction({ t: "add", item: clone(created) });
            S.selection = created.id;
            setTool("select");
        } else if (ed.type === "text" && !text.trim()) {
            removeItem(ed.id);
            pushAction({ t: "del", item: ed.before });
        } else if (text !== ed.before.text) {
            var after = clone(S.items.get(ed.id) || ed.before);
            after.text = text;
            putItem(after);
            pushAction({ t: "edit", before: ed.before, after: clone(after) });
        }
        updateButtons();
    }

    /* ------------------------------------------------------------- pointers */
    function constrain(start, end, type, shift) {
        if (!shift) { return end; }
        var dx = (end.x - start.x) * BOARD_W, dy = (end.y - start.y) * BOARD_H;
        if (type === "rect" || type === "ellipse") {
            var side = Math.max(Math.abs(dx), Math.abs(dy));
            return { x: start.x + Math.sign(dx || 1) * side / BOARD_W, y: start.y + Math.sign(dy || 1) * side / BOARD_H };
        }
        var angle = Math.round(Math.atan2(dy, dx) / (Math.PI / 4)) * (Math.PI / 4);
        var len = Math.hypot(dx, dy);
        return { x: start.x + Math.cos(angle) * len / BOARD_W, y: start.y + Math.sin(angle) * len / BOARD_H };
    }

    function addSegment(a, b) {
        var d = S.drag;
        var seg = { x0: a.x, y0: a.y, x1: b.x, y1: b.y, color: d.color, width: d.width, sid: d.sid };
        if (d.erase) { seg.erase = true; }
        S.freehand.push(seg);
        d.segs.push(seg);
        if (S.freehand.length > MAX_LOCAL_SEGMENTS) { S.freehand.shift(); }
        send("draw", seg);
        requestRender();
    }

    function beginStroke(n) {
        var erase = S.tool === "eraser";
        S.drag = {
            kind: "stroke", sid: newId(), erase: erase, last: n, segs: [],
            color: erase ? "#ffffff" : S.color, width: erase ? Math.min(20, S.size * 3) : S.size
        };
        addSegment(n, n);
    }

    function shifted(it, dx, dy) {
        var o = clone(it);
        if (isShape(it)) {
            dx = clamp(dx, -Math.min(it.x0, it.x1), 1 - Math.max(it.x0, it.x1));
            dy = clamp(dy, -Math.min(it.y0, it.y1), 1 - Math.max(it.y0, it.y1));
            o.x0 = round4(it.x0 + dx); o.x1 = round4(it.x1 + dx);
            o.y0 = round4(it.y0 + dy); o.y1 = round4(it.y1 + dy);
        } else {
            var maxX = 1 - (it.type === "note" ? NOTE_W / BOARD_W : 0.02);
            var maxY = 1 - (it.type === "note" ? NOTE_H / BOARD_H : 0.03);
            o.x = round4(it.x + clamp(dx, -it.x, maxX - it.x));
            o.y = round4(it.y + clamp(dy, -it.y, maxY - it.y));
        }
        return o;
    }

    function startPan(e) {
        commitEditor();
        S.drag = { kind: "pan", x: e.clientX, y: e.clientY };
        stage.classList.add("panning", "active");
    }

    function onPointerDown(e) {
        if (e.button !== undefined && e.button !== 0 && e.button !== 1) { return; }
        hideHint();
        if (e.button === 1 || S.tool === "hand" || S.space) { startPan(e); return; }
        if (S.editing) { commitEditor(); }
        if (board.setPointerCapture && e.pointerId !== undefined) {
            try { board.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
        }
        var w = toWorld(e);
        var n = normalise(w);
        var tool = S.tool;
        if (tool === "pen" || tool === "eraser") { beginStroke(n); }
        else if (SHAPES.indexOf(tool) !== -1) {
            S.draft = { id: newId(), type: tool, x0: n.x, y0: n.y, x1: n.x, y1: n.y, color: S.color, width: S.size };
            S.drag = { kind: "shape", start: n };
        } else if (tool === "text") {
            var hitText = hitTest(w.x, w.y);
            if (hitText && hitText.type === "text") { S.selection = hitText.id; openEditor(hitText, false); }
            else { openEditor({ id: newId(), type: "text", x: n.x, y: n.y, text: "", color: S.color, size: S.fontSize }, true); }
        } else if (tool === "note") {
            var nx = round4(clamp(n.x - NOTE_W / 2 / BOARD_W, 0, 1 - NOTE_W / BOARD_W));
            var ny = round4(clamp(n.y - NOTE_H / 2 / BOARD_H, 0, 1 - NOTE_H / BOARD_H));
            openEditor({ id: newId(), type: "note", x: nx, y: ny, text: "", color: S.noteColor }, true);
        } else if (tool === "select") {
            var hit = hitTest(w.x, w.y);
            S.selection = hit ? hit.id : null;
            if (hit) { S.drag = { kind: "move", id: hit.id, start: n, before: clone(hit), moved: false, sentAt: 0 }; }
            syncOptions();
            updateButtons();
        }
        requestRender();
    }

    function onPointerMove(e) {
        var d = S.drag;
        if (d && d.kind === "pan") {
            panBy(e.clientX - d.x, e.clientY - d.y);
            d.x = e.clientX;
            d.y = e.clientY;
            return;
        }
        var w = toWorld(e);
        var n = normalise(w);
        emitCursor(n);
        if (!d) { return; }
        if (d.kind === "stroke") {
            if (n.x !== d.last.x || n.y !== d.last.y) { addSegment(d.last, n); d.last = n; }
        } else if (d.kind === "shape" && S.draft) {
            var end = constrain(d.start, n, S.draft.type, e.shiftKey);
            S.draft.x1 = round4(clamp(end.x, 0, 1));
            S.draft.y1 = round4(clamp(end.y, 0, 1));
            requestRender();
        } else if (d.kind === "move") {
            var moved = shifted(d.before, n.x - d.start.x, n.y - d.start.y);
            S.items.set(moved.id, moved);
            d.moved = true;
            var now = Date.now();
            if (now - d.sentAt > 60) { d.sentAt = now; send("item", { op: "upsert", item: moved }); }
            requestRender();
        }
    }

    function onPointerUp() {
        var d = S.drag;
        if (!d) { return; }
        S.drag = null;
        stage.classList.remove("active");
        if (d.kind === "pan") { return; }
        if (d.kind === "stroke") {
            pushAction({ t: "stroke", sid: d.sid, segs: d.segs });
        } else if (d.kind === "shape" && S.draft) {
            var it = S.draft;
            S.draft = null;
            var length = Math.hypot((it.x1 - it.x0) * BOARD_W, (it.y1 - it.y0) * BOARD_H);
            if (length > 6) { putItem(it); pushAction({ t: "add", item: clone(it) }); }
        } else if (d.kind === "move" && d.moved) {
            var after = clone(S.items.get(d.id));
            putItem(after);
            pushAction({ t: "edit", before: d.before, after: after });
        }
        updateButtons();
        requestRender();
    }

    function onDoubleClick(e) {
        if (S.tool === "hand" || S.space) { return; }
        var w = toWorld(e);
        var hit = hitTest(w.x, w.y);
        if (hit && (hit.type === "text" || hit.type === "note")) {
            S.drag = null;
            S.selection = hit.id;
            setTool("select");
            openEditor(hit, false);
        }
    }

    function onWheel(e) {
        e.preventDefault();
        if (e.ctrlKey || e.metaKey) {
            var w = toWorld(e);
            zoomAt(Math.exp(-e.deltaY * 0.0015), w.sx, w.sy);
        } else {
            commitEditor();
            panBy(-e.deltaX, -e.deltaY);
        }
    }

    /* -------------------------------------------------------------- tool UI */
    function syncOptions() {
        var t = S.tool;
        var sel = S.selection && S.items.get(S.selection);
        var drawsColor = t === "pen" || SHAPES.indexOf(t) !== -1 || t === "text" || (sel && sel.type !== "note");
        byId("colorGroup").hidden = !drawsColor;
        byId("sizeGroup").hidden = !(t === "pen" || t === "eraser" || SHAPES.indexOf(t) !== -1 || (sel && isShape(sel)));
        byId("fontGroup").hidden = !(t === "text" || (sel && sel.type === "text"));
        byId("noteGroup").hidden = !(t === "note" || (sel && sel.type === "note"));
        byId("sizeLabel").textContent = t === "eraser" ? "Eraser" : "Size";
    }

    function setTool(name) {
        commitEditor();
        S.tool = name;
        if (name !== "select") { S.selection = null; }
        var buttons = document.querySelectorAll(".tool");
        for (var i = 0; i < buttons.length; i++) {
            buttons[i].classList.toggle("active", buttons[i].getAttribute("data-tool") === name);
        }
        stage.setAttribute("data-tool", name);
        syncOptions();
        updateButtons();
        requestRender();
    }

    function editSelected(change) {
        var it = S.selection && S.items.get(S.selection);
        if (!it) { return false; }
        var before = clone(it);
        var after = clone(it);
        change(after);
        if (JSON.stringify(before) === JSON.stringify(after)) { return true; }
        putItem(after);
        pushAction({ t: "edit", before: before, after: clone(after) });
        requestRender();
        return true;
    }

    function setColor(color) {
        S.color = color;
        byId("color").value = color;
        var swatches = byId("swatches").querySelectorAll(".swatch");
        for (var i = 0; i < swatches.length; i++) {
            swatches[i].classList.toggle("selected", swatches[i].getAttribute("data-color") === color);
        }
        var sel = S.selection && S.items.get(S.selection);
        if (sel && sel.type !== "note") { editSelected(function (it) { it.color = color; }); }
        else if (S.tool === "eraser" || S.tool === "hand" || S.tool === "select" || S.tool === "note") { setTool("pen"); }
    }

    function setBrush(value) {
        S.size = clamp(Math.round(value), 1, 20);
        byId("brush").value = String(S.size);
        byId("sizeValue").textContent = S.size + "px";
        var sel = S.selection && S.items.get(S.selection);
        if (sel && isShape(sel)) { editSelected(function (it) { it.width = S.size; }); }
    }

    function setNoteColor(color) {
        S.noteColor = color;
        var items = byId("noteSwatches").querySelectorAll(".swatch");
        for (var i = 0; i < items.length; i++) {
            items[i].classList.toggle("selected", items[i].getAttribute("data-color") === color);
        }
        var sel = S.selection && S.items.get(S.selection);
        if (sel && sel.type === "note") { editSelected(function (it) { it.color = color; }); }
        else if (S.tool !== "note") { setTool("note"); }
    }

    function buildSwatches() {
        function make(box, list, cls, handler) {
            list.forEach(function (c) {
                var b = document.createElement("button");
                b.type = "button";
                b.className = "swatch " + cls;
                b.style.background = c;
                b.setAttribute("data-color", c);
                b.setAttribute("aria-label", "Color " + c);
                b.addEventListener("click", function () { handler(c); });
                box.appendChild(b);
            });
        }
        make(byId("swatches"), SWATCHES, "", setColor);
        make(byId("noteSwatches"), NOTE_COLORS, "note", setNoteColor);
    }

    function setBackground(kind) {
        S.background = kind;
        if (kind === "dark" && S.color === "#111827") { setColor("#f8fafc"); }
        if (kind !== "dark" && S.color === "#f8fafc") { setColor("#111827"); }
        requestRender();
    }

    function hideHint() { byId("hint").classList.add("hide"); }

    /* ---------------------------------------------------------- participants */
    function renderPeople() {
        var list = byId("peopleList");
        list.innerHTML = "";
        var avatars = byId("avatars");
        avatars.innerHTML = "";
        S.people.forEach(function (p, index) {
            var li = document.createElement("li");
            var av = document.createElement("div");
            av.className = "avatar";
            av.style.background = HEX_RE.test(p.color) ? p.color : "#64748b";
            av.textContent = initials(p.name);
            var name = document.createElement("span");
            name.textContent = p.name;
            li.appendChild(av);
            li.appendChild(name);
            if (S.me && p.id === S.me.id) {
                var you = document.createElement("span");
                you.className = "you";
                you.textContent = "(you)";
                li.appendChild(you);
            }
            list.appendChild(li);
            if (index < 5) {
                var small = av.cloneNode(true);
                small.title = p.name;
                avatars.appendChild(small);
            }
        });
        if (S.people.length > 5) {
            var more = document.createElement("div");
            more.className = "avatar more";
            more.textContent = "+" + (S.people.length - 5);
            avatars.appendChild(more);
        }
        byId("userCount").textContent = String(S.people.length);
        byId("peopleCount").textContent = String(S.people.length);
    }

    /* --------------------------------------------------------------- cursors */
    var cursors = {};

    function placeCursor(c) {
        var sx = S.view.ox + c.x * BOARD_W * S.view.scale;
        var sy = S.view.oy + c.y * BOARD_H * S.view.scale;
        c.el.style.transform = "translate(" + sx + "px," + sy + "px)";
    }

    function placeAllCursors() {
        Object.keys(cursors).forEach(function (id) { placeCursor(cursors[id]); });
    }

    function showCursor(m) {
        var c = cursors[m.id];
        if (!c) {
            var el = document.createElement("div");
            el.className = "cursor";
            var color = HEX_RE.test(m.color) ? m.color : "#64748b";
            el.innerHTML = "<svg viewBox='0 0 24 24'><path d='M4 3l16 8-7 2-3 7z' stroke='#ffffff' stroke-width='1.5'></path></svg><span></span>";
            el.querySelector("path").setAttribute("fill", color);
            var label = el.querySelector("span");
            label.textContent = m.name;
            label.style.background = color;
            cursorBox.appendChild(el);
            c = { el: el, x: 0, y: 0, seen: 0 };
            cursors[m.id] = c;
        }
        c.x = m.x;
        c.y = m.y;
        c.seen = Date.now();
        c.el.classList.remove("idle");
        placeCursor(c);
    }

    function dropCursor(id) {
        var c = cursors[id];
        if (c && c.el.parentNode) { c.el.parentNode.removeChild(c.el); }
        delete cursors[id];
    }

    function clearCursors() { Object.keys(cursors).forEach(dropCursor); }

    function emitCursor(n) {
        var now = Date.now();
        if (now - S.lastCursorAt < 40) { return; }
        S.lastCursorAt = now;
        send("cursor", { x: n.x, y: n.y });
    }

    /* ------------------------------------------------------- workspace flow */
    function setStatus(connected, message) {
        byId("status").textContent = message;
        byId("statusDot").classList.toggle("online", connected);
    }

    function showModal(text, error) {
        byId("modalText").textContent = text;
        var box = byId("modalError");
        box.hidden = !error;
        box.textContent = error || "";
        byId("modal").hidden = false;
        byId("nameInput").focus();
    }

    function hideModal() { byId("modal").hidden = true; }

    function savedName() {
        try { return window.localStorage.getItem("devguard-name") || ""; } catch (err) { return ""; }
    }

    function rememberName(name) {
        try { window.localStorage.setItem("devguard-name", name); } catch (err) { /* ignore */ }
    }

    function joinWorkspace(options) {
        var name = (byId("nameInput").value || "").trim() || savedName();
        rememberName(name);
        S.desired = { room: options.room || null, create: Boolean(options.create), name: name };
        if (S.socket && S.socket.connected) { S.socket.emit("join", S.desired); }
    }

    function workspaceUrl() {
        return window.location.origin + window.location.pathname + "?room=" + encodeURIComponent(S.room || "");
    }

    function copyLink() {
        if (!S.room) { return; }
        var url = workspaceUrl();
        var done = function () { toast("Workspace link copied", "join"); };
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(url).then(done, function () { fallbackCopy(url, done); });
        } else { fallbackCopy(url, done); }
    }

    function fallbackCopy(text, done) {
        var area = document.createElement("textarea");
        area.value = text;
        document.body.appendChild(area);
        area.select();
        try { document.execCommand("copy"); done(); } catch (err) { toast("Copy this link: " + text); }
        document.body.removeChild(area);
    }

    function onJoined(data) {
        S.joined = true;
        S.room = data.room;
        S.me = data.you;
        S.people = data.participants || [];
        S.freehand = (data.segments || []).slice();
        S.items = new Map();
        (data.items || []).forEach(function (it) { S.items.set(it.id, it); });
        S.selection = null;
        S.undo = [];
        S.redo = [];
        S.desired = { room: data.room, create: false, name: data.you.name };
        clearCursors();
        byId("roomId").textContent = data.room;
        byId("workspaceChip").hidden = false;
        try { window.history.replaceState(null, "", "?room=" + encodeURIComponent(data.room)); } catch (err) { /* ignore */ }
        hideModal();
        renderPeople();
        updateButtons();
        syncOptions();
        requestRender();
        toast("You joined workspace " + data.room, "join");
    }

    function connectSocket() {
        if (typeof io === "undefined") {
            setStatus(false, "Socket.IO library unavailable");
            return;
        }
        var socket = io();
        S.socket = socket;
        socket.on("connect", function () {
            setStatus(true, "Connected");
            if (S.desired) { socket.emit("join", S.desired); }
        });
        socket.on("disconnect", function () {
            S.joined = false;
            setStatus(false, "Reconnecting...");
            toast("Connection lost. Reconnecting...", "error");
        });
        socket.on("connect_error", function () { setStatus(false, "Connection problem"); });
        socket.on("joined", function (data) {
            var again = S.room === data.room && S.joined === false && S.me !== null;
            onJoined(data);
            if (again) { toast("Reconnected", "join"); }
        });
        socket.on("join_error", function (data) { showModal("Could not join that workspace.", data && data.message); });
        socket.on("participants", function (data) { S.people = data.participants || []; renderPeople(); });
        socket.on("notice", function (m) {
            toast(m.name + (m.kind === "join" ? " joined the workspace" : " left the workspace"), m.kind);
        });
        socket.on("draw", function (seg) {
            S.freehand.push(seg);
            if (S.freehand.length > MAX_LOCAL_SEGMENTS) { S.freehand.shift(); }
            requestRender();
        });
        socket.on("segments", function (data) {
            (data.segments || []).forEach(function (s) { S.freehand.push(s); });
            requestRender();
        });
        socket.on("undo", function (data) { removeStroke(data.sid); requestRender(); });
        socket.on("item", function (m) {
            if (m.op === "upsert") { S.items.set(m.item.id, m.item); }
            else if (m.op === "delete") {
                S.items.delete(m.id);
                if (S.selection === m.id) { S.selection = null; updateButtons(); }
            }
            requestRender();
        });
        socket.on("clear", function () {
            S.freehand = []; S.items.clear(); S.selection = null; S.undo = []; S.redo = [];
            updateButtons();
            requestRender();
        });
        socket.on("cursor", showCursor);
        socket.on("cursor_gone", function (m) { dropCursor(m.id); });
    }

    /* ---------------------------------------------------------- export, theme */
    function exportPng() {
        commitEditor();
        var out = document.createElement("canvas");
        out.width = BOARD_W;
        out.height = BOARD_H;
        var scratch = document.createElement("canvas");
        scratch.width = BOARD_W;
        scratch.height = BOARD_H;
        paintBoard(out.getContext("2d"), out, { scale: 1, ox: 0, oy: 0 }, 1, scratch);
        var link = document.createElement("a");
        link.download = "devguard-" + (S.room || "canvas") + ".png";
        link.href = out.toDataURL("image/png");
        document.body.appendChild(link);
        link.click();
        document.body.removeChild(link);
        toast("PNG exported");
    }

    function readStageColor() {
        var value = window.getComputedStyle(document.documentElement).getPropertyValue("--stage");
        S.stageColor = (value && value.trim()) || "#dfe5f1";
    }

    function applyTheme(name) {
        document.documentElement.setAttribute("data-theme", name);
        try { window.localStorage.setItem("devguard-theme", name); } catch (err) { /* ignore */ }
        readStageColor();
        requestRender();
    }

    function initTheme() {
        var saved = null;
        try { saved = window.localStorage.getItem("devguard-theme"); } catch (err) { saved = null; }
        if (saved === "dark" || saved === "light") { document.documentElement.setAttribute("data-theme", saved); }
        readStageColor();
        byId("theme").addEventListener("click", function () {
            applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");
        });
    }

    /* -------------------------------------------------------------- keyboard */
    var TOOL_KEYS = { v: "select", h: "hand", p: "pen", e: "eraser", l: "line", a: "arrow", r: "rect", o: "ellipse", t: "text", n: "note" };

    function onKeyDown(e) {
        var tag = e.target && e.target.tagName ? e.target.tagName : "";
        if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") { return; }
        if (!byId("modal").hidden) { return; }
        var key = (e.key || "").toLowerCase();
        var mod = e.ctrlKey || e.metaKey;
        if (mod && key === "z" && !e.shiftKey) { e.preventDefault(); doUndo(); return; }
        if (mod && (key === "y" || (key === "z" && e.shiftKey))) { e.preventDefault(); doRedo(); return; }
        if (mod) { return; }
        if (key === " ") { e.preventDefault(); S.space = true; stage.classList.add("panning"); return; }
        if (key === "delete" || key === "backspace") { deleteSelected(); return; }
        if (key === "escape") { S.draft = null; S.drag = null; S.selection = null; updateButtons(); requestRender(); return; }
        if (key === "+" || key === "=") { zoomAt(1.2, S.w / 2, S.h / 2); return; }
        if (key === "-") { zoomAt(1 / 1.2, S.w / 2, S.h / 2); return; }
        if (key === "0") { fitView(); return; }
        if (key === "[") { setBrush(S.size - 1); return; }
        if (key === "]") { setBrush(S.size + 1); return; }
        if (TOOL_KEYS[key]) { setTool(TOOL_KEYS[key]); }
    }

    function onKeyUp(e) {
        if ((e.key || "") === " ") { S.space = false; stage.classList.remove("panning"); }
    }

    /* ------------------------------------------------------------------ init */
    function bindUi() {
        var buttons = document.querySelectorAll(".tool");
        for (var i = 0; i < buttons.length; i++) {
            (function (b) { b.addEventListener("click", function () { setTool(b.getAttribute("data-tool")); }); })(buttons[i]);
        }
        byId("color").addEventListener("input", function (e) { setColor(e.target.value); });
        byId("brush").addEventListener("input", function (e) { setBrush(Number(e.target.value)); });
        byId("fontSize").addEventListener("change", function (e) {
            S.fontSize = Number(e.target.value);
            var sel = S.selection && S.items.get(S.selection);
            if (sel && sel.type === "text") { editSelected(function (it) { it.size = S.fontSize; }); }
        });
        byId("background").addEventListener("change", function (e) { setBackground(e.target.value); });
        byId("undo").addEventListener("click", doUndo);
        byId("redo").addEventListener("click", doRedo);
        byId("deleteItem").addEventListener("click", deleteSelected);
        byId("zoomIn").addEventListener("click", function () { zoomAt(1.2, S.w / 2, S.h / 2); });
        byId("zoomOut").addEventListener("click", function () { zoomAt(1 / 1.2, S.w / 2, S.h / 2); });
        byId("fitView").addEventListener("click", fitView);
        byId("clear").addEventListener("click", clearAll);
        byId("download").addEventListener("click", exportPng);
        byId("copyLink").addEventListener("click", copyLink);
        byId("switchRoom").addEventListener("click", function () {
            showModal("Create a new workspace, or join another one with its ID.");
        });

        board.addEventListener("pointerdown", onPointerDown);
        board.addEventListener("pointermove", onPointerMove);
        board.addEventListener("dblclick", onDoubleClick);
        board.addEventListener("wheel", onWheel, { passive: false });
        window.addEventListener("pointerup", onPointerUp);
        window.addEventListener("pointercancel", onPointerUp);
        window.addEventListener("keydown", onKeyDown);
        window.addEventListener("keyup", onKeyUp);
        window.addEventListener("resize", resizeCanvas);
        if (window.ResizeObserver) { new window.ResizeObserver(resizeCanvas).observe(stage); }

        editor.addEventListener("keydown", function (e) {
            if (e.key === "Escape") { e.preventDefault(); commitEditor(true); }
            else if (e.key === "Enter" && S.editing && S.editing.type === "text" && !e.shiftKey) { e.preventDefault(); commitEditor(); }
            else if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); commitEditor(); }
        });
        editor.addEventListener("blur", function () { commitEditor(); });

        byId("createBtn").addEventListener("click", function () { joinWorkspace({ create: true }); });
        byId("joinBtn").addEventListener("click", function () {
            var room = (byId("roomInput").value || "").trim();
            if (!ROOM_RE.test(room)) { showModal("Enter a valid workspace ID.", "Workspace IDs use 3 to 32 letters, numbers, - or _."); return; }
            joinWorkspace({ room: room });
        });
        byId("publicBtn").addEventListener("click", function () { joinWorkspace({ room: "main" }); });
        byId("nameInput").addEventListener("keydown", function (e) {
            if (e.key === "Enter") { byId("createBtn").click(); }
        });
        byId("roomInput").addEventListener("keydown", function (e) {
            if (e.key === "Enter") { byId("joinBtn").click(); }
        });
    }

    function startWorkspaceFlow() {
        var params = new window.URLSearchParams(window.location.search);
        var urlRoom = params.get("room");
        byId("nameInput").value = savedName();
        if (urlRoom && ROOM_RE.test(urlRoom)) {
            byId("roomInput").value = urlRoom;
            if (savedName()) { S.desired = { room: urlRoom, create: false, name: savedName() }; }
            else { showModal("You were invited to workspace " + urlRoom + ". Enter your name to join."); }
        } else {
            showModal("Create a private workspace or join one with a workspace ID.");
        }
    }

    function init() {
        buildSwatches();
        setColor(S.color);
        setNoteColor(S.noteColor);
        setBrush(S.size);
        initTheme();
        bindUi();
        setTool("pen");
        resizeCanvas();
        connectSocket();
        startWorkspaceFlow();
        window.setInterval(function () {
            var now = Date.now();
            Object.keys(cursors).forEach(function (id) {
                if (now - cursors[id].seen > 6000) { cursors[id].el.classList.add("idle"); }
            });
        }, 2000);
        window.setTimeout(hideHint, 9000);
    }

    init();
})();
