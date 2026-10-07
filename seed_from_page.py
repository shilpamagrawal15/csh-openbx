"""Idempotent seed: copy the resources that used to be hard-coded in index.html
into the Postgres `resources` table, and make the demo users loggable.

    DATABASE_URL=postgres://... python3 seed_from_page.py

- `page_resources.json` is the exact `resources` array that was hard-coded in
  index.html (extracted once, before the page switched to GET /api/resources).
- Rows that already exist in the DB win: for multilingual JSONB fields the page
  only fills in languages the DB row is missing; scalar columns are only filled
  where the DB value is NULL.
- Page hours like {"Mon - Sat": "9:00 AM - 3:30 PM"} are converted to the DB's
  per-day format {"mon": "09:00-15:30", ..., "sun": "closed"}. Days the page
  did not mention are stored as "closed".
- Every seed user whose password_hash is not a real bcrypt hash of the demo
  password gets one (demo password: demo1234).
- Also applies the two small schema additions (safe to re-run).
"""
import json
import os
import re
from pathlib import Path

import bcrypt
import psycopg
from psycopg.types.json import Jsonb

HERE = Path(__file__).resolve().parent
DEMO_PASSWORD = "demo1234"
DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]

MIGRATIONS = [
    # page-only field `openNow` (kept so nothing from the page is lost)
    "ALTER TABLE resources ADD COLUMN IF NOT EXISTS open_now BOOLEAN NOT NULL DEFAULT TRUE",
    # ties a submitted question to the logged-in user (NULL for guests)
    # keeps the page's original list order (new resources without one sort last)
    "ALTER TABLE resources ADD COLUMN IF NOT EXISTS display_order INTEGER",
    "ALTER TABLE questions ADD COLUMN IF NOT EXISTS user_id VARCHAR "
    "REFERENCES users(id) ON DELETE SET NULL",
]


def _day(token):
    t = token.strip().lower()[:3]
    if t not in DAYS:
        raise ValueError(f"unknown day {token!r}")
    return DAYS.index(t)


def _days_from_label(label):
    """'Mon - Sat' / 'Tue & Thu' / 'Mon, Wed, Fri' / 'Sunday' -> [day indexes]"""
    out = []
    for part in re.split(r"[,&]", label):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out.extend(range(_day(a), _day(b) + 1))
        else:
            out.append(_day(part))
    return out


def _time24(t):
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*(AM|PM)\s*", t, re.I)
    if not m:
        raise ValueError(f"bad time {t!r}")
    h, mi, ap = int(m.group(1)), m.group(2), m.group(3).upper()
    if ap == "PM" and h != 12:
        h += 12
    if ap == "AM" and h == 12:
        h = 0
    return f"{h:02d}:{mi}"


def _value(v):
    v = v.strip()
    if v.lower() == "closed":
        return "closed"
    if "24 hours" in v.lower():
        return "00:00-24:00"
    a, b = v.split(" - ")
    return f"{_time24(a)}-{_time24(b)}"


def page_hours_to_db(hours):
    result = {d: "closed" for d in DAYS}
    for label, val in hours.items():
        for i in _days_from_label(label):
            result[DAYS[i]] = _value(val)
    return result


UPSERT = """
INSERT INTO resources (id, category, emoji, name, address, neighborhood, phone,
                       longitude, latitude, open_status, requirements, hours,
                       ai_summary, services, open_now, display_order)
VALUES (%(id)s, %(category)s, %(emoji)s, %(name)s, %(address)s, %(neighborhood)s,
        %(phone)s, %(lng)s, %(lat)s, %(open_status)s, %(requirements)s, %(hours)s,
        %(ai_summary)s, %(services)s, %(open_now)s, %(display_order)s)
ON CONFLICT (id) DO UPDATE SET
    -- existing DB values win; the page only fills gaps
    category     = COALESCE(resources.category, EXCLUDED.category),
    emoji        = COALESCE(resources.emoji, EXCLUDED.emoji),
    name         = EXCLUDED.name || resources.name,
    address      = COALESCE(resources.address, EXCLUDED.address),
    neighborhood = COALESCE(resources.neighborhood, EXCLUDED.neighborhood),
    phone        = COALESCE(resources.phone, EXCLUDED.phone),
    longitude    = COALESCE(resources.longitude, EXCLUDED.longitude),
    latitude     = COALESCE(resources.latitude, EXCLUDED.latitude),
    open_status  = EXCLUDED.open_status || COALESCE(resources.open_status, '{}'),
    requirements = EXCLUDED.requirements || COALESCE(resources.requirements, '{}'),
    hours        = COALESCE(resources.hours, EXCLUDED.hours),
    ai_summary   = EXCLUDED.ai_summary || COALESCE(resources.ai_summary, '{}'),
    services     = EXCLUDED.services || COALESCE(resources.services, '{}'),
    display_order = COALESCE(resources.display_order, EXCLUDED.display_order)
RETURNING (xmax = 0) AS inserted
"""


def main():
    page = json.loads((HERE / "page_resources.json").read_text(encoding="utf-8"))
    with psycopg.connect(os.environ["DATABASE_URL"]) as conn:
        for sql in MIGRATIONS:
            conn.execute(sql)

        inserted = merged = 0
        for order, r in enumerate(page, start=1):
            row = conn.execute(UPSERT, {
                "id": r["id"], "category": r["category"], "emoji": r.get("emoji"),
                "name": Jsonb(r["name"]), "address": r.get("address"),
                "neighborhood": r.get("neighborhood"), "phone": r.get("phone"),
                "lng": r.get("lng"), "lat": r.get("lat"),
                "open_status": Jsonb(r.get("openStatus", {})),
                "requirements": Jsonb(r.get("requirements", {})),
                "hours": Jsonb(page_hours_to_db(r.get("hours", {}))),
                "ai_summary": Jsonb(r.get("aiSummary", {})),
                "services": Jsonb(r.get("services", {})),
                "open_now": r.get("openNow", True),
                "display_order": order,
            }).fetchone()
            if row[0]:
                inserted += 1
            else:
                merged += 1

        fixed = 0
        for uid, h in conn.execute("SELECT id, password_hash FROM users").fetchall():
            try:
                ok = bcrypt.checkpw(DEMO_PASSWORD.encode(), h.encode())
            except ValueError:
                ok = False
            # only replace the fake placeholder hashes of the three seed users
            if not ok and uid in ("usr_admin_01", "usr_counselor_02", "usr_client_88"):
                new = bcrypt.hashpw(DEMO_PASSWORD.encode(), bcrypt.gensalt(12)).decode()
                conn.execute("UPDATE users SET password_hash=%s WHERE id=%s", (new, uid))
                fixed += 1

        total = conn.execute("SELECT count(*) FROM resources").fetchone()[0]
        print(f"resources: {inserted} inserted, {merged} merged, {total} total; "
              f"demo password set for {fixed} user(s)")


if __name__ == "__main__":
    main()
