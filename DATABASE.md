# Open Bx database backend

`index.html` is served by a small Flask app (`server.py`) that reads and writes a
PostgreSQL database. The page looks and works the same as before; the
resources, saved centers, questions, and search log now live in the database.
JS changes in `index.html` are marked with `[DB]` comments.

## Tables (see `schema.sql`)

| Table | What it holds |
|---|---|
| `resources` | Bronx resource centers. Text fields (`name`, `open_status`, `ai_summary`, `services`) are JSON keyed by language code (`en`, `es`, `vi`, `zh`, `ar`). `hours` is per day: `{"mon": "09:00-15:30", ..., "sun": "closed"}`. |
| `users` | Accounts (`admin`, `counselor`, `user`). `saved_resources` is a list of resource ids. Passwords are bcrypt hashes. |
| `questions` | "Ask Question" submissions. `status` is `pending` / `in_progress` / `answered`; `user_id` is set if the asker was logged in. |
| `search_logs` | Queries typed into the AI search box (query, matched category, result count). No location is stored. |

## API

| Method & path | Who | Does |
|---|---|---|
| `GET /` | anyone | the page |
| `GET /api/resources`, `GET /api/resources/<id>` | anyone | resources in the shape the page uses (`lat`, `lng`, `openStatus`, `aiSummary`, display-ready `hours`) |
| `POST /api/signup` `{email, password, full_name}` | anyone | create a `user` account and log in |
| `POST /api/login` `{email, password}` / `POST /api/logout` / `GET /api/me` | anyone | session login |
| `GET /api/saved`, `POST /api/saved/<id>`, `DELETE /api/saved/<id>` | logged in | the user's saved centers |
| `POST /api/questions` `{lang, question, phone}` | anyone | submit a question |
| `GET /api/questions`, `PATCH /api/questions/<id>` `{status, admin_notes, assigned_to}` | admin / counselor | question dashboard |
| `POST /api/search-logs` `{query, category_matched, results_count}` | anyone | log a search |
| `GET /api/search-logs` | admin / counselor | latest 200 searches |

## Demo logins (password `demo1234` for all)

- `admin@openbx.org`: Rosa Martinez, admin
- `counseling@openbx.org`: David Chen, counselor
- `community_user@gmail.com`: Maria Santos, user

"🔒 Admin Access" now asks for a staff email and password (admin or counselor).
"👤 Log In" lets anyone log in or sign up so saved centers stick to their account.
Guests can still save centers; those stay in the browser tab only.

## Run locally

```bash
pip install -r requirements.txt
export DATABASE_URL='postgresql://...'      # never commit this
export SECRET_KEY='any-long-random-string'  # optional locally
python3 -m flask --app server run           # http://127.0.0.1:5000
```

Production (Render): start command `gunicorn server:app`, with `DATABASE_URL` and `SECRET_KEY` set as environment variables.

## Seeding

`python3 seed_from_page.py` (with `DATABASE_URL` set) loads `page_resources.json`
(the resource list that used to be hard-coded in `index.html`) into `resources`,
adds the small schema additions, and sets the demo passwords. It is safe to run
again: existing rows win, and the page data only fills in missing languages or
empty fields.
