FROM python:3.11-slim

WORKDIR /app

# 필요한 패키지 설치
# aiohttp 는 reservation_async(예약 실행 경로)에 필요하다 — 빠지면 api_server
# import 자체가 실패해 컨테이너가 즉시 죽는다.
RUN pip install --no-cache-dir flask requests beautifulsoup4 aiohttp gunicorn

# 소스 복사 — 빌드 컨텍스트는 레포 루트다 (docker-compose.yml 의 context: ..)
# api_server 가 import 하는 로컬 모듈 전부를 넣어야 한다:
#   api_server → config, reservation_async, reservation_http → utils
COPY config.py .
COPY utils.py .
COPY reservation_async.py .
COPY reservation_http.py .
COPY config.toml .
COPY api-server/api_server.py .

# accounts.txt(비밀번호)는 이미지에 넣지 않는다 — 볼륨으로 마운트한다.
# 없으면 config.ConfigError 로 즉시 죽으므로 조용히 잘못 뜨는 일은 없다.

# 포트 노출
EXPOSE 3100

# gunicorn으로 프로덕션 실행
CMD ["gunicorn", "--bind", "0.0.0.0:3100", "--workers", "2", "--timeout", "120", "api_server:app"]
