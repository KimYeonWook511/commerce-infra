# 서버 모니터링 (pistat)

`/proc` 과 `/sys` 를 직접 읽어 CPU·메모리·디스크·온도·네트워크를 그래프로 그리고,
컨테이너의 상태·재시작 횟수·자원 사용량·로그를 함께 본다. 최근 한 시간을
메모리에 담아 하나의 시간축 위에 겹쳐 그려서, 세로로 훑으면 같은 순간에 모든
지표가 무엇을 하고 있었는지 한 번에 읽힌다.

pip 패키지도 CDN 도 쓰지 않는다. 표준 라이브러리와 단일 HTML 파일뿐이다.

## 구성

```text
인터넷 ──443──▶ nginx (컨테이너)
                 │  /status/  auth_basic
                 ▼  host.docker.internal:8088
              pistat (호스트, systemd)
                 │  127.0.0.1:2375
                 ▼
           docker-socket-proxy (컨테이너)
                 │  읽기 전용
                 ▼
           /var/run/docker.sock
```

pistat 은 `/proc`·`/sys`·`vcgencmd` 를 읽어야 해서 컨테이너가 아니라 호스트에서
돈다. nginx 는 컨테이너 안이라 호스트로 나가는 이름이 필요하고, 그것을
`extra_hosts` 로 준다.

**pistat 은 TLS 도 비밀번호도 처리하지 않는다.** 인증은 전부 nginx 가 맡는다.
설정은 `docker/nginx/conf.d/root.https.conf` 의 `/status/` 블록에 있다.

## 설치

```bash
cd docker

# 1. 비밀번호 파일 (없으면 nginx 가 기동하지 못한다)
ID=원하는아이디
read -rsp "비밀번호: " PW && echo
docker run --rm httpd:alpine htpasswd -nbB "$ID" "$PW" > nginx/.htpasswd-pistat
unset PW

# 2. 소켓 프록시는 인프라 compose 에 포함되어 있다
docker compose -f docker-compose.infra.yml up -d
curl -s 127.0.0.1:2375/_ping     # OK 가 나오면 정상

# 3. 방화벽에서 nginx 가 모니터로 나가는 경로를 연다
#    네트워크가 생긴 뒤라야 게이트웨이 주소가 존재한다
sudo ufw allow from 172.18.0.0/16 to 172.18.0.1 port 8088 proto tcp \
  comment 'pistat from commerce-network'

# 4. 모니터 등록
sudo cp pi-monitoring/pistat.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now pistat
systemctl status pistat
```

`pi` 계정이 아니거나 저장소 위치가 다르면 유닛 파일의 `User=` 와 경로를 고친다.

비밀번호는 컨테이너로 만든다. 해시 한 줄 만들자고 호스트에 웹서버 패키지를 깔 이유가
없다. `htpasswd` 가 이미 있으면 `htpasswd -cB nginx/.htpasswd-pistat "$ID"` 로도 같은
결과가 나온다. 계정을 더하려면 `>` 대신 `>>` 를 쓴다.

nginx 는 요청마다 이 파일을 읽으므로 계정을 바꿔도 재시작이 필요 없다. 다만 편집기로
저장하면 파일이 새 것으로 바뀌어 컨테이너가 옛 내용을 계속 볼 수 있다. `>` 리다이렉트로
쓰거나, 편집기를 썼다면 nginx 를 재시작한다.

컨테이너에서 호스트로 가는 연결도 방화벽의 INPUT 을 거친다. 루프백이 아니라 브리지를
타기 때문이다. 3번을 빠뜨리면 브라우저에 504 가 뜬다. 출발지·목적지·포트를 모두 좁혔으므로
외부나 같은 공유기의 기기에는 열리지 않는다.

포트가 이미 쓰이고 있지 않은지 `ss -tlnp | grep 8088` 로 확인한다. 겹치면 `PISTAT_PORT`
를 바꾸고 방화벽 규칙과 `root.https.conf` 의 `proxy_pass` 도 같이 맞춘다.

`PISTAT_HOST` 는 도커 브리지 주소여야 한다. 컨테이너의 `127.0.0.1` 은 호스트가
아니라서, 루프백에 바인딩하면 nginx 가 닿지 못한다. 주소는
`ip -4 addr show docker0` 로 확인한다.

## 프로젝트별로 나눠 보기

한 호스트에서 여러 프로젝트를 돌리면 컨테이너 목록이 뒤섞인다. 유닛 파일의
`PISTAT_GROUPS` 로 묶는다.

```bash
Environment=PISTAT_GROUPS=commerce:commerce-|crocobird:crocobird-,@crocobird
```

그룹은 `|` 로, 한 그룹의 규칙은 `,` 로 나눈다. 규칙은 세 가지다.

| 표기 | 기준 |
|---|---|
| `commerce-` | 컨테이너 이름 접두사 |
| `@commerce` | compose 프로젝트 이름 (`name:` 또는 디렉토리명) |
| `#commerce-network` | 도커 네트워크 이름 |

위에서부터 순서대로 맞춰보고, 어디에도 안 맞는 컨테이너는 `기타` 로 간다.

**선언한 그룹은 컨테이너가 하나도 없어도 화면에 남는다.** 아직 만들지 않은
프로젝트를 미리 선언해두면 그 자리가 비어 보이고, 나중에 컨테이너를 띄웠는데
계속 비어 있으면 이름 규칙이 어긋났다는 뜻이 된다. 규칙에 안 맞는 컨테이너가
`기타` 로 조용히 흘러가는 것을 알아채기 위한 장치다.

`PISTAT_GROUPS` 를 비우면 목록 하나로 나온다. 그룹 이름을 누르면 접힌다.

## 설정값

유닛 파일에서 지정한다.

| 변수 | 기본값 | 의미 |
|---|---|---|
| `PISTAT_HOST` | `127.0.0.1` | 바인딩 주소 |
| `PISTAT_PORT` | `8088` | 바인딩 포트 |
| `PISTAT_INTERVAL` | `5` | 샘플 간격(초) |
| `PISTAT_HISTORY_MIN` | `60` | 보관할 기록(분) |
| `PISTAT_DISK` | `/` | 용량을 표시할 파일시스템 |
| `PISTAT_DOCKER` | `tcp://127.0.0.1:2375` | 도커 접속 대상 |
| `PISTAT_LOGS` | `1` | 컨테이너 로그 보기 (`0` 이면 끔) |
| `PISTAT_LOG_TAIL` | `300` | 한 번에 가져올 로그 최대 줄 수 |
| `PISTAT_GROUPS` | (없음) | 프로젝트별 그룹 규칙 |
| `PISTAT_GROUP_OTHER` | `기타` | 어느 그룹에도 안 맞는 컨테이너의 이름 |

기록은 메모리에만 있어서 서비스를 재시작하면 사라진다. 의도한 동작이다. 한
시간치가 200KB 남짓인데, 5초마다 SD 카드에 쓰는 건 카드 수명을 깎는 지름길이다.

## 엔드포인트

`/status/` 아래에 붙는다.

| 경로 | 응답 |
|---|---|
| `/` | 대시보드 |
| `/api/status` | 최신 샘플, 장비 정보, 스로틀 플래그 |
| `/api/history` | 보관 중인 기록. `?since=<epoch>` 로 자를 수 있음 |
| `/api/containers` | 컨테이너 목록과 사용량, 선언된 그룹 |
| `/api/logs?id=&tail=` | 컨테이너 로그 |
| `/api/health` | `ok` 한 단어. 인증 없이 열려 있다 |

## 노출에 대한 주의

`commerce-network` 의 대역은 `docker-compose.infra.yml` 의 `ipam` 으로 고정한다. 도커가
대역을 자동으로 고르게 두면 네트워크를 다시 만들 때 값이 바뀌어, 모니터가 바인딩한
주소와 nginx 가 찾아가는 주소가 어긋난다.

**`8088` 과 `2375` 를 포트포워딩하면 안 된다.** 둘 다 인증이 없다. 공유기에서
여는 것은 `80` 과 `443` 뿐이어야 한다. `8088` 에 직접 닿을 수 있으면 비밀번호를
통째로 건너뛰고, `2375` 에 닿을 수 있으면 컨테이너 정보와 로그가 그대로 열린다.

비밀번호 자동 대입은 `limit_req` 가 속도만 늦춘다. 실패한 시도만 골라 차단하려면
[fail2ban.md](fail2ban.md) 를 따른다.

**컨테이너 로그에는 환경변수 덤프, 접속 정보, 스택 트레이스가 섞여 들어갈 수
있다.** 이 페이지를 다른 사람과 공유하면 그 사람도 로그를 전부 보게 된다. 로그만
끄려면 유닛 파일에서 `PISTAT_LOGS=0` 으로 바꾸고 재시작한다. 나머지 지표는 그대로
나온다.

로그 조회는 현재 목록에 있는 컨테이너 ID 만 허용한다. 임의의 경로가 도커 API 로
흘러들어가지 않게 하기 위해서다.

소켓 프록시는 GET 과 컨테이너 조회만 통과시킨다. 생성·중지·삭제·명령 실행은 전부
막혀 있다. 소켓에 직접 접근할 수 있으면 특권 컨테이너를 띄워 호스트를 장악할 수
있어, 소켓 접근 권한은 사실상 root 권한이기 때문이다.

## 서버가 죽는 걸 감지하려면

이 페이지는 파이와 함께 죽기 때문에 파이가 내려간 걸 알려줄 수 없다. 외부
서비스를 `https://kyw511.ddns.net/status/api/health` 에 걸어둔다. UptimeRobot
무료 요금제가 5분마다 확인하고 응답이 끊기면 메일을 보내준다. 이 경로만 인증을
빼둔 것이 그래서다.

## 온도와 스로틀 표시에 대해

`vcgencmd get_throttled` 는 전압 부족 플래그를 알려준다. 파이가 이상하게 동작하는
가장 흔한 원인이 이것이다. 전원이 약하면 갑작스러운 재부팅, SD 카드 손상, 원인
모를 느려짐으로 나타나는데 전부 소프트웨어 문제처럼 보인다. 전압 부족 경고가 뜨면
다른 걸 보기 전에 어댑터부터 교체한다. 5V 3A 어댑터에 짧고 굵은 케이블이면 된다.

온도가 80°C 를 넘으면 CPU 가 스로틀링을 시작한다. 방열판을 붙이면 해결된다.

## CPU 와 메모리를 읽는 방법

도커의 `stats` API 는 컨테이너 하나당 1~2초가 걸려 5초 주기에 맞지 않는다. 그래서
수치는 `/sys/fs/cgroup` 에서 직접 읽고, 이름과 상태는 API 에서 가져와 컨테이너 ID
로 합친다. cgroup v1 과 v2, systemd 와 cgroupfs 드라이버를 모두 찾아본다.

CPU 는 코어 하나를 가득 쓴 상태가 100% 다. `docker stats` 와 같은 기준이라
4코어 파이에서는 400% 까지 올라갈 수 있다.
