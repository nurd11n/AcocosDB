"""Gunicorn config for the web container.

workers = 2*CPU+1 (the standard sync-worker rule of thumb), computed at boot so
the same image is right on a 1-core VPS or an 8-core box. --max-requests recycles
a worker periodically to cap any slow memory creep; the jitter staggers recycles
so they don't all happen at once. --timeout 60 kills a request wedged longer than
that (a report build, a stuck upstream) rather than tying up a worker forever.
"""

import multiprocessing

bind = "0.0.0.0:8000"
workers = multiprocessing.cpu_count() * 2 + 1
timeout = 60
max_requests = 1000
max_requests_jitter = 100
# Access logs to stdout so Docker's json-file driver (rotated) captures them.
accesslog = "-"
errorlog = "-"
