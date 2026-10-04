"""
Make `python server/app.py` take over its port reliably.

Why this exists: Hypercorn sets SO_REUSEADDR on every listening socket. On
Windows that option lets a SECOND process bind a port another process is
already listening on -- so if an old server is still alive (a closed
terminal, an IDE run, a second window), starting a new one "succeeds"
while the OLD process keeps answering the browser. New pages, old API,
every request 404s, and restarting changes nothing.

So, before binding, the server:
  1. checks whether something already answers on the port;
  2. if it is an older prism-agent server (a Python process serving the
     PRISM Travel pages), stops it and waits for the port to free up;
  3. otherwise (some other program, or it can't be stopped) picks the next
     free port and says so loudly;
and binds with SO_EXCLUSIVEADDRUSE on Windows so no later process can
silently share the port again.

Everything OS-specific is behind small functions with pure parsers, so the
Windows netstat/tasklist handling is unit-tested on any platform.
"""
from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from typing import Callable, Optional

IS_WINDOWS = sys.platform.startswith("win")
_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0  # CREATE_NO_WINDOW


# ---- pure parsers (tested on every OS) -------------------------------------

def parse_netstat_listeners(text: str, port: int) -> list[int]:
    """PIDs listening on TCP `port` in `netstat -ano -p TCP` output.

    A line is a listener when its local address ends in :port and its
    foreign address ends in :0 -- that avoids depending on the word
    "LISTENING", which Windows localizes."""
    pids: list[int] = []
    for line in text.splitlines():
        cols = line.split()
        if len(cols) < 4 or cols[0].upper() not in ("TCP", "TCPV6"):
            continue
        local, foreign, pid = cols[1], cols[2], cols[-1]
        if not local.rsplit(":", 1)[-1] == str(port) or not foreign.endswith(":0"):
            continue
        if pid.isdigit() and int(pid) not in pids and int(pid) != 0:
            pids.append(int(pid))
    return pids


def parse_tasklist_image(text: str) -> str:
    """Image name from `tasklist /FI "PID eq N" /FO CSV /NH` ("" if none)."""
    line = text.strip().splitlines()[0] if text.strip() else ""
    if not line.startswith('"'):
        return ""
    return line.split('","', 1)[0].strip('"')


def is_python_image(name: str) -> bool:
    n = os.path.basename(name or "").lower()
    return n.startswith("python") or n in ("py.exe", "py", "pythonw.exe")


# ---- probing ---------------------------------------------------------------

def _probe_host(host: str) -> str:
    return "127.0.0.1" if host in ("0.0.0.0", "", "::", "localhost") else host


def is_listening(host: str, port: int, timeout: float = 0.6) -> bool:
    try:
        with socket.create_connection((_probe_host(host), port), timeout=timeout):
            return True
    except OSError:
        return False


def identify(host: str, port: int, timeout: float = 2.0) -> dict:
    """What answers on the port: {"prism": bool, "api": int|None}."""
    base = f"http://{_probe_host(host)}:{port}"
    info = {"prism": False, "api": None}
    try:
        with urllib.request.urlopen(base + "/", timeout=timeout) as resp:
            info["prism"] = "PRISM Travel" in resp.read(200_000).decode("utf-8", "replace")
    except Exception:
        pass
    try:
        import json
        with urllib.request.urlopen(base + "/api/version", timeout=timeout) as resp:
            info["api"] = json.loads(resp.read().decode("utf-8")).get("api")
            info["prism"] = True
    except Exception:
        pass
    return info


# ---- finding and stopping the old process ----------------------------------

def _run(cmd: list[str], timeout: float = 6.0) -> str:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout,
                              creationflags=_NO_WINDOW).stdout or ""
    except Exception:
        return ""


def listener_pids(port: int) -> list[int]:
    if IS_WINDOWS:
        pids = parse_netstat_listeners(_run(["netstat", "-ano", "-p", "TCP"]), port)
        pids += [p for p in parse_netstat_listeners(_run(["netstat", "-ano", "-p", "TCPv6"]), port) if p not in pids]
        return [p for p in pids if p != os.getpid()]
    out = _run(["lsof", "-nP", "-t", f"-iTCP:{port}", "-sTCP:LISTEN"])
    if not out.strip():
        out = _run(["fuser", f"{port}/tcp"]).replace(f"{port}/tcp:", "")
    pids = []
    for tok in out.split():
        if tok.isdigit() and int(tok) != os.getpid() and int(tok) not in pids:
            pids.append(int(tok))
    return pids


def process_name(pid: int) -> str:
    if IS_WINDOWS:
        return parse_tasklist_image(_run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"]))
    try:
        with open(f"/proc/{pid}/comm", encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return _run(["ps", "-p", str(pid), "-o", "comm="]).strip()


def stop_process(pid: int) -> None:
    if IS_WINDOWS:
        _run(["taskkill", "/PID", str(pid), "/T", "/F"])
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    for _ in range(30):
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except OSError:
            return
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


def wait_until_free(host: str, port: int, seconds: float = 6.0) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if not is_listening(host, port, timeout=0.3):
            return True
        time.sleep(0.2)
    return not is_listening(host, port, timeout=0.3)


def next_free_port(host: str, start: int, tries: int = 20) -> int:
    for port in range(start, start + tries):
        if not is_listening(host, port, timeout=0.2):
            return port
    return 0


# ---- the decision -----------------------------------------------------------

def claim_port(host: str, port: int, *, api_version: int, say: Callable[[str], None] = print,
               replace: Optional[bool] = None) -> int:
    """Return the port to bind: `port` itself once any stale prism-agent
    server on it has been stopped, or the next free port if something else
    owns it. Set PRISM_REPLACE_OLD=0 to never stop another process."""
    if not is_listening(host, port):
        return port
    if replace is None:
        replace = os.environ.get("PRISM_REPLACE_OLD", "1") not in ("0", "false", "no")
    info = identify(host, port)
    what = ("an older prism-agent server" if info["prism"] and info["api"] != api_version
            else "another prism-agent server" if info["prism"] else "another program")
    if info["prism"] and replace:
        pids = [p for p in listener_pids(port) if is_python_image(process_name(p))]
        for pid in pids:
            say(f"  Port {port} is held by {what} (PID {pid}) -- stopping it so this one can serve.")
            stop_process(pid)
        if pids and wait_until_free(host, port):
            return port
    alt = next_free_port(host, port + 1)
    hint = ("netstat -ano | findstr :%d   then   taskkill /PID <pid> /F" % port if IS_WINDOWS
            else "lsof -i :%d   then   kill <pid>" % port)
    say("")
    say(f"  !! Port {port} is busy ({what}) and could not be freed automatically.")
    say(f"  !! To free it yourself:  {hint}")
    if not alt:
        raise SystemExit(f"No free port found near {port}.")
    say(f"  !! Serving on port {alt} instead -- open http://127.0.0.1:{alt}")
    say("")
    return alt


def exclusive_bind_config(config_cls):
    """A hypercorn Config subclass whose TCP sockets refuse to share their
    port (SO_EXCLUSIVEADDRUSE on Windows; POSIX already refuses two
    listeners, SO_REUSEADDR there only skips TIME_WAIT)."""
    if not IS_WINDOWS or not hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
        return config_cls

    class ExclusiveConfig(config_cls):
        def _create_sockets(self, binds, type_=socket.SOCK_STREAM):
            plain = [b for b in binds if not b.startswith(("unix:", "fd://"))]
            other = [b for b in binds if b not in plain]
            sockets = super()._create_sockets(other, type_) if other else []
            for bind in plain:
                host, _, port = bind.replace("[", "").replace("]", "").rpartition(":")
                sock = socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, type_)
                if type_ == socket.SOCK_STREAM:
                    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
                sock.bind((host, int(port)))
                sock.setblocking(False)
                try:
                    sock.set_inheritable(True)
                except AttributeError:
                    pass
                sockets.append(sock)
            return sockets

    return ExclusiveConfig
