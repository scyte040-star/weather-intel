"""Run: python test_app.py -- checks duplicate grouping, verification rules and request handling (no network)."""
import http.client
import json
import socket
import threading
from unittest import mock

import app

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

# Spending limits: per-IP hourly, then the daily cap across everyone
with mock.patch.object(app, "HOURLY_PER_IP", 2), mock.patch.object(app, "DAILY_LIMIT", 3), \
        mock.patch.object(app, "recent_calls", app.defaultdict(list)):
    assert [app.over_limit("1.1.1.1") is None for _ in range(3)] == [True, True, False]
    assert app.over_limit("2.2.2.2") is None             # 3rd call overall
    assert "daily limit" in app.over_limit("3.3.3.3")    # cap reached for everyone

# HTTP handling
srv =app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]


def status_of(raw):
    with socket.create_connection(("127.0.0.1", port), timeout=3) as s:
        s.sendall(raw)
        return b"".join(iter(lambda: s.recv(4096), b"")).split(b" ")[1]  # read to EOF so the server can finish


body = json.dumps({"text": "Flood in Andheri, Mumbai", "source_type": "news"}).encode()
post = b"POST /api/reports HTTP/1.1\r\nHost: x\r\nContent-Type: %s\r\nContent-Length: %s\r\n\r\n"
assert status_of(post % (b"application/json", b"abc")) == b"400"       # used to drop the connection
assert status_of(post % (b"application/json", b"-1")) == b"400"        # used to hang the thread
assert status_of(post % (b"text/plain", str(len(body)).encode()) + body) == b"415"  # cross-site form post
assert status_of(b"GET /?utm=1 HTTP/1.1\r\nHost: x\r\n\r\n") == b"200"

with mock.patch.object(app, "extract", side_effect=KeyError("boom")):  # unexpected failure -> JSON error, not a dropped connection
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    c.request("POST", "/api/reports", body, {"Content-Type": "application/json"})
    r = c.getresponse()
    assert r.status == 500 and "error" in json.loads(r.read())
srv.shutdown()

print("ok")
