# SSL 인증서 자동 갱신 (cron)

본 프로젝트는 Let’s Encrypt + certbot을 사용하여
SSL 인증서를 발급 및 자동 갱신한다.

인증서 갱신은 OS 수준의 cron을 통해 수행되며,
cron이 실행할 스크립트와 인프라 구성은 GitHub(IaC)로 관리한다.

---

## 개요

- 인증서 발급: 수동 1회 실행 (`cert-issue.sh`)
- 인증서 갱신: cron을 통해 주기 실행 (`cert-renew.sh`)
- 갱신 방식: certbot `renew`
- 갱신 후 처리: nginx 재시작으로 인증서 반영
- 스크립트 실행 권한 필수로 줄 것!

---

## 관련 파일 구조

```text
commerce-infra/
├── docker/
│   ├── docker-compose.infra.yml
│   ├── docker-compose.certbot.yml
│   ├── scripts/
│   │   ├── cert-issue.sh   # 최초 인증서 발급 (수동)
│   │   └── cert-renew.sh   # 인증서 갱신 (cron 대상)
│   └── certbot/
│       ├── conf/           # 인증서 및 개인키 (gitignore)
│       └── www/            # ACME challenge 경로
└── docs/
    └── cron.md
```

---

## cron 등록

`crontab -e` 로 아래를 등록한다. 경로는 서버에 배치한 실제 경로로 맞춘다.

```cron
PATH=/usr/local/bin:/usr/bin:/bin
@reboot sleep 120; /home/pi/commerce-platform/commerce-infra/docker/scripts/cert-renew.sh >> /home/pi/cert-renew.log 2>&1
0 4 * * 1 /home/pi/commerce-platform/commerce-infra/docker/scripts/cert-renew.sh >> /home/pi/cert-renew.log 2>&1
```

- `PATH` — cron은 로그인 셸이 아니라 PATH가 거의 비어 있다. 없으면 스크립트 안의 `docker` 를 찾지 못하고 실패한다.
- `@reboot` — cron은 꺼져 있는 동안 지나간 실행을 따라잡지 않는다. 부팅 직후 한 번 확인해 그 공백을 메운다. `sleep 120` 은 도커 데몬이 준비될 때까지 기다리는 시간.
- 로그 리다이렉트 — cron은 실패해도 화면에 알리지 않는다. 로그가 없으면 갱신이 멈춘 것을 만료될 때까지 모른다.

주 1회로 충분하다. certbot `renew` 는 만료 30일 이내인 인증서만 갱신하므로, 한두 번 걸러도 복구할 여유가 남는다.

## 확인

```bash
crontab -l
./scripts/cert-renew.sh   # 수동 실행이 에러 없이 끝나면 cron에서도 돈다
```
