# commerce-infra

운영 서버의 인프라를 Docker Compose로 정의한 저장소. nginx(리버스 프록시)·MySQL·Redis·Kafka와 SSL 인증서 발급/갱신을 관리한다.
애플리케이션 컨테이너는 여기서 정의하지 않고, 이 저장소가 만든 `commerce-network`에 붙는다.

## 구성

| 서비스 | 이미지 | 호스트 포트 |
|---|---|---|
| nginx | `nginx:1.25` | `80`, `443` |
| mysql | `mysql:8.0` | `127.0.0.1:3306` |
| redis | `redis:7` | `127.0.0.1:6379` |
| kafka | `apache/kafka:4.1.1` (KRaft 단일 노드) | `127.0.0.1:9092` |

nginx를 제외한 나머지는 루프백에만 바인딩한다. 외부에서 붙으려면 SSH 터널을 쓴다.

```bash
ssh -N -L 3306:127.0.0.1:3306  <user>@<server>   # MySQL
ssh -N -L 6379:127.0.0.1:6379  <user>@<server>   # Redis
ssh -N -L 19092:127.0.0.1:9092 <user>@<server>   # Kafka (클라이언트는 localhost:19092로 접속)
```

## 디렉토리

```text
docker/
├── docker-compose.infra.yml    # 상시 실행 서비스
├── docker-compose.certbot.yml  # certbot (필요할 때만 run)
├── .env.example                # MySQL 계정 템플릿
├── nginx/
│   ├── nginx.conf
│   ├── conf.d/                 # 도메인별 server 블록 (http는 301 리다이렉트, https는 프록시/정적)
│   └── html/                   # 루트 도메인 정적 페이지
├── certbot/
│   ├── conf/                   # 인증서·개인키 (gitignore)
│   └── www/                    # ACME challenge 경로
└── scripts/
    ├── cert-issue.sh           # 최초 발급 (수동 1회)
    └── cert-renew.sh           # 갱신 (cron 대상)
docs/cron.md                    # 인증서 자동 갱신 운영 문서
```

## 도메인

no-ip 무료 DDNS를 쓴다. 공인 IP 갱신은 공유기의 DDNS 클라이언트가 맡는다.

- `kyw511.ddns.net` → nginx 정적 페이지
- `kyw511.ddns.net/status/` → 모니터링 페이지 (비밀번호)
- `api.kyw511.ddns.net` → 백엔드 프록시 (**현재 미사용**)

인증서는 `certbot/conf/live/kyw511.ddns.net/` 하나이며 루트 도메인만 담는다.

api 서브도메인은 no-ip 무료 플랜으로 만들 수 없다 — 호스트명이 1개로 제한되고 와일드카드가 유료다.
DNS 레코드가 없으면 ACME challenge가 실패해 인증서 전체가 발급되지 않으므로 `cert-issue.sh`의 `DOMAINS`에서 제외했다.
`conf.d/api.*.conf`는 남겨두었으니, 서브도메인을 확보하면 `DOMAINS`에 도메인을 더해 재발급하는 것으로 되살아난다.

## 서버 구축 순서

```bash
# 1. 환경 변수 준비
cd docker
cp .env.example .env   # MySQL 계정 값 채우기

# 2. 모니터링 페이지 비밀번호 (없으면 nginx가 기동하지 못한다)
htpasswd -cB nginx/.htpasswd-pistat <아이디>

# 3. 인프라 기동 (commerce-network가 이때 생성된다)
docker compose -f docker-compose.infra.yml up -d

# 4. SSL 인증서 최초 발급
./scripts/cert-issue.sh

# 5. 인증서 자동 갱신 cron 등록 → docs/cron.md

# 6. 애플리케이션 배포 (별도 저장소)
#    컨테이너 이름 commerce-backend, 네트워크 commerce-network를 external로 참조해야
#    nginx의 proxy_pass가 해석된다.
```

인증서 갱신 cron 설정은 [docs/cron.md](docs/cron.md) 참고.

## 모니터링

`/status/` 는 호스트에서 도는 모니터링 프로세스(`8088`)로 넘어간다. 그 프로세스는
TLS 도 비밀번호도 처리하지 않으므로, 여기서 `auth_basic` 으로 막고 `limit_req` 로
자동 대입을 차단한다. `/status/api/health` 만 외부 감시 서비스를 위해 인증을 뺀다.

**`8088` 을 포트포워딩하면 비밀번호를 통째로 건너뛰게 된다.** 공유기에서 여는 것은
`80` 과 `443` 뿐이어야 한다.
