import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

ROOT = Path(__file__).resolve().parent.parent


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class Server:
    def __init__(self):
        self.db_path = os.environ.get("PASSPORT_TEST_DB", "")
        self.port = int(os.environ["PASSPORT_TEST_PORT"])
        self.base = f"http://127.0.0.1:{self.port}"
        self.admin_token = os.environ["PASSPORT_ADMIN_TOKEN"]
        self.gate_token = os.environ["PASSPORT_GATE_TOKEN"]
        self.proc = None

    def start(self):
        env = dict(os.environ)
        env["PYTHONPATH"] = str(ROOT)
        env["PASSPORT_HOST"] = "127.0.0.1"
        env["PASSPORT_PORT"] = str(self.port)
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "uvicorn", "app.main:app",
             "--host", "127.0.0.1", "--port", str(self.port), "--log-level", "warning"],
            cwd=ROOT, env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        deadline = time.time() + 20
        while time.time() < deadline:
            try:
                if httpx.get(f"{self.base}/healthz", timeout=1).status_code == 200:
                    return
            except httpx.TransportError:
                pass
            if self.proc.poll() is not None:
                out = self.proc.stdout.read().decode() if self.proc.stdout else ""
                raise RuntimeError(f"server exited early:\n{out}")
            time.sleep(0.15)
        raise RuntimeError("server did not become ready")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
        self.proc = None

    def restart(self):
        self.stop()
        time.sleep(0.5)
        self.start()

    def client(self, token=None):
        headers = {"Authorization": f"Bearer {token or self.admin_token}"}
        return httpx.Client(base_url=self.base, headers=headers, timeout=10)


@pytest.fixture(scope="session")
def srv(tmp_path_factory):
    db = tmp_path_factory.mktemp("db") / "test.db"
    os.environ["PASSPORT_TEST_DB"] = str(db)
    os.environ["PASSPORT_TEST_PORT"] = str(_free_port())
    os.environ["PASSPORT_DB"] = str(db)
    os.environ["PASSPORT_ADMIN_TOKEN"] = "test-admin"
    os.environ["PASSPORT_GATE_TOKEN"] = "test-gate"
    os.environ["PASSPORT_SWEEP_INTERVAL"] = "1"
    server = Server()
    server.start()
    yield server
    server.stop()


@pytest.fixture(scope="session", autouse=True)
def register_gates(srv):
    with srv.client() as c:
        for gid, name in [("g1", "一号门"), ("g2", "二号门"), ("g3", "三号门")]:
            r = c.post("/api/admin/gates", json={"id": gid, "name": name})
            assert r.status_code in (200, 409)
        for zid, zname in [("Z1", "一区"), ("Z2", "二区")]:
            r = c.post("/api/admin/zones", json={"id": zid, "name": zname})
            assert r.status_code in (200, 409)
        for gid in ("g1", "g2", "g3"):
            r = c.put(f"/api/admin/gates/{gid}/zone", json={"zone_id": "Z1"})
            assert r.status_code == 200
