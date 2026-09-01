#!/usr/bin/env bash
set -euo pipefail

# ========== 설정 ==========

# 이 스크립트가 위치한 디렉토리
# $0: 현재 실행 중인 스크립트의 경로 (./scripts/cert-renew.sh)
# dirname "$0": 파일 경로에서 디렉토리 부분만 추출 (./scripts)
# && pwd: 앞에 cd가 성공하면 pwd 실행
BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"



# ========== 실행 ==========
cd "${BASE_DIR}"
# renew: 이미 발급된 인증서들을 검사. 만료가 임박한 것만 자동으로 갱신
docker compose -f docker-compose.certbot.yml run --rm certbot renew
docker compose -f docker-compose.infra.yml restart nginx
