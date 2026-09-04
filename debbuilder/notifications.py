"""Notification integration for DebBuilder.

Runtime notifications are part of the lifecycle architecture. They are
best-effort side effects: delivery failures must never break builds,
validation, publication, or automatic pipeline execution.
"""
from __future__ import annotations

import json
import re
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Callable

from .settings_store import ntfy_token, ntfy_token_configured, save_ntfy_token

SECRET_REDACTIONS = [
    re.compile(r"(?i)(authorization:\s*bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)(bearer\s+)[^\s,;]+"),
    re.compile(r"(?i)((?:token|secret|password|passwd|apikey|api_key|client_secret)\s*[=:]\s*)[^\s,;]+"),
]


def _ntfy_url(server_url: str, topic: str) -> str:
    return f"{server_url.rstrip('/')}/{urllib.parse.quote(topic, safe='')}"


def redact(value):
    if isinstance(value, dict):
        return {key: redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    if not isinstance(value, str):
        return value
    redacted = value
    for pattern in SECRET_REDACTIONS:
        redacted = pattern.sub(r"\1[redacted]", redacted)
    parsed = urllib.parse.urlsplit(redacted)
    if parsed.scheme in {"http", "https"}:
        query = []
        changed = False
        for key, item in urllib.parse.parse_qsl(parsed.query, keep_blank_values=True):
            if re.search(r"(?i)(token|secret|password|passwd|apikey|api_key|client_secret)", key + item):
                query.append((key, "[redacted]"))
                changed = True
            else:
                query.append((key, item))
        if changed:
            redacted = urllib.parse.urlunsplit(parsed._replace(query=urllib.parse.urlencode(query)))
    return redacted


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
            status = getattr(response, "status", 200)
        return {"ok": True, "status": status, "response": redact(body[:1000])}
    except Exception as exc:
        return {"ok": False, "error": redact(str(exc))}


def _state_path(data_dir: Path) -> Path:
    return data_dir / "notification-state.json"


def _load_state(data_dir: Path) -> dict:
    try:
        data = json.loads(_state_path(data_dir).read_text())
    except Exception:
        data = {}
    if not isinstance(data, dict):
        data = {}
    data.setdefault("sent", {})
    data.setdefault("recipes", {})
    return data


def _save_state(data_dir: Path, state: dict) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    _state_path(data_dir).write_text(json.dumps(state, indent=2, sort_keys=True) + "\n")


def _run_id(run: dict | None, fallback: str = "") -> str:
    return str((run or {}).get("id") or fallback or "")


def _version(run: dict | None, payload: dict | None = None) -> str:
    payload = payload or {}
    for source in (payload.get("version"), payload.get("published_version")):
        if source:
            return str(source)
    artifact = ((run or {}).get("artifact") or {}).get("inspection") or {}
    if artifact.get("version"):
        return str(artifact["version"])
    version = (run or {}).get("version") or {}
    if isinstance(version, dict):
        return str(version.get("debian") or version.get("upstream") or "")
    return str(version or "")


def _package(app_module, run: dict | None = None, recipe: dict | None = None, payload: dict | None = None) -> str:
    payload = payload or {}
    for value in (payload.get("package"),):
        if value:
            return str(value)
    artifact = ((run or {}).get("artifact") or {}).get("inspection") or {}
    if artifact.get("package"):
        return str(artifact["package"])
    if recipe:
        try:
            value = app_module.recipe_package_name(recipe)
            if value:
                return str(value)
        except Exception:
            pass
    if run:
        try:
            value = app_module.build_run_package(run)
            if value:
                return str(value)
        except Exception:
            pass
    return str((run or {}).get("recipe_id") or "package")


def _run_url(settings: dict, run_id: str) -> str:
    base = str(((settings.get("general") or {}).get("url")) or "").strip().rstrip("/")
    if not base or not run_id:
        return ""
    return f"{base}/?view=logs&run={urllib.parse.quote(run_id, safe='')}"


def _error_message(payload: dict | None = None, run: dict | None = None) -> str:
    error = (payload or {}).get("error") or (run or {}).get("error") or {}
    if isinstance(error, dict):
        return str(error.get("message") or error.get("code") or "unknown error")
    return str(error or "unknown error")


class NotificationService:
    def __init__(self, app_module, sender: Callable[..., dict] | None = None):
        self.app = app_module
        self.sender = sender or (lambda title, message, **kwargs: send_ntfy(
            self.app.DATA,
            self.app.app_settings(),
            title,
            message,
            **kwargs,
        ))

    def _settings(self) -> dict:
        try:
            return self.app.app_settings()
        except Exception:
            return {}

    def _emit(self, title: str, message: str, *, priority: str = "default", tags: str = "package", key: str) -> dict:
        state = _load_state(self.app.DATA)
        if key and key in state["sent"]:
            return {"ok": False, "skipped": True, "reason": "duplicate"}
        if key:
            state["sent"][key] = {"at": time.time(), "title": title}
            _save_state(self.app.DATA, state)
        try:
            result = self.sender(title, redact(message), priority=priority, tags=tags)
        except Exception as exc:
            result = {"ok": False, "error": redact(str(exc))}
        return redact(result)

    def _recipe_key(self, package: str, run: dict | None = None) -> str:
        recipe_id = str((run or {}).get("recipe_id") or "")
        return f"{package}|{recipe_id}" if recipe_id else package

    def _set_failure(self, recipe_key: str, failure: dict) -> None:
        state = _load_state(self.app.DATA)
        state["recipes"].setdefault(recipe_key, {})["active_failure"] = failure
        _save_state(self.app.DATA, state)

    def _recovery_failure(self, recipe_key: str, stage: str, recovered_run_id: str) -> dict | None:
        state = _load_state(self.app.DATA)
        row = state["recipes"].get(recipe_key) or {}
        failure = row.get("active_failure")
        if not failure or failure.get("stage") != stage:
            return None
        row.pop("active_failure", None)
        row["last_recovered_at"] = time.time()
        row["last_recovered_run_id"] = recovered_run_id
        state["recipes"][recipe_key] = row
        _save_state(self.app.DATA, state)
        return failure

    def notify_failure(self, stage: str, *, run: dict | None = None, recipe: dict | None = None, payload: dict | None = None, run_id: str = "") -> dict:
        payload = payload or {}
        rid = _run_id(run, run_id or str(payload.get("build_run_id") or ""))
        package = _package(self.app, run=run, recipe=recipe, payload=payload)
        version = _version(run, payload) or "unknown"
        message = _error_message(payload, run)
        settings = self._settings()
        url = _run_url(settings, rid)
        title = f"DebBuilder attention: {package}"
        lines = [
            f"Recipe/package: {package}",
            f"Version: {version}",
            f"Failed stage: {stage}",
            f"Run: {rid or 'unknown'}",
            f"Error: {message}",
        ]
        if url:
            lines.append(f"Open run: {url}")
        recipe_key = self._recipe_key(package, run)
        self._set_failure(recipe_key, {"stage": stage, "run_id": rid, "version": version, "message": message, "package": package})
        return self._emit(title, "\n".join(lines), priority="high", tags="x,package", key=f"failure:{recipe_key}:{stage}:{rid}")

    def notify_recovery(self, stage: str, *, run: dict | None = None, recipe: dict | None = None, payload: dict | None = None, run_id: str = "") -> dict:
        payload = payload or {}
        rid = _run_id(run, run_id or str(payload.get("build_run_id") or ""))
        package = _package(self.app, run=run, recipe=recipe, payload=payload)
        recipe_key = self._recipe_key(package, run)
        failure = self._recovery_failure(recipe_key, stage, rid)
        if not failure:
            return {"ok": False, "skipped": True, "reason": "no active failure"}
        version = _version(run, payload) or failure.get("version") or "unknown"
        settings = self._settings()
        url = _run_url(settings, rid)
        lines = [
            f"Recipe/package: {package}",
            f"Version: {version}",
            f"Recovered stage: {stage}",
            f"Run: {rid or 'unknown'}",
            f"Previous failure: {failure.get('message') or 'unknown error'}",
        ]
        if url:
            lines.append(f"Open run: {url}")
        return self._emit(
            f"DebBuilder recovered: {package}",
            "\n".join(lines),
            tags="white_check_mark,package",
            key=f"recovered:{recipe_key}:{stage}:{rid}",
        )

    def notify_build_lifecycle(self, event: str, *, run: dict, recipe: dict | None = None) -> dict:
        if event == "build_failed":
            return self.notify_failure("build", run=run, recipe=recipe)
        if event == "build_succeeded":
            return self.notify_recovery("build", run=run, recipe=recipe)
        return {"ok": False, "skipped": True, "reason": "build start notifications suppressed"}

    def notify_validation_result(self, result: dict) -> dict:
        stage = "validation"
        run = self._load_run(str(result.get("build_run_id") or ""))
        if result.get("status") == "failed":
            return self.notify_failure(stage, run=run, payload=result)
        if result.get("status") == "success":
            return self.notify_recovery(stage, run=run, payload=result)
        return {"ok": False, "skipped": True, "reason": "validation not terminal"}

    def notify_publication_result(self, result: dict) -> dict:
        stage = "publication"
        run = self._load_run(str(result.get("build_run_id") or ""))
        if result.get("status") == "failed":
            return self.notify_failure(stage, run=run, payload=result)
        if result.get("status") == "success":
            return self.notify_recovery(stage, run=run, payload=result)
        return {"ok": False, "skipped": True, "reason": "publication not terminal"}

    def notify_automatic_completion(self, result: dict) -> dict:
        automation = result.get("automation") or {}
        publication = automation.get("publication") or result.get("publication") or {}
        if publication.get("status") != "success":
            return {"ok": False, "skipped": True, "reason": "automatic publication not completed"}
        rid = str(result.get("run_id") or publication.get("build_run_id") or "")
        run = self._load_run(rid)
        package = _package(self.app, run=run, payload=publication)
        version = _version(run, publication) or "unknown"
        state = _load_state(self.app.DATA)
        recipe_key = self._recipe_key(package, run)
        if (state["recipes"].get(recipe_key) or {}).get("last_recovered_run_id") == rid:
            return {"ok": False, "skipped": True, "reason": "recovery already reported"}
        url = _run_url(self._settings(), rid)
        lines = [
            f"Recipe/package: {package}",
            f"Version: {version}",
            f"Run: {rid or 'unknown'}",
            "Automatic update completed successfully.",
        ]
        if url:
            lines.append(f"Open run: {url}")
        return self._emit(
            f"DebBuilder automatic update complete: {package}",
            "\n".join(lines),
            tags="white_check_mark,package",
            key=f"automatic-complete:{package}:{rid}",
        )

    def _load_run(self, run_id: str) -> dict:
        if not run_id:
            return {}
        try:
            return self.app.BuildStore(self.app.DATA / "builds").load(run_id) or {}
        except Exception:
            return {}


def install(app_module) -> None:
    """Attach ntfy settings, test endpoint, and lifecycle notifications."""
    original_settings_view = app_module.settings_view
    original_update_settings = app_module.update_settings
    original_record_execution = app_module.record_execution
    original_lifecycle = app_module.package_lifecycle_operation
    original_run_with_automation = app_module.run_recipe_pipeline_with_automation
    original_validate_build_artifact = app_module.validate_build_artifact
    original_publish_build_artifact = app_module.publish_build_artifact
    original_do_post = app_module.Handler.do_POST
    service = NotificationService(app_module)

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

    def record_execution_with_ntfy(run_id, workflow, returncode, started, ended, **kwargs):
        original_record_execution(run_id, workflow, returncode, started, ended, **kwargs)
        if kwargs.get("notification", True) is False or returncode == 0:
            return
        package = app_module.recipe_package_name(workflow) or workflow.get("name") or "package"
        service.notify_failure("build", run={"id": run_id, "recipe_id": workflow.get("name") or package, "version": kwargs.get("version") or ""}, recipe=workflow)

    def lifecycle_with_ntfy(name: str, action: str, payload: dict) -> dict:
        try:
            result = original_lifecycle(name, action, payload)
        except Exception as exc:
            if action == "publish" and not bool(payload.get("dry_run", True)):
                service.notify_failure("publication", run={"id": name, "recipe_id": name}, payload={"package": name, "error": {"message": str(exc)}})
            raise
        if action == "publish":
            publication = result.get("publication") or {}
            if publication.get("status") in {"failed", "error"}:
                service.notify_failure("publication", run={"id": name, "recipe_id": name}, payload={"package": name, "error": publication.get("error") or result.get("error") or {}})
            elif publication.get("status") in {"published", "success"}:
                service.notify_recovery("publication", run={"id": name, "recipe_id": name}, payload={"package": name, "published_version": publication.get("version") or publication.get("published_version") or ""})
        return result

    def validate_build_artifact_with_ntfy(run_id: str, payload: dict | None = None) -> dict:
        result = original_validate_build_artifact(run_id, payload)
        service.notify_validation_result(result)
        return result

    def publish_build_artifact_with_ntfy(run_id: str, payload: dict | None = None) -> dict:
        result = original_publish_build_artifact(run_id, payload)
        service.notify_publication_result(result)
        return result

    def run_recipe_pipeline_with_ntfy(workflow: dict, *, dry_run: bool = True) -> dict:
        result = original_run_with_automation(workflow, dry_run=dry_run)
        if not dry_run:
            service.notify_automatic_completion(result)
        return result

    def do_post_with_ntfy(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path == "/api/notifications/test":
            if not self._authorized():
                return
            result = service._emit("DebBuilder", "Test ntfy notification sent from DebBuilder.", tags="test_tube,package", key=f"test:{time.time()}")
            app_module.json_response(self, {"ok": bool(result.get("ok")), "notification": result}, 200 if result.get("ok") else 502)
            return
        return original_do_post(self)

    app_module.settings_view = settings_view_with_ntfy
    app_module.update_settings = update_settings_with_ntfy
    app_module.record_execution = record_execution_with_ntfy
    app_module.package_lifecycle_operation = lifecycle_with_ntfy
    app_module.validate_build_artifact = validate_build_artifact_with_ntfy
    app_module.publish_build_artifact = publish_build_artifact_with_ntfy
    app_module.run_recipe_pipeline_with_automation = run_recipe_pipeline_with_ntfy
    app_module.Handler.do_POST = do_post_with_ntfy
    app_module.LIFECYCLE_NOTIFIER = service.notify_build_lifecycle
    def pipeline_notifier(*, status, package, version, detail=""):
        if status == "success":
            return {"ok": False, "skipped": True, "reason": "legacy success notifications suppressed"}
        return service.notify_failure(
            "pipeline",
            run={"id": package, "recipe_id": package, "version": version},
            payload={"package": package, "error": {"message": detail or status}},
        )

    app_module.PIPELINE_NOTIFIER = pipeline_notifier
