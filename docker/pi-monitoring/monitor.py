#!/usr/bin/env python3
"""
pistat - 라즈베리파이 홈서버 모니터.

/proc 과 /sys 를 직접 읽어 지표를 모으고, 최근 기록을 메모리에 담아두었다가
JSON API 와 대시보드 페이지로 내보낸다.

표준 라이브러리만 사용한다. pip 설치가 필요 없다.

    python3 monitor.py

기본값은 127.0.0.1:8088 이다. TLS 와 비밀번호는 앞단의 nginx 가 처리하므로
이 포트를 외부에 직접 열지 않는다.
"""

import json
import os
import subprocess
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --- 설정 ---------------------------------------------------------------

HOST = os.environ.get("PISTAT_HOST", "127.0.0.1")
PORT = int(os.environ.get("PISTAT_PORT", "8088"))
INTERVAL = float(os.environ.get("PISTAT_INTERVAL", "5"))  # 샘플 간격(초)
HISTORY_MINUTES = int(os.environ.get("PISTAT_HISTORY_MIN", "60"))
DISK_PATH = os.environ.get("PISTAT_DISK", "/")

HISTORY_LEN = max(60, int(HISTORY_MINUTES * 60 / INTERVAL))
BASE_DIR = Path(__file__).resolve().parent

try:
    import docker_probe
except Exception:  # 파일이 없어도 나머지는 정상 동작해야 한다
    docker_probe = None

# --- 지표 읽기 ----------------------------------------------------------


def read_text(path, default=None):
    try:
        return Path(path).read_text()
    except OSError:
        return default


def cpu_times():
    """/proc/stat 의 전체 시간과 유휴 시간."""
    line = read_text("/proc/stat", "")
    for row in line.splitlines():
        if row.startswith("cpu "):
            v = [int(x) for x in row.split()[1:]]
            idle = v[3] + (v[4] if len(v) > 4 else 0)  # idle + iowait
            return sum(v), idle
    return None, None


def mem_info():
    """전체/사용 바이트. MemAvailable 을 쓴다. 회수 가능한 캐시를 반영한 값이라
    실제로 메모리가 부족한지를 알려주는 건 이 숫자다."""
    info = {}
    for row in (read_text("/proc/meminfo", "") or "").splitlines():
        key, _, rest = row.partition(":")
        parts = rest.split()
        if parts:
            info[key] = int(parts[0]) * 1024
    total = info.get("MemTotal", 0)
    avail = info.get("MemAvailable", info.get("MemFree", 0))
    swap_total = info.get("SwapTotal", 0)
    swap_used = swap_total - info.get("SwapFree", 0)
    return total, total - avail, swap_total, swap_used


def disk_info(path=DISK_PATH):
    try:
        s = os.statvfs(path)
    except OSError:
        return 0, 0
    total = s.f_blocks * s.f_frsize
    free = s.f_bavail * s.f_frsize
    return total, total - free


def cpu_temp():
    """섭씨 온도. 온도 센서가 없는 하드웨어에서는 None."""
    for zone in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
        raw = read_text(zone / "temp")
        if raw and raw.strip().lstrip("-").isdigit():
            return int(raw.strip()) / 1000.0
    return None


def net_bytes():
    """실제 인터페이스의 수신/송신 합계. 루프백과 가상 브리지는 제외한다.
    컨테이너 트래픽이 이중으로 잡히는 걸 막기 위해서다."""
    rx = tx = 0
    for row in (read_text("/proc/net/dev", "") or "").splitlines()[2:]:
        name, _, rest = row.partition(":")
        name = name.strip()
        if name == "lo" or name.startswith(("docker", "br-", "veth", "virbr")):
            continue
        f = rest.split()
        if len(f) >= 9:
            rx += int(f[0])
            tx += int(f[8])
    return rx, tx


def load_avg():
    parts = (read_text("/proc/loadavg", "") or "").split()
    return [float(x) for x in parts[:3]] if len(parts) >= 3 else [0.0, 0.0, 0.0]


def uptime_seconds():
    raw = (read_text("/proc/uptime", "") or "0").split()
    return float(raw[0]) if raw else 0.0


_THROTTLE_BITS = {
    0: ("under_voltage", "지금 전압이 부족합니다"),
    1: ("freq_capped", "지금 클럭이 제한되고 있습니다"),
    2: ("throttled", "지금 스로틀링 중입니다"),
    3: ("soft_temp_limit", "지금 온도 제한에 걸려 있습니다"),
    16: ("under_voltage_since_boot", "부팅 이후 전압 부족이 있었습니다"),
    17: ("freq_capped_since_boot", "부팅 이후 클럭 제한이 있었습니다"),
    18: ("throttled_since_boot", "부팅 이후 스로틀링이 있었습니다"),
    19: ("soft_temp_limit_since_boot", "부팅 이후 온도 제한에 걸린 적이 있습니다"),
}

_throttle_cache = {"at": 0.0, "value": None}


def throttle_flags():
    """라즈베리파이 전용. 전압 부족 플래그는 `vcgencmd get_throttled` 로만 볼 수
    있는데, 파이가 이상하게 동작하는 가장 흔한 원인이 바로 이것이다.
    vcgencmd 는 상대적으로 느려서 결과를 캐싱한다."""
    now = time.time()
    if now - _throttle_cache["at"] < 30:
        return _throttle_cache["value"]
    result = None
    try:
        out = subprocess.run(
            ["vcgencmd", "get_throttled"],
            capture_output=True, text=True, timeout=3,
        ).stdout.strip()
        if "=" in out:
            bits = int(out.split("=")[1], 16)
            result = {
                "raw": out.split("=")[1],
                "flags": [
                    {"key": k, "label": label}
                    for bit, (k, label) in _THROTTLE_BITS.items()
                    if bits & (1 << bit)
                ],
            }
    except (OSError, ValueError, subprocess.SubprocessError):
        result = None
    _throttle_cache.update(at=now, value=result)
    return result


def host_facts():
    model = (read_text("/proc/device-tree/model", "") or "").strip("\x00").strip()
    os_name = ""
    for row in (read_text("/etc/os-release", "") or "").splitlines():
        if row.startswith("PRETTY_NAME="):
            os_name = row.split("=", 1)[1].strip().strip('"')
    return {
        "hostname": os.uname().nodename,
        "model": model or os.uname().machine,
        "os": os_name or os.uname().sysname,
        "kernel": os.uname().release,
        "cores": os.cpu_count() or 1,
    }


# --- 샘플러 -------------------------------------------------------------


class Sampler:
    """INTERVAL 초마다 링 버퍼에 기록한다. 덕분에 페이지를 새로 열어도 빈 그래프가
    아니라 지난 한 시간이 바로 그려진다."""

    def __init__(self):
        self.history = deque(maxlen=HISTORY_LEN)
        self.lock = threading.Lock()
        self.facts = host_facts()
        self._prev_cpu = cpu_times()
        self._prev_net = net_bytes()
        self._prev_at = time.time()
        self.containers = []
        self._docker = docker_probe.ContainerSampler() if docker_probe else None

    def sample(self):
        now = time.time()
        elapsed = max(now - self._prev_at, 0.001)

        total, idle = cpu_times()
        cpu_pct = 0.0
        if total and self._prev_cpu[0]:
            d_total = total - self._prev_cpu[0]
            d_idle = idle - self._prev_cpu[1]
            if d_total > 0:
                cpu_pct = max(0.0, min(100.0, (1 - d_idle / d_total) * 100))
        self._prev_cpu = (total, idle)

        rx, tx = net_bytes()
        rx_bps = max(0, rx - self._prev_net[0]) / elapsed
        tx_bps = max(0, tx - self._prev_net[1]) / elapsed
        self._prev_net = (rx, tx)
        self._prev_at = now

        mem_total, mem_used, swap_total, swap_used = mem_info()
        disk_total, disk_used = disk_info()
        load = load_avg()
        temp = cpu_temp()

        point = {
            "t": round(now, 1),
            "cpu": round(cpu_pct, 1),
            "mem_pct": round(mem_used / mem_total * 100, 1) if mem_total else 0,
            "mem_used": mem_used,
            "mem_total": mem_total,
            "swap_used": swap_used,
            "swap_total": swap_total,
            "disk_pct": round(disk_used / disk_total * 100, 1) if disk_total else 0,
            "disk_used": disk_used,
            "disk_total": disk_total,
            "temp": round(temp, 1) if temp is not None else None,
            "load": load,
            "load_pct": round(load[0] / self.facts["cores"] * 100, 1),
            "rx_bps": round(rx_bps),
            "tx_bps": round(tx_bps),
            "uptime": round(uptime_seconds()),
        }
        containers = []
        if self._docker is not None:
            try:
                containers = self._docker.sample()
            except Exception as exc:
                print(f"[pistat] 컨테이너 조회 실패: {exc}", flush=True)

        with self.lock:
            self.history.append(point)
            self.containers = containers
        return point

    def docker_state(self):
        with self.lock:
            containers = list(self.containers)
        if self._docker is None:
            return {"available": False, "error": "docker_probe.py 없음", "containers": []}
        return {
            "available": self._docker.ok,
            "error": self._docker.error,
            "logs_enabled": docker_probe.LOGS_ENABLED,
            "groups": docker_probe.group_names(),
            "other": docker_probe.OTHER_GROUP,
            "containers": containers,
        }

    def run(self):
        self.sample()  # 첫 샘플은 기준값이 없어 버린다
        while True:
            time.sleep(INTERVAL)
            try:
                self.sample()
            except Exception as exc:  # 스레드는 계속 살려둔다
                print(f"[pistat] 샘플 실패: {exc}", flush=True)

    def latest(self):
        with self.lock:
            return self.history[-1] if self.history else None

    def snapshot(self, since=None):
        with self.lock:
            points = list(self.history)
        if since is not None:
            points = [p for p in points if p["t"] > since]
        return points


sampler = Sampler()


# --- HTTP ---------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "pistat"

    def log_message(self, *args):
        pass  # nginx 가 이미 기록한다

    def _send(self, code, body, ctype, cache="no-store"):
        if isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload):
        self._send(200, json.dumps(payload), "application/json; charset=utf-8")

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        query = self.path.split("?", 1)[1] if "?" in self.path else ""

        if path == "/":
            html = (BASE_DIR / "index.html").read_text(encoding="utf-8")
            self._send(200, html, "text/html; charset=utf-8")

        elif path == "/api/status":
            point = sampler.latest()
            if point is None:
                self._json({"error": "warming up"})
                return
            self._json({
                "now": point,
                "host": sampler.facts,
                "throttle": throttle_flags(),
                "interval": INTERVAL,
            })

        elif path == "/api/history":
            since = None
            for part in query.split("&"):
                if part.startswith("since="):
                    try:
                        since = float(part[6:])
                    except ValueError:
                        pass
            self._json({
                "points": sampler.snapshot(since),
                "host": sampler.facts,
                "throttle": throttle_flags(),
                "interval": INTERVAL,
            })

        elif path == "/api/containers":
            self._json(sampler.docker_state())

        elif path == "/api/logs":
            params = dict(
                p.split("=", 1) for p in query.split("&") if "=" in p
            )
            cid = params.get("id", "")
            # 목록에 있는 컨테이너만 허용한다. 임의의 경로가 도커 API 로
            # 흘러들어가지 않도록 하기 위해서다.
            known = {c["full_id"] for c in sampler.docker_state()["containers"]}
            if cid not in known:
                self._json({"error": "알 수 없는 컨테이너"})
                return
            if docker_probe is None or not docker_probe.LOGS_ENABLED:
                self._json({"error": "로그 보기가 꺼져 있습니다"})
                return
            try:
                tail = int(params.get("tail", "120"))
            except ValueError:
                tail = 120
            try:
                self._json({"lines": docker_probe.logs(cid, tail)})
            except Exception as exc:
                self._json({"error": str(exc)})

        elif path == "/api/health":
            # UptimeRobot 같은 외부 감시 서비스용 평문 응답.
            self._send(200, "ok", "text/plain; charset=utf-8")

        else:
            self._send(404, "not found", "text/plain; charset=utf-8")


def main():
    threading.Thread(target=sampler.run, daemon=True).start()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"[pistat] http://{HOST}:{PORT} 에서 대기 중", flush=True)
    print(f"[pistat] {INTERVAL}초 간격 샘플링, {HISTORY_MINUTES}분 보관", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[pistat] 종료", flush=True)


if __name__ == "__main__":
    main()
