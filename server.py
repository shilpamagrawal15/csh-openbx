"""Open Bx backend: serves index.html and a small JSON API over Postgres.

Run locally:  DATABASE_URL=postgres://... python3 -m flask --app server run
On Render:    gunicorn server:app   (env: DATABASE_URL, SECRET_KEY)
"""
import os
import re
import secrets
from contextlib import contextmanager
from functools import wraps
from pathlib import Path
from zoneinfo import ZoneInfo

import bcrypt
import psycopg
from flask import Flask, jsonify, request, send_file, session
from psycopg.rows import dict_row

HERE = Path(__file__).resolve().parent

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
app.json.sort_keys = False  # keep hours rows in day order
NYC = ZoneInfo("America/New_York")

DAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_LABEL = {"mon": "Mon", "tue": "Tue", "wed": "Wed", "thu": "Thu",
             "fri": "Fri", "sat": "Sat", "sun": "Sun"}
# questionLang <select> in the page uses language names; the DB stores codes
LANG_CODES = {"English": "en", "Spanish": "es", "Vietnamese": "vi",
              "Chinese": "zh", "Arabic": "ar"}
LANG_NAMES = {v: k for k, v in LANG_CODES.items()}
STATUS_LABEL = {"pending": "Pending", "in_progress": "In Progress",
                "answered": "✓ Answered"}
STAFF_ROLES = ("admin", "counselor")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


# ---------------------------------------------------------------- database
@contextmanager
def db():
    """One short-lived connection per request; commits on success."""
    with psycopg.connect(os.environ["DATABASE_URL"], row_factory=dict_row) as conn:
        yield conn


def new_id(prefix):
    return f"{prefix}_{secrets.token_hex(5)}"


# ---------------------------------------------------------------- adapters
def _fmt_time(t):
    h, m = map(int, t.split(":"))
    ap = "AM" if h < 12 or h == 24 else "PM"
    h12 = h % 12 or 12
    return f"{h12}:{m:02d} {ap}"


def _fmt_value(v):
    if not v or v.lower() == "closed":
        return "Closed"
    if v == "00:00-24:00":
        return "24 Hours"
    a, b = v.split("-")
    return f"{_fmt_time(a)} - {_fmt_time(b)}"


def format_hours(hours):
    """{"mon": "09:00-15:30", ...} -> {"Mon - Sat": "9:00 AM - 3:30 PM", "Sun": "Closed"}
    (the display shape the page's hours table iterates over)."""
    if not hours:
        return {}
    if not all(d in hours for d in DAYS):  # already free-form: pass through
        return hours
    if all(hours[d] == "00:00-24:00" for d in DAYS):
        return {"Mon - Sun": "24 Hours / 7 Days"}
    groups = {}  # value -> [day indexes], in first-seen order
    for i, d in enumerate(DAYS):
        groups.setdefault(hours[d], []).append(i)
    out = {}
    for value, idxs in groups.items():
        runs, start = [], idxs[0]
        for a, b in zip(idxs, idxs[1:] + [None]):
            if b != a + 1:
                runs.append((start, a))
                start = b
        label = ", ".join(DAY_LABEL[DAYS[s]] if s == e
                          else f"{DAY_LABEL[DAYS[s]]} - {DAY_LABEL[DAYS[e]]}"
                          for s, e in runs)
        out[label] = _fmt_value(value)
    return out


def resource_to_page(r):
    """DB row -> the object shape index.html's JS expects."""
    return {
        "id": r["id"],
        "name": r["name"] or {},
        "category": r["category"],
        "emoji": r["emoji"],
        "lat": float(r["latitude"]) if r["latitude"] is not None else None,
        "lng": float(r["longitude"]) if r["longitude"] is not None else None,
        "address": r["address"],
        "neighborhood": r["neighborhood"],
        "phone": r["phone"],
        "website": r["website"],
        "openStatus": r["open_status"] or {},
        "openNow": r["open_now"],
        "requirements": r["requirements"] or {},
        "hours": format_hours(r["hours"]),
        "aiSummary": r["ai_summary"] or {},
        "services": r["services"] or {},
    }


def question_to_page(q):
    """DB row -> the userQuestionsList entry shape used by the admin table."""
    return {
        "id": q["id"],
        "timestamp": q["created_at"].astimezone(NYC).strftime("%m/%d %I:%M %p"),
        "created_at": q["created_at"].isoformat(),
        "lang": LANG_NAMES.get(q["language"], q["language"]),
        "question": q["question"],
        "phone": q["phone"] or "None Provided",
        "status": STATUS_LABEL.get(q["status"], q["status"]),
        "statusCode": q["status"],
        "assignedTo": q["assigned_to"],
        "adminNotes": q["admin_notes"],
    }


def public_user(u):
    return {"id": u["id"], "email": u["email"], "full_name": u["full_name"],
            "role": u["role"], "language_preference": u["language_preference"],
            "saved_resources": list(u["saved_resources"] or [])}


# ---------------------------------------------------------------- auth helpers
def current_user(conn):
    uid = session.get("user_id")
    if not uid:
        return None
    return conn.execute("SELECT * FROM users WHERE id=%s", (uid,)).fetchone()


def login_required(staff=False):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            with db() as conn:
                user = current_user(conn)
                if not user:
                    return jsonify(error="Login required"), 401
                if staff and user["role"] not in STAFF_ROLES:
                    return jsonify(error="Staff only"), 403
                return fn(conn, user, *a, **kw)
        return wrapper
    return deco


def body():
    return request.get_json(silent=True) or {}


# ---------------------------------------------------------------- page
@app.get("/")
def index():
    return send_file(HERE / "index.html")


# ---------------------------------------------------------------- resources
@app.get("/api/resources")
def list_resources():
    with db() as conn:
        rows = conn.execute("SELECT * FROM resources ORDER BY display_order NULLS LAST, id").fetchall()
    return jsonify([resource_to_page(r) for r in rows])


@app.get("/api/resources/<rid>")
def get_resource(rid):
    with db() as conn:
        r = conn.execute("SELECT * FROM resources WHERE id=%s", (rid,)).fetchone()
    if not r:
        return jsonify(error="Not found"), 404
    return jsonify(resource_to_page(r))


# ---------------------------------------------------------------- auth
@app.post("/api/login")
def login():
    d = body()
    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""
    with db() as conn:
        u = conn.execute("SELECT * FROM users WHERE lower(email)=%s", (email,)).fetchone()
    ok = False
    if u:
        try:
            ok = bcrypt.checkpw(password.encode(), u["password_hash"].encode())
        except ValueError:
            ok = False
    if not ok:
        return jsonify(error="Invalid email or password"), 401
    session.clear()
    session["user_id"] = u["id"]
    return jsonify(user=public_user(u))


@app.post("/api/signup")
def signup():
    d = body()
    email = (d.get("email") or "").strip().lower()
    password = d.get("password") or ""
    name = (d.get("full_name") or d.get("name") or "").strip()[:100]
    lang = d.get("language_preference") or "en"
    if not EMAIL_RE.match(email) or len(email) > 255:
        return jsonify(error="Enter a valid email"), 400
    if len(password) < 8:
        return jsonify(error="Password must be at least 8 characters"), 400
    if not name:
        return jsonify(error="Enter your name"), 400
    if lang not in LANG_NAMES:
        lang = "en"
    pw_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt(12)).decode()
    with db() as conn:
        if conn.execute("SELECT 1 FROM users WHERE lower(email)=%s", (email,)).fetchone():
            return jsonify(error="An account with that email already exists"), 409
        u = conn.execute(
            "INSERT INTO users (id, email, password_hash, role, full_name, language_preference) "
            "VALUES (%s, %s, %s, 'user', %s, %s) RETURNING *",
            (new_id("usr"), email, pw_hash, name, lang)).fetchone()
    session.clear()
    session["user_id"] = u["id"]
    return jsonify(user=public_user(u)), 201


@app.post("/api/logout")
def logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def me():
    with db() as conn:
        u = current_user(conn)
    return jsonify(user=public_user(u) if u else None)


# ---------------------------------------------------------------- saved resources
@app.get("/api/saved")
@login_required()
def get_saved(conn, user):
    return jsonify(saved_resources=list(user["saved_resources"]))


@app.post("/api/saved/<rid>")
@login_required()
def add_saved(conn, user, rid):
    if not conn.execute("SELECT 1 FROM resources WHERE id=%s", (rid,)).fetchone():
        return jsonify(error="Unknown resource"), 404
    row = conn.execute(
        "UPDATE users SET saved_resources = CASE WHEN %s = ANY(saved_resources) "
        "THEN saved_resources ELSE array_append(saved_resources, %s) END "
        "WHERE id=%s RETURNING saved_resources", (rid, rid, user["id"])).fetchone()
    return jsonify(saved_resources=list(row["saved_resources"]))


@app.delete("/api/saved/<rid>")
@login_required()
def remove_saved(conn, user, rid):
    row = conn.execute(
        "UPDATE users SET saved_resources = array_remove(saved_resources, %s) "
        "WHERE id=%s RETURNING saved_resources", (rid, user["id"])).fetchone()
    return jsonify(saved_resources=list(row["saved_resources"]))


# ---------------------------------------------------------------- questions
@app.post("/api/questions")
def submit_question():
    d = body()
    text = (d.get("question") or "").strip()
    if not text:
        return jsonify(error="Question is required"), 400
    lang = d.get("lang") or d.get("language") or "en"
    lang = LANG_CODES.get(lang, lang)
    if lang not in LANG_NAMES:
        lang = "en"
    phone = (d.get("phone") or "").strip()[:30] or None  # column is varchar(30)
    with db() as conn:
        user = current_user(conn)
        q = conn.execute(
            "INSERT INTO questions (id, language, question, phone, user_id) "
            "VALUES (%s, %s, %s, %s, %s) RETURNING *",
            (new_id("q"), lang, text[:4000], phone, user["id"] if user else None)).fetchone()
    return jsonify(question=question_to_page(q)), 201


@app.get("/api/questions")
@login_required(staff=True)
def list_questions(conn, user):
    rows = conn.execute("SELECT * FROM questions ORDER BY created_at").fetchall()
    return jsonify([question_to_page(q) for q in rows])


@app.patch("/api/questions/<qid>")
@login_required(staff=True)
def update_question(conn, user, qid):
    d = body()
    sets, vals = [], []
    if "status" in d:
        if d["status"] not in STATUS_LABEL:
            return jsonify(error="Bad status"), 400
        sets.append("status=%s")
        vals.append(d["status"])
        if d["status"] != "pending" and "assigned_to" not in d:
            sets.append("assigned_to=COALESCE(assigned_to, %s)")
            vals.append(user["id"])
    if "admin_notes" in d:
        sets.append("admin_notes=%s")
        vals.append(str(d["admin_notes"]))
    if "assigned_to" in d:
        sets.append("assigned_to=%s")
        vals.append(d["assigned_to"])
    if not sets:
        return jsonify(error="Nothing to update"), 400
    q = conn.execute(f"UPDATE questions SET {', '.join(sets)} WHERE id=%s RETURNING *",
                     (*vals, qid)).fetchone()
    if not q:
        return jsonify(error="Not found"), 404
    return jsonify(question=question_to_page(q))


# ---------------------------------------------------------------- search logs
@app.post("/api/search-logs")
def log_search():
    d = body()
    query = (d.get("query") or "").strip()[:500]
    if not query:
        return jsonify(error="query required"), 400
    try:
        count = int(d.get("results_count") or 0)
    except (TypeError, ValueError):
        count = 0
    cat = (str(d.get("category_matched") or "")[:30]) or None
    with db() as conn:
        row = conn.execute(
            "INSERT INTO search_logs (id, query, category_matched, results_count) "
            "VALUES (%s, %s, %s, %s) RETURNING id", (new_id("log"), query, cat, count)).fetchone()
    return jsonify(id=row["id"]), 201


@app.get("/api/search-logs")
@login_required(staff=True)
def list_search_logs(conn, user):
    rows = conn.execute(
        "SELECT id, searched_at, query, category_matched, results_count "
        "FROM search_logs ORDER BY searched_at DESC LIMIT 200").fetchall()
    for r in rows:
        r["searched_at"] = r["searched_at"].isoformat()
    return jsonify(rows)


if __name__ == "__main__":
    app.run(debug=True)
