from app import app, socketio

SEG = {"x0": 0.1, "y0": 0.1, "x1": 0.2, "y1": 0.2, "color": "#112233", "width": 4}
RECT = {"id": "rect000001", "type": "rect", "x0": 0.1, "y0": 0.1, "x1": 0.4, "y1": 0.3,
        "color": "#ef4444", "width": 3}
TEXT = {"id": "text000001", "type": "text", "x": 0.2, "y": 0.2, "text": "Hello team",
        "color": "#111827", "size": 24}
NOTE = {"id": "note000001", "type": "note", "x": 0.5, "y": 0.5, "text": "Ship it",
        "color": "#fde68a"}


def msgs(client, name):
    return [m["args"][0] for m in client.get_received() if m["name"] == name]


def join(name, room=None, create=False):
    client = socketio.test_client(app)
    client.emit("join", {"name": name, "room": room, "create": create})
    return client


def test_create_workspace_generates_unique_id():
    a, b = join("Asha", create=True), join("Ben", create=True)
    ja, jb = msgs(a, "joined")[0], msgs(b, "joined")[0]
    assert ja["room"] != jb["room"] and len(ja["room"]) >= 6
    assert ja["you"]["name"] == "Asha" and len(ja["participants"]) == 1
    a.disconnect(); b.disconnect()


def test_join_existing_workspace_updates_everyone():
    a = join("Asha", create=True)
    room = msgs(a, "joined")[0]["room"]
    b = join("Ben", room=room)
    people_a = msgs(a, "participants")[-1]["participants"]
    assert sorted(p["name"] for p in people_a) == ["Asha", "Ben"]
    assert len(msgs(b, "joined")[0]["participants"]) == 2
    a.disconnect(); b.disconnect()


def test_join_and_leave_notices_and_cursor_gone():
    a = join("Asha", room="team01")
    a.get_received()
    b = join("Ben", room="team01")
    notice = msgs(a, "notice")
    assert notice and notice[0]["kind"] == "join" and notice[0]["name"] == "Ben" and notice[0]["count"] == 2
    b.disconnect()
    received = a.get_received()
    leave = [m["args"][0] for m in received if m["name"] == "notice"]
    assert leave and leave[0]["kind"] == "leave" and leave[0]["name"] == "Ben" and leave[0]["count"] == 1
    assert any(m["name"] == "cursor_gone" for m in received)
    assert [p["name"] for p in [m["args"][0] for m in received if m["name"] == "participants"][-1]["participants"]] == ["Asha"]
    a.disconnect()


def test_workspaces_are_isolated():
    a, b, c = join("A", room="alpha1"), join("B", room="beta22"), join("C", room="alpha1")
    for x in (a, b, c):
        x.get_received()
    a.emit("draw", {**SEG, "sid": "stroke0001"})
    a.emit("item", {"op": "upsert", "item": RECT})
    a.emit("cursor", {"x": 0.5, "y": 0.5})
    assert [m["name"] for m in b.get_received()] == []
    got = [m["name"] for m in c.get_received()]
    assert "draw" in got and "item" in got and "cursor" in got
    a.emit("clear")
    assert [m["name"] for m in b.get_received()] == []
    assert "clear" in [m["name"] for m in c.get_received()]
    for x in (a, b, c):
        x.disconnect()


def test_late_joiner_receives_strokes_and_items():
    a = join("A", room="team02")
    a.emit("draw", {**SEG, "sid": "stroke0001"})
    a.emit("item", {"op": "upsert", "item": TEXT})
    a.emit("item", {"op": "upsert", "item": NOTE})
    b = join("B", room="team02")
    joined = msgs(b, "joined")[0]
    assert len(joined["segments"]) == 1
    assert [i["type"] for i in joined["items"]] == ["text", "note"]
    a.disconnect(); b.disconnect()


def test_workspace_keeps_drawing_after_everyone_leaves():
    a = join("A", room="team03")
    a.emit("draw", {**SEG, "sid": "stroke0001"})
    a.disconnect()
    b = join("B", room="team03")
    assert len(msgs(b, "joined")[0]["segments"]) == 1
    b.disconnect()


def test_cursor_relay_has_name_and_colour_and_validates():
    a, b = join("Asha", room="team04"), join("Ben", room="team04")
    b.get_received()
    a.emit("cursor", {"x": 0.25, "y": 0.75})
    cursor = msgs(b, "cursor")[0]
    assert cursor["name"] == "Asha" and cursor["x"] == 0.25 and cursor["color"].startswith("#")
    a.emit("cursor", {"x": 5, "y": 0.1})
    a.emit("cursor", {"x": "bad", "y": 0.1})
    assert msgs(b, "cursor") == []
    a.disconnect(); b.disconnect()


def test_participants_get_distinct_colours_and_clean_names():
    a, b = join("Asha", room="team05"), join("<b>Ben</b>", room="team05")
    people = msgs(b, "participants")[-1]["participants"]
    assert len({p["color"] for p in people}) == 2
    assert "bBenb" in [p["name"] for p in people]
    assert all("<" not in p["name"] for p in people)
    a.disconnect(); b.disconnect()


def test_shape_text_and_note_items_sync_and_delete():
    a, b = join("A", room="team06"), join("B", room="team06")
    b.get_received()
    for item in (RECT, TEXT, NOTE):
        a.emit("item", {"op": "upsert", "item": item})
    got = msgs(b, "item")
    assert [g["item"]["type"] for g in got] == ["rect", "text", "note"]
    moved = {**NOTE, "x": 0.6, "y": 0.7, "text": "Edited"}
    a.emit("item", {"op": "upsert", "item": moved})
    assert msgs(b, "item")[0]["item"]["text"] == "Edited"
    a.emit("item", {"op": "delete", "id": NOTE["id"]})
    assert msgs(b, "item")[0] == {"op": "delete", "id": NOTE["id"]}
    c = join("C", room="team06")
    assert [i["id"] for i in msgs(c, "joined")[0]["items"]] == [RECT["id"], TEXT["id"]]
    for x in (a, b, c):
        x.disconnect()


def test_invalid_items_are_dropped():
    a, b = join("A", room="team07"), join("B", room="team07")
    b.get_received()
    bad = [
        {**RECT, "type": "star"}, {**RECT, "x0": 2}, {**RECT, "color": "red"}, {**RECT, "width": 99},
        {**RECT, "id": "bad id!"}, {**TEXT, "text": "   "}, {**TEXT, "size": 1000},
        {**NOTE, "text": 123}, "nope", {"op": "upsert"}, {"op": "upsert", "item": "x"},
    ]
    for item in bad:
        a.emit("item", {"op": "upsert", "item": item} if isinstance(item, dict) and "op" not in item else item)
    a.emit("item", {"op": "explode"})
    a.emit("item", {"op": "delete", "id": "missing0001"})
    assert msgs(b, "item") == []
    a.disconnect(); b.disconnect()


def test_undo_and_restore_stroke_for_everyone():
    a, b = join("A", room="team08"), join("B", room="team08")
    seg1, seg2 = {**SEG, "sid": "strokeAAA1"}, {**SEG, "x1": 0.3, "sid": "strokeAAA1"}
    a.emit("draw", seg1); a.emit("draw", seg2)
    b.get_received()
    a.emit("undo", {"sid": "strokeAAA1"})
    assert msgs(b, "undo") == [{"sid": "strokeAAA1"}]
    c = join("C", room="team08")
    assert msgs(c, "joined")[0]["segments"] == []
    a.emit("restore", {"segments": [seg1, seg2, {"bad": 1}]})
    restored = msgs(b, "segments")[0]["segments"]
    assert len(restored) == 2
    d = join("D", room="team08")
    assert len(msgs(d, "joined")[0]["segments"]) == 2
    for x in (a, b, c, d):
        x.disconnect()


def test_eraser_segments_are_stored_with_flag():
    a = join("A", room="team09")
    a.emit("draw", {**SEG, "color": "#ffffff", "erase": True, "sid": "eraser0001"})
    b = join("B", room="team09")
    assert msgs(b, "joined")[0]["segments"][0]["erase"] is True
    a.disconnect(); b.disconnect()


def test_clear_empties_strokes_and_items_of_that_room_only():
    a, b = join("A", room="team10"), join("B", room="team11")
    for x in (a, b):
        x.emit("draw", {**SEG, "sid": "stroke0001"})
        x.emit("item", {"op": "upsert", "item": RECT})
    a.emit("clear")
    a2, b2 = join("A2", room="team10"), join("B2", room="team11")
    cleared, untouched = msgs(a2, "joined")[0], msgs(b2, "joined")[0]
    assert cleared["segments"] == [] and cleared["items"] == []
    assert len(untouched["segments"]) == 1 and len(untouched["items"]) == 1
    for x in (a, b, a2, b2):
        x.disconnect()


def test_switching_workspace_leaves_the_old_one():
    a = join("Asha", room="oldroom")
    b = join("Ben", room="oldroom")
    b.get_received()
    a.emit("join", {"name": "Asha", "room": "newroom"})
    received = b.get_received()
    assert any(m["name"] == "notice" and m["args"][0]["kind"] == "leave" for m in received)
    a.emit("draw", {**SEG, "sid": "stroke0001"})
    assert msgs(b, "draw") == []
    a.disconnect(); b.disconnect()


def test_invalid_workspace_id_is_rejected():
    a = socketio.test_client(app)
    for bad in ("ab", "has space", "x" * 40, "../etc", 123):
        a.emit("join", {"name": "A", "room": bad})
    errors = msgs(a, "join_error")
    assert len(errors) == 5
    a.disconnect()


def test_same_room_join_only_renames():
    a = join("Asha", room="team12")
    a.get_received()
    a.emit("join", {"name": "Asha K", "room": "team12"})
    people = msgs(a, "participants")[-1]["participants"]
    assert [p["name"] for p in people] == ["Asha K"]
    a.disconnect()


def test_legacy_clients_without_join_share_the_public_room():
    a, b = socketio.test_client(app), join("B", room="main")
    b.get_received()
    a.emit("draw", SEG)
    assert len(msgs(b, "draw")) == 1
    a.emit("clear")
    assert "clear" in [m["name"] for m in b.get_received()]
    a.disconnect(); b.disconnect()


def test_page_and_metrics_expose_new_features(monkeypatch):
    import app as module
    monkeypatch.setattr(module, "APP_COLOR", "green")
    html = app.test_client().get("/").get_data(as_text=True)
    assert "c-green" in html and "served by green" in html
    for control in ('id="board"', 'id="undo"', 'id="redo"', 'id="modal"', 'id="copyLink"',
                    'id="peopleList"', 'id="background"', 'data-tool="note"', 'data-tool="arrow"',
                    'studio.js', 'studio.css'):
        assert control in html
    body = app.test_client().get("/metrics").get_data(as_text=True)
    for name in ("devguard_active_rooms", "devguard_item_events_total", "devguard_stored_segments"):
        assert name in body
