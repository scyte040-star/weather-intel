"""Run: python test_app.py -- checks duplicate grouping, verification rules and request handling (no network)."""
import http.client
import json
import os
import socket
import tempfile
import threading
from pathlib import Path
from unittest import mock

import app
import auth
import feeds
import shelters

T = 1_000_000


def rep(id, minutes, types, lat, lon, src="citizen", name=None, flags=(), timing="observed"):
    return {"id": id, "t": T + minutes * 60, "text": id, "source_type": src, "source_name": name or id,
            "event_types": list(types), "severity": "moderate", "timing": timing, "red_flags": list(flags),
            "places": [{"name": id, "level": "locality", "district": "", "state": "S", "country": "India",
                        "lat": lat, "lon": lon}]}


def by_id(events):
    return {e["id"]: e for e in events}


# Nearby, same type, close in time -> one event; far away / other type / other timing / hours later -> separate
ev = by_id(app.cluster([
    rep("a", 0, ["Flood"], 19.12, 72.85),
    rep("b", 10, ["Flood", "Heavy Rainfall"], 19.07, 72.88),     # ~6 km away
    rep("far", 5, ["Flood"], 18.52, 73.86),                      # Pune, ~120 km
    rep("other", 5, ["Fog"], 19.12, 72.85),
    rep("fc", 5, ["Flood"], 19.12, 72.85, timing="forecast"),
    rep("late", 60 * 9, ["Flood"], 19.12, 72.85),                 # 9 h after "b"
    rep("nowhere", 5, ["Flood"], None, None),                     # unmapped never merges
]))
assert set(ev) == {"a", "far", "other", "fc", "late", "nowhere"}, ev.keys()
assert len(ev["a"]["reports"]) == 2 and ev["a"]["event_types"][0] == "Flood"
assert ev["nowhere"]["lat"] is None

# Verification
v = lambda *rs: app.verify(list(rs))[0]
assert v(rep("x", 0, ["Flood"], 0, 0)) == "review"
assert v(rep("x", 0, ["Flood"], 0, 0), rep("y", 0, ["Flood"], 0, 0)) == "review"
assert v(*(rep(i, 0, ["Flood"], 0, 0) for i in "xyz")) == "verified"
assert v(*(rep(i, 0, ["Flood"], 0, 0, name="anonymous") for i in "xyz")) == "review"   # anonymous = one source
assert v(rep("x", 0, ["Flood"], 0, 0, src="official")) == "verified"
assert v(rep("x", 0, ["Tsunami"], 0, 0, flags=["forward to all"])) == "misleading"
assert v(*(rep(i, 0, ["Tsunami"], 0, 0, flags=["forward to all"]) for i in "xyzw")) == "misleading"  # viral hoax

# Ambiguous place (no state) is never geocoded -- returns before any network call
assert app.geocode({"name": "Aurangabad", "level": "city", "district": "", "state": "", "country": "India"}) is None

# Geocoder: no repeated parts in the query ("Pune, Pune, ..."), and a non-JSON reply returns None instead of raising
sent = []
fake_reply = lambda req, timeout: sent.append(req.full_url) or mock.Mock(read=lambda *a: b"<html>blocked</html>")
with mock.patch.object(app, "urlopen", fake_reply), mock.patch.object(app.time, "sleep"):
    assert app.geocode({"name": "Pune", "level": "city", "district": "Pune", "state": "Maharashtra", "country": "India"}) is None
assert "q=Pune%2C+Maharashtra%2C+India&" in sent[0], sent

# A state-level place with an empty state field still counts for the state filter
odisha = rep("o", 0, ["Cyclone"], 20.9, 85.1)
odisha["places"][0].update(name="Odisha", level="state", state="")
assert app.summarize([odisha])["states"] == ["Odisha"]

# Rate limits: separate keys, sliding window
with mock.patch.object(app, "recent_calls", app.defaultdict(list)):
    assert [app.allow("login:1.1.1.1", 2, 60) for _ in range(3)] == [True, True, False]
    assert app.allow("login:2.2.2.2", 2, 60)  # another address has its own budget

# Moderators: a verified user report makes its event verified; signed-in users count once however they sign
assert app.verify([dict(rep("x", 0, ["Flood"], 0, 0), review="verified")]) == ("verified", "Verified by a moderator")
same_user = [dict(rep(i, 0, ["Flood"], 0, 0), source_id="user:7") for i in "xyz"]
assert app.verify(same_user)[0] == "review"

# Live feeds: CAP event names -> our types (no double-counting "heavy rain" as "rain")
assert feeds.event_types("Heavy Rain") == ["Heavy Rainfall"]
assert feeds.event_types("Moderate Rain with Thunderstorm and lightning") == ["Rain", "Thunderstorm", "Lightning"]
assert feeds.event_types("Something new") == ["Other"]

# CAP area descriptions -> places
assert feeds.cap_places("Ganga, Rishikesh, Dehradun, Uttarakhand", 30.11, 78.31)[0]["state"] == "Uttarakhand"
districts = feeds.cap_places("Karur, Tiruchirappalli districts of Tamil Nadu", None, None)
assert [(p["name"], p["level"], p["state"]) for p in districts] == [("Karur", "district", "Tamil Nadu"),
                                                                    ("Tiruchirappalli", "district", "Tamil Nadu")]
assert [(p["name"], p["level"]) for p in feeds.cap_places("8 districts of Rajasthan", None, None)] == [("Rajasthan", "state")]
# A state SDMA naming only mandals: keep the name, fall back to the sender's state for the pin
assert feeds.sender_state("Andhra Pradesh SDMA") == feeds.sender_state("IMD", "Andhra-Pradesh-SDMA") == "Andhra Pradesh"
assert feeds.sender_state("IMD Chennai", "IMD-Chennai") == ""
assert [(p["name"], p["level"], p["state"]) for p in feeds.cap_places("tpt-kodur Mandal", None, None, "Andhra Pradesh")] == \
    [("tpt-kodur Mandal", "locality", "Andhra Pradesh"), ("Andhra Pradesh", "state", "Andhra Pradesh")]

# A real SACHET CAP alert (CWC river flood), trimmed
CAP_XML = """<cap:alert xmlns:cap="urn:oasis:names:tc:emergency:cap:1.2"><cap:sender>Uttarakhand-SDMA</cap:sender>
<cap:sent>2026-09-27T21:57:53+05:30</cap:sent><cap:status>Actual</cap:status><cap:msgType>Update</cap:msgType>
<cap:info><cap:language>en-IN</cap:language><cap:event>Flood</cap:event><cap:severity>Severe</cap:severity>
<cap:certainty>Observed</cap:certainty><cap:expires>2026-09-28T10:00:00+05:30</cap:expires>
<cap:headline>River Ganga at Rishikesh continues to flow in above normal flood situation.</cap:headline>
<cap:description>At 9:00 pm it was flowing at 340.02 m, 0.52 m above its Warning Level.</cap:description>
<cap:area><cap:areaDesc>Ganga, Rishikesh, Dehradun, Uttarakhand </cap:areaDesc><cap:altitude>30.11</cap:altitude>
<cap:ceiling>78.31</cap:ceiling></cap:area></cap:info></cap:alert>"""
cap = feeds.parse_cap("123", "CWC", CAP_XML.encode())
assert (cap["event_types"], cap["severity"], cap["timing"], cap["source_type"]) == (["Flood"], "high", "observed", "official")
assert (cap["places"][0]["lat"], cap["places"][0]["lon"]) == (30.11, 78.31) and cap["text"].startswith("At 9:00 pm")
assert feeds.parse_cap("124", "CWC", CAP_XML.replace("Actual", "Exercise").encode()) is None  # drills are skipped

# Open-Meteo readings vs IMD thresholds
calm = {"time": [1, 2], "precipitation": [1, 2], "temperature_2m": [30, 31], "wind_gusts_10m": [10, 20]}
assert feeds.weather_events("Pune", "Maharashtra", 18.5, 73.9, calm) == []
storm = {"time": [1, 2], "precipitation": [60, 70], "temperature_2m": [30, 31], "wind_gusts_10m": [10, 95]}
got = {r["event_types"][0]: r["severity"] for r in feeds.weather_events("Pune", "Maharashtra", 18.5, 73.9, storm)}
assert got == {"Heavy Rainfall": "high", "Strong Winds": "high"}, got  # 130 mm = "very heavy"; 95 km/h gusts

# Passwords
stored = auth.hash_pw("correct horse")
assert auth.check_pw("correct horse", stored) and not auth.check_pw("wrong horse", stored) and "correct" not in stored

# HTTP handling, against throwaway data files. Test-only accounts; the admin comes from the environment as in production.
tmp = Path(tempfile.mkdtemp())
ADMIN, ADMIN_PW = "admin@example.test", "test-admin-password"
with mock.patch.object(auth, "DB", tmp / "test.db"), mock.patch.object(app, "REPORTS_FILE", tmp / "reports.json"), \
        mock.patch.object(app, "submitted", []), mock.patch.object(app, "recent_calls", app.defaultdict(list)), \
        mock.patch.object(app, "reverse_geocode", return_value=["Andheri", "Maharashtra"]), \
        mock.patch.dict(os.environ, {"ADMIN_EMAIL": ADMIN, "ADMIN_PASSWORD": ADMIN_PW}):
    auth.init()
    shelters.init()
    srv = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    port = srv.server_address[1]

    def status_of(raw):
        with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
            s.sendall(raw)
            return b"".join(iter(lambda: s.recv(4096), b"")).split(b" ")[1]  # read to EOF so the server can finish

    def call(method, path, body=None, sid=None):
        """-> (status, parsed JSON or raw body, session token from Set-Cookie or None, headers)"""
        c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        if sid:
            headers["Cookie"] = f"sid={sid}"
        c.request(method, path, json.dumps(body) if body is not None else None, headers)
        r = c.getresponse()
        raw, cookie = r.read(), r.getheader("Set-Cookie") or ""
        data = json.loads(raw) if r.getheader("Content-Type", "").startswith("application/json") else raw
        return r.status, data, cookie.split(";")[0].partition("=")[2] if cookie else None, dict(r.getheaders())

    body = json.dumps({"text": "Flood in Andheri, Mumbai", "source_type": "news"}).encode()
    raw_post = b"POST /api/reports HTTP/1.1\r\nHost: x\r\nContent-Type: %s\r\nContent-Length: %s\r\n\r\n"
    assert status_of(raw_post % (b"application/json", b"abc")) == b"400"       # used to drop the connection
    assert status_of(raw_post % (b"application/json", b"-1")) == b"400"        # used to hang the thread
    assert status_of(raw_post % (b"text/plain", str(len(body)).encode()) + body) == b"415"  # cross-site form post
    assert status_of(b"GET /?utm=1 HTTP/1.1\r\nHost: x\r\n\r\n") == b"200"

    # Accounts
    status, data, sid, headers = call("POST", "/api/signup", {"name": "Asha Rao", "email": "Asha@Example.test", "password": "monsoon-2026"})
    assert status == 201 and sid and data["user"]["email"] == "asha@example.test" and data["user"]["role"] == "user"
    assert "HttpOnly" in headers["Set-Cookie"] and "SameSite=Lax" in headers["Set-Cookie"]
    assert call("GET", "/api/me", sid=sid)[1]["user"]["name"] == "Asha Rao"
    assert call("GET", "/api/me")[1]["user"] is None
    assert call("POST", "/api/signup", {"name": "Again", "email": "asha@example.test", "password": "monsoon-2026"})[0] == 400
    assert call("POST", "/api/signup", {"name": "Short", "email": "short@example.test", "password": "1234567"})[0] == 400
    assert call("POST", "/api/login", {"email": "asha@example.test", "password": "wrong-password"})[0] == 401
    assert call("POST", "/api/login", {"email": "nobody@example.test", "password": "wrong-password"})[0] == 401
    status, _, sid2, _ = call("POST", "/api/login", {"email": "asha@example.test", "password": "monsoon-2026"})
    assert status == 200 and sid2 and sid2 != sid

    # Reports need an account; the structured form works without an API key
    report = {"event_type": "Flood", "severity": "high", "lat": 19.12, "lon": 72.85, "description": "Water up to the knees on SV Road"}
    assert call("POST", "/api/reports", report)[0] == 401
    status, r, _, _ = call("POST", "/api/reports", report, sid=sid)
    assert status == 201 and r["source_id"].startswith("user:") and r["places"][0]["state"] == "Maharashtra", r
    assert call("POST", "/api/reports", {**report, "lat": 51.5, "lon": -0.1}, sid=sid)[0] == 400  # outside India
    assert call("POST", "/api/reports", {**report, "event_type": "Meteor"}, sid=sid)[0] == 400
    with mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "test"}), mock.patch.object(app, "extract", side_effect=KeyError("boom")), \
            mock.patch.object(app.traceback, "print_exc"):  # the expected crash would otherwise print a traceback
        status, data, _, _ = call("POST", "/api/reports", {"text": "Flood in Andheri, Mumbai"}, sid=sid)
        assert status == 500 and "error" in data  # unexpected failure -> JSON error, not a dropped connection

    # My area
    area = {"label": "Home", "lat": 19.1, "lon": 72.9, "radius_km": 50, "min_severity": "moderate"}
    assert call("POST", "/api/me/alert", {"alert": area}, sid=sid)[1]["user"]["alert"]["label"] == "Home"
    assert call("POST", "/api/me/alert", {"alert": {**area, "radius_km": 5000}}, sid=sid)[0] == 400
    assert call("POST", "/api/me/alert", {"alert": None}, sid=sid)[1]["user"]["alert"] is None

    # Admin pages and APIs are for admins only
    assert call("GET", "/admin")[0] == 302
    assert call("GET", "/admin", sid=sid)[0] == 403
    assert call("GET", "/api/admin/users", sid=sid)[0] == 403
    status, _, admin_sid, _ = call("POST", "/api/login", {"email": ADMIN, "password": ADMIN_PW})
    assert status == 200 and call("GET", "/admin", sid=admin_sid)[0] == 200
    users = call("GET", "/api/admin/users", sid=admin_sid)[1]
    asha = next(u for u in users["users"] if u["email"] == "asha@example.test")
    assert call("POST", f"/api/admin/users/{users['me']}", {"disabled": True}, sid=admin_sid)[0] == 400  # not yourself

    # Moderation: verify -> the event is verified; reject -> it leaves the map
    assert call("POST", f"/api/admin/reports/{r['id']}", {"review": "verified"}, sid=admin_sid)[0] == 200
    mine = [e for e in call("GET", "/api/events")[1]["events"] if e["id"] == r["id"]]
    assert mine and mine[0]["status"] == "verified" and mine[0]["why"] == "Verified by a moderator"
    call("POST", f"/api/admin/reports/{r['id']}", {"review": "rejected"}, sid=admin_sid)
    assert not [e for e in call("GET", "/api/events")[1]["events"] if e["id"] == r["id"]]

    # Shelters: operators register them, the control room approves them, and only approved ones are public
    status, data, op_sid, _ = call("POST", "/api/signup", {"name": "Ravi Camp", "email": "ravi@example.test", "password": "shelter-2026", "role": "shelter"})
    assert status == 201 and data["user"]["role"] == "shelter"
    _, _, op2_sid, _ = call("POST", "/api/signup", {"name": "Other Op", "email": "other@example.test", "password": "shelter-2026", "role": "shelter"})
    camp = {"name": "Pune relief camp", "type": "Relief camp", "address": "Shivajinagar, Pune", "lat": 18.52, "lon": 73.86,
            "capacity": 200, "occupancy": 150, "facilities": ["Food", "Beds"], "active": True,
            "funds_needed": 50000, "funds_note": "Blankets", "phone": "+91 98765 43210"}
    assert call("POST", "/api/my/shelters", camp, sid=sid)[0] == 403                       # a regular account can't
    assert call("POST", "/api/my/shelters", {**camp, "funds_note": ""}, sid=op_sid)[0] == 400  # funds need a purpose
    assert call("POST", "/api/my/shelters", {**camp, "phone": "call me"}, sid=op_sid)[0] == 400
    assert call("POST", "/api/my/shelters", {**camp, "lat": 51.5, "lon": -0.1}, sid=op_sid)[0] == 400
    assert call("POST", "/api/my/shelters", {**camp, "facilities": ["Spa"]}, sid=op_sid)[0] == 400
    status, data, _, _ = call("POST", "/api/my/shelters", camp, sid=op_sid)
    shelter_id = data["shelter"]["id"]
    assert status == 201 and data["shelter"]["status"] == "pending" and data["shelter"]["state"] == "Maharashtra"
    assert call("GET", "/api/shelters")[1]["shelters"] == []                               # hidden until approved
    assert call("POST", f"/api/my/shelters/{shelter_id}/quick", {"active": False, "occupancy": 0}, sid=op2_sid)[0] == 404
    assert call("POST", f"/api/admin/shelters/{shelter_id}", {"status": "approved"}, sid=op_sid)[0] == 403
    assert call("POST", f"/api/admin/shelters/{shelter_id}", {"status": "approved"}, sid=admin_sid)[0] == 200
    public = call("GET", "/api/shelters")[1]["shelters"]
    assert [s["name"] for s in public] == ["Pune relief camp"] and "owner_id" not in public[0] and "review_note" not in public[0]
    quick = call("POST", f"/api/my/shelters/{shelter_id}/quick", {"active": True, "occupancy": 195}, sid=op_sid)[1]["shelter"]
    assert quick["occupancy"] == 195 and quick["active"]

    # Control room: a severe event ~120 km from the only open shelter is flagged; the full shelter is too
    call("POST", "/api/reports", {**report, "severity": "extreme"}, sid=op_sid)  # Mumbai
    assert call("GET", "/api/control", sid=op_sid)[0] == 403 and call("GET", "/control", sid=sid)[0] == 403
    assert call("GET", "/control", sid=admin_sid)[0] == 200
    room = call("GET", "/api/control", sid=admin_sid)[1]
    checks = {c["title"]: c["state"] for c in room["checks"]}
    assert checks["Shelter coverage"] == "fail" and checks["Shelter capacity"] == "warn", checks
    assert room["uncovered"] and 100 < room["uncovered"][0]["nearest_km"] < 140 and room["summary"]["beds_free"] == 5
    assert room["summary"]["funds"] == 50000

    # The shelter page: sign-in first; a regular account sees the opt-in and can switch
    assert call("GET", "/shelter")[3]["Location"].startswith("/login?as=shelter")
    assert call("GET", "/shelter", sid=sid)[0] == 200
    assert call("POST", "/api/me/role", {"role": "admin"}, sid=sid)[0] == 400
    assert call("POST", "/api/my/shelters/{}/delete".format(shelter_id), {}, sid=op_sid)[0] == 200
    assert call("GET", "/api/shelters")[1]["shelters"] == []

    # Password change signs out other devices; a reset gives a one-time password; disabling ends sessions
    assert call("POST", "/api/me/password", {"current": "monsoon-2026", "new": "new-monsoon-2026"}, sid=sid)[0] == 200
    assert call("GET", "/api/me", sid=sid2)[1]["user"] is None and call("GET", "/api/me", sid=sid)[1]["user"]
    temp = call("POST", f"/api/admin/users/{asha['id']}", {"reset_password": True}, sid=admin_sid)[1]["temporary_password"]
    assert call("GET", "/api/me", sid=sid)[1]["user"] is None
    status, _, sid, _ = call("POST", "/api/login", {"email": "asha@example.test", "password": temp})
    assert status == 200
    assert call("POST", f"/api/admin/users/{asha['id']}", {"disabled": True}, sid=admin_sid)[1]["user"]["disabled"]
    assert call("GET", "/api/me", sid=sid)[1]["user"] is None
    assert call("POST", "/api/login", {"email": "asha@example.test", "password": temp})[0] == 401

    # Sign out
    assert call("POST", "/api/logout", {}, sid=admin_sid)[0] == 200
    assert call("GET", "/api/me", sid=admin_sid)[1]["user"] is None

    # Too many sign-in attempts from one address
    statuses = [call("POST", "/api/login", {"email": ADMIN, "password": "guess"})[0] for _ in range(12)]
    assert statuses[-1] == 429, statuses
    srv.shutdown()

print("ok")
