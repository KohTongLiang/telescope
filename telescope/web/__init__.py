"""Local web UI over the telescope database.

Read-only by construction: the viewer opens the database with
``PRAGMA query_only`` and never runs a migration. It is safe to leave running
while ``telescope sync`` and ``telescope digest`` write to it.

FastAPI + Jinja2 + htmx, with htmx vendored rather than pulled from a CDN so
the app works offline.
"""
