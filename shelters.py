"""Shelters: relief camps and other safe places. Operators register and update them; the control room approves them."""
import json
import os
import re
import time

import auth
import feeds

TYPES = ["Relief camp", "School", "Community hall", "Stadium", "Religious place", "Hospital", "Government building", "Other"]
FACILITIES = ["Food", "Drinking water", "Beds", "Medical aid", "Toilets", "Electricity", "Mobile charging",
              "Women's area", "Child care", "Wheelchair access", "Pets allowed", "Internet"]
STATUSES = ["pending", "approved", "rejected"]
PHONE_RE = re.compile(r"^\+?[0-9][0-9 ()-]{5,19}$")
PUBLIC = ["id", "name", "type", "address", "state", "lat", "lon", "capacity", "occupancy", "facilities", "active",
          "funds_needed", "funds_note", "supplies", "phone", "notes", "updated", "demo"]


class Invalid(ValueError):
    pass


def init():
    with auth.tx() as db:
        db.execute("""CREATE TABLE IF NOT EXISTS shelters (
            id INTEGER PRIMARY KEY, owner_id INTEGER, name TEXT NOT NULL, type TEXT NOT NULL, address TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT '', lat REAL NOT NULL, lon REAL NOT NULL, capacity INTEGER NOT NULL,
            occupancy INTEGER NOT NULL DEFAULT 0, facilities TEXT NOT NULL DEFAULT '[]', active INTEGER NOT NULL DEFAULT 1,
            funds_needed INTEGER NOT NULL DEFAULT 0, funds_note TEXT NOT NULL DEFAULT '', supplies TEXT NOT NULL DEFAULT '',
            phone TEXT NOT NULL DEFAULT '', notes TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'pending',
            review_note TEXT NOT NULL DEFAULT '', demo INTEGER NOT NULL DEFAULT 0, created REAL NOT NULL, updated REAL NOT NULL)""")
    if os.environ.get("DEMO_SHELTERS") == "1":
        seed_demo()


def _dict(row):
    s = dict(row)
    s["facilities"], s["active"], s["demo"] = json.loads(s["facilities"]), bool(s["active"]), bool(s["demo"])
    return s


def public(s):
    return {k: s[k] for k in PUBLIC}


def clean(b):
    """Validate an operator's form -> the editable fields."""
    def text(key, lo, hi, label):
        v = str(b.get(key) or "").strip()
        if not lo <= len(v) <= hi:
            raise Invalid(f"{label} must be {lo} to {hi} characters." if lo else f"{label} can be at most {hi} characters.")
        return v

    def number(key, lo, hi, label):
        try:
            v = int(b.get(key) or 0)
        except (TypeError, ValueError):
            raise Invalid(f"{label} must be a whole number.")
        if not lo <= v <= hi:
            raise Invalid(f"{label} must be between {lo:,} and {hi:,}.")
        return v

    s = {"name": text("name", 3, 120, "Name"), "address": text("address", 3, 200, "Address"),
         "capacity": number("capacity", 1, 100_000, "Capacity"), "occupancy": number("occupancy", 0, 100_000, "People staying"),
         "funds_needed": number("funds_needed", 0, 1_000_000_000, "Funds needed"),
         "funds_note": text("funds_note", 0, 300, "What the funds are for"), "supplies": text("supplies", 0, 500, "Supplies needed"),
         "notes": text("notes", 0, 1000, "Notes"), "state": text("state", 0, 60, "State"),
         "type": b.get("type"), "active": bool(b.get("active", True))}
    if s["type"] not in TYPES:
        raise Invalid("Choose what kind of shelter this is.")
    try:
        s["lat"], s["lon"] = float(b["lat"]), float(b["lon"])
    except (KeyError, TypeError, ValueError):
        raise Invalid("Choose where the shelter is.")
    if not feeds.in_india(s["lat"], s["lon"]):
        raise Invalid("Pick a location in India.")
    facilities = b.get("facilities") or []
    if not isinstance(facilities, list) or any(f not in FACILITIES for f in facilities):
        raise Invalid("Unknown facility.")
    s["facilities"] = json.dumps(list(dict.fromkeys(facilities)))
    s["phone"] = str(b.get("phone") or "").strip()
    if s["phone"] and not PHONE_RE.match(s["phone"]):
        raise Invalid("Enter a valid phone number, e.g. +91 98765 43210.")
    if s["funds_needed"] and not s["funds_note"]:
        raise Invalid("Say what the funds are for.")
    return s


def create(owner_id, fields):
    now = time.time()
    cols = ["owner_id", "created", "updated", *fields]
    with auth.tx() as db:
        cur = db.execute(f"INSERT INTO shelters ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                         [owner_id, now, now, *fields.values()])
    return get(cur.lastrowid)


def update(shelter_id, fields):
    fields = {**fields, "updated": time.time()}
    with auth.tx() as db:
        db.execute(f"UPDATE shelters SET {', '.join(f'{k} = ?' for k in fields)} WHERE id = ?", [*fields.values(), shelter_id])
    return get(shelter_id)


def quick(shelter_id, active, occupancy):
    """The one-tap update operators make during an emergency: open or closed, and how many people are staying."""
    try:
        occupancy = int(occupancy)
    except (TypeError, ValueError):
        raise Invalid("People staying must be a whole number.")
    if not 0 <= occupancy <= 100_000:
        raise Invalid("People staying must be between 0 and 100,000.")
    return update(shelter_id, {"active": bool(active), "occupancy": occupancy})


def get(shelter_id):
    with auth.tx() as db:
        row = db.execute("SELECT * FROM shelters WHERE id = ?", (shelter_id,)).fetchone()
    return row and _dict(row)


def list_all(where="1 = 1", args=()):
    with auth.tx() as db:
        return [_dict(r) for r in db.execute(f"SELECT * FROM shelters WHERE {where} ORDER BY name", args)]


def list_public():
    return [public(s) for s in list_all("status = 'approved'")]


def list_owned(owner_id):
    return list_all("owner_id = ?", (owner_id,))


def set_status(shelter_id, status, note=""):
    with auth.tx() as db:
        db.execute("UPDATE shelters SET status = ?, review_note = ? WHERE id = ?", (status, note[:300], shelter_id))
    return get(shelter_id)


def delete(shelter_id):
    with auth.tx() as db:
        db.execute("DELETE FROM shelters WHERE id = ?", (shelter_id,))


DEMO = [  # clearly labelled samples (DEMO_SHELTERS=1), so a fresh demo isn't an empty map; no phone numbers
    ("Relief camp", "Patna", "Bihar", 25.61, 85.14, 400, 310, ["Food", "Drinking water", "Beds", "Toilets", "Medical aid"], True, 250000, "Tarpaulins and dry rations"),
    ("School", "Varanasi", "Uttar Pradesh", 25.32, 82.97, 250, 245, ["Food", "Drinking water", "Toilets", "Electricity"], True, 0, ""),
    ("Community hall", "Dehradun", "Uttarakhand", 30.32, 78.03, 150, 40, ["Beds", "Medical aid", "Mobile charging", "Women's area"], True, 80000, "Blankets for the cold nights"),
    ("Stadium", "Guwahati", "Assam", 26.14, 91.74, 1200, 860, ["Food", "Drinking water", "Toilets", "Child care", "Medical aid"], True, 500000, "Water purification and medicines"),
    ("Government building", "Bhubaneswar", "Odisha", 20.30, 85.82, 600, 0, ["Beds", "Electricity", "Wheelchair access"], False, 0, ""),
    ("Relief camp", "Visakhapatnam", "Andhra Pradesh", 17.69, 83.22, 350, 120, ["Food", "Drinking water", "Beds", "Pets allowed"], True, 0, ""),
    ("Religious place", "Kannauj", "Uttar Pradesh", 27.05, 79.92, 180, 175, ["Food", "Drinking water", "Toilets"], True, 60000, "Cooking gas and utensils"),
    ("School", "Kochi", "Kerala", 9.93, 76.27, 220, 15, ["Food", "Drinking water", "Beds", "Internet", "Mobile charging"], True, 0, ""),
]


def seed_demo():
    with auth.tx() as db:
        if db.execute("SELECT 1 FROM shelters WHERE demo = 1").fetchone():
            return
        now = time.time()
        for i, (kind, city, state, lat, lon, cap, occ, fac, active, funds, note) in enumerate(DEMO):
            db.execute("""INSERT INTO shelters (name, type, address, state, lat, lon, capacity, occupancy, facilities, active,
                          funds_needed, funds_note, notes, status, demo, created, updated)
                          VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved', 1, ?, ?)""",
                       (f"Sample {kind.lower()}, {city}", kind, f"{city}, {state}", state, lat, lon, cap, occ, json.dumps(fac),
                        int(active), funds, note, "Sample shelter for demonstration; not a real facility.", now, now - i * 3 * 3600))
