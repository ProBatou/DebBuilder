"""Build an APT status model from APT's own resolved package decisions.

This helper runs inside the preparation container.  It does not solve
dependencies: it materializes the exact package/version/architecture choices
already emitted by ``apt-get --simulate`` so APT can solve the next phase
against the modeled post-previous state without unpacking a package.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path


DROP_FIELDS = frozenset({
    "Filename", "Size", "MD5sum", "SHA1", "SHA256", "SHA512", "Description-md5",
})


def parse_stanzas(text: str) -> list[dict[str, str]]:
    rows: list[dict[str, str]] = []
    current: dict[str, str] = {}
    last = ""
    for line in text.splitlines() + [""]:
        if not line:
            if current:
                rows.append(current)
                current, last = {}, ""
            continue
        if line[0].isspace() and last:
            current[last] += "\n" + line
            continue
        if ":" not in line:
            raise ValueError("Debian control stanza contains a malformed field")
        key, value = line.split(":", 1)
        if key in current:
            raise ValueError(f"Debian control stanza repeats {key}")
        current[key], last = value.lstrip(), key
    return rows


def render_stanzas(rows: list[dict[str, str]]) -> str:
    blocks = []
    for row in rows:
        fields = [f"{key}: {value}" for key, value in row.items()]
        blocks.append("\n".join(fields))
    return "\n\n".join(blocks) + "\n"


def package_stanza(package: str, version: str) -> dict[str, str]:
    result = subprocess.run(
        ["apt-cache", "show", "--no-all-versions", f"{package}={version}"],
        check=True, capture_output=True, text=True,
    )
    matches = [row for row in parse_stanzas(result.stdout) if row.get("Package") == package.split(":", 1)[0] and row.get("Version") == version]
    if len(matches) != 1:
        raise ValueError(f"APT metadata for {package}={version} is not unique")
    return matches[0]


def artifact_stanza(path: Path) -> dict[str, str]:
    result = subprocess.run(["dpkg-deb", "--field", str(path)], check=True, capture_output=True, text=True)
    rows = parse_stanzas(result.stdout)
    if len(rows) != 1:
        raise ValueError("artifact control metadata is not one stanza")
    return rows[0]


def build_modeled_status(
    base_status: Path,
    output: Path,
    artifact: Path,
    decisions: list[tuple[str, str]],
) -> None:
    rows = parse_stanzas(base_status.read_text(encoding="utf-8"))
    additions = [package_stanza(package, version) for package, version in decisions]
    additions.append(artifact_stanza(artifact))
    for addition in additions:
        addition = {key: value for key, value in addition.items() if key not in DROP_FIELDS}
        addition["Status"] = "install ok installed"
        name = addition.get("Package", "")
        architecture = addition.get("Architecture", "")
        if not name or not architecture or not addition.get("Version"):
            raise ValueError("modeled package metadata is incomplete")
        rows = [row for row in rows if not (row.get("Package") == name and row.get("Architecture") == architecture)]
        # Status first matches conventional dpkg status layout but has no
        # semantic effect on APT's RFC822 parser.
        ordered = {"Package": name, "Status": addition.pop("Status")}
        ordered.update(addition)
        rows.append(ordered)
    output.write_text(render_stanzas(rows), encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-status", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--artifact", required=True, type=Path)
    parser.add_argument("--decision", action="append", default=[])
    args = parser.parse_args()
    decisions = []
    for value in args.decision:
        package, separator, version = value.partition("=")
        if not separator or not package or not version:
            raise ValueError("modeled package decision must be package=version")
        decisions.append((package, version))
    build_modeled_status(args.base_status, args.output, args.artifact, decisions)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

