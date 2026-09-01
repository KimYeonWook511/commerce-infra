#!/usr/bin/env bash
set -euo pipefail

# ========== 설정 ==========
EMAIL="kimyeonwook511@gmail.com"
# api 서브도메인은 no-ip 무료 플랜으로 만들 수 없어 제외했다 (호스트명 1개 제한, 와일드카드 유료).
# 서브도메인을 확보하면 -d api.kyw511.ddns.net 를 추가하고 재발급할 것.
DOMAINS=(-d kyw511.ddns.net)

# 이 스크립트가 위치한 디렉토리
# $0: 현재 실행 중인 스크립트의 경로 (./scripts/cert-renew.sh)
# dirname "$0": 파일 경로에서 디렉토리 부분만 추출 (./scripts)
# && pwd: 앞에 cd가 성공하면 pwd 실행
BASE_DIR="$(cd "$(dirname "$0")/.." && pwd)"



# ========== 실행 ==========
cd "${BASE_DIR}"

# -d: 백그라운드 실행
# nginx 서비스가 떠 있으면 그대로(무반응), 없으면 띄움
docker compose -f docker-compose.infra.yml up -d nginx

# run: certbot은 데몬이 아니라 CLI 도구이기 때문에 run
# --rm: 실행 끝나면 컨테이너 자동 삭제
# certbot: compose 파일에 정의된 서비스 이름
# certonly: “인증서만 발급하고, 웹서버 설정은 건드리지 마라”
docker compose -f docker-compose.certbot.yml run --rm certbot certonly \
  --webroot \
  --webroot-path=/var/www/certbot \
  "${DOMAINS[@]}" \
  --agree-tos \
  --email "${EMAIL}" \
  --no-eff-email
  # HTTP-01 ACME challenge 방식
  # certbot이 토큰 파일을 만들 디렉토리 (컨테이너에서의 경로임)
  # 도메인 목록 (배열)
  # Let’s Encrypt 약관 자동 동의
  # 인증서 관련 연락용 이메일
  # EFF(전자프런티어재단) 홍보 메일 수신 거부

docker compose -f docker-compose.infra.yml restart nginx
