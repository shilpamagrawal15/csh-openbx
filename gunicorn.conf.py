# Picked up automatically by `gunicorn server:app` (Render start command).
# preload_app imports server.py once in the master, so all workers share the same
# fallback SECRET_KEY if the env var is unset (otherwise logins would randomly fail).
preload_app = True
