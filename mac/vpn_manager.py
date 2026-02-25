#!/usr/bin/env python3
"""
VPN Manager — hev-socks5-tunnel + Suborbital integration for Luke's Mirage.

Manages transparent SOCKS5 proxying on BlueStacks instances via ADB using
hev-socks5-tunnel (a lightweight ~386KB arm64 binary). No on-device VPN app
needed — the GUI pushes the binary, writes a YAML config, starts the daemon,
and sets up ip routing rules so all traffic flows through the SOCKS5 proxy.

Optionally integrates with the Suborbital proxy API to fetch and assign
proxies from the user's account.

Requires: randomize_instances.adb_shell, randomize_instances.find_adb_exe
"""
from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import re
import socket
import ssl
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

# Reuse the ADB helper from randomize_instances (same directory)
import randomize_instances

# ─── hev-socks5-tunnel paths & constants ─────────────────────────────────────

# Path on the device
TUNNEL_BIN = "/data/local/tmp/hev-socks5-tunnel"
TUNNEL_CONFIG = "/data/local/tmp/hev-socks5-tunnel.yml"
TUNNEL_PID = "/data/local/tmp/hev-socks5-tunnel.pid"
TUNNEL_MARK = 438       # fwmark to prevent routing loops
TUNNEL_TABLE = 20       # ip routing table for tun0 default route
KILLSWITCH_CHAIN = "JORK_KS"  # iptables chain name for kill switch (short to avoid issues)

# Path to the binary bundled with the GUI (mac/bin/)
if getattr(sys, 'frozen', False):
    _LOCAL_TUNNEL_BIN = os.path.join(sys._MEIPASS, "bin", "hev-socks5-tunnel")
else:
    _LOCAL_TUNNEL_BIN = os.path.join(os.path.dirname(__file__), "bin", "hev-socks5-tunnel")

SUBORBITAL_BASE = "https://dashboard.suborbit.al/api"

# Polling interval (seconds) when VPN tab is active
POLL_INTERVAL = 5


def _dbg(msg: str):
    print(f"[vpn] {msg}", file=sys.__stderr__, flush=True)


# ─── Suborbital API Client ───────────────────────────────────────────────────


class SuborbitalError(Exception):
    """Raised when Suborbital API returns an error."""
    pass


class SuborbitalClient:
    """Lightweight client for the Suborbital proxy provider REST API.

    Authentication: HTTP Basic auth with the user's Suborbital account
    email and password. All methods use urllib.request (stdlib) — no deps.
    Uses an opener with cookie jar and SSL context to handle Cloudflare.
    """

    # Realistic browser UA to avoid Cloudflare 403 blocks
    _UA = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
    )

    def __init__(self, email: str, password: str):
        self._email = email
        self._password = password
        creds = base64.b64encode(f"{email}:{password}".encode()).decode()
        self._auth_header = f"Basic {creds}"

        # Build an opener that handles cookies (Cloudflare cf_clearance),
        # redirects, and uses a proper SSL context — avoids broken-pipe
        # on TLS renegotiation and Cloudflare challenges.
        self._cookie_jar = http.cookiejar.CookieJar()
        cookie_handler = urllib.request.HTTPCookieProcessor(self._cookie_jar)
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        https_handler = urllib.request.HTTPSHandler(context=ssl_ctx)
        self._opener = urllib.request.build_opener(https_handler, cookie_handler)

    def _request(self, method: str, path: str, data: Optional[dict] = None,
                 _retry: int = 0) -> dict | list:
        """Make an authenticated request to the Suborbital API."""
        url = f"{SUBORBITAL_BASE}{path}"
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("Authorization", self._auth_header)
        req.add_header("Content-Type", "application/json")
        req.add_header("Accept", "application/json")
        req.add_header("User-Agent", self._UA)
        req.add_header("Connection", "keep-alive")

        try:
            resp = self._opener.open(req, timeout=20)
            raw = resp.read().decode()
            return json.loads(raw) if raw.strip() else {}
        except urllib.error.HTTPError as e:
            resp_body = ""
            try:
                resp_body = e.read().decode()
            except Exception:
                pass
            if e.code == 401:
                raise SuborbitalError("Invalid email or password") from e
            if e.code == 403:
                raise SuborbitalError("Access denied — Cloudflare may be blocking the request") from e
            raise SuborbitalError(f"HTTP {e.code}: {resp_body[:200]}") from e
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError) as e:
            # Cloudflare sometimes drops the first connection; retry once
            if _retry < 1:
                _dbg(f"Suborbital connection reset, retrying... ({e})")
                time.sleep(1)
                return self._request(method, path, data, _retry=_retry + 1)
            raise SuborbitalError(f"Connection lost: {e}. Check your network/VPN.") from e
        except urllib.error.URLError as e:
            reason = str(e.reason) if hasattr(e, 'reason') else str(e)
            if "Broken pipe" in reason or "broken pipe" in reason:
                if _retry < 1:
                    _dbg(f"Suborbital broken pipe, retrying... ({reason})")
                    time.sleep(1)
                    return self._request(method, path, data, _retry=_retry + 1)
            raise SuborbitalError(f"Connection error: {reason}") from e
        except OSError as e:
            # Catch [Errno 32] Broken pipe at the OS level
            if e.errno == 32 and _retry < 1:
                _dbg(f"Suborbital OS broken pipe, retrying...")
                time.sleep(1)
                return self._request(method, path, data, _retry=_retry + 1)
            raise SuborbitalError(f"Network error: {e}") from e
        except Exception as e:
            raise SuborbitalError(str(e)) from e

    def get_user(self) -> dict:
        """GET /user — verify credentials and return account info."""
        return self._request("GET", "/user")

    def list_proxies(self) -> list[dict]:
        """GET /user/ips — list all proxies owned by the user.

        Returns: [{ip, port, username, password, country, type, expires, ...}]
        """
        result = self._request("GET", "/user/ips")
        if isinstance(result, list):
            return result
        # Some API versions wrap in {"data": [...]}
        if isinstance(result, dict) and "data" in result:
            return result["data"]
        return []

    def get_stock(self) -> dict:
        """GET /ips/current-stock — available proxy stock by country."""
        return self._request("GET", "/ips/current-stock")

    def get_bandwidth(self) -> dict:
        """GET /user/bandwidth — bandwidth usage stats."""
        return self._request("GET", "/user/bandwidth")


# ─── hev-socks5-tunnel ADB helpers ───────────────────────────────────────────


def _build_tunnel_config(
    server: str, port: int, username: str = "", password: str = "",
) -> str:
    """Build YAML config for hev-socks5-tunnel."""
    # Auth block (only if credentials provided)
    # In YAML, single quotes are escaped by doubling them: ' -> ''
    auth_lines = ""
    if username and password:
        safe_user = username.replace("'", "''")
        safe_pass = password.replace("'", "''")
        auth_lines = f"  username: '{safe_user}'\n  password: '{safe_pass}'\n"

    return (
        f"tunnel:\n"
        f"  name: tun0\n"
        f"  mtu: 1500\n"
        f"  ipv4: 198.18.0.1\n"
        f"\n"
        f"socks5:\n"
        f"  port: {port}\n"
        f"  address: '{server}'\n"
        f"  udp: 'tcp'\n"
        f"{auth_lines}"
        f"  mark: {TUNNEL_MARK}\n"
        f"\n"
        f"misc:\n"
        f"  task-stack-size: 24576\n"
        f"  connect-timeout: 5000\n"
        f"  read-write-timeout: 60000\n"
        f"  log-level: warn\n"
        f"  pid-file: {TUNNEL_PID}\n"
    )


def _ensure_binary(adb_exe: str, serial: str) -> bool:
    """Push hev-socks5-tunnel binary to device if not already present.

    Returns True if binary is ready, False on error.
    """
    # Check if already on device
    cp = randomize_instances.adb_shell(
        adb_exe, serial,
        f"su -c 'test -x {TUNNEL_BIN} && echo OK'",
        timeout_sec=6,
    )
    if cp.returncode == 0 and "OK" in cp.stdout:
        return True

    # Push from local mac/bin/
    if not os.path.isfile(_LOCAL_TUNNEL_BIN):
        _dbg(f"ERROR: Tunnel binary not found at {_LOCAL_TUNNEL_BIN}")
        return False

    _dbg(f"Pushing tunnel binary to {serial}...")
    # Try push with retries (ADB can be flaky right after boot)
    for attempt in range(3):
        try:
            push_cp = subprocess.run(
                [adb_exe, "-s", serial, "push", _LOCAL_TUNNEL_BIN, TUNNEL_BIN],
                capture_output=True, text=True, timeout=30,
            )
            if push_cp.returncode == 0:
                break
            _dbg(f"Push attempt {attempt+1} failed: {push_cp.stderr.strip()}")
        except Exception as e:
            _dbg(f"Push attempt {attempt+1} exception: {e}")
        time.sleep(1)
    else:
        _dbg("Push failed after 3 attempts")
        return False

    randomize_instances.adb_shell(
        adb_exe, serial,
        f"su -c 'chmod 755 {TUNNEL_BIN}'",
        timeout_sec=6,
    )
    _dbg(f"Binary pushed to {serial}")
    return True


def _cleanup_routing(adb_exe: str, serial: str):
    """Remove tunnel routing (split routes + ip rules). Idempotent, ignores errors."""
    for cmd in [
        # Remove split routes from main table
        "su -c 'ip route del 0.0.0.0/1 dev tun0 2>/dev/null; true'",
        "su -c 'ip route del 128.0.0.0/1 dev tun0 2>/dev/null; true'",
        # Remove our ip rules (fwmark bypass + main lookup)
        f"su -c 'ip rule del fwmark {TUNNEL_MARK} lookup eth0 pref 9000 2>/dev/null; true'",
        "su -c 'ip rule del lookup main pref 9500 2>/dev/null; true'",
        # Remove legacy ip rules (from older versions)
        f"su -c 'ip rule del lookup {TUNNEL_TABLE} pref 20 2>/dev/null; true'",
        f"su -c 'ip rule del lookup {TUNNEL_TABLE} pref 14000 2>/dev/null; true'",
        f"su -c 'ip rule del fwmark {TUNNEL_MARK} lookup main pref 10 2>/dev/null; true'",
        # Flush the old routing table (legacy cleanup)
        f"su -c 'ip route flush table {TUNNEL_TABLE} 2>/dev/null; true'",
    ]:
        randomize_instances.adb_shell(adb_exe, serial, cmd, timeout_sec=6)


def _resolve_host(hostname: str) -> str:
    """Resolve a hostname to IP address (from the Mac, before tunnel is up).

    If already an IP address, returns it as-is.
    """
    try:
        socket.inet_aton(hostname)
        return hostname  # Already an IP
    except socket.error:
        pass
    try:
        return socket.gethostbyname(hostname)
    except socket.gaierror:
        return hostname  # Can't resolve; try using it as-is


def write_proxy_config(
    adb_exe: str,
    serial: str,
    server: str,
    port: int,
    username: str = "",
    password: str = "",
) -> dict:
    """Start hev-socks5-tunnel with the given SOCKS5 proxy config.

    Pushes the binary if needed, writes YAML config, starts the daemon,
    and sets up ip routing so all traffic flows through the tunnel.

    Routing strategy: split routes (0.0.0.0/1 + 128.0.0.0/1) in the main
    table.  These are more specific than the default 0.0.0.0/0 so they win,
    but they don't touch Android's ip-rule/fwmark infrastructure at all,
    which keeps ADB, DNS-over-netd, and local bridge traffic working.

    Returns: {"ok": True} or {"ok": False, "error": "..."}
    """
    # 0. Resolve hostname to IP (must happen BEFORE tunnel is up, since DNS
    #    will go through the tunnel after routing is set up — chicken/egg)
    server_ip = _resolve_host(server)
    _dbg(f"Resolved {server} → {server_ip}")

    # 1. Ensure binary is on the device
    if not _ensure_binary(adb_exe, serial):
        return {"ok": False, "error": "Failed to push tunnel binary to device"}

    # 2. Kill any existing tunnel (both via PID file and pkill fallback)
    randomize_instances.adb_shell(
        adb_exe, serial,
        f"su -c 'kill $(head -1 {TUNNEL_PID} 2>/dev/null) 2>/dev/null; "
        f"pkill -9 hev-socks5 2>/dev/null; "
        f"rm -f {TUNNEL_PID} 2>/dev/null; true'",
        timeout_sec=6,
    )
    time.sleep(0.3)

    # 3. Clean up old routing + stale tun0 interface.
    #    After a force-kill the tun0 device can linger in "Resource busy"
    #    state, preventing the tunnel from creating a new one.
    _cleanup_routing(adb_exe, serial)
    randomize_instances.adb_shell(
        adb_exe, serial,
        "su -c 'ip link delete tun0 2>/dev/null; true'",
        timeout_sec=6,
    )

    # 4. Ensure a default gateway exists in the main table (BlueStacks
    #    sometimes omits it; 10.0.2.2 is the NAT gateway)
    randomize_instances.adb_shell(
        adb_exe, serial,
        "su -c 'ip route add default via 10.0.2.2 dev eth0 2>/dev/null; true'",
        timeout_sec=6,
    )

    # 5. Write YAML config (use resolved IP, not hostname)
    #    Base64-encode to avoid any shell quoting issues with passwords
    yaml_content = _build_tunnel_config(server_ip, port, username, password)
    b64 = base64.b64encode(yaml_content.encode()).decode()
    write_cmd = f"su -c 'echo {b64} | base64 -d > {TUNNEL_CONFIG}'"
    cp = randomize_instances.adb_shell(adb_exe, serial, write_cmd, timeout_sec=10)
    if cp.returncode != 0:
        return {"ok": False, "error": f"Failed to write config: {cp.stderr.strip()}"}

    # 6. Disable reverse-path filtering
    randomize_instances.adb_shell(
        adb_exe, serial,
        "su -c 'sysctl -w net.ipv4.conf.all.rp_filter=0 2>/dev/null'",
        timeout_sec=6,
    )

    # 7. Start the tunnel daemon
    randomize_instances.adb_shell(
        adb_exe, serial,
        f"su -c 'nohup {TUNNEL_BIN} {TUNNEL_CONFIG} >/dev/null 2>&1 &'",
        timeout_sec=6,
    )

    # 8. Wait for tun0 to come up (poll instead of fixed sleep)
    tun0_up = False
    for _attempt in range(10):
        tun_check = randomize_instances.adb_shell(
            adb_exe, serial,
            "su -c 'ip link show tun0 2>/dev/null && echo TUN0_UP'",
            timeout_sec=4,
        )
        if "TUN0_UP" in tun_check.stdout:
            tun0_up = True
            break
        time.sleep(0.5)
    if not tun0_up:
        _dbg("Warning: tun0 did not come up within 5s, proceeding anyway")

    # 9. Disable rp_filter on tun0
    randomize_instances.adb_shell(
        adb_exe, serial,
        "su -c 'sysctl -w net.ipv4.conf.tun0.rp_filter=0 2>/dev/null'",
        timeout_sec=6,
    )

    # 10. Route the SOCKS5 proxy server's IP directly via the gateway so its
    #     packets never enter tun0 (prevents routing loop).
    randomize_instances.adb_shell(
        adb_exe, serial,
        f"su -c 'ip route add {server_ip}/32 via 10.0.2.2 dev eth0 2>/dev/null; true'",
        timeout_sec=6,
    )

    # 11. Add split routes: 0.0.0.0/1 + 128.0.0.0/1 via tun0 in the main table.
    randomize_instances.adb_shell(
        adb_exe, serial,
        "su -c 'ip route add 0.0.0.0/1 dev tun0 2>/dev/null; true'",
        timeout_sec=6,
    )
    randomize_instances.adb_shell(
        adb_exe, serial,
        "su -c 'ip route add 128.0.0.0/1 dev tun0 2>/dev/null; true'",
        timeout_sec=6,
    )

    # 12. Add ip rules so Android's fwmark routing doesn't bypass split routes.
    #     - Rule 9000: tunnel's own marked traffic (fwmark TUNNEL_MARK) goes to
    #       the eth0 table directly so it reaches the proxy server without looping.
    #     - Rule 9500: all other traffic goes to main table where our split
    #       routes live. This fires before Android's fwmark rules (pref 10000+).
    randomize_instances.adb_shell(
        adb_exe, serial,
        f"su -c 'ip rule add fwmark {TUNNEL_MARK} lookup eth0 pref 9000 2>/dev/null; true'",
        timeout_sec=6,
    )
    randomize_instances.adb_shell(
        adb_exe, serial,
        "su -c 'ip rule add lookup main pref 9500 2>/dev/null; true'",
        timeout_sec=6,
    )

    # 13. Verify tunnel is running
    time.sleep(0.5)
    cp_verify = randomize_instances.adb_shell(
        adb_exe, serial,
        f"su -c 'kill -0 $(head -1 {TUNNEL_PID} 2>/dev/null) 2>/dev/null && echo RUNNING || echo DEAD'",
        timeout_sec=6,
    )
    if "RUNNING" not in cp_verify.stdout:
        _cleanup_routing(adb_exe, serial)
        return {"ok": False, "error": "Tunnel daemon failed to start — check proxy credentials"}

    _dbg(f"Tunnel started for {serial}: {server_ip}:{port} (resolved from {server})")

    # 13. Auto-patch timezone + carrier country to match the proxy exit IP.
    #     Retry a few times since the tunnel may need a moment to stabilize.
    for _geo_attempt in range(3):
        try:
            time.sleep(1.5)
            geo = randomize_instances._patch_geo_from_ip(adb_exe, serial)
            if geo:
                _dbg(f"Geo patched for {serial}: {geo}")
                break
        except Exception as e:
            _dbg(f"Geo patch attempt {_geo_attempt+1} failed: {e}")
    else:
        _dbg(f"Geo patch failed after 3 attempts for {serial}")

    return {"ok": True}


def read_vpn_status(adb_exe: str, serial: str) -> dict:
    """Read tunnel status from the device.

    Returns: {state: "connected"|"reconnecting"|"stopped"|"unknown", server: "..."}
    """
    status = {"state": "unknown", "server": ""}

    # Check if daemon is running via PID file
    cp_pid = randomize_instances.adb_shell(
        adb_exe, serial,
        f"su -c 'kill -0 $(head -1 {TUNNEL_PID} 2>/dev/null) 2>/dev/null && echo RUNNING || echo STOPPED'",
        timeout_sec=6,
    )

    daemon_running = cp_pid.returncode == 0 and "RUNNING" in cp_pid.stdout

    if daemon_running:
        # Verify tun0 interface is up
        cp_tun = randomize_instances.adb_shell(
            adb_exe, serial,
            "ip link show tun0 2>/dev/null",
            timeout_sec=6,
        )
        if cp_tun.returncode == 0 and "tun0" in cp_tun.stdout:
            status["state"] = "connected"
        else:
            status["state"] = "reconnecting"
    else:
        status["state"] = "stopped"

    # Read server from config file
    cp_conf = randomize_instances.adb_shell(
        adb_exe, serial,
        f"su -c 'head -50 {TUNNEL_CONFIG} 2>/dev/null'",
        timeout_sec=6,
    )
    if cp_conf.returncode == 0 and cp_conf.stdout.strip():
        # Parse address and port from YAML
        m_addr = re.search(r"address:\s*'?([^'\n]+)", cp_conf.stdout)
        m_port = re.search(r"^\s*port:\s*(\d+)", cp_conf.stdout, re.MULTILINE)
        if m_addr:
            addr = m_addr.group(1).strip()
            prt = m_port.group(1).strip() if m_port else ""
            status["server"] = f"{addr}:{prt}" if prt else addr

    return status


def read_current_config(adb_exe: str, serial: str) -> dict:
    """Read the current tunnel config from the device.

    Returns: {server, port, username, password} or empty dict if not configured.
    """
    cp = randomize_instances.adb_shell(
        adb_exe, serial,
        f"su -c 'head -50 {TUNNEL_CONFIG} 2>/dev/null'",
        timeout_sec=6,
    )

    if cp.returncode != 0 or not cp.stdout.strip():
        return {}

    config = {}
    text = cp.stdout

    m_addr = re.search(r"address:\s*'?([^'\n]+)", text)
    if m_addr:
        config["server"] = m_addr.group(1).strip()

    m_port = re.search(r"^\s*port:\s*(\d+)", text, re.MULTILINE)
    if m_port:
        config["port"] = m_port.group(1).strip()

    m_user = re.search(r"username:\s*'?([^'\n]+)", text)
    if m_user:
        config["username"] = m_user.group(1).strip()

    m_pass = re.search(r"password:\s*'?([^'\n]+)", text)
    if m_pass:
        config["password"] = m_pass.group(1).strip()

    return config


def disconnect_vpn(adb_exe: str, serial: str) -> dict:
    """Disconnect the SOCKS5 tunnel on the device.

    Removes routing rules, kills the daemon, cleans up files.

    Returns: {"ok": True} or {"ok": False, "error": "..."}
    """
    # 0. Read the proxy server IP from config so we can clean up its host route
    config = read_current_config(adb_exe, serial)
    proxy_ip = config.get("server", "")

    # 1. Remove routing rules (idempotent)
    _cleanup_routing(adb_exe, serial)

    # 1b. Remove the proxy server host route
    if proxy_ip:
        randomize_instances.adb_shell(
            adb_exe, serial,
            f"su -c 'ip route del {proxy_ip}/32 via 10.0.2.2 dev eth0 2>/dev/null; true'",
            timeout_sec=6,
        )

    # 2. Kill the tunnel daemon
    randomize_instances.adb_shell(
        adb_exe, serial,
        f"su -c 'kill $(head -1 {TUNNEL_PID} 2>/dev/null) 2>/dev/null; true'",
        timeout_sec=6,
    )

    # 3. Clean up PID + config files
    randomize_instances.adb_shell(
        adb_exe, serial,
        f"su -c 'rm -f {TUNNEL_PID} {TUNNEL_CONFIG} 2>/dev/null'",
        timeout_sec=6,
    )

    _dbg(f"Tunnel disconnected for {serial}")
    return {"ok": True}


def enable_kill_switch(adb_exe: str, serial: str):
    """Kill switch placeholder — currently disabled.

    The iptables-based kill switch was causing device freezes and ADB
    disconnections on BlueStacks. Needs a different approach (e.g.
    monitoring tun0 and killing apps if it drops) rather than blanket
    iptables DROP rules which interfere with ADB and system processes.
    """
    # TODO: Implement a safe kill switch that doesn't use iptables DROP
    _dbg(f"Kill switch requested for {serial} (currently disabled — placeholder)")


def disable_kill_switch(adb_exe: str, serial: str):
    """Remove kill switch iptables chain. Idempotent — safe to call even if not enabled."""
    cmds = [
        # Remove jump rule from OUTPUT
        f"iptables -D OUTPUT -j {KILLSWITCH_CHAIN} 2>/dev/null; true",
        # Flush and delete our chain
        f"iptables -F {KILLSWITCH_CHAIN} 2>/dev/null; true",
        f"iptables -X {KILLSWITCH_CHAIN} 2>/dev/null; true",
    ]
    for cmd in cmds:
        randomize_instances.adb_shell(adb_exe, serial, f"su -c '{cmd}'", timeout_sec=6)
    _dbg(f"Kill switch disabled for {serial}")


# ─── VPN Manager (multi-instance orchestrator) ───────────────────────────────


class VPNManager:
    """Manages hev-socks5-tunnel across multiple BlueStacks instances.

    Provides:
    - Background status polling (daemon thread)
    - Single and bulk proxy application
    - Suborbital API integration
    - Status push callbacks to the GUI
    """

    def __init__(self, adb_exe: str, on_status_change: Optional[Callable] = None):
        self._adb_exe = adb_exe
        self._on_status_change = on_status_change
        self._suborbital: Optional[SuborbitalClient] = None

        # Polling state
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_stop = threading.Event()
        self._poll_instances: list[dict] = []  # [{name, serial}, ...]
        self._poll_lock = threading.Lock()

        # Cached statuses
        self._statuses: dict[str, dict] = {}
        self._status_lock = threading.Lock()

        # Always-on VPN: auto-reconnect when VPN drops
        # {instance_name: {server, port, username, password}}
        self._always_on: dict[str, dict] = {}

        # Always-on reconnect tracking: prevent rapid reconnect loops
        # {instance_name: {last_attempt: float, attempts: int}}
        self._reconnect_state: dict[str, dict] = {}
        self._RECONNECT_BASE_DELAY = 10    # seconds before first retry
        self._RECONNECT_MAX_DELAY = 300    # max backoff (5 minutes)
        self._RECONNECT_MAX_ATTEMPTS = 10  # give up after this many consecutive failures

        # Kill switch: block traffic when VPN is not connected
        self._kill_switch: set[str] = set()  # instance names with kill switch enabled

        # Post-boot cooldown: tracks when boot_completed was first seen
        # {instance_name: timestamp} — waits 30s before ADB operations
        self._boot_ready: dict[str, float] = {}

    # ── Status polling ──

    def start_polling(self, instances: list[dict]):
        """Start or update background polling for VPN status.

        instances: [{name: str, serial: str}, ...]

        If a poller is already running, merges the new instances into the
        existing poll list (keyed by name) instead of restarting. This
        prevents the frontend's tab-based polling from killing the
        always-on poller that was started by vpn_restore_always_on().
        """
        with self._poll_lock:
            if self._poll_thread and self._poll_thread.is_alive():
                # Poller already running — merge new instances into list
                existing = {i["name"]: i for i in self._poll_instances}
                for inst in instances:
                    existing[inst["name"]] = inst  # update or add
                self._poll_instances = list(existing.values())
                _dbg(f"Polling updated: now tracking {len(self._poll_instances)} instances")
                return
            self._poll_instances = list(instances)

        self.stop_polling()

        self._poll_stop.clear()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, daemon=True, name="vpn-poll"
        )
        self._poll_thread.start()
        _dbg(f"Polling started for {len(instances)} instances")

    def stop_polling(self):
        """Stop background status polling."""
        self._poll_stop.set()
        if self._poll_thread and self._poll_thread.is_alive():
            self._poll_thread.join(timeout=POLL_INTERVAL + 2)
        self._poll_thread = None
        _dbg("Polling stopped")

    def _poll_loop(self):
        """Background thread: poll VPN status for all tracked instances.

        Also handles always-on auto-reconnect: if an instance has always-on
        enabled and its status is "stopped", we automatically re-apply the
        saved proxy config to reconnect.

        IMPORTANT: Waits for instances to be fully booted before attempting
        reconnect. ADB commands during early boot can block the BlueStacks
        UI from initializing, causing the "Starting BlueStacks Air" hang.
        """
        while not self._poll_stop.is_set():
            with self._poll_lock:
                instances = list(self._poll_instances)

            for inst in instances:
                if self._poll_stop.is_set():
                    break
                try:
                    # Gate: skip instances that aren't fully booted yet.
                    # ADB commands during early boot interfere with BlueStacks
                    # UI initialization and can cause "stuck on splash" hangs.
                    # We also enforce a 30s cooldown after first detecting boot
                    # because sys.boot_completed fires BEFORE the BlueStacks
                    # player UI finishes loading. Heavy ADB during that window
                    # (especially adb push) causes protocol faults and UI hangs.
                    try:
                        boot_cp = randomize_instances.adb_shell(
                            self._adb_exe, inst["serial"],
                            "getprop sys.boot_completed",
                            timeout_sec=4,
                        )
                        if boot_cp.returncode != 0 or "1" not in boot_cp.stdout:
                            # Not booted yet — reset cooldown and skip
                            self._boot_ready.pop(inst["name"], None)
                            continue
                    except Exception:
                        self._boot_ready.pop(inst["name"], None)
                        continue  # ADB not reachable — instance probably not running

                    # Post-boot cooldown: first time we see boot_completed=1,
                    # record the timestamp and wait 30s before doing anything.
                    if inst["name"] not in self._boot_ready:
                        self._boot_ready[inst["name"]] = time.time()
                        _dbg(f"Boot detected for {inst['name']} — "
                             f"waiting 30s for BlueStacks UI to finish")
                        continue
                    cooldown_elapsed = time.time() - self._boot_ready[inst["name"]]
                    if cooldown_elapsed < 30:
                        continue  # Still in cooldown
                    _dbg(f"Cooldown done for {inst['name']} ({cooldown_elapsed:.0f}s) — checking VPN status")

                    status = read_vpn_status(self._adb_exe, inst["serial"])
                    _dbg(f"VPN status for {inst['name']}: {status}")
                    old = self._statuses.get(inst["name"])
                    with self._status_lock:
                        self._statuses[inst["name"]] = status

                    # Push update if status changed (or first poll)
                    if old != status and self._on_status_change:
                        self._on_status_change(inst["name"], status)

                    # Always-on auto-reconnect: if VPN dropped and always-on
                    # is enabled, reconnect using the saved proxy config.
                    # Uses exponential backoff to prevent rapid reconnect loops
                    # that can overwhelm the device / crash BlueStacks.
                    if (status.get("state") == "stopped"
                            and inst["name"] in self._always_on):
                        cfg = self._always_on[inst["name"]]
                        rs = self._reconnect_state.get(inst["name"],
                                                       {"last_attempt": 0, "attempts": 0})
                        now = time.time()
                        # Exponential backoff: 10s, 20s, 40s, 80s, ... up to 300s
                        delay = min(
                            self._RECONNECT_BASE_DELAY * (2 ** rs["attempts"]),
                            self._RECONNECT_MAX_DELAY,
                        )
                        if (rs["attempts"] >= self._RECONNECT_MAX_ATTEMPTS):
                            # Give up after too many consecutive failures
                            if rs["attempts"] == self._RECONNECT_MAX_ATTEMPTS:
                                _dbg(f"Always-on: gave up on {inst['name']} after "
                                     f"{rs['attempts']} attempts")
                                rs["attempts"] += 1  # prevent repeat log
                                self._reconnect_state[inst["name"]] = rs
                        elif now - rs["last_attempt"] >= delay:
                            _dbg(f"Always-on: reconnecting {inst['name']} "
                                 f"(attempt {rs['attempts'] + 1}, delay was {delay:.0f}s)...")
                            rs["last_attempt"] = now
                            rs["attempts"] += 1
                            self._reconnect_state[inst["name"]] = rs
                            try:
                                result = self.apply_proxy(
                                    inst["name"], inst["serial"],
                                    cfg["server"], int(cfg["port"]),
                                    cfg.get("username", ""), cfg.get("password", ""),
                                )
                                if result.get("ok"):
                                    # Success — reset backoff
                                    self._reconnect_state[inst["name"]] = {
                                        "last_attempt": 0, "attempts": 0}
                                    _dbg(f"Always-on: reconnected {inst['name']}")
                                    # Re-enable kill switch if it was active
                                    if inst["name"] in self._kill_switch:
                                        enable_kill_switch(self._adb_exe, inst["serial"])
                                else:
                                    _dbg(f"Always-on: reconnect failed for {inst['name']}: "
                                         f"{result.get('error', 'unknown')}")
                            except Exception as reconnect_err:
                                _dbg(f"Always-on reconnect exception for {inst['name']}: "
                                     f"{reconnect_err}")
                    elif (status.get("state") == "connected"
                          and inst["name"] in self._reconnect_state):
                        # VPN is up — clear backoff state
                        self._reconnect_state.pop(inst["name"], None)

                except Exception as e:
                    _dbg(f"Poll error for {inst['name']}: {e}")
                    err_status = {"state": "unknown", "error": str(e)}
                    with self._status_lock:
                        self._statuses[inst["name"]] = err_status
                    if self._on_status_change:
                        self._on_status_change(inst["name"], err_status)

            # Wait before next poll cycle
            self._poll_stop.wait(timeout=POLL_INTERVAL)

    # ── Single instance operations ──

    def apply_proxy(
        self,
        instance_name: str,
        serial: str,
        server: str,
        port: int,
        username: str = "",
        password: str = "",
    ) -> dict:
        """Apply proxy config and start VPN on one instance."""
        result = write_proxy_config(self._adb_exe, serial, server, port, username, password)

        if result.get("ok"):
            # Update cached status optimistically
            with self._status_lock:
                self._statuses[instance_name] = {"state": "reconnecting", "server": f"{server}:{port}"}
            if self._on_status_change:
                self._on_status_change(instance_name, self._statuses[instance_name])

        return result

    def apply_proxy_bulk(self, assignments: list[dict]) -> dict:
        """Apply proxy config to multiple instances in parallel.

        assignments: [{instance_name, serial, server, port, username, password}, ...]
        Returns: {instance_name: {ok: bool, error?: str}, ...}
        """
        results = {}
        threads = []

        def _apply_one(a):
            r = self.apply_proxy(
                a["instance_name"], a["serial"],
                a["server"], a["port"],
                a.get("username", ""), a.get("password", ""),
            )
            results[a["instance_name"]] = r

        for assignment in assignments:
            t = threading.Thread(target=_apply_one, args=(assignment,))
            threads.append(t)
            t.start()

        for t in threads:
            t.join(timeout=30)

        return results

    def disconnect(self, instance_name: str, serial: str) -> dict:
        """Disconnect VPN for one instance.

        Also disables kill switch and removes always-on for this instance
        to prevent auto-reconnect after an explicit disconnect.
        """
        result = disconnect_vpn(self._adb_exe, serial)

        # Disable kill switch if it was enabled
        if instance_name in self._kill_switch:
            try:
                disable_kill_switch(self._adb_exe, serial)
            except Exception as e:
                _dbg(f"Kill switch cleanup error for {instance_name}: {e}")
            self._kill_switch.discard(instance_name)

        # Remove always-on so we don't auto-reconnect after explicit disconnect
        self._always_on.pop(instance_name, None)

        with self._status_lock:
            self._statuses[instance_name] = {"state": "stopped", "server": ""}
        if self._on_status_change:
            self._on_status_change(instance_name, self._statuses[instance_name])

        return result

    def disconnect_all(self, instances: list[dict]) -> dict:
        """Disconnect VPN for all specified instances.

        instances: [{name, serial}, ...]
        """
        results = {}
        for inst in instances:
            results[inst["name"]] = self.disconnect(inst["name"], inst["serial"])
        return results

    # ── Always-on & Kill switch ──

    def set_always_on(self, instance_name: str, proxy_config: dict | None):
        """Enable or disable always-on VPN for an instance.

        proxy_config: {server, port, username, password} or None to disable.
        When enabled, the poll loop will auto-reconnect if status is "stopped".
        """
        if proxy_config:
            self._always_on[instance_name] = proxy_config
            _dbg(f"Always-on enabled for {instance_name}")
        else:
            self._always_on.pop(instance_name, None)
            _dbg(f"Always-on disabled for {instance_name}")

    def set_kill_switch(self, instance_name: str, serial: str, enabled: bool):
        """Enable or disable kill switch for an instance.

        When enabled, iptables rules block all non-local/non-tunnel traffic,
        preventing IP leaks if the VPN drops.
        """
        if enabled:
            self._kill_switch.add(instance_name)
            enable_kill_switch(self._adb_exe, serial)
        else:
            self._kill_switch.discard(instance_name)
            disable_kill_switch(self._adb_exe, serial)

    # ── Suborbital integration ──

    def set_suborbital_credentials(self, username: str, password: str):
        """Configure Suborbital with account username and password."""
        if username and password:
            self._suborbital = SuborbitalClient(username, password)
            _dbg(f"Suborbital client initialized for user: {username}")
        else:
            self._suborbital = None
            _dbg("Suborbital client cleared")

    def verify_suborbital(self) -> dict:
        """Verify Suborbital credentials by fetching user info.

        Returns: user dict on success, raises SuborbitalError on failure.
        """
        if not self._suborbital:
            raise SuborbitalError("Suborbital credentials not configured")
        return self._suborbital.get_user()

    def fetch_proxies(self) -> list[dict]:
        """Fetch proxy list from Suborbital.

        Returns: [{ip, port, username, password, country, type, ...}]
        Raises SuborbitalError if API key not set or request fails.
        """
        if not self._suborbital:
            raise SuborbitalError("Suborbital API key not configured")
        return self._suborbital.list_proxies()

    def fetch_stock(self) -> dict:
        """Fetch available proxy stock from Suborbital."""
        if not self._suborbital:
            raise SuborbitalError("Suborbital API key not configured")
        return self._suborbital.get_stock()

    def fetch_bandwidth(self) -> dict:
        """Fetch bandwidth usage from Suborbital."""
        if not self._suborbital:
            raise SuborbitalError("Suborbital API key not configured")
        return self._suborbital.get_bandwidth()

    # ── Status queries ──

    def get_all_statuses(self) -> dict:
        """Get cached VPN statuses for all tracked instances."""
        with self._status_lock:
            return dict(self._statuses)

    def get_status(self, instance_name: str) -> dict:
        """Get cached VPN status for one instance."""
        with self._status_lock:
            return self._statuses.get(instance_name, {"state": "unknown"})

    def refresh_status(self, instance_name: str, serial: str):
        """Force-refresh status for one instance (e.g., after randomize)."""
        try:
            status = read_vpn_status(self._adb_exe, serial)
            with self._status_lock:
                self._statuses[instance_name] = status
            if self._on_status_change:
                self._on_status_change(instance_name, status)
        except Exception as e:
            _dbg(f"Refresh error for {instance_name}: {e}")
