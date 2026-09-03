"""Deterministic systemd unit generation from Recipe v1."""
from __future__ import annotations


def _lines(key: str, values: list[str]) -> list[str]:
    return [f"{key}={_safe(value)}" for value in values if value]


def _safe(value: object) -> str:
    text = str(value)
    if "\n" in text or "\r" in text or "\x00" in text:
        raise ValueError("systemd directive values must be single-line strings")
    return text


def generate_unit(service: dict) -> str:
    if not service.get("enabled"):
        return ""
    unit = ["[Unit]"]
    if service.get("description"):
        unit.append(f"Description={_safe(service['description'])}")
    unit += _lines("After", service.get("after") or [])
    unit += _lines("Wants", service.get("wants") or [])
    unit += _lines("Requires", service.get("requires") or [])
    body = ["", "[Service]"]
    scalar = (
        ("Type", "type"), ("User", "user"), ("Group", "group"),
    )
    for directive, key in scalar:
        if service.get(key):
            body.append(f"{directive}={_safe(service[key])}")
    if service.get("working_directory"):
        body.append(f"WorkingDirectory={_safe(service['working_directory'])}")
    body += _lines("EnvironmentFile", service.get("environment_files") or [])
    for key, value in (service.get("environment") or {}).items():
        escaped = _safe(value).replace("\\", "\\\\").replace('"', '\\"')
        body.append(f'Environment="{_safe(key)}={escaped}"')
    body += _lines("ExecStartPre", service.get("exec_start_pre") or [])
    if service.get("command"):
        body.append(f"ExecStart={_safe(service['command'])}")
    body += _lines("ExecStartPost", service.get("exec_start_post") or [])
    body += _lines("ExecStop", service.get("exec_stop") or [])
    optional = (
        ("Restart", "restart"), ("RestartSec", "restart_sec"),
        ("TimeoutStartSec", "timeout_start_sec"), ("TimeoutStopSec", "timeout_stop_sec"),
        ("KillSignal", "kill_signal"), ("StandardOutput", "standard_output"),
        ("StandardError", "standard_error"),
    )
    for directive, key in optional:
        if service.get(key):
            body.append(f"{directive}={_safe(service[key])}")
    return "\n".join(unit + body + ["", "[Install]", "WantedBy=multi-user.target", ""]) + "\n"
