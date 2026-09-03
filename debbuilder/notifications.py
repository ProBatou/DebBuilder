"""Notification integration for DebBuilder.

ntfy is deliberately implemented as a best-effort side channel: notification
failures must never break builds or repository publication.
"""
from __future__ import annotations

import json
import urllib.parse
import urllib.request
from pathlib import Path

from .settings_store import ntfy_token, ntfy_token_configured, save_ntfy_token


def _ntfy_url(server_url: str, topic: str) -> str:
    return f"{server_url.rstrip('/')}/{urllib.parse.quote(topic, safe='')}"


def send_ntfy(data_dir: Path, settings: dict, title: str, message: str, *, priority: str = "default", tags: str = "package") -> dict:
    notifications = settings.get("notifications") or {}
    if notifications.get("type") != "ntfy":
        return {"ok": False, "skipped": True, "reason": "ntfy disabled"}
    server_url = str(notifications.get("server_url") or "").strip()
    topic = str(notifications.get("topic") or "").strip()
    if not server_url or not topic:
        return {"ok": False, "skipped": True, "reason": "ntfy not configured"}
    headers = {
        "Title": title,
        "Priority": priority,
        "Tags": tags,
        "Content-Type": "text/plain; charset=utf-8",
        "User-Agent": "debbuilder/ntfy",
    }
    token = ntfy_token(data_dir)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        _ntfy_url(server_url, topic),
        data=message.encode("utf-8"),
        method="POST",
        headers=headers,
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read().decode("utf-8", "replace")
        return {"ok": True, "status": getattr(response, "status", 200), "response": body[:1000]}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


def install(app_module) -> None:
    """Attach ntfy settings, test endpoint, and lifecycle notifications."""
    original_settings_view = app_module.settings_view
    original_update_settings = app_module.update_settings
    original_record_execution = app_module.record_execution
    original_lifecycle = app_module.package_lifecycle_operation
    original_do_post = app_module.Handler.do_POST

    def settings_view_with_ntfy() -> dict:
        view = original_settings_view()
        notifications = dict(view.get("notifications") or {})
        notifications["token"] = "masked"
        notifications["token_configured"] = ntfy_token_configured(app_module.DATA)
        view["notifications"] = notifications
        return view

    def update_settings_with_ntfy(payload: dict) -> dict:
        notifications = payload.get("notifications") if isinstance(payload, dict) else None
        if isinstance(notifications, dict) and notifications.get("token"):
            save_ntfy_token(app_module.DATA, str(notifications["token"]))
        original_update_settings(payload)
        return settings_view_with_ntfy()

    def notify(title: str, message: str, *, priority: str = "default", tags: str = "package") -> dict:
        return send_ntfy(app_module.DATA, app_module.app_settings(), title, message, priority=priority, tags=tags)

    def record_execution_with_ntfy(run_id, workflow, returncode, started, ended, **kwargs):
        original_record_execution(run_id, workflow, returncode, started, ended, **kwargs)
        if kwargs.get("notification", True) is False:
            return
        package = app_module.recipe_package_name(workflow) or workflow.get("name") or "package"
        if returncode == 0:
            notify("Build succeeded", f"{package} : build {run_id} finished successfully.", tags="white_check_mark,package")
        else:
            notify("Build failed", f"{package} : build {run_id} failed (code {returncode}).", priority="high", tags="x,package")

    def lifecycle_with_ntfy(name: str, action: str, payload: dict) -> dict:
        try:
            result = original_lifecycle(name, action, payload)
        except Exception as exc:
            if action == "publish" and not bool(payload.get("dry_run", True)):
                notify("Publication failed", f"{name}: {exc}", priority="high", tags="x,package")
            raise
        if action == "check-updates" and result.get("state") == "update_available":
            notify(
                "Update available",
                f"{name}: {result.get('published_version') or 'absent'} → {result.get('source_version') or 'unknown'}.",
                tags="arrow_up,package",
            )
        if action == "publish":
            publication = result.get("publication") or {}
            if publication.get("status") == "published":
                notify("Publication succeeded", f"{name} was published to the APT repository.", tags="white_check_mark,package")
        return result

    def do_post_with_ntfy(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/notifications/test":
            if not self._authorized():
                return
            result = notify("DebBuilder", "Test ntfy notification sent from DebBuilder.", tags="test_tube,package")
            app_module.json_response(self, {"ok": bool(result.get("ok")), "notification": result}, 200 if result.get("ok") else 502)
            return
        return original_do_post(self)

    app_module.settings_view = settings_view_with_ntfy
    app_module.update_settings = update_settings_with_ntfy
    app_module.record_execution = record_execution_with_ntfy
    app_module.package_lifecycle_operation = lifecycle_with_ntfy
    app_module.Handler.do_POST = do_post_with_ntfy
    app_module.PIPELINE_NOTIFIER = lambda *, status, package, version, detail="": notify(
        "Build published" if status == "success" else "Build failed",
        f"{package} {version} built and published" if status == "success" else f"Build failed {package} {version}: {detail}".strip(),
        priority="default" if status == "success" else "high",
        tags="white_check_mark,package" if status == "success" else "x,package",
    )
