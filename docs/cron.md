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
