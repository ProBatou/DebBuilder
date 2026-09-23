"""Serve the seeded browser showcase without background upstream requests."""
from debbuilder import app
from server import Handler


def fixture_scheduler(http_server):
    scheduler = app.create_automation_scheduler(http_server)
    # Browser journeys consume the canonical advisory observations seeded by
    # showcase.py. Scheduler behavior has its own focused backend tests.
    scheduler.run_pass = lambda **_kwargs: None
    return scheduler


if __name__ == "__main__":
    raise SystemExit(app.serve_application(Handler, automation_scheduler_factory=fixture_scheduler))
