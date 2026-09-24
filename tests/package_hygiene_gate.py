"""Exercise a real DebBuilder package in an isolated offline systemd container."""
from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path


def _run(arguments: list[str], *, timeout: int = 120) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeError(f"{arguments[:3]} failed ({result.returncode}): {result.stderr[-1500:]}")
    return result.stdout.strip()


def prove(package: Path, image: str, *, legacy_environment: bool = False) -> dict:
    package = package.resolve(strict=True)
    name = f"debbuilder-hygiene-{uuid.uuid4().hex[:12]}"
    try:
        _run([
            "podman", "create", "--pull", "never", "--cgroups", "split",
            "--name", name, "--network", "none", "--systemd", "always",
            "--volume", f"{package}:/debbuilder-input/current.deb:ro",
            image, "/sbin/init",
        ])
        _run(["podman", "start", name])
        if legacy_environment:
            template = (Path(__file__).resolve().parents[1] / "packaging/debbuilder.env").read_text()
            preserved = template.replace("PYTHONDONTWRITEBYTECODE=1", "PYTHONDONTWRITEBYTECODE=0")
            if preserved == template:
                raise RuntimeError("Packaged environment template lacks the expected bytecode setting")
            _run(["podman", "exec", name, "install", "-d", "/etc/debbuilder"])
            created = subprocess.run(
                ["podman", "exec", "--interactive", name, "sh", "-c", "cat > /etc/debbuilder/debbuilder.env"],
                input=preserved, capture_output=True, text=True, timeout=10,
            )
            created.check_returncode()
        _run(["podman", "exec", name, "dpkg", "--install", "/debbuilder-input/current.deb"], timeout=300)
        _run(["podman", "exec", name, "systemctl", "is-active", "--quiet", "debbuilder.service"])
        for _ in range(20):
            probe = subprocess.run(
                ["podman", "exec", name, "curl", "--max-time", "2", "--fail", "--silent", "--output", "/dev/null", "http://127.0.0.1:8099/"],
                capture_output=True, text=True, timeout=10,
            )
            if probe.returncode == 0:
                break
            time.sleep(1)
        else:
            status = subprocess.run(["podman", "exec", name, "systemctl", "status", "--no-pager", "debbuilder.service"], capture_output=True, text=True, timeout=10)
            raise RuntimeError(f"Service listener did not respond: {status.stdout[-1500:]} {status.stderr[-500:]}")
        _run(["podman", "exec", name, "dpkg", "--install", "/debbuilder-input/current.deb"], timeout=300)
        _run(["podman", "exec", name, "systemctl", "is-active", "--quiet", "debbuilder.service"])
        _run(["podman", "exec", name, "systemctl", "stop", "debbuilder.service"])
        _run(["podman", "exec", name, "dpkg", "--remove", "debbuilder"], timeout=300)
        _run(["podman", "exec", name, "dpkg", "--purge", "debbuilder"], timeout=300)
        residue = _run([
            "podman", "exec", name, "sh", "-c",
            "find /opt/debbuilder -name '__pycache__' -o -name '*.pyc' 2>/dev/null || true",
        ])
        if residue:
            raise RuntimeError(f"Unowned application bytecode remains after purge: {residue}")
        _run(["podman", "exec", name, "test", "!", "-e", "/opt/debbuilder"])
        _run(["podman", "exec", name, "test", "-d", "/var/lib/debbuilder/repository"])
        return {"install": "pass", "service_start": "pass", "service_use": "pass", "reinstall": "pass", "service_stop": "pass", "remove": "pass", "purge": "pass", "legacy_environment": legacy_environment, "bytecode_residue": []}
    finally:
        subprocess.run(["podman", "rm", "--force", name], capture_output=True, text=True, timeout=30)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, required=True)
    parser.add_argument("--image", required=True)
    parser.add_argument("--legacy-environment", action="store_true")
    args = parser.parse_args()
    print(json.dumps(prove(args.package, args.image, legacy_environment=args.legacy_environment), sort_keys=True))
