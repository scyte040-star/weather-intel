"""Live, keyless data sources. Each fetcher returns report dicts shaped like submitted reports (see app.py)."""
import json
import re
import ssl
import time
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

try:
    import certifi  # Mozilla's CA list: SACHET's certificate chains to a root that Windows' Python store lacks
    SSL = ssl.create_default_context(cafile=certifi.where())
except ImportError:
    SSL = ssl.create_default_context()

UA = {"User-Agent": "weather-intel-prototype/0.1 (+https://github.com/scyte040-star/weather-intel)"}


def get(url, timeout=20):
    with urlopen(Request(url, headers=UA), timeout=timeout, context=SSL) as r:
        return r.read()


def epoch(iso):
    d = datetime.fromisoformat(iso)
    return (d if d.tzinfo else d.replace(tzinfo=timezone.utc)).timestamp()  # feeds without an offset use UTC


def in_india(lat, lon):
    return 6 <= lat <= 38 and 68 <= lon <= 98


def place(name, level, state="", lat=None, lon=None):
    return {"name": name.strip(), "level": level, "district": "", "state": state.strip(), "country": "India",
            "lat": lat, "lon": lon}


def report(id, t, text, source_type, source_name, url, types, severity, timing, places, expires=None):
    return {"id": id, "t": t, "expires": expires, "text": text, "source_type": source_type, "source_name": source_name,
            "url": url, "event_types": types, "severity": severity, "timing": timing, "red_flags": [], "places": places}


# --- NDMA SACHET: official CAP alerts from IMD, CWC and state disaster management authorities ---

CAP = {"cap": "urn:oasis:names:tc:emergency:cap:1.2"}
CAP_SEVERITY = {"Minor": "low", "Moderate": "moderate", "Severe": "high", "Extreme": "extreme"}
KEYWORDS = [("flash flood", "Flash Flood"), ("flood", "Flood"), ("cloudburst", "Cloudburst"),
            ("heavy rain", "Heavy Rainfall"), ("rain", "Rain"), ("thunder", "Thunderstorm"), ("lightning", "Lightning"),
            ("hail", "Hailstorm"), ("squall", "Strong Winds"), ("wind", "Strong Winds"), ("cyclon", "Cyclone"),
            ("depression", "Cyclone"), ("dust", "Dust Storm"), ("heat", "Heatwave"), ("cold", "Cold Wave"),
            ("fog", "Fog"), ("landslide", "Landslide"), ("avalanche", "Avalanche"), ("drought", "Drought"),
            ("fire", "Wildfire"), ("earthquake", "Earthquake"), ("tsunami", "Tsunami")]
_cap_cache = {}  # RSS guid -> parsed report (None = not usable); a CAP file never changes once published


def event_types(event):
    e, found = event.lower(), []
    for word, t in KEYWORDS:
        if word in e:
            e = e.replace(word, " ")  # consume it, so "heavy rain" isn't also counted as plain "rain"
            found.append(t)
    return list(dict.fromkeys(found)) or ["Other"]


def sender_state(*names):
    """'Andhra Pradesh SDMA' / 'Uttar-Pradesh-SDMA' -> the state; '' for national senders like IMD or CWC."""
    for name in names:
        m = re.match(r"(.+?)[\s-]*SDMA$", (name or "").strip())
        if m:
            return m.group(1).replace("-", " ")
    return ""


def cap_places(desc, lat, lon, fallback_state=""):
    """areaDesc -> places. Examples: 'Ganga, Rishikesh, Dehradun, Uttarakhand' (with a point),
    'Karur, Tiruchirappalli districts of Tamil Nadu', '8 districts of Rajasthan', '12 Mandals' (from a state SDMA)."""
    m = re.search(r"\bof\s+([^,]+)$", desc) or re.search(r",\s*([^,]+)$", desc)
    state = m.group(1) if m else (fallback_state or desc)
    if lat is not None:
        return [place(desc, "locality", state, lat, lon)]
    districts = re.match(r"(.*?)\s+districts?\s+of\s+", desc, re.I)
    if districts and not districts.group(1)[:1].isdigit():  # named districts; "8 districts of X" names none
        return [place(d, "district", state) for d in re.split(r",|\band\b", districts.group(1)) if d.strip()]
    if not m and fallback_state and desc:  # keep the local name, but let the state pin it if it can't be found
        return [place(desc, "locality", state), place(state, "state", state)]
    return [place(state, "state", state)] if state else []


def parse_cap(guid, sender, body):
    alert = ET.fromstring(body)
    if alert.findtext("cap:status", "", CAP) != "Actual" or alert.findtext("cap:msgType", "", CAP) == "Cancel":
        return None
    infos = alert.findall("cap:info", CAP)
    info = next((i for i in infos if i.findtext("cap:language", "", CAP).lower().startswith("en")), None)
    info = info if info is not None else (infos[0] if infos else None)
    if info is None:
        return None
    field = lambda tag: info.findtext(f"cap:{tag}", "", CAP).strip()
    area = info.find("cap:area", CAP)
    desc = area.findtext("cap:areaDesc", "", CAP).strip() if area is not None else ""
    try:  # SACHET puts a representative point's latitude/longitude in <altitude>/<ceiling>
        lat, lon = float(area.findtext("cap:altitude", "", CAP)), float(area.findtext("cap:ceiling", "", CAP))
        if not in_india(lat, lon):
            lat = lon = None
    except (AttributeError, ValueError):
        lat = lon = None
    return report(f"sachet-{guid}", epoch(alert.findtext("cap:sent", "", CAP)), field("description") or field("headline"),
                  "official", sender, field("web") or "https://sachet.ndma.gov.in/",
                  event_types(f"{field('event')} {field('headline')}"),  # headline adds e.g. "...with lightning"
                  CAP_SEVERITY.get(field("severity"), "moderate"),
                  "observed" if field("certainty") == "Observed" else "forecast",
                  cap_places(desc, lat, lon, sender_state(sender, alert.findtext("cap:sender", "", CAP))),
                  epoch(field("expires")) if field("expires") else None)


def sachet():
    rss = ET.fromstring(get("https://sachet.ndma.gov.in/cap_public_website/rss/rss_india.xml"))
    items = [(i.findtext("guid"), i.findtext("link"),
              re.sub(r".*\((.*)\).*", r"\1", i.findtext("author") or "") or "NDMA") for i in rss.iter("item")]

    def fetch(item):
        guid, link, sender = item
        try:
            return guid, parse_cap(guid, sender, get(link)), True
        except (OSError, ET.ParseError, ValueError):
            return guid, None, False  # not cached: retried on the next refresh

    with ThreadPoolExecutor(4) as pool:
        for guid, rep, ok in pool.map(fetch, [x for x in items if x[0] not in _cap_cache]):
            if ok:
                _cap_cache[guid] = rep
    for guid in set(_cap_cache) - {x[0] for x in items}:  # dropped from the feed
        del _cap_cache[guid]
    now = time.time()
    return [r for r in (_cap_cache.get(x[0]) for x in items) if r and (r["expires"] or r["t"] + 86400) > now]


# --- GDACS (UN/EC Global Disaster Alert and Coordination System): current events affecting India ---

GDACS_TYPES = {"TC": "Cyclone", "FL": "Flood", "EQ": "Earthquake", "DR": "Drought", "WF": "Wildfire", "TS": "Tsunami"}
GDACS_SEVERITY = {"Green": "moderate", "Orange": "high", "Red": "extreme"}


def gdacs():
    out = []
    for f in json.loads(get("https://www.gdacs.org/gdacsapi/api/events/geteventlist/SEARCH?country=India"))["features"]:
        p = f["properties"]
        if p.get("iscurrent") != "true":
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        detail = (p.get("severitydata") or {}).get("severitytext", "")
        out.append(report(f"gdacs-{p['eventtype']}-{p['eventid']}", epoch(p["todate"]),
                          f"{p['htmldescription']} {detail}".strip(), "official", "GDACS", p["url"]["report"],
                          [GDACS_TYPES.get(p["eventtype"], "Other")], GDACS_SEVERITY.get(p["alertlevel"], "moderate"),
                          "observed", [place(p["name"], "region", lat=lat, lon=lon)]))
    return out


# --- USGS: earthquakes located in India, past 3 days ---

def usgs():
    out = []
    for f in json.loads(get("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_week.geojson"))["features"]:
        p = f["properties"]
        t = p["time"] / 1000
        if not (p.get("place") or "").endswith("India") or time.time() - t > 3 * 86400:  # not "Indian Ocean"
            continue
        lon, lat = f["geometry"]["coordinates"][:2]
        mag = p.get("mag") or 0
        severity = "low" if mag < 4 else "moderate" if mag < 5 else "high" if mag < 6.5 else "extreme"
        out.append(report(f"usgs-{f['id']}", t, p["title"], "official", "USGS", p["url"], ["Earthquake"], severity,
                          "observed", [place(p["place"], "locality", lat=lat, lon=lon)]))
    return out


# --- Open-Meteo: past-24-hour weather for major cities, flagged against IMD thresholds ---

CITIES = [("Mumbai", "Maharashtra", 19.076, 72.878), ("Delhi", "Delhi", 28.614, 77.209),
          ("Kolkata", "West Bengal", 22.573, 88.364), ("Chennai", "Tamil Nadu", 13.083, 80.271),
          ("Bengaluru", "Karnataka", 12.972, 77.595), ("Hyderabad", "Telangana", 17.385, 78.487),
          ("Ahmedabad", "Gujarat", 23.023, 72.571), ("Pune", "Maharashtra", 18.520, 73.857),
          ("Jaipur", "Rajasthan", 26.912, 75.787), ("Lucknow", "Uttar Pradesh", 26.847, 80.946),
          ("Patna", "Bihar", 25.594, 85.138), ("Bhopal", "Madhya Pradesh", 23.260, 77.413),
          ("Bhubaneswar", "Odisha", 20.296, 85.825), ("Guwahati", "Assam", 26.145, 91.736),
          ("Thiruvananthapuram", "Kerala", 8.524, 76.937), ("Kochi", "Kerala", 9.931, 76.267),
          ("Chandigarh", "Chandigarh", 30.733, 76.779), ("Dehradun", "Uttarakhand", 30.317, 78.032),
          ("Shimla", "Himachal Pradesh", 31.105, 77.173), ("Srinagar", "Jammu and Kashmir", 34.084, 74.797),
          ("Ranchi", "Jharkhand", 23.344, 85.310), ("Raipur", "Chhattisgarh", 21.251, 81.630),
          ("Visakhapatnam", "Andhra Pradesh", 17.687, 83.219), ("Nagpur", "Maharashtra", 21.146, 79.088),
          ("Indore", "Madhya Pradesh", 22.720, 75.858), ("Surat", "Gujarat", 21.170, 72.831),
          ("Varanasi", "Uttar Pradesh", 25.318, 82.974), ("Jodhpur", "Rajasthan", 26.239, 73.024),
          ("Panaji", "Goa", 15.491, 73.828), ("Imphal", "Manipur", 24.817, 93.937),
          ("Agartala", "Tripura", 23.832, 91.287), ("Port Blair", "Andaman and Nicobar Islands", 11.623, 92.727),
          ("Mangaluru", "Karnataka", 12.914, 74.856), ("Madurai", "Tamil Nadu", 9.925, 78.120),
          ("Leh", "Ladakh", 34.153, 77.577)]


def weather_events(city, state, lat, lon, hourly):
    """One city's last 24 hourly readings -> reports for readings past IMD thresholds."""
    times, out = hourly["time"], []
    rain = sum(v or 0 for v in hourly["precipitation"])
    if rain >= 64.5:  # IMD 24-hour rainfall scale
        word, sev = (("extremely heavy", "extreme") if rain >= 204.5 else ("very heavy", "high") if rain >= 115.6
                     else ("heavy", "moderate"))
        out.append(("Heavy Rainfall", sev, times[-1],
                    f"{rain:.0f} mm of rain in the past 24 hours at {city}: {word} rainfall on IMD's scale"))
    temps = [v if v is not None else -99 for v in hourly["temperature_2m"]]
    if max(temps) >= 45:  # IMD: an actual maximum of 45 °C or more is a heatwave
        out.append(("Heatwave", "high" if max(temps) >= 47 else "moderate", times[temps.index(max(temps))],
                    f"Temperature reached {max(temps):.1f} °C at {city} in the past 24 hours"))
    gusts = [v or 0 for v in hourly["wind_gusts_10m"]]
    if max(gusts) >= 62:  # gale force
        out.append(("Strong Winds", "high" if max(gusts) >= 89 else "moderate", times[gusts.index(max(gusts))],
                    f"Wind gusts up to {max(gusts):.0f} km/h at {city} in the past 24 hours"))
    return [report(f"om-{city}-{kind}", t, text + " (model estimate)", "model", "Open-Meteo", "https://open-meteo.com/",
                   [kind], sev, "observed", [place(city, "city", state, lat, lon)]) for kind, sev, t, text in out]


def open_meteo():
    q = urlencode({"latitude": ",".join(str(c[2]) for c in CITIES), "longitude": ",".join(str(c[3]) for c in CITIES),
                   "hourly": "precipitation,temperature_2m,wind_gusts_10m", "past_hours": 24, "forecast_hours": 0,
                   "timeformat": "unixtime"})
    data = json.loads(get("https://api.open-meteo.com/v1/forecast?" + q))
    return [r for c, d in zip(CITIES, data) for r in weather_events(*c, d["hourly"])]


# Fastest first: SACHET's first run fetches ~100 alert files and geocodes some districts.
SOURCES = {"GDACS": gdacs, "USGS": usgs, "Open-Meteo": open_meteo, "NDMA SACHET": sachet}
