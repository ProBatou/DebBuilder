"""Isolated end-to-end probe of the packaged entrypoint's two sockets."""
import http.client
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from debbuilder.local_repository_bootstrap import bootstrap_repository

ROOT = Path(__file__).resolve().parents[1]


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def get(port, path):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    connection.request("GET", path)
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


def update_url(port, value):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
    connection.request("POST", "/api/settings", body=json.dumps({"apt": {"repository": value}}),
                       headers={"Content-Type": "application/json"})
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


@unittest.skipUnless(all(shutil.which(name) for name in ("gpg", "gpgv", "reprepro", "dpkg-deb")), "repository tools unavailable")
class RealPublicRepositoryProbe(unittest.TestCase):
    def test_published_package_key_two_listeners_shutdown_and_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            data, repo = root / "data", root / "repo"
            admin_port, public_port = free_port(), free_port()
            while public_port == admin_port:
                public_port = free_port()
            identity = bootstrap_repository(repository_root=repo, data_root=data, suite="stable", component="main")
            self.assertIn("not configured", (repo / "install.sh").read_text())
            package_root = root / "fixture"
            (package_root / "DEBIAN").mkdir(parents=True)
            (package_root / "DEBIAN/control").write_text(
                "Package: debbuilder-probe\nVersion: 1.0\nArchitecture: all\nSection: misc\n"
                "Priority: optional\nMaintainer: Probe <probe@example.invalid>\n"
                "Description: Isolated repository probe\n"
            )
            package = root / "probe.deb"
            subprocess.run(["dpkg-deb", "--build", str(package_root), str(package)], check=True, capture_output=True)
            environment = {**os.environ, "GNUPGHOME": str(data / ".gnupg"), "LC_ALL": "C"}
            subprocess.run(["reprepro", "-b", str(repo), "includedeb", "stable", str(package)],
                           env=environment, check=True, capture_output=True)
            pool = list((repo / "pool").rglob("*.deb"))
            self.assertEqual(len(pool), 1)
            environment.update({
                "DEBBUILDER_DATA_DIR": str(data), "DEBBUILDER_REPO_ROOT": str(repo),
                "DEBBUILDER_REPO_URL": f"http://127.0.0.1:{public_port}",
                "DEBBUILDER_SUITE": "stable", "DEBBUILDER_COMPONENT": "main",
                "DEBBUILDER_HOST": "127.0.0.1", "DEBBUILDER_PORT": str(admin_port),
                "DEBBUILDER_REPOSITORY_HOST": "127.0.0.1", "DEBBUILDER_REPOSITORY_PORT": str(public_port),
                "DEBBUILDER_AUTH_MODE": "none", "PYTHONUNBUFFERED": "1",
            })
            log = root / "server.log"
            for _cycle in range(2):
                with log.open("ab") as output:
                    process = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=environment,
                                               stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        for _ in range(100):
                            if process.poll() is not None:
                                self.fail(f"server exited early: {log.read_text()[-3000:]}")
                            try:
                                admin = get(admin_port, "/api/status")
                                landing = get(public_port, "/")
                                break
                            except (ConnectionError, TimeoutError, OSError):
                                time.sleep(0.1)
                        else:
                            self.fail(f"server not ready: {log.read_text()[-3000:]}")
                        self.assertEqual(admin[0], 200)
                        self.assertEqual(landing[0], 200)
                        self.assertEqual(get(public_port, "/api/status")[0], 404)
                        self.assertEqual(get(public_port, "/install.sh")[0], 200)
                        self.assertEqual(get(public_port, "/repository.gpg")[1], (repo / "repository.gpg").read_bytes())
                        self.assertEqual(get(public_port, "/dists/stable/InRelease")[1],
                                         (repo / "dists/stable/InRelease").read_bytes())
                        self.assertEqual(get(public_port, "/" + pool[0].relative_to(repo).as_posix())[1], pool[0].read_bytes())
                        key = subprocess.run(["gpg", "--batch", "--with-colons", "--fingerprint", "--show-keys",
                                              str(repo / "repository.gpg")], check=True, capture_output=True).stdout
                        self.assertIn(identity["fingerprint"].encode(), key)
                        if _cycle == 0:
                            self.assertEqual(update_url(admin_port, "https://repo.example.test/base")[0], 200)
                            self.assertIn(b"URIs: https://repo.example.test/base", get(public_port, "/install.sh")[1])
                            self.assertEqual(update_url(admin_port, f"http://127.0.0.1:{public_port}")[0], 200)
                    finally:
                        process.send_signal(signal.SIGTERM)
                        process.wait(timeout=25)
                self.assertEqual(process.returncode, 0, log.read_text()[-3000:])
                for port in (admin_port, public_port):
                    with self.assertRaises((ConnectionError, TimeoutError, OSError)):
                        get(port, "/")
            # A mandatory public port must fail startup visibly when occupied.
            with socket.socket() as occupied:
                occupied.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                occupied.bind(("127.0.0.1", public_port))
                occupied.listen()
                with log.open("ab") as output:
                    process = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=environment,
                                               stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        self.assertNotEqual(process.wait(timeout=20), 0)
                    finally:
                        if process.poll() is None:
                            process.kill()
                            process.wait(timeout=5)
            self.assertIn("Address already in use", log.read_text())
            collision_environment = {**environment, "DEBBUILDER_REPOSITORY_PORT": str(admin_port)}
            with log.open("ab") as output:
                process = subprocess.Popen([sys.executable, "server.py"], cwd=ROOT, env=collision_environment,
                                           stdout=output, stderr=subprocess.STDOUT, start_new_session=True)
                try:
                    self.assertNotEqual(process.wait(timeout=20), 0)
                finally:
                    if process.poll() is None:
                        process.kill()
                        process.wait(timeout=5)
            self.assertIn("same host and port", log.read_text())
