# API 서버 운영 (Docker)

n8n, cron 등 외부 시스템에서 HTTP로 예약을 자동화할 때 쓰는 REST API 서버.
Docker로 상시 띄워 두는 것을 전제로 하지만, 로컬에서 직접 실행해도 된다.

CLI로 직접 예약하는 방법은 [README](../README.md)를 본다. 이 문서는 API 서버만 다룬다.

## 목차

1. [실행](#1-실행)
2. [설정 파일 마운트](#2-설정-파일-마운트)
3. [예약 조건 설정](#3-예약-조건-설정)
4. [엔드포인트 레퍼런스](#4-엔드포인트-레퍼런스)
5. [n8n 연동](#5-n8n-연동)
6. [유용한 명령어](#6-유용한-명령어)
7. [트러블슈팅](#7-트러블슈팅)

---

## 1. 실행

### 포트는 실행 방식에 따라 다르다

| 실행 방식 | 포트 | 정하는 곳 |
|---|---|---|
| Docker | **3100** | `Dockerfile`의 `gunicorn --bind 0.0.0.0:3100` |
| 로컬 (`python3 api-server/api_server.py`) | **5000** | `config.toml`의 `[api] port` |

> ⚠️ **Docker는 `[api] port`를 읽지 않는다.** 컨테이너는 gunicorn으로 뜨고
> `api_server.py`의 `main()`을 거치지 않기 때문이다. Docker에서 포트를 바꾸려면
> `Dockerfile`의 `CMD`를 고치거나, 아래처럼 호스트 포트만 바꿔 매핑한다.
>
> 이 문서의 예시는 Docker 기준으로 `3100`을 쓴다. 로컬 실행 중이라면 `5000`으로 읽는다.

### Docker Compose (권장)

`docker-compose` 명령은 **`api-server/` 안에서** 실행한다 (`docker-compose.yml`이 여기 있다).
빌드 컨텍스트는 `context: ..`로 레포 루트를 가리키므로, 이미지에는 루트의
`config.py`·`reservation_*.py`가 함께 들어간다.

```bash
cd api-server
# 백그라운드 시작
docker-compose up -d

# 로그 확인
docker-compose logs -f

# 설정 변경 후 재시작
docker-compose restart

# 중지
docker-compose down

# Dockerfile 변경 시 재빌드
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```

### Docker 직접 실행

```bash
# 이미지 빌드 — 컨텍스트는 레포 루트, Dockerfile 만 api-server/ 에서 가져온다
docker build -f api-server/Dockerfile -t tennis-reservation .

# 실행
docker run -d \
  --name tennis-reservation \
  -p 3100:3100 \
  -e TZ=Asia/Seoul \
  -v /volume1/docker/apiserver/app/accounts.txt:/app/accounts.txt:ro \
  -v /volume1/docker/apiserver/app/config.toml:/app/config.toml:ro \
  tennis-reservation
```

### 로컬 실행 (Docker 없이)

```bash
# 레포 루트에서
python3 api-server/api_server.py                             # config.toml의 [api] host/port
python3 api-server/api_server.py --port 8080 --host 0.0.0.0  # 직접 지정
```

### 스모크 테스트

```bash
curl http://localhost:3100/health
curl http://localhost:3100/config
```

`/config`가 지금 로드된 계정·오픈 시각을 그대로 돌려주므로, 볼륨 마운트가
제대로 붙었는지 여기서 확인한다.

---

## 2. 설정 파일 마운트

API 서버는 요청 본문으로 예약을 받으므로 `reservation.json`(뷰어가 만드는 회차 계획)은
쓰지 않는다. 필요한 것은 둘이다.

| 파일 | 이미지에 | 이유 |
|------|---------|------|
| `config.toml` | **들어감** (비밀 없음) | 실행 파라미터. 볼륨으로 덮어써도 됨 |
| `accounts.txt` | **안 들어감** | 비밀번호가 있으므로 반드시 볼륨으로 넣는다 |

`accounts.txt`가 없으면 `config.ConfigError`로 즉시 죽으므로, 자격증명 없이
조용히 뜨는 일은 없다.

### 준비

```bash
# 설정 디렉토리 생성
mkdir -p /volume1/docker/apiserver/app

# 설정을 호스트로 복사 (최초 1회)
docker cp tennis-reservation:/app/config.toml /volume1/docker/apiserver/app/config.toml

# 계정 파일은 직접 만든다 (이미지에 없다)
printf '홍길동,your_id,your_password\n' > /volume1/docker/apiserver/app/accounts.txt
chmod 600 /volume1/docker/apiserver/app/accounts.txt
```

### docker-compose.yml 볼륨 설정

```yaml
services:
  tennis-reservation:
    environment:
      - TZ=Asia/Seoul
    volumes:
      - /volume1/docker/apiserver/app/accounts.txt:/app/accounts.txt:ro
      - /volume1/docker/apiserver/app/config.toml:/app/config.toml:ro
```

### 주의사항

- 볼륨 마운트 경로는 **절대 경로**를 쓴다.
- `accounts.txt`는 비밀번호가 들어 있으므로 `chmod 600` 으로 권한을 좁게 둔다.

---

## 3. 예약 조건 설정

```bash
vi /volume1/docker/apiserver/app/config.toml
```

수정 예시:

```toml
[schedule]
day    = 0    # 0 = 날짜 무관 즉시 실행 (API 서버는 보통 0)
hour   = 10
minute = 0

[network]
max_concurrent = 3

# accounts.txt (별도 파일)
#   홍길동,your_id,your_password
```

설정 파일 전체 구조 → [README](../README.md#2-설정-configtoml) 참조

### 수정 후 반드시 재시작

```bash
docker-compose restart
# 또는
docker restart tennis-reservation
```

Python 프로세스는 시작할 때만 `config.toml`을 읽는다. 수정 후 재시작하지
않으면 변경사항이 반영되지 않는다.

---

## 4. 엔드포인트 레퍼런스

| Method | Path | 설명 |
|--------|------|------|
| GET | `/health` | 헬스 체크 |
| GET | `/config` | 현재 설정 조회 |
| POST | `/check-login` | 로그인 테스트 |
| POST | `/check-slots` | 특정 날짜/코트 빈자리 확인 |
| POST | `/reserve` | 예약 실행 (복수) |
| POST | `/reserve-single` | 단건 예약 |
| POST | `/search-weekend` | 주말 빈자리 검색 |
| POST | `/search-all` | 전체 날짜 빈자리 검색 |

모든 요청에서 `user_id`/`user_pw`를 생략하면 **`config.toml`의 첫 번째 계정**을 쓴다.

### POST /reserve — 복수 예약

**방법 2: 직접 지정 (권장)**

```bash
curl -X POST http://localhost:3100/reserve \
  -H "Content-Type: application/json" \
  -d '{
    "reservations": [
      {"date": "2026-02-09", "hour": 8,  "court": 1},
      {"date": "2026-02-09", "hour": 6,  "court": 2},
      {"date": "2026-02-16", "hour": 10, "court": 3}
    ],
    "test_mode": true
  }'
```

**방법 1: 조합형 (dates × hours × courts)**

```bash
curl -X POST http://localhost:3100/reserve \
  -H "Content-Type: application/json" \
  -d '{
    "dates": ["2026-02-09"],
    "hours": [8, 10],
    "courts": [1, 2, 3],
    "test_mode": true
  }'
```

**방법 3: 코트별 시간대**

```bash
curl -X POST http://localhost:3100/reserve \
  -H "Content-Type: application/json" \
  -d '{
    "dates": ["2026-02-09", "2026-02-16"],
    "court_schedules": [
      {"court": 1, "hours": [8, 10]},
      {"court": 2, "hours": [6, 8]},
      {"court": 3, "hours": [10, 12]}
    ],
    "test_mode": true
  }'
```

> 방법 1·3의 `dates`·`hours`·`courts`는 **요청 본문에 반드시 넣어야 한다.**
> `config.toml`은 이 조합형 표기를 지원하지 않으므로 기본값을 제공하지 않는다
> (비어 있으면 400). 방법 2의 `reservations`만 CLI와 형식을 공유한다.

### POST /reserve-single — 단건 예약

```bash
curl -X POST http://localhost:3100/reserve-single \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-02-09", "hour": 8, "court": 1, "test_mode": true}'
```

### POST /check-login — 로그인 테스트

```bash
curl -X POST http://localhost:3100/check-login
```

### POST /check-slots — 빈자리 확인

```bash
curl -X POST http://localhost:3100/check-slots \
  -H "Content-Type: application/json" \
  -d '{"date": "2026-02-09", "court": 3}'
```

### POST /search-weekend, /search-all — 빈자리 검색

```bash
# 주말 (기본: 모든 코트, 6/8/10시)
curl -X POST http://localhost:3100/search-weekend \
  -H "Content-Type: application/json" \
  -d '{"year": 2026, "month": 2}'

# 코트/시간 지정
curl -X POST http://localhost:3100/search-weekend \
  -H "Content-Type: application/json" \
  -d '{"year": 2026, "month": 2, "courts": [1, 2], "hours": [8, 10]}'

# 전체 날짜
curl -X POST http://localhost:3100/search-all \
  -H "Content-Type: application/json" \
  -d '{"year": 2026, "month": 2}'
```

| 항목 | `/search-weekend` | `/search-all` |
|------|----------------|------------|
| 검색 날짜 | 주말만 (토/일) | 모든 날짜 |
| 검색 시간 | 지정 가능 (기본 6·8·10시) | 전체 (6~20시) |
| 속도 | 빠름 | 느림 |
| `is_weekend` 필드 | 없음 | 있음 |
| `skipped_dates` 필드 (휴장 추정일) | 없음 | 있음 |

```json
{
  "year": 2026, "month": 2, "total": 15,
  "results": [
    {"date": "2026-02-01", "day": "토", "court": 1, "hour": 6, "time": "06:00~08:00"}
  ]
}
```

### 응답 형식 (예약)

```json
{
  "success": true,
  "summary": "3/4건 성공",
  "results": [
    {"date": "2026-02-09", "hour": 8,  "court": 1, "success": true,  "message": "대관접수 완료"},
    {"date": "2026-02-09", "hour": 10, "court": 1, "success": false, "message": "이미 예약된 시간"}
  ]
}
```

| message | 의미 |
|---------|------|
| `대관접수 완료` | 예약 성공 |
| `이미 예약 있음 (1일 1건 제한)` | 해당 날짜에 이미 1건 예약됨 |
| `이미 예약된 시간` | 해당 시간대 이미 마감 |
| `예약 마감` | 해당 날짜 예약 마감 |
| `시간 데이터 오류` | 잘못된 슬롯 값 |
| `알 수 없는 서버 응답` | 로그에서 `proc.php` 응답 내용 확인 |

### 오픈 시각 자동화

API 서버는 `wait_for_open=False`로 동작하므로 **즉시 실행**된다. 정확한 시각에
맞추려면 외부 cron으로 호출한다.

```bash
# 매월 25일 10:00
0 10 25 * * curl -X POST http://localhost:3100/reserve \
  -H "Content-Type: application/json" \
  -d '{"reservations":[{"date":"2026-03-01","hour":8,"court":1}],"test_mode":false}'
```

---

## 5. n8n 연동

n8n과 같은 Docker 네트워크에서 실행 시:

```bash
docker network create tennis-net
docker run -d --network tennis-net --name tennis-reservation ...
docker run -d --network tennis-net --name n8n ...
```

n8n HTTP Request 노드 설정:
- URL: `http://tennis-reservation:3100/reserve`
- Method: `POST`
- Body:
  ```json
  {
    "reservations": [
      {"date": "2026-02-09", "hour": 8, "court": 1}
    ],
    "test_mode": false
  }
  ```

---

## 6. 유용한 명령어

```bash
# 실행 중인 컨테이너 확인
docker ps

# 컨테이너 내부 접속
docker exec -it tennis-reservation /bin/bash

# 실시간 로그
docker logs -f tennis-reservation

# 컨테이너 재시작
docker restart tennis-reservation

# 이미지 재빌드 (레포 루트에서)
docker build --no-cache -f api-server/Dockerfile -t tennis-reservation .

# 이미지 삭제
docker rmi tennis-reservation
```

---

## 7. 트러블슈팅

### 포트가 이미 사용 중

컨테이너 내부 포트(3100)는 그대로 두고 호스트 포트만 바꾼다.

```bash
docker run -d -p 8080:3100 tennis-reservation
```

### 컨테이너가 즉시 종료됨

```bash
docker logs tennis-reservation
# 또는 bash로 직접 진입
docker run -it --entrypoint /bin/bash tennis-reservation
```

원인은 보통 둘 중 하나다.

- `config.ConfigError` — `config.toml`이 잘못됐다. 로그에 어느 섹션·어느 항목인지
  찍히므로 그 위치를 고친다.
- `ModuleNotFoundError` — `api_server.py`가 import 하는 로컬 모듈이 이미지에
  안 들어갔다. 필요한 것은 `config.py`·`utils.py`·`reservation_async.py`·
  `reservation_http.py` 넷이며, `Dockerfile`의 `COPY` 목록과 맞는지 확인한다.

### config.toml 수정이 반영되지 않음

```bash
docker-compose restart
```

### "No such file or directory" (볼륨 마운트 시)

호스트 경로에 파일이 있는지 확인:

```bash
ls -l /volume1/docker/apiserver/app/config.toml
# 없으면
docker cp tennis-reservation:/app/config.toml /volume1/docker/apiserver/app/config.toml
```

### 이미지 강제 재빌드

```bash
docker-compose down
docker-compose build --no-cache
docker-compose up -d
```
