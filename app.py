"""Weather intelligence prototype: reports -> Claude extraction -> geocode -> group duplicates -> verify -> live map.

Run:   pip install -r requirements.txt; python app.py        then open http://localhost:8000
Live data (NDMA SACHET, GDACS, USGS, Open-Meteo) needs no key. Set ANTHROPIC_API_KEY to also analyse typed-in
reports. DEMO_DATA=1 adds the simulated seed reports. Hosting: HOST=0.0.0.0 (PORT from the environment).
"""
import json
import math
import os
import threading
import time
import uuid
from collections import Counter, defaultdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import feeds

HERE = Path(__file__).parent
PROMPT = (HERE / "extract_prompt.md").read_text(encoding="utf-8")
REPORTS_FILE = HERE / "reports.json"
GEOCACHE_FILE = HERE / "geocache.json"

EVENT_TYPES = ["Heavy Rainfall", "Rain", "Flood", "Flash Flood", "Cloudburst", "Thunderstorm", "Lightning", "Hailstorm",
               "Strong Winds", "Cyclone", "Dust Storm", "Heatwave", "Cold Wave", "Fog", "Landslide", "Avalanche",
               "Drought", "Wildfire", "Earthquake", "Tsunami", "Other"]
SEVERITY = ["low", "moderate", "high", "extreme"]
LEVELS = ["locality", "city", "district", "region", "state", "country"]  # most specific first
PUBLIC_SOURCES = ["citizen", "social", "news"]  # "official" only arrives via trusted feeds, never the public form
RADIUS_KM = 25  # ponytail: one radius for every place level; scale by level if events over/under-merge
WINDOW_S = 6 * 3600
# Every analysed report is a paid Claude API call, and a hosted form is open to anyone with the link.
DAILY_LIMIT = int(os.environ.get("DAILY_REPORT_LIMIT", "100"))  # all visitors combined: the hard cap on spend
HOURLY_PER_IP = int(os.environ.get("HOURLY_REPORTS_PER_IP", "10"))

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
recent_calls = defaultdict(list)  # "*" (everyone) or client IP -> timestamps of recent extraction calls


def over_limit(ip):
    """Record one extraction call, or return why it's refused."""
    now = time.time()
    with rate_lock:
        recent_calls["*"] = [t for t in recent_calls["*"] if now - t < 86400]
        recent_calls[ip] = [t for t in recent_calls[ip] if now - t < 3600]
        if len(recent_calls["*"]) >= DAILY_LIMIT:
            return "This demo has reached its daily limit for analysing reports. Try again tomorrow."
        if len(recent_calls[ip]) >= HOURLY_PER_IP:
            return f"You can submit {HOURLY_PER_IP} reports an hour. Try again later."
        recent_calls["*"].append(now)
        recent_calls[ip].append(now)
        return None


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
    return [r for r in seed if r["t"] <= now] + [r for rs in list(live.values()) for r in rs] + submitted


class ExtractionError(Exception):
    pass


def extract(text):
    try:
        import anthropic  # lazy, so the seed-only demo runs without the SDK installed
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


def geocode(place):
    """Place fields -> (lat, lon) via OpenStreetMap Nominatim, or None. Never guesses an ambiguous place."""
    if place["level"] not in ("state", "region", "country") and not place["state"]:
        return None
    name, district, state, country = (place[k] for k in ("name", "district", "state", "country"))
    # dedupe parts: "Pune, Pune, Maharashtra" (city == district) is common and confuses the geocoder
    queries = [", ".join(dict.fromkeys(p for p in parts if p)) for parts in
               ((name, district, state, country), (name, state, country))]
    with geo_lock:  # Nominatim policy: at most 1 request/second, identify the app
        for q in dict.fromkeys(queries):
            if q not in geocache:
                time.sleep(1)
                url = "https://nominatim.openstreetmap.org/search?" + urlencode({"q": q, "format": "jsonv2", "limit": 1})
                try:
                    hits = json.load(urlopen(Request(url, headers={"User-Agent": "weather-intel-prototype/0.1"}), timeout=10))
                    geocache[q] = [float(hits[0]["lat"]), float(hits[0]["lon"])] if hits else None
                except (OSError, ValueError, KeyError, IndexError, TypeError):
                    return None  # network failure or unexpected reply (e.g. an HTML block page): don't cache
                save(GEOCACHE_FILE, geocache)
            if geocache[q]:
                return geocache[q]
    return None


def km(a, b):
    la1, lo1, la2, lo2 = map(math.radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    return 12742 * math.asin(math.sqrt(h))


def mapped(r):
    return [p for p in r["places"] if p.get("lat") is not None]


def verify(group):
    """Transparent rules, not a model score: official source > red flags > independent corroboration."""
    sources = {r["source_name"].lower() for r in group}
    if any(r["source_type"] == "official" for r in group):
        return "verified", "Confirmed by an official source"
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
        "sources": len({r["source_name"].lower() for r in g}),
        "first_t": g[0]["t"],
        "last_t": g[-1]["t"],
        "reports": [{k: r.get(k) for k in ("t", "text", "source_type", "source_name", "red_flags", "url")} for r in g],
    }


class Handler(BaseHTTPRequestHandler):
    def send(self, status, body, ctype):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_json(self, status, obj):
        self.send(status, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/":
            return self.send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/events":
            return self.send_json(200, {"now": time.time(), "events": cluster(visible_reports()), "sources": feed_status,
                                        "demo": bool(seed), "ai": bool(os.environ.get("ANTHROPIC_API_KEY"))})
        self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?")[0] != "/api/reports":
            return self.send_json(404, {"error": "not found"})
        # Requiring a JSON content type makes browsers preflight cross-site requests, which this server never
        # approves -- so another website can't post reports (and spend API credits) through a visitor's browser.
        if not self.headers.get("Content-Type", "").startswith("application/json"):
            return self.send_json(415, {"error": "Content-Type must be application/json"})
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = -1
        if not 0 <= length <= 20_000:  # a negative length would make read() block until the client hangs up
            return self.send_json(400 if length < 0 else 413, {"error": "missing, invalid or too large Content-Length"})
        try:
            body = json.loads(self.rfile.read(length))
        except ValueError:
            return self.send_json(400, {"error": "invalid JSON"})
        if not isinstance(body, dict):
            return self.send_json(400, {"error": "expected a JSON object"})
        text = str(body.get("text", "")).strip()
        source_type = body.get("source_type")
        source_name = str(body.get("source_name", "")).strip()[:80] or "anonymous"  # all anonymous = one source
        if not 10 <= len(text) <= 5000:
            return self.send_json(400, {"error": "report text must be 10-5000 characters"})
        if source_type not in PUBLIC_SOURCES:
            return self.send_json(400, {"error": f"source_type must be one of {PUBLIC_SOURCES}"})
        # ponytail: behind Render's proxy the client IP is the first X-Forwarded-For entry, which a client can
        # spoof -- so the per-IP limit is best-effort; DAILY_LIMIT is the cap that actually bounds spend.
        ip = self.headers.get("X-Forwarded-For", self.client_address[0]).split(",")[0].strip()
        refused = over_limit(ip)
        if refused:
            return self.send_json(429, {"error": refused})
        try:
            data = extract(text)
        except ExtractionError as e:
            return self.send_json(502, {"error": str(e)})
        except Exception as e:  # anything unexpected still gets a JSON reply instead of a dropped connection
            return self.send_json(500, {"error": f"Extraction failed: {type(e).__name__}: {e}"})
        if not data["event_types"]:
            return self.send_json(422, {"error": "No weather or hazard event found in this text"})
        for p in data["places"]:
            p["lat"], p["lon"] = geocode(p) or (None, None)
        report = {"id": uuid.uuid4().hex[:8], "t": time.time(), "text": text,
                  "source_type": source_type, "source_name": source_name, **data}
        with write_lock:  # ponytail: whole-file JSON rewrite per report; move to SQLite for real traffic
            submitted.append(report)
            save(REPORTS_FILE, submitted)
        self.send_json(201, report)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    host, port = os.environ.get("HOST", "127.0.0.1"), int(os.environ.get("PORT", "8000"))
    print(f"Weather intelligence prototype on http://{'localhost' if host == '127.0.0.1' else host}:{port}", flush=True)
    threading.Thread(target=refresh_feeds, daemon=True).start()
    ThreadingHTTPServer((host, port), Handler).serve_forever()
