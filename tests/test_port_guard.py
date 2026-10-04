"""
server/port_guard.py: an old server still holding the port is detected and
replaced (or the new server moves to a free port) instead of silently
sharing the port -- the Windows SO_REUSEADDR trap.
"""
import http.server
import socket
import subprocess
import sys
import threading
import time

import pytest

from server import port_guard as pg

NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1180
  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       15344
  TCP    127.0.0.1:8000         0.0.0.0:0              LISTENING       9012
  TCP    127.0.0.1:8000         127.0.0.1:53211        ESTABLISHED     15344
  TCP    127.0.0.1:53211        127.0.0.1:8000         ESTABLISHED     7720
  TCP    127.0.0.1:18000        0.0.0.0:0              LISTENING       4444
  TCP    [::]:8000              [::]:0                 ABHÖREN         2020
"""


def test_netstat_listeners_any_language():
    assert pg.parse_netstat_listeners(NETSTAT, 8000) == [15344, 9012, 2020]
    assert pg.parse_netstat_listeners(NETSTAT, 135) == [1180]
    assert pg.parse_netstat_listeners(NETSTAT, 9999) == []


def test_tasklist_and_python_detection():
    assert pg.parse_tasklist_image('"python.exe","15344","Console","1","61,212 K"\r\n') == "python.exe"
    assert pg.parse_tasklist_image("INFO: No tasks are running which match the specified criteria.") == ""
    assert pg.is_python_image("python.exe") and pg.is_python_image("python3.11")
    assert not pg.is_python_image("chrome.exe")


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Page(http.server.BaseHTTPRequestHandler):
    body = b"hello"

    def do_GET(self):
        self.send_response(200 if self.path == "/" else 404)
        self.end_headers()
        self.wfile.write(self.body if self.path == "/" else b"nope")

    def log_message(self, *a):
        pass


def test_free_port_is_used_as_is():
    port = _free_port()
    assert pg.claim_port("127.0.0.1", port, api_version=3, say=lambda m: None) == port


def test_foreign_program_on_the_port_moves_us_to_a_free_one():
    port = _free_port()
    srv = http.server.HTTPServer(("127.0.0.1", port), _Page)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        said = []
        got = pg.claim_port("127.0.0.1", port, api_version=3, say=said.append)
        assert got != port and got > port and any("instead" in m for m in said)
    finally:
        srv.shutdown()


@pytest.mark.skipif(sys.platform.startswith("win"), reason="uses a POSIX child process + lsof/fuser/proc")
def test_stale_prism_server_is_stopped_and_port_reclaimed():
    port = _free_port()
    code = (
        "import http.server\n"
        "class H(http.server.BaseHTTPRequestHandler):\n"
        "    def do_GET(self):\n"
        "        self.send_response(200 if self.path == '/' else 404); self.end_headers()\n"
        "        self.wfile.write(b'<title>PRISM Travel</title>' if self.path == '/' else b'old')\n"
        "    def log_message(self, *a): pass\n"
        f"http.server.HTTPServer(('127.0.0.1', {port}), H).serve_forever()\n"
    )
    old = subprocess.Popen([sys.executable, "-c", code])
    try:
        for _ in range(50):
            if pg.is_listening("127.0.0.1", port):
                break
            time.sleep(0.1)
        if not pg.listener_pids(port):
            pytest.skip("no lsof/fuser here to find the listening process")
        said = []
        assert pg.claim_port("127.0.0.1", port, api_version=3, say=said.append) == port
        assert old.wait(timeout=5) is not None
        assert any("older prism-agent server" in m for m in said)
    finally:
        if old.poll() is None:
            old.kill()


def test_windows_flow_finds_and_kills_only_python_listeners(monkeypatch):
    """The Windows branch end to end with netstat/tasklist/taskkill faked:
    only the python listeners on the port are stopped."""
    calls = []
    state = {"listening": True}

    def fake_run(cmd, timeout=6.0):
        calls.append(cmd)
        if cmd[:2] == ["netstat", "-ano"]:
            return NETSTAT if cmd[-1] == "TCP" else ""
        if cmd[0] == "tasklist":
            pid = cmd[2].split()[-1]
            name = {"15344": "python.exe", "9012": "python.exe", "2020": "svchost.exe"}[pid]
            return f'"{name}","{pid}","Console","1","61,212 K"\r\n'
        if cmd[0] == "taskkill":
            if sum(1 for c in calls if c[0] == "taskkill") == 2:   # both old servers gone
                state["listening"] = False
            return "SUCCESS"
        return ""

    monkeypatch.setattr(pg, "IS_WINDOWS", True)
    monkeypatch.setattr(pg, "_run", fake_run)
    monkeypatch.setattr(pg, "is_listening", lambda h, p, timeout=0.6: state["listening"] if p == 8000 else False)
    monkeypatch.setattr(pg, "identify", lambda h, p: {"prism": True, "api": None})
    said = []
    assert pg.claim_port("127.0.0.1", 8000, api_version=3, say=said.append) == 8000
    killed = [c[2] for c in calls if c[0] == "taskkill"]
    assert killed == ["15344", "9012"]          # svchost (PID 2020) left alone
    assert all(c[:4] == ["taskkill", "/PID", c[2], "/T"] for c in calls if c[0] == "taskkill")
