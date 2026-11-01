from app import app, socketio

VALID = {"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.2,
         "color": "#112233", "width": 4}


def test_socketio_library_is_served():
    r = app.test_client().get("/static/socket.io.min.js")
    assert r.status_code == 200
    r.close()


def test_health_reports_color(monkeypatch):
    import app as module
    monkeypatch.setattr(module, "APP_COLOR", "blue")
    assert app.test_client().get("/health").get_json() == {
        "status": "healthy", "color": "blue"}


def test_valid_draw_reaches_other_client_only():
    a = socketio.test_client(app)
    b = socketio.test_client(app)
    a.get_received()
    b.get_received()
    a.emit("draw", VALID)
    assert any(m["name"] == "draw" for m in b.get_received())
    assert not any(m["name"] == "draw" for m in a.get_received())
    a.disconnect()
    b.disconnect()


def test_invalid_draw_is_dropped():
    a = socketio.test_client(app)
    b = socketio.test_client(app)
    b.get_received()
    bad = [
        {**VALID, "x0": 5},
        {**VALID, "color": "red"},
        {**VALID, "width": 99},
        {**VALID, "width": float("nan")},
        {"x0": 0.1},
        "not a dict",
    ]
    for payload in bad:
        a.emit("draw", payload)
    assert not any(m["name"] == "draw" for m in b.get_received())
    a.disconnect()
    b.disconnect()


def test_clear_is_broadcast():
    a = socketio.test_client(app)
    b = socketio.test_client(app)
    b.get_received()
    a.emit("clear")
    assert any(m["name"] == "clear" for m in b.get_received())
    a.disconnect()
    b.disconnect()