"""Gunicorn settings for the web container.

Gunicorn's defaults are one process with one synchronous worker, which means
the whole site answered one request at a time: a reviewer loading the queue,
a builder uploading a screenshot and a Slack call sitting on its five-second
timeout all waited in the same line, however many cores the box had.

So this runs several processes, each with a pool of threads (gthread). The
processes are what use the cores; the threads are what keep a process busy
while a request is parked on Postgres, R2, Slack or HCA, which is where most
of a request here actually goes. Django is thread-safe for this, and every
thread gets its own database connection.

Every knob reads from the environment so a box can be tuned without a deploy:

    WEB_CONCURRENCY   worker processes  (default: 2 x cores + 1, at most 8)
    GUNICORN_THREADS  threads per worker (default: 4)

Keep workers x threads, plus the cron container and a few to spare, under
Postgres's max_connections (100 by default): with CONN_MAX_AGE set, each of
those threads holds its connection open between requests.
"""

import multiprocessing
import os


def _env_int(name, default):
    try:
        return int(os.environ[name])
    except (KeyError, ValueError):
        return default


bind = os.environ.get("GUNICORN_BIND", "0.0.0.0:8000")

workers = _env_int("WEB_CONCURRENCY", min(multiprocessing.cpu_count() * 2 + 1, 8))
worker_class = "gthread"
threads = _env_int("GUNICORN_THREADS", 4)

# Under gthread this is how long a worker can go without checking in, not how
# long one request may take, so a slow upload on one thread doesn't get the
# whole process killed.
timeout = _env_int("GUNICORN_TIMEOUT", 60)
graceful_timeout = 30
# Holds the connection from the proxy in front of us open between requests
# instead of a new TCP handshake for every page and asset.
keepalive = 5

# Recycle each worker every thousand-odd requests so a slow leak can't build
# up for weeks. The jitter keeps them from all restarting at the same moment.
max_requests = 1000
max_requests_jitter = 100

# Off unless asked for: one line per request, assets included, is a lot of
# noise in `docker compose logs` for a site this size.
accesslog = "-" if os.environ.get("GUNICORN_ACCESS_LOG") == "True" else None
errorlog = "-"
