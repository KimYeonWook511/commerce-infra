# 로그인 시도 차단 (fail2ban)

모니터링 페이지(`/status/`)는 인터넷에 열려 있고 비밀번호 하나로 막혀 있다.
누군가 자동으로 비밀번호를 대입하면 nginx 는 그때마다 실패를 기록만 하고 계속 받아준다.
fail2ban 은 그 기록을 세어 **실패가 반복되는 IP 를 방화벽에서 차단**한다.

`limit_req` 는 모든 요청을 똑같이 제한하므로 정상 사용자도 함께 느려진다.
fail2ban 은 **실패한 시도만** 세기 때문에 정상 사용자는 영향을 받지 않는다.

## 전제 — 로그가 파일로 남아야 한다

fail2ban 은 파일을 지켜본다. nginx 컨테이너는 로그를 stdout 으로도 보내지만
(`docker logs`), 그것만으로는 읽을 대상이 없다. `nginx.conf` 가 같은 로그를
`/var/log/nginx/host/` 에도 쓰고, 그 디렉토리는 `docker/nginx/log/` 로 마운트되어 있다.

```bash
ls -l docker/nginx/log/          # access.log, error.log
tail docker/nginx/log/error.log
```

기록되는 줄은 이런 형태다.

```text
[error] user "someone": password mismatch, client: 203.0.113.9, server: kyw511.ddns.net
```

아이디, 클라이언트 IP, 시각이 남는다. `access.log` 에는 401 응답과 User-Agent 가 남는다.

## 로그 회전

nginx 는 로그를 스스로 자르지 않는다. 두지 않으면 SD 카드가 찬다.
`/etc/logrotate.d/commerce-nginx` 를 만든다.

```text
/home/pi/commerce-platform/commerce-infra/docker/nginx/log/*.log {
    weekly
    rotate 8
    compress
    delaycompress
    missingok
    notifempty
    sharedscripts
    postrotate
        docker exec commerce-infra-nginx nginx -s reopen 2>/dev/null || true
    endscript
}
```

`nginx -s reopen` 이 없으면 nginx 가 잘려나간 옛 파일에 계속 쓴다.

```bash
sudo logrotate -d /etc/logrotate.d/commerce-nginx    # -d 는 검사만
```

## 설치

```bash
sudo apt install -y fail2ban
```

`/etc/fail2ban/jail.d/nginx-auth.conf` 를 만든다.

```ini
[nginx-http-auth]
enabled   = true
filter    = nginx-http-auth
logpath   = /home/pi/commerce-platform/commerce-infra/docker/nginx/log/error.log
maxretry  = 5
findtime  = 10m
bantime   = 1h

# 도커가 포워딩한 포트로 들어온 패킷은 INPUT 이 아니라 FORWARD 를 지난다.
# 기본값(INPUT)으로 두면 차단 로그만 남고 실제로는 그대로 들어온다.
banaction = iptables-multiport
chain     = DOCKER-USER

# 자기 자신을 가두지 않도록 내부 대역은 제외한다.
ignoreip  = 127.0.0.1/8 192.168.0.0/16 172.16.0.0/12
```

```bash
sudo systemctl enable --now fail2ban
sudo systemctl restart fail2ban
```

**`chain = DOCKER-USER` 가 이 설정의 핵심이다.** 이것을 빠뜨리면 fail2ban 이 정상
동작하는 것처럼 보이지만 차단이 되지 않는다.

## 확인

```bash
sudo fail2ban-client status                    # 활성 jail 목록
sudo fail2ban-client status nginx-http-auth    # 실패 횟수, 차단된 IP
sudo iptables -S DOCKER-USER                   # 실제로 규칙이 걸렸는지
```

일부러 틀린 비밀번호로 여섯 번 시도하면 차단되는 것을 볼 수 있다. 내부에서
시도하면 `ignoreip` 에 걸리므로, 확인은 휴대폰 데이터 같은 외부 회선에서 한다.

풀어줄 때:

```bash
sudo fail2ban-client set nginx-http-auth unbanip <IP>
```

## 남는 기록

| 무엇 | 어디 |
|---|---|
| 시도한 아이디·IP·시각 | `docker/nginx/log/error.log` |
| 401 응답과 User-Agent | `docker/nginx/log/access.log` |
| 차단·해제 이력 | `/var/log/fail2ban.log` |
| 현재 차단 중인 IP | `fail2ban-client status nginx-http-auth` |

누가 어디서 몇 번 시도했는지 보려면:

```bash
grep "password mismatch" docker/nginx/log/error.log \
  | grep -oE 'client: [0-9.]+' | sort | uniq -c | sort -rn
```
