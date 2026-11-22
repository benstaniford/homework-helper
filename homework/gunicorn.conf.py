# Gunicorn configuration file for production

# Server socket
bind = "0.0.0.0:5000"
backlog = 2048

# One worker: it keeps the warm Canvas sessions and the helper's chats in memory,
# and it alone serves the helper's tools on the broker's tools.sock. Threads handle
# concurrency (helper answers stream for up to a couple of minutes).
# Loading a report makes several Canvas calls, so allow a generous timeout.
workers = 1
worker_class = "gthread"
threads = 8
timeout = 120
keepalive = 2

# No max_requests: recycling the single worker would drop every open helper chat.

# Logging
accesslog = "-"
errorlog = "-"
loglevel = "info"
access_log_format = '%(h)s %(l)s %(u)s %(t)s "%(r)s" %(s)s %(b)s "%(f)s" "%(a)s" %(D)s'

# Process naming
proc_name = "homework"

# Security
limit_request_line = 4094
limit_request_fields = 100
limit_request_field_size = 8190

# Performance
preload_app = True


def post_fork(server, worker):
    # Threads don't survive fork, so the helper's tool server starts in the worker.
    import app
    app.tutor.start()
