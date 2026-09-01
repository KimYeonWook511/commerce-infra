#!/usr/bin/env python3
"""
컨테이너 정보 수집기.

두 곳에서 나눠 읽는다.

  - 도커 API: 이름, 상태, 재시작 횟수, 헬스체크, 로그
  - cgroup:   CPU, 메모리

도커의 stats API 는 컨테이너 하나당 1~2초가 걸려서 5초 주기에 맞지 않는다.
cgroup 파일은 즉시 읽히므로 수치는 그쪽에서 가져와 ID 로 합친다.

PISTAT_DOCKER 로 접속 대상을 지정한다.
  tcp://127.0.0.1:2375   소켓 프록시 (권장)
  /var/run/docker.sock   직접 접속

PISTAT_GROUPS 로 컨테이너를 프로젝트별로 묶는다. 한 대의 호스트에서 여러
프로젝트를 돌릴 때 목록이 뒤섞이는 것을 막는다.
"""

import http.client
import json
import os
import re
import socket
import time
from pathlib import Path

DOCKER_HOST = os.environ.get("PISTAT_DOCKER", "tcp://127.0.0.1:2375")
LOGS_ENABLED = os.environ.get("PISTAT_LOGS", "1") == "1"
LOG_TAIL_MAX = int(os.environ.get("PISTAT_LOG_TAIL", "300"))
GROUPS_RAW = os.environ.get("PISTAT_GROUPS", "")
OTHER_GROUP = os.environ.get("PISTAT_GROUP_OTHER", "기타")


# --- 그룹 ---------------------------------------------------------------

# 한 호스트에서 여러 프로젝트를 돌릴 때 컨테이너를 프로젝트별로 묶는다.
#
#   PISTAT_GROUPS=commerce:commerce-|crocobird:crocobird-,@crocobird
#
# 그룹은 `|` 로, 한 그룹의 규칙은 `,` 로 나눈다. 규칙은 세 가지다.
#
#   crocobird-    컨테이너 이름 접두사
#   @crocobird    compose 프로젝트 이름
#   #crocobird-net  도커 네트워크 이름
#
# 위에서부터 순서대로 맞춰보고, 어디에도 안 맞으면 OTHER_GROUP 으로 간다.


def _parse_groups(raw):
    groups = []
    for chunk in raw.split("|"):
        name, _, rules = chunk.strip().partition(":")
        name = name.strip()
        if not name:
            continue
        rule = {"name": name, "prefixes": [], "projects": [], "networks": []}
        for token in rules.split(","):
            token = token.strip()
            if token.startswith("@"):
                rule["projects"].append(token[1:])
            elif token.startswith("#"):
                rule["networks"].append(token[1:])
            elif token:
                rule["prefixes"].append(token)
        groups.append(rule)
    return groups


GROUPS = _parse_groups(GROUPS_RAW)


def group_names():
    """선언된 그룹 이름. 컨테이너가 하나도 없어도 화면에 자리를 남기려고 쓴다.
    규칙에 안 맞는 컨테이너가 조용히 사라지지 않고 눈에 띄게 하기 위해서다."""
    return [g["name"] for g in GROUPS]


def group_of(name, project, networks):
    for g in GROUPS:
        if any(name.startswith(p) for p in g["prefixes"]):
            return g["name"]
        if project and project in g["projects"]:
            return g["name"]
        if any(n in networks for n in g["networks"]):
            return g["name"]
    return OTHER_GROUP


# --- 도커 API -----------------------------------------------------------


class _UnixConnection(http.client.HTTPConnection):
    """유닉스 소켓 위에서 HTTP 를 말하기 위한 어댑터."""

    def __init__(self, path, timeout):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(self._path)
        self.sock = sock


def _connect(timeout):
    if DOCKER_HOST.startswith("tcp://"):
        host, _, port = DOCKER_HOST[6:].partition(":")
        return http.client.HTTPConnection(host, int(port or 2375), timeout=timeout)
    return _UnixConnection(DOCKER_HOST, timeout)


def api(path, timeout=5, raw=False):
    conn = _connect(timeout)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read()
        if resp.status >= 400:
            raise RuntimeError(f"도커 API {resp.status}: {body[:120]!r}")
        return body if raw else json.loads(body)
    finally:
        conn.close()


def available():
    """접속 가능 여부와, 안 되면 그 이유."""
    try:
        api("/_ping", timeout=2, raw=True)
        return True, None
    except Exception as exc:
        return False, str(exc)


# --- cgroup -------------------------------------------------------------

# 도커의 cgroup 드라이버(systemd / cgroupfs)와 cgroup 버전(v1 / v2)에 따라
# 경로가 달라진다. 후보를 순서대로 찾는다.
_V2_DIRS = [
    "/sys/fs/cgroup/system.slice/docker-{id}.scope",
    "/sys/fs/cgroup/docker/{id}",
    "/sys/fs/cgroup/unified/docker/{id}",
]
_V1_CPU = [
    "/sys/fs/cgroup/cpu,cpuacct/docker/{id}/cpuacct.usage",
    "/sys/fs/cgroup/cpuacct/docker/{id}/cpuacct.usage",
]
_V1_MEM = [
    "/sys/fs/cgroup/memory/docker/{id}/memory.usage_in_bytes",
]
_V1_MEM_MAX = [
    "/sys/fs/cgroup/memory/docker/{id}/memory.limit_in_bytes",
]

_dir_cache = {}


def _v2_dir(cid):
    if cid in _dir_cache:
        return _dir_cache[cid]
    for pattern in _V2_DIRS:
        path = Path(pattern.format(id=cid))
        if (path / "cpu.stat").exists():
            _dir_cache[cid] = path
            return path
    _dir_cache[cid] = None
    return None


def _read_int(path):
    try:
        text = Path(path).read_text().strip()
        return int(text) if text.isdigit() else None
    except OSError:
        return None


def cgroup_usage(cid):
    """(CPU 누적 사용 시간(ns), 메모리 사용 바이트, 메모리 상한)."""
    base = _v2_dir(cid)
    if base is not None:
        cpu_ns = None
        try:
            for row in (base / "cpu.stat").read_text().splitlines():
                if row.startswith("usage_usec"):
                    cpu_ns = int(row.split()[1]) * 1000
                    break
        except OSError:
            pass
        mem = _read_int(base / "memory.current")
        raw_max = None
        try:
            raw_max = (base / "memory.max").read_text().strip()
        except OSError:
            pass
        limit = int(raw_max) if raw_max and raw_max.isdigit() else None
        return cpu_ns, mem, limit

    # cgroup v1
    cpu_ns = next((v for v in (_read_int(p.format(id=cid)) for p in _V1_CPU) if v), None)
    mem = next((v for v in (_read_int(p.format(id=cid)) for p in _V1_MEM) if v), None)
    limit = next((v for v in (_read_int(p.format(id=cid)) for p in _V1_MEM_MAX) if v), None)
    # v1 은 상한이 없을 때 터무니없이 큰 값을 넣는다
    if limit and limit > (1 << 62):
        limit = None
    return cpu_ns, mem, limit


def net_usage(pid):
    """컨테이너는 자체 네트워크 네임스페이스를 쓰므로, 그 프로세스의
    /proc/<pid>/net/dev 가 곧 컨테이너의 트래픽이다."""
    if not pid:
        return None, None
    try:
        rows = Path(f"/proc/{pid}/net/dev").read_text().splitlines()[2:]
    except OSError:
        return None, None
    rx = tx = 0
    for row in rows:
        name, _, rest = row.partition(":")
        if name.strip() == "lo":
            continue
        f = rest.split()
        if len(f) >= 9:
            rx += int(f[0])
            tx += int(f[8])
    return rx, tx


# --- 로그 ---------------------------------------------------------------


def _demux(data):
    """TTY 없이 실행된 컨테이너의 로그는 8바이트 헤더가 붙은 프레임으로 온다.
    [스트림종류, 0,0,0, 길이 4바이트] 순서다. TTY 가 있으면 헤더 없이 평문."""
    out = []
    i = 0
    framed = len(data) >= 8 and data[0] in (0, 1, 2) and data[1:4] == b"\x00\x00\x00"
    if not framed:
        return [("out", data.decode("utf-8", "replace"))]
    while i + 8 <= len(data):
        stream = data[i]
        size = int.from_bytes(data[i + 4:i + 8], "big")
        chunk = data[i + 8:i + 8 + size]
        out.append(("err" if stream == 2 else "out",
                    chunk.decode("utf-8", "replace")))
        i += 8 + size
    return out


_TS = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+Z?)\s?(.*)$")


def logs(cid, tail=120):
    """최근 로그 몇 줄. 시각과 stdout/stderr 구분을 함께 돌려준다."""
    if not LOGS_ENABLED:
        return None
    tail = max(1, min(int(tail), LOG_TAIL_MAX))
    raw = api(
        f"/containers/{cid}/logs?stdout=1&stderr=1&timestamps=1&tail={tail}",
        timeout=8, raw=True,
    )
    lines = []
    for stream, text in _demux(raw):
        for line in text.splitlines():
            if not line.strip():
                continue
            m = _TS.match(line)
            lines.append({
                "at": m.group(1) if m else None,
                "text": m.group(2) if m else line,
                "stream": stream,
            })
    return lines[-tail:]


# --- 수집 ---------------------------------------------------------------


class ContainerSampler:
    """컨테이너 목록과 사용량을 주기적으로 갱신한다. CPU 와 네트워크는 누적값이라
    직전 샘플과의 차이를 내야 하므로 이전 값을 들고 있는다."""

    def __init__(self):
        self.prev = {}      # cid -> (시각, cpu_ns, rx, tx)
        self.error = None
        self.ok = False

    def sample(self):
        try:
            listed = api("/containers/json?all=1", timeout=6)
        except Exception as exc:
            self.ok = False
            self.error = str(exc)
            return []

        self.ok = True
        self.error = None
        now = time.time()
        out = []
        seen = set()

        for item in listed:
            cid = item.get("Id", "")
            seen.add(cid)
            name = (item.get("Names") or ["/?"])[0].lstrip("/")
            state = item.get("State", "")

            # 라벨과 네트워크는 목록 응답에 이미 들어 있다. 그룹을 정하려고
            # 상세 조회를 한 번 더 하지는 않는다.
            labels = item.get("Labels") or {}
            networks = ((item.get("NetworkSettings") or {}).get("Networks") or {})
            group = group_of(name, labels.get("com.docker.compose.project"), networks)

            detail = {}
            try:
                detail = api(f"/containers/{cid}/json", timeout=5)
            except Exception:
                pass

            st = detail.get("State", {}) or {}
            pid = st.get("Pid") or 0
            health = ((st.get("Health") or {}).get("Status")) if st else None

            cpu_ns, mem, limit = (None, None, None)
            rx = tx = None
            if state == "running":
                cpu_ns, mem, limit = cgroup_usage(cid)
                rx, tx = net_usage(pid)

            prev = self.prev.get(cid)
            cpu_pct = rx_bps = tx_bps = None
            if prev and state == "running":
                elapsed = max(now - prev[0], 0.001)
                if cpu_ns is not None and prev[1] is not None:
                    delta = max(0, cpu_ns - prev[1])
                    # 100% = 코어 하나를 가득 쓴 상태. docker stats 와 같은 기준.
                    cpu_pct = round(delta / 1e9 / elapsed * 100, 1)
                if rx is not None and prev[2] is not None:
                    rx_bps = round(max(0, rx - prev[2]) / elapsed)
                    tx_bps = round(max(0, tx - prev[3]) / elapsed)
            self.prev[cid] = (now, cpu_ns, rx, tx)

            started = st.get("StartedAt") or ""
            out.append({
                "id": cid[:12],
                "full_id": cid,
                "name": name,
                "group": group,
                "image": (item.get("Image") or "").split("@")[0],
                "state": state,
                "status": item.get("Status", ""),
                "health": health,
                "restarts": detail.get("RestartCount", 0),
                "started_at": started,
                "uptime": _age(started) if state == "running" else None,
                "cpu": cpu_pct,
                "mem": mem,
                "mem_limit": limit,
                "rx_bps": rx_bps,
                "tx_bps": tx_bps,
                "exit_code": st.get("ExitCode") if state == "exited" else None,
            })

        for gone in set(self.prev) - seen:
            self.prev.pop(gone, None)
            _dir_cache.pop(gone, None)

        order = {"running": 0, "restarting": 1, "paused": 2, "created": 3, "exited": 4}
        rank = {g: i for i, g in enumerate(group_names())}
        out.sort(key=lambda c: (rank.get(c["group"], len(rank)),
                                order.get(c["state"], 5), c["name"]))
        return out


def _age(iso):
    """도커의 나노초 타임스탬프를 초 단위 경과 시간으로."""
    if not iso:
        return None
    try:
        text = iso.replace("Z", "+00:00")
        if "." in text:
            head, _, tail = text.partition(".")
            frac = tail[:6].ljust(6, "0")
            offset = tail[len(tail.rstrip("0123456789")):] if "+" in tail else ""
            plus = tail.find("+")
            offset = tail[plus:] if plus >= 0 else "+00:00"
            text = f"{head}.{frac}{offset}"
        from datetime import datetime, timezone
        started = datetime.fromisoformat(text)
        return max(0, (datetime.now(timezone.utc) - started).total_seconds())
    except Exception:
        return None
