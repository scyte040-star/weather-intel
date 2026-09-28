"""Weather intelligence prototype: reports -> Claude extraction -> geocode -> group duplicates -> verify -> live map.

Run:   pip install -r requirements.txt; python app.py        then open http://localhost:8000
Live data (NDMA SACHET, GDACS, USGS, Open-Meteo) needs no key. Signed-in users can report events and set alerts;
set ADMIN_EMAIL + ADMIN_PASSWORD for an admin account (/admin). ANTHROPIC_API_KEY also lets users paste free text
for Claude to analyse. DEMO_DATA=1 adds the simulated seed reports. Hosting: HOST=0.0.0.0 (PORT from the environment).
"""
import json
import math
import os
import threading
import time
import traceback
import uuid
from collections import Counter, defaultdict
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlparse
from urllib.request import Request, urlopen

import auth
import feeds
import shelters

HERE = Path(__file__).parent
PROMPT = (HERE / "extract_prompt.md").read_text(encoding="utf-8")
REPORTS_FILE = HERE / "reports.json"
GEOCACHE_FILE = HERE / "geocache.json"

EVENT_TYPES = ["Heavy Rainfall", "Rain", "Flood", "Flash Flood", "Cloudburst", "Thunderstorm", "Lightning", "Hailstorm",
               "Strong Winds", "Cyclone", "Dust Storm", "Heatwave", "Cold Wave", "Fog", "Landslide", "Avalanche",
               "Drought", "Wildfire", "Earthquake", "Tsunami", "Other"]
SEVERITY = ["low", "moderate", "high", "extreme"]
LEVELS = ["locality", "city", "district", "region", "state", "country"]  # most specific first
PUBLIC_SOURCES = ["citizen", "social", "news"]  # "official" only arrives via trusted feeds, never from users
RADIUS_KM = 25  # ponytail: one radius for every place level; scale by level if events over/under-merge
WINDOW_S = 6 * 3600
# Every analysed report is a paid Claude API call.
DAILY_LIMIT = int(os.environ.get("DAILY_REPORT_LIMIT", "100"))  # all users combined: the hard cap on spend
HOURLY_AI_PER_USER = int(os.environ.get("HOURLY_REPORTS_PER_USER", "10"))

SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["event_types", "severity", "timing", "red_flags", "places"],
    "properties": {
        "event_types": {"type": "array", "items": {"type": "string", "enum": EVENT_TYPES}},
        "severity": {"type": "string", "enum": SEVERITY},
        "timing": {"type": "string", "enum": ["observed", "forecast"]},
        "red_flags": {"type": "array", "items": {"type": "string"}},
        "places": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["name", "level", "district", "state", "country"],
            "properties": {
                "name": {"type": "string"},
                "level": {"type": "string", "enum": LEVELS},
                "district": {"type": "string"},
                "state": {"type": "string"},
                "country": {"type": "string"},
            },
        }},
    },
}


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return default


def save(path, obj):
    tmp = path.with_suffix(".tmp")  # write-then-rename: a crash mid-write can't leave a half-written file
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


START = time.time()
seed = load(HERE / "seed.json", []) if os.environ.get("DEMO_DATA") == "1" else []  # simulated, off by default
for r in seed:
    r["t"] = START - r.pop("minutes_ago") * 60  # negative minutes_ago = report "arrives" live after startup
submitted = load(REPORTS_FILE, [])
geocache = load(GEOCACHE_FILE, {})
write_lock, geo_lock, rate_lock = threading.Lock(), threading.Lock(), threading.Lock()
recent_calls = defaultdict(list)  # rate-limit key -> timestamps of recent attempts


def allow(key, limit, window):
    """Record one attempt under `key`; False if `limit` attempts already happened in the last `window` seconds."""
    now = time.time()
    with rate_lock:
        recent_calls[key] = [t for t in recent_calls[key] if now - t < window]
        if len(recent_calls[key]) >= limit:
            return False
        recent_calls[key].append(now)
        return True


REFRESH_S = 300
live = {name: [] for name in feeds.SOURCES}  # keys fixed up front, so readers can iterate while the thread writes
feed_status = {name: {"state": "loading"} for name in feeds.SOURCES}


def refresh_feeds():
    while True:
        for name, fetch in feeds.SOURCES.items():
            try:
                reports = fetch()
                for p in (p for r in reports for p in r["places"] if p["lat"] is None):
                    p["lat"], p["lon"] = geocode(p) or (None, None)  # cached, so only new places cost a lookup
                live[name] = reports
                feed_status[name] = {"state": "ok", "count": len(reports), "updated": time.time()}
            except Exception as e:  # one broken feed must not stop the others
                feed_status[name] = {**feed_status[name], "state": "error", "error": f"{type(e).__name__}: {e}"[:200]}
                print(f"{name} feed failed: {e}", flush=True)
        time.sleep(REFRESH_S)


def visible_reports():
    now = time.time()
    return ([r for r in seed if r["t"] <= now] + [r for rs in list(live.values()) for r in rs]
            + [r for r in submitted if r.get("review") != "rejected"])


class ExtractionError(Exception):
    pass


def extract(text):
    try:
        import anthropic  # lazy, so the app runs without the SDK installed
    except ImportError:
        raise ExtractionError("AI extraction needs the Anthropic SDK: pip install anthropic")
    try:
        resp = anthropic.Anthropic().beta.messages.create(
            model="claude-opus-5",
            max_tokens=8000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # if a safety classifier declines, re-run on Anthropic's recommended fallback model
            system=PROMPT,
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
            messages=[{"role": "user", "content": text}],
        )
    except anthropic.AuthenticationError:
        raise ExtractionError("Anthropic credentials rejected: check ANTHROPIC_API_KEY")
    except anthropic.RateLimitError:
        raise ExtractionError("Claude API rate limit hit, try again shortly")
    except anthropic.APIStatusError as e:
        raise ExtractionError(f"Claude API error {e.status_code}: {e.message}")
    except anthropic.APIConnectionError:
        raise ExtractionError("Could not reach the Claude API")
    except TypeError as e:  # SDK raises this when no credentials are configured at all
        raise ExtractionError(f"No Anthropic credentials: set ANTHROPIC_API_KEY and restart ({e})")
    if resp.stop_reason != "end_turn":
        raise ExtractionError(f"Extraction stopped early ({resp.stop_reason})")
    try:
        data = json.loads([b.text for b in resp.content if b.type == "text"][-1])
    except (IndexError, ValueError):
        raise ExtractionError("Claude returned no usable JSON")
    data["event_types"] = list(dict.fromkeys(data["event_types"]))
    return data


def nominatim(endpoint, pick, **params):
    """Cached OpenStreetMap Nominatim call -> pick(reply), or None on failure (failures aren't cached)."""
    key = f"{endpoint}?{urlencode(sorted(params.items()))}"
    with geo_lock:  # Nominatim policy: at most 1 request/second, identify the app
        if key not in geocache:
            time.sleep(1)
            url = f"https://nominatim.openstreetmap.org/{endpoint}?" + urlencode({**params, "format": "jsonv2"})
            try:
                geocache[key] = pick(json.load(urlopen(Request(url, headers=feeds.UA), timeout=10)))
            except (OSError, ValueError, KeyError, IndexError, TypeError):
                return None  # network failure or unexpected reply (e.g. an HTML block page)
            save(GEOCACHE_FILE, geocache)
        return geocache[key]


def geocode(place):
    """Place fields -> (lat, lon), or None. Never guesses an ambiguous place."""
    if place["level"] not in ("state", "region", "country") and not place["state"]:
        return None
    name, district, state, country = (place[k] for k in ("name", "district", "state", "country"))
    # dedupe parts: "Pune, Pune, Maharashtra" (city == district) is common and confuses the geocoder
    queries = [", ".join(dict.fromkeys(p for p in parts if p)) for parts in
               ((name, district, state, country), (name, state, country))]
    for q in dict.fromkeys(queries):
        hit = nominatim("search", lambda h: [float(h[0]["lat"]), float(h[0]["lon"])] if h else None, q=q, limit=1)
        if hit:
            return hit
    return None


def reverse_geocode(lat, lon):
    """Map point -> [place name, state]; empty strings when unknown."""
    pick = lambda h: [h.get("name") or h.get("display_name", "").split(",")[0], (h.get("address") or {}).get("state", "")]
    return nominatim("reverse", pick, lat=round(lat, 3), lon=round(lon, 3), zoom=12) or ["", ""]


def search_places(q):
    def label(h):  # "Andheri, Andheri East, Maharashtra": two local parts plus the state, which tells same-named places apart
        state = (h.get("address") or {}).get("state", "")
        parts = [p for p in h["display_name"].split(", ")[:2] if p != state]
        return ", ".join(parts + [state] if state else parts)
    pick = lambda hits: [{"label": label(h), "lat": float(h["lat"]), "lon": float(h["lon"])} for h in hits]
    return nominatim("search", pick, q=q, countrycodes="in", addressdetails=1, limit=5) or []


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def mapped(r):
    return [p for p in r["places"] if p.get("lat") is not None]


def source_key(r):
    return r.get("source_id") or r["source_name"].lower()  # signed-in users count as one source each


def verify(group):
    """Transparent rules, not a model score: official or moderator > red flags > independent corroboration."""
    sources = {source_key(r) for r in group}
    if any(r["source_type"] == "official" for r in group):
        return "verified", "Confirmed by an official source"
    if any(r.get("review") == "verified" for r in group):
        return "verified", "Verified by a moderator"
    flagged = [r for r in group if r["red_flags"]]
    if len(flagged) * 2 >= len(group):  # a viral hoax stays flagged however many accounts share it
        return "misleading", "Red flags: " + "; ".join(sorted({f for r in flagged for f in r["red_flags"]}))
    if len(sources) >= 3:
        return "verified", f"{len(sources)} independent sources agree"
    return "review", f"Only {len(sources)} independent source{'s' if len(sources) > 1 else ''}; needs 3, or an official source"


def cluster(reports):
    """Group reports of one event: same timing, a shared event type, a place within RADIUS_KM, within WINDOW_S."""
    # ponytail: greedy O(n^2) scan on every request; index by time + geohash past a few thousand reports
    groups = []
    for r in sorted(reports, key=lambda r: r["t"]):
        pts = mapped(r)
        home = next((g for g in groups if pts
                     and g[0]["timing"] == r["timing"]
                     and r["t"] - g[-1]["t"] <= WINDOW_S
                     and {e for x in g for e in x["event_types"]} & set(r["event_types"])
                     and any(km(p, q) <= RADIUS_KM for x in g for q in mapped(x) for p in pts)), None)
        if home:
            home.append(r)
        else:
            groups.append([r])
    return [summarize(g) for g in groups]


def summarize(g):
    places = sorted((p for r in g for p in r["places"]), key=lambda p: LEVELS.index(p["level"]))
    pin = next((p for p in places if p.get("lat") is not None), None)
    status, why = verify(g)
    return {
        "id": g[0]["id"],
        "event_types": [t for t, _ in Counter(e for r in g for e in r["event_types"]).most_common()],
        "severity": max((r["severity"] for r in g), key=SEVERITY.index),
        "timing": g[0]["timing"],
        "status": status,
        "why": why,
        "lat": pin and pin["lat"],
        "lon": pin and pin["lon"],
        "places": list(dict.fromkeys(p["name"] for p in places)),
        "states": sorted({p["state"] or (p["name"] if p["level"] == "state" else "") for p in places} - {""}),
        "source_types": sorted({r["source_type"] for r in g}),
        "sources": len({source_key(r) for r in g}),
        "first_t": g[0]["t"],
        "last_t": g[-1]["t"],
        "reports": [{k: r.get(k) for k in ("t", "text", "source_type", "source_name", "red_flags", "url")} for r in g],
    }


class HTTPError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def clean_alert(a):
    try:
        alert = {"label": str(a["label"]).strip()[:80] or "My area", "lat": float(a["lat"]), "lon": float(a["lon"]),
                 "radius_km": int(a["radius_km"]), "min_severity": a["min_severity"], "notify": bool(a.get("notify"))}
    except (KeyError, TypeError, ValueError):
        raise HTTPError(400, "Choose a location, a distance and a minimum severity.")
    if (not feeds.in_india(alert["lat"], alert["lon"]) or not 5 <= alert["radius_km"] <= 500
            or alert["min_severity"] not in SEVERITY):
        raise HTTPError(400, "Pick a location in India, a distance of 5 to 500 km and a valid severity.")
    return alert


def structured_report(b):
    """The report form without AI: the user picks the event type, severity and location themselves."""
    try:
        types, severity, timing = [b["event_type"]], b["severity"], b.get("timing", "observed")
        lat, lon = float(b["lat"]), float(b["lon"])
    except (KeyError, TypeError, ValueError):
        raise HTTPError(400, "Choose what happened, how severe it is and where.")
    if types[0] not in EVENT_TYPES or severity not in SEVERITY or timing not in ("observed", "forecast"):
        raise HTTPError(400, "Unknown event type, severity or timing.")
    if not feeds.in_india(lat, lon):
        raise HTTPError(400, "Pick a location in India.")
    text = str(b.get("description", "")).strip()
    if not 10 <= len(text) <= 2000:
        raise HTTPError(400, "Describe what you see in 10 to 2000 characters.")
    name, state = reverse_geocode(lat, lon)
    name = str(b.get("place") or "").strip()[:120] or name or f"{lat:.3f}, {lon:.3f}"
    return text, {"event_types": types, "severity": severity, "timing": timing, "red_flags": [],
                  "places": [{"name": name, "level": "locality", "district": "", "state": state, "country": "India",
                              "lat": lat, "lon": lon}]}


PAGES = {"/": "index.html", "/login": "auth.html", "/signup": "auth.html", "/admin": "admin.html",
         "/shelter": "shelter.html", "/control": "control.html"}
PAGE_ROLES = {"/admin": ("admin",), "/control": ("admin",), "/shelter": ("shelter", "admin")}  # pages that need a role
STATIC = {"/static/base.css": "text/css; charset=utf-8", "/static/account.js": "text/javascript; charset=utf-8",
          "/static/picker.js": "text/javascript; charset=utf-8"}
ROLES = ["user", "shelter", "admin"]
COVER_KM = 50  # the control room expects an open shelter within this distance of every severe event
STALE_S = 24 * 3600  # shelters not updated for this long are flagged


def control_room():
    """Everything the control room checks, in one payload."""
    now = time.time()
    events = cluster(visible_reports())
    everything = shelters.list_all()
    approved = [s for s in everything if s["status"] == "approved"]
    open_ = [s for s in approved if s["active"]]
    severe = [e for e in events if e["lat"] is not None and SEVERITY.index(e["severity"]) >= 2]
    uncovered = []
    for e in severe:
        nearest = min((km(e, s) for s in open_), default=None)
        if nearest is None or nearest > COVER_KM:
            uncovered.append({**{k: e[k] for k in ("id", "event_types", "severity", "timing", "places", "states", "lat", "lon")},
                              "nearest_km": nearest and round(nearest)})
    full = [s for s in open_ if s["occupancy"] >= 0.9 * s["capacity"]]
    stale = [s for s in approved if now - s["updated"] > STALE_S]
    pending = [s for s in everything if s["status"] == "pending"]
    reports_waiting = [r for r in submitted if not r.get("review")]
    broken = [name for name, s in feed_status.items() if s["state"] == "error"]
    funds = sum(s["funds_needed"] for s in approved)

    def check(title, ok, detail, level="warn", target=None):
        return {"title": title, "state": "ok" if ok else level, "detail": detail, "target": target}

    checks = [
        check("Live data sources", not broken, f"Failing: {', '.join(broken)}" if broken else "All sources are responding",
              "fail" if len(broken) > 1 else "warn", "sources"),
        check("Shelter coverage", not uncovered,
              f"{len(uncovered)} of {len(severe)} severe events have no open shelter within {COVER_KM} km" if uncovered
              else f"Every severe event has an open shelter within {COVER_KM} km", "fail", "uncovered"),
        check("Shelter capacity", not full, f"{len(full)} open shelter{'s are' if len(full) != 1 else ' is'} 90% full or more"
              if full else "Every open shelter has space", target="shelters"),
        check("Shelter updates", not stale, f"{len(stale)} shelter{'s have' if len(stale) != 1 else ' has'} not reported in 24 hours"
              if stale else "Every shelter has reported in the last 24 hours", target="shelters"),
        check("Shelters awaiting approval", not pending, f"{len(pending)} waiting for approval" if pending
              else "Nothing waiting", target="shelters"),
        check("Citizen reports awaiting review", not reports_waiting, f"{len(reports_waiting)} waiting in the admin page"
              if reports_waiting else "Nothing waiting", target="reports"),
    ]
    beds = sum(max(0, s["capacity"] - s["occupancy"]) for s in open_)
    return {
        "now": now, "checks": checks, "sources": feed_status, "uncovered": uncovered,
        "summary": {"events": len(events), "severe": len(severe), "shelters": len(approved), "open": len(open_),
                    "pending": len(pending), "beds_free": beds, "capacity": sum(s["capacity"] for s in open_),
                    "occupancy": sum(s["occupancy"] for s in open_), "funds": funds, "reports_waiting": len(reports_waiting)},
        "severe": [{k: e[k] for k in ("id", "event_types", "severity", "timing", "places", "lat", "lon")} for e in severe],
        "shelters": [{**s, "stale": now - s["updated"] > STALE_S, "full": s in full} for s in everything],
    }


class Handler(BaseHTTPRequestHandler):
    def send(self, status, body, ctype, headers=()):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")  # no other site may frame the sign-in or admin pages
        self.send_header("Referrer-Policy", "same-origin")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, obj, headers=()):
        self.send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", headers)

    # --- request helpers

    def ip(self):
        # ponytail: behind Render's proxy the client IP is the first X-Forwarded-For entry, which a client can
        # spoof -- so per-IP limits are best-effort; DAILY_LIMIT is the cap that actually bounds API spend.
        return self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()

    def token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except CookieError:
            return None
        return cookie["sid"].value if "sid" in cookie else None

    def user(self):
        return auth.user_for(self.token())

    def session_cookie(self, token, max_age=auth.SESSION_S):
        secure = "; Secure" if self.headers.get("X-Forwarded-Proto") == "https" else ""
        return "Set-Cookie", f"sid={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}{secure}"

    def read_body(self):
        # Requiring a JSON content type makes browsers preflight cross-site requests, which this server never
        # approves -- so another website can't sign people in, post reports or change settings through them.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            raise HTTPError(415, "Content-Type must be application/json")
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= 20_000:  # a negative length would make read() block until the client hangs up
            raise HTTPError(400 if length < 0 else 413, "missing, invalid or too large Content-Length")
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            raise HTTPError(400, "invalid JSON")
        if not isinstance(body, dict):
            raise HTTPError(400, "expected a JSON object")
        return body

    def respond(self, handler, *args):
        try:
            handler(*args)
        except HTTPError as e:
            self.send_json(e.status, {"error": str(e)})
        except Exception as e:  # anything unexpected still gets a JSON reply instead of a dropped connection
            traceback.print_exc()
            self.send_json(500, {"error": f"Server error: {type(e).__name__}"})

    def do_GET(self):
        self.respond(self.get, urlparse(self.path))

    def do_POST(self):
        self.respond(lambda: self.post(urlparse(self.path).path, self.read_body()))

    # --- routes

    def get(self, url):
        path = url.path
        if path in PAGES:
            if path in PAGE_ROLES:
                me = self.user()
                if not me:
                    signin = "/login?as=shelter&next=/shelter" if path == "/shelter" else f"/login?next={path}"
                    return self.send(302, b"", "text/plain", [("Location", signin)])
                if me["role"] not in PAGE_ROLES[path]:
                    if path == "/shelter":  # a regular account can opt in to running shelters from this page
                        return self.send(200, (HERE / PAGES[path]).read_bytes(), "text/html; charset=utf-8")
                    return self.send(403, b"This page is for administrators.", "text/plain; charset=utf-8")
            return self.send(200, (HERE / PAGES[path]).read_bytes(), "text/html; charset=utf-8")
        if path in STATIC:
            return self.send(200, (HERE / path.lstrip("/")).read_bytes(), STATIC[path])
        if path == "/api/events":
            return self.send_json(200, {"now": time.time(), "events": cluster(visible_reports()), "sources": feed_status,
                                        "demo": bool(seed), "ai": bool(os.environ.get("ANTHROPIC_API_KEY"))})
        if path == "/api/me":
            return self.send_json(200, {"user": self.user()})
        if path == "/api/shelters":
            return self.send_json(200, {"shelters": shelters.list_public(), "types": shelters.TYPES,
                                        "facilities": shelters.FACILITIES})
        if path == "/api/my/shelters":
            me = self.user()
            if not me:
                raise HTTPError(401, "Sign in first.")
            return self.send_json(200, {"shelters": shelters.list_owned(me["id"]), "types": shelters.TYPES,
                                        "facilities": shelters.FACILITIES})
        if path == "/api/control":
            me = self.user()
            if not me or me["role"] != "admin":
                raise HTTPError(403, "Administrators only.")
            return self.send_json(200, control_room())
        if path == "/api/places":
            q = (parse_qs(url.query).get("q") or [""])[0].strip()[:100]
            if len(q) < 2:
                raise HTTPError(400, "Type at least 2 characters.")
            if not allow(f"search:{self.ip()}", 30, 60):
                raise HTTPError(429, "Too many searches. Wait a minute and try again.")
            return self.send_json(200, {"places": search_places(q)})
        if path.startswith("/api/admin/"):
            me = self.user()
            if not me or me["role"] != "admin":
                raise HTTPError(403, "Administrators only.")
            if path == "/api/admin/reports":
                return self.send_json(200, {"reports": sorted(submitted, key=lambda r: -r["t"])})
            if path == "/api/admin/users":
                return self.send_json(200, {"users": auth.list_users(), "me": me["id"]})
        raise HTTPError(404, "not found")

    def post(self, path, body):
        if path == "/api/signup":
            if not allow(f"signup:{self.ip()}", 5, 3600):
                raise HTTPError(429, "Too many new accounts from your network. Try again in an hour.")
            user, error = auth.signup(str(body.get("name", "")), str(body.get("email", "")), str(body.get("password", "")))
            if error:
                raise HTTPError(400, error)
            if body.get("role") == "shelter":  # safe to self-assign: an operator's shelters stay hidden until approved
                user = auth.update_user(user["id"], role="shelter")
            return self.send_json(201, {"user": user}, [self.session_cookie(auth.new_session(user["id"]))])
        if path == "/api/login":
            if not allow(f"login:{self.ip()}", 10, 900):
                raise HTTPError(429, "Too many sign-in attempts. Wait 15 minutes and try again.")
            user = auth.login(str(body.get("email", "")), str(body.get("password", "")))
            if not user:
                raise HTTPError(401, "That email and password don't match an active account.")
            return self.send_json(200, {"user": user}, [self.session_cookie(auth.new_session(user["id"]))])
        if path == "/api/logout":
            auth.end_session(self.token())
            return self.send_json(200, {"ok": True}, [self.session_cookie("", 0)])

        me = self.user()
        if not me:
            raise HTTPError(401, "Sign in first.")
        if path == "/api/me/password":
            error = auth.change_password(me["id"], str(body.get("current", "")), str(body.get("new", "")), self.token())
            if error:
                raise HTTPError(400, error)
            return self.send_json(200, {"ok": True})
        if path == "/api/me/alert":
            alert = body.get("alert")
            auth.set_alert(me["id"], clean_alert(alert) if alert is not None else None)
            return self.send_json(200, {"user": self.user()})
        if path == "/api/me/role":
            if body.get("role") != "shelter" or me["role"] != "user":
                raise HTTPError(400, "Regular accounts can switch to a shelter operator account; nothing else changes here.")
            return self.send_json(200, {"user": auth.update_user(me["id"], role="shelter")})
        if path == "/api/reports":
            return self.send_json(201, self.new_report(me, body))
        if path.startswith("/api/my/shelters"):
            return self.my_shelters(me, path.removeprefix("/api/my/shelters").strip("/"), body)
        if path.startswith("/api/admin/"):
            if me["role"] != "admin":
                raise HTTPError(403, "Administrators only.")
            return self.admin(me, path.removeprefix("/api/admin/"), body)
        raise HTTPError(404, "not found")

    def new_report(self, me, body):
        source_type = body.get("source_type", "citizen")
        if source_type not in PUBLIC_SOURCES:
            raise HTTPError(400, f"source_type must be one of {PUBLIC_SOURCES}")
        if body.get("text"):  # free text for Claude to analyse
            if not os.environ.get("ANTHROPIC_API_KEY"):
                raise HTTPError(503, "AI analysis isn't set up on this server. Fill in the form fields instead.")
            text = str(body["text"]).strip()
            if not 10 <= len(text) <= 5000:
                raise HTTPError(400, "Report text must be 10 to 5000 characters.")
            if not allow(f"ai:{me['id']}", HOURLY_AI_PER_USER, 3600):
                raise HTTPError(429, f"You can have {HOURLY_AI_PER_USER} reports analysed an hour. Try again later.")
            if not allow("ai:*", DAILY_LIMIT, 86400):
                raise HTTPError(429, "This site has reached its daily limit for AI analysis. Use the form fields instead.")
            try:
                data = extract(text)
            except ExtractionError as e:
                raise HTTPError(502, str(e))
            if not data["event_types"]:
                raise HTTPError(422, "No weather or hazard event found in this text.")
            for p in data["places"]:
                p["lat"], p["lon"] = geocode(p) or (None, None)
        else:
            text, data = structured_report(body)
            if not allow(f"report:{me['id']}", 30, 3600):
                raise HTTPError(429, "You've sent 30 reports in the last hour. Try again later.")
        report = {"id": uuid.uuid4().hex[:8], "t": time.time(), "text": text, "source_type": source_type,
                  "source_name": str(body.get("source_name") or me["name"]).strip()[:80],
                  "source_id": f"user:{me['id']}", "user_id": me["id"], "review": None, **data}
        with write_lock:  # ponytail: whole-file JSON rewrite per report; move to SQLite for real traffic
            submitted.append(report)
            save(REPORTS_FILE, submitted)
        return report

    def my_shelters(self, me, rest, body):
        """Operators manage their own shelters; admins can manage any."""
        if me["role"] not in ("shelter", "admin"):
            raise HTTPError(403, "Switch to a shelter operator account first.")
        try:
            if not rest:  # create
                if len(shelters.list_owned(me["id"])) >= 20:
                    raise HTTPError(400, "An account can register up to 20 shelters.")
                fields = self.with_state(shelters.clean(body))
                return self.send_json(201, {"shelter": shelters.create(me["id"], fields)})
            ident, _, action = rest.partition("/")
            shelter = shelters.get(int(ident)) if ident.isdigit() else None
            if not shelter or (shelter["owner_id"] != me["id"] and me["role"] != "admin"):
                raise HTTPError(404, "No such shelter.")
            if action == "quick":
                return self.send_json(200, {"shelter": shelters.quick(shelter["id"], body.get("active"), body.get("occupancy"))})
            if action == "delete":
                shelters.delete(shelter["id"])
                return self.send_json(200, {"ok": True})
            if not action:
                return self.send_json(200, {"shelter": shelters.update(shelter["id"], self.with_state(shelters.clean(body)))})
        except shelters.Invalid as e:
            raise HTTPError(400, str(e))
        raise HTTPError(404, "not found")

    @staticmethod
    def with_state(fields):
        if not fields["state"]:  # fill the state from the map point, for the state filter and the control room
            fields["state"] = reverse_geocode(fields["lat"], fields["lon"])[1]
        return fields

    def admin(self, me, path, body):
        kind, _, ident = path.partition("/")
        if kind == "shelters":
            shelter = shelters.get(int(ident)) if ident.isdigit() else None
            if not shelter:
                raise HTTPError(404, "No such shelter.")
            if body.get("status") not in shelters.STATUSES:
                raise HTTPError(400, "status must be pending, approved or rejected.")
            return self.send_json(200, {"shelter": shelters.set_status(shelter["id"], body["status"], str(body.get("note", "")))})
        if kind == "reports":
            with write_lock:
                report = next((r for r in submitted if r["id"] == ident), None)
                if not report:
                    raise HTTPError(404, "No such report.")
                if body.get("delete"):
                    submitted.remove(report)
                elif body.get("review") in ("verified", "rejected", "pending"):
                    report["review"] = None if body["review"] == "pending" else body["review"]
                else:
                    raise HTTPError(400, "Send review (verified, rejected or pending) or delete.")
                save(REPORTS_FILE, submitted)
            return self.send_json(200, {"ok": True})
        if kind == "users":
            try:
                user_id = int(ident)
            except ValueError:
                raise HTTPError(404, "No such user.")
            if body.get("reset_password"):
                temp = auth.reset_password(user_id)
                if not temp:
                    raise HTTPError(404, "No such user.")
                return self.send_json(200, {"temporary_password": temp})
            role, disabled = body.get("role"), body.get("disabled")
            if user_id == me["id"] and (role is not None or disabled is not None):
                raise HTTPError(400, "You can't change your own role or disable your own account.")
            if role not in (None, *ROLES) or disabled not in (None, True, False):
                raise HTTPError(400, "role must be user, shelter or admin; disabled must be true or false.")
            user = auth.update_user(user_id, role, disabled)
            if not user:
                raise HTTPError(404, "No such user.")
            return self.send_json(200, {"user": user})
        raise HTTPError(404, "not found")

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    auth.init()
    shelters.init()
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
    print(f"Weather intelligence prototype on http://{'localhost' if host == '127.0.0.1' else host}:{port}", flush=True)
    threading.Thread(target=refresh_feeds, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()
