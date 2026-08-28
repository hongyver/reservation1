# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

고양시 테니스장(대화문화체육센터) 자동 예약 프로그램. 매월 25일 10시에 오픈되는 예약을 자동으로 처리.

실행 모드:
- **HTTP 모드** (기본): `aiohttp` + `BeautifulSoup`으로 브라우저 없이 직접 HTTP 요청 (asyncio 기반)
- **브라우저 모드**: `selenium`으로 Chrome 브라우저 자동화 (`--browser` 플래그)
- **다중 계정 모드**: `main.py`(런처)로 N개 계정을 터미널 분할 창에서 동시 실행 (`--tmux`로 tmux 사용)
- **예약 현황 뷰어**: `viewer.py`로 달력 UI에서 예약 확인·편집·저장

## 실행 명령어

전체 명령과 플래그는 [README](README.md#3-실행)에 있다. 여기에는 코드를 고칠 때
쓰는 것만 적는다.

```bash
# accounts.txt 에 '이름,아이디,비밀번호' 를 한 줄씩 (최초 1회)
python3 main.py --dry-run              # 실행될 자식 명령 확인 (창 미생성)
python3 main.py --test 300             # 전 계정, 300초 후 오픈으로 정각 흐름 재현
python3 reserve.py --account user1 --test  # 워커 단독 디버깅 (런처 우회)
python3 viewer.py                      # 예약 배정 달력
```

`main.py`가 유일한 사용자 진입점이고 `reserve.py`는 워커다. 워커를 직접 실행하는
경우는 런처를 우회해 디버깅할 때뿐이다.

### `--test` 한 플래그로 두 가지 검증

`--test`는 **항상 `proc.php` 직전에 멈춘다.** 값 유무가 오픈 시각만 가른다.

| 명령 | 오픈 시각 | 쓰임 |
|------|----------|------|
| `--test` | 즉시 (`RESERVATION_DAY=0` 강제) | 로그인·슬롯 파싱·폼 조립을 아무 날에나 바로 확인 |
| `--test 300` / `--test 10:45` | 그 시각으로 강제 | 대기 → T-20s/T-4s 예열 → 정각 발사까지 실전대로 재현 |

값 없는 `--test`가 `day=0`을 강제하는 이유: 이전에는 오픈일(25일)이 아니면
`wait_for_reservation_open_async()`가 False를 돌려줘 **로그인만 하고 중단**됐다.
날짜에 따라 동작이 갈리지 않도록 "값 없으면 즉시"로 고정했다.

런처는 값이 있을 때 상대 초를 절대 `HH:MM`으로 **한 번만** 변환해 전 계정에
같은 값을 넘긴다 (계정마다 오픈 시각이 어긋나지 않도록).

## 아키텍처

```
main.py              # 진입점(런처). 전 계정을 터미널 분할 창에서 동시 실행
                     # 각 pane에서 `reserve.py --account <아이디>` 를 띄운다
reserve.py           # 단일 계정 실행. argparse로 모드 분기, 자격증명 수집
                     # 로그인 테스트·빈자리 검색·브라우저 모드도 여기
accounts.txt         # 이름,아이디,비밀번호 — 비밀번호는 여기에만. gitignore 대상
config.toml          # 실행 파라미터 (오픈시각·네트워크·사이트 상수). 비밀 없음 → 커밋 대상
reservation.json     # 이번 회차 예약 계획 (일련번호+아이디+슬롯). gitignore 대상
config.py            # config.toml + accounts.txt 로더
                     #   load_accounts() / find_account(id) — pw 조회의 유일한 경로
schedule.py          # reservation.json 로드·저장. 아이디 전수 검증, no 재부여
reservation_async.py # 핵심 HTTP 예약 로직 (asyncio + aiohttp)
utils.py             # 예약 타이밍 대기 함수 (동기 + 비동기) + 테스트 오픈 시각 파싱
reservation_http.py  # 구 HTTP 예약 로직 (requests) — fallback 보존
reservation.py       # Selenium 브라우저 모드 (선택 실행)
api-server/          # API 서버 일습 (Flask + Docker). 공유 모듈은 루트를 import 한다
  api_server.py      #   Flask API 서버 (n8n / 외부 호출용)
  Dockerfile         #   빌드 컨텍스트는 레포 루트 (config.py·reservation_* 필요)
  docker-compose.yml #   context: .. / dockerfile: api-server/Dockerfile
viewer.py            # 예약 현황 달력 뷰어 (HTTP 서버 내장, tomlkit으로 config.toml 저장)
```

### 예약 실행 흐름 (`reservation_async.py`)

1. `_build_tasks()` → `config.RESERVATION_CONFIG`(= 해당 계정의 `reservations`)에서 작업 목록 조립
2. **Phase 1** — 로그인 전 대기 (`wait_before_login_async`): 오픈 10분 전까지 asyncio.sleep
3. **Phase 2** — N개 봇 병렬 로그인 (`asyncio.gather`): 각 예약 건마다 독립 세션(PHPSESSID) 생성
4. **Phase 3** — 오픈 시간 정밀 대기 (`wait_for_reservation_open_async`): 마지막 10초는 10ms 단위.
   keepalive 만료 대비로 오픈 직전 2회 warmup 트리거 (fire-and-forget, 봇별 지터로 분산):
   - 1차(남은 20초): `warmup_target_page()` — 대상 페이지 조회로 연결 예열 (지터 0~1초, 타임아웃 8초)
   - 2차(남은 4초): 같은 대상 페이지 재조회 — 연결 재예열 (지터 0~0.3초, 타임아웃 3초).
     메인 페이지 GET은 PHP 세션의 "마지막 조회 페이지" 상태를 덮어쓸 수 있어 쓰지 않는다
5. **Phase 4** — `asyncio.gather` + `Semaphore(MAX_CONCURRENT)`로 동시 예약 실행

각 예약 봇의 내부 흐름:
1. `get_reservation_page()` → 대상 코트/날짜 페이지 HTML 조회
2. `get_available_slots()` → BeautifulSoup으로 가능 슬롯 파싱
3. `submit_reservation()` → 3단계 제출:
   - 1단계는 `reserve()`가 조회한 HTML → 재조회 순으로 폼 확보 (재사용은 1회용, 재시도 시 재조회)
   - POST `rent_period_apply.php` → `DocumentForm` + `useForm` 필드 수집
   - POST `rent_period_proc.php` → 최종 대관신청

### 슬롯 값(`rent_chk[]`) 형식 — 예약 가능 여부가 값 자체에 인코딩된다

| 값 | 형태 | 의미 |
|-----|------|------|
| `06000800` | 8자리 `HHMMHHMM` | **이미 예약됨** ("일정있음") — 신청 불가 |
| `1400160076` | 8자리 + 코트×시간대 슬롯 ID | **예약 가능** — 신청용 값 |

신청 가능한 슬롯에만 뒤에 슬롯 ID가 붙는다 (C1: 06시→29·08시→30…, C3: 14시→76,
C4: 14시→100 — 코트×시간대별 고정값이나 하드코딩하지 않는다).
`get_available_slots()`는 `len(value) > 8`로 이를 걸러낸다.

> ⚠️ **정각 크리티컬 경로를 수정하기 전에 `POSTMORTEM_20260825.md`를 반드시 읽을 것.**
> 서버에 보낼 값(특히 `rent_chk[]`)을 코드로 합성·예측·정규화하지 않는다.
> 테스트 모드(`--test`)는 `proc.php` 직전에 멈추므로 슬롯 값 오류를
> 검출하지 못한다 — 리허설 79/79 성공 직후 실전 36건이 전건 실패한 전례가 있다.

**정각에 페이지 GET을 생략할 수 없는 이유**: 미오픈 날짜 페이지에는 `rent_chk[]`
자체가 없어(실측 확인) 슬롯 ID를 미리 알 방법이 없다. 과거 프리페치 최적화는
슬롯 값을 `"HHMM+2h"` 8자리로 예측 구성했는데, 이는 정확히 "이미 예약됨" 값과
같아 서버가 `"존재하지않는 시간 데이터"`로 전건 거부했다 (2026-08-25 36/36 실패).
따라서 오픈 직전 조회는 **연결 예열 전용**이고, 슬롯 값은 정각에 조회한 실제
페이지에서만 얻는다.

### 타이밍 분석 로그

예약 실행 시 `logs/timing_YYYYMMDD_HHMMSS_{ID}.jsonl`에 예약 1건당 1줄(JSON) 기록:

- 요약 필드: `fire_ts`(정각 발사 시각), `sem_wait_ms`(세마포어 대기), `total_ms`, `success`, `message`
- `events[]`: 로그인부터의 모든 HTTP 시도 — `path`, `attempt`, `outcome`(`ok`/`timeout`/`conn_error`/`http_NNN`/`error`), `elapsed_ms`, `bytes`

정각 지연 분석 방법: `fire_ts` vs 10:00:00.000 차이(시작 지연), 첫 GET의 `elapsed_ms`(서버 응답 지연),
`timeout`/`conn_error` 빈도(접속 폭주), 연속 이벤트 사이 간격(파싱 CPU 병목)을 확인한다.

### 왜 예약 1건 = 독립 세션인가

같은 PHPSESSID로 `apply.php`를 병렬 호출하면 PHP 세션 상태가 덮어쓰여져
`proc.php`에서 `"정상적인 방법으로 신청해주세요. (1-1)"` 오류가 발생한다.
각 예약 봇이 독립 로그인으로 별도 PHPSESSID를 확보하므로 세션 충돌이 없다.
로그인은 `asyncio.gather`로 병렬화하여 O(1) 시간에 완료된다.

### 코트 번호 매핑

`config.toml`의 `[site.court_value_map]`에서 코트 번호(1~4)를 사이트 내부 value로
변환한다 (TOML 테이블 키는 문자열이므로 `config.py`가 int로 캐스팅):

```toml
[site.court_value_map]
"1" = "2"
"2" = "7"
"3" = "8"
"4" = "9"
```

정각에 그대로 서버로 전송되는 값이라 잘못 고치면 전건 실패한다.

### 예약 결과 메시지 — 정확한 정의

`reserve()` / `submit_reservation()` 이 돌려주는 `message` 는 타이밍 로그
(`logs/timing_*.jsonl`)의 집계 단위다. **원인이 다르면 문구도 달라야** 사후 분석이
된다. 반대로 **원인이 같으면 접두를 통일**해 `startswith("이미 예약됨")` 으로 묶인다.

**정각 전 — 페이지를 보고 우리가 판정** (`_unavailable_reason()`)

`rent_chk[]` 값의 자릿수가 상태를 그대로 말해준다:

| 페이지 상태 | 메시지 |
|---|---|
| `rent_chk[]` 필드 자체가 없음 | `미오픈 날짜 (신청 폼 없음)` |
| 값이 전부 8자리 | `이미 예약됨 (전 시간대 마감)` |
| 원하는 시각이 8자리 | `이미 예약됨 (HH:00)` |
| 원하는 시각이 목록에 없음 | `운영하지 않는 시간 (HH:00)` |
| 페이지를 못 받음 | `예약 페이지 조회 실패` |

이 판정은 **실패 분기에서만** 수행하므로 정각 성공 경로에 비용이 없다.
예전에는 위 넷을 `예약 가능 시간대 없음` / `HH:00 예약 불가` 둘로 뭉뚱그려,
로그만 보고는 **만석인지 날짜가 안 열린 건지 구분할 수 없었다.**

**정각 후 — 서버 응답으로 판정** (`proc.php`)

| 응답에 포함된 문구 | 메시지 | 성공 |
|---|---|---|
| `정상적으로 완료` | `대관접수 완료` | **True** |
| `한 건 이상 예약` | `이미 예약됨 (1일 1건 제한)` | False |
| `예약이 완료된 시간` | `이미 예약됨 (다른 사람이 선점)` | False |
| `이미 예약` / `중복` | `이미 예약됨 (중복)` | False |
| `마감` | `예약 마감` | False |
| `존재하지않는` | `슬롯 값 오류 (존재하지않는 시간)` | False |
| 그 외 | `알 수 없는 서버 응답` (전문을 로그에 남김) | False |

> `슬롯 값 오류` 가 대량으로 뜨면 **2026-08-25 전건 실패와 같은 상황**이다.
> 서버에 보낸 `rent_chk[]` 값이 잘못됐다는 뜻이므로 `POSTMORTEM_20260825.md` 를 본다.

`마감` 은 "예약된" 것이 아니라 접수 자체가 닫힌 것이라 `이미 예약됨` 으로 묶지 않는다.

### 실제 서버 응답 (proc.php)

| 상황 | 응답 내용 | 판정 |
|------|----------|------|
| 성공 | `alert("대관접수가 정상적으로 완료되었습니다..")` | True |
| 이미 예약됨 | `alert("예약이 완료된 시간입니다.(3)")` | False |
| 1일 1건 초과 | `alert("한 건 이상 예약이 완료되어 있습니다.")` | False |
| 세션 불일치 | `alert("정상적인 방법으로 신청해주세요. (1-1)")` | False |
| 잘못된 슬롯 값 | `alert("존재하지않는 ...")` | False |

판단 로직은 **실패 조건을 먼저** 검사한다. `"완료"`는 실패 메시지에도 등장하므로
광역 매칭 없이 `"정상적으로 완료"`만 성공으로 인정한다.

### 인코딩 처리

서버가 `Content-Type: charset=EUC-KR`을 선언하지만 일부 응답(apply.php)은
실제로 UTF-8 바이트를 포함한다. `_request_with_retry`에서 바이트를 직접 읽어
`euc-kr → cp949 → utf-8 → replace` 순으로 폴백 디코딩한다.

## 설정 (`config.toml`)

설정은 세 파일로 갈린다. **민감도와 변경 주기가 달라서**다.

| 파일 | 담는 것 | 민감도 | 변경 주기 | git |
|------|---------|--------|-----------|-----|
| `accounts.txt` | 이름,아이디,**비밀번호** | 높음 | 거의 안 바뀜 | ignore |
| `config.toml` | 실행 파라미터 | 없음 | 가끔 | **커밋** |
| `reservation.json` | 이번 회차 예약 | 낮음 | 매달 | ignore |

세 파일은 **아이디로 이어진다.** 예전 `accounts.txt`+`.env` 이원화는 *행 번호*로
이어서 중간 행을 지우면 조용히 남의 예약을 실행했다. 이름으로 이으면 계정이
없어졌을 때 조회 실패로 즉시 드러난다. `schedule.load()`가 로드 시점에
**모든 아이디가 `accounts.txt`에 있는지 전수 검증**한다.

> ⚠️ 예전의 "행 번호 = 계정 번호, 빈 행으로 결번 유지" 규칙은 **폐기**됐다.
> 행을 그냥 지워도 안전하다.

- `config.py`는 로더일 뿐이다. `tomllib`으로 읽어 기존 상수 이름 그대로 노출하고,
  `config.reload()`로 다시 읽는다 (viewer.py 저장 직후 사용).
- `config.toml`에는 비밀이 없어 커밋한다. `accounts.txt`·`reservation.json`은 `.gitignore` 대상.
- 읽기는 표준 `tomllib`(3.11+), 쓰기는 `tomlkit`(주석·서식 보존)으로 역할을 나눈다.

### 섹션 구성

| 섹션 | 담는 것 | 노출되는 상수 |
|------|---------|--------------|
| `[schedule]` | `day`·`hour`·`minute`, `login_advance_minutes`, `slots_per_account` | `RESERVATION_DAY`·`RESERVATION_HOUR`·`RESERVATION_MINUTE`, `LOGIN_ADVANCE_MINUTES`, `SLOTS_PER_ACCOUNT` |
| `[site]` | URL, `all_courts`, `available_hours`, `search_default_hours`, `[site.court_value_map]` | `MAIN_URL`, `TENNIS_RESERVATION_URL`, `ALL_COURTS`, `AVAILABLE_HOURS`, `SEARCH_DEFAULT_HOURS`, `COURT_VALUE_MAP` |
| `[network]` | 동시성·타임아웃·재시도·지터 | `MAX_CONCURRENT`, `CONNECTION_TIMEOUT`, `READ_TIMEOUT`, `MAX_RETRIES`, `SUBMIT_MAX_ATTEMPTS`, `CRITICAL_MAX_RETRIES`, `RETRY_BACKOFF_*`, `FIRE_JITTER_MS`, `SESSION_*` |
| `[browser]` | Selenium 모드 | `HEADLESS`, `PAGE_LOAD_TIMEOUT`, `ELEMENT_WAIT_TIMEOUT` |
| `[api]` | Flask 바인딩 | `API_HOST`, `API_PORT` |
| `[launcher]` | `group_size` — 터미널 창당 계정 수 (1~4) | `GROUP_SIZE` |


`[schedule] day = 0`이면 날짜 무관 즉시 실행.

### 계정 (`accounts.txt`) 과 예약 (`reservation.json`)

```
홍길동,user1,pass1        # 앞 2개 콤마만 분리 → pw 에 콤마·슬래시 가능
,user2,pass2              # 이름 생략 가능
```

```json
{ "version": 1, "generated_at": "...",
  "entries": [ {"no": 1, "id": "user1", "name": "홍길동",
                "slots": ["2026-09-05:8:3"]} ] }
```

- **아이디가 유일한 식별자다.** `accounts.txt` 행 번호는 표시 순서일 뿐이다.
- `no`는 저장할 때마다 1부터 재부여하는 일련번호로, `main.py --only`가 쓴다.
  체크 안 된 계정은 계획에서 빠지므로 `accounts.txt` 행 번호와 다르다.
- `name`은 사람이 읽기 위한 것이고 로더는 무시한다 (진실은 `accounts.txt`).
- `slots` 형식은 `config.parse_slots()`가 검증한다 (날짜·시간·코트).
- `config.RESERVATION_CONFIG`는 기본이 빈 목록이고, `reserve.py`가 실행할 계정의
  것을 주입한다. `schedule`이 `config`를 import하므로 반대 방향 import는 없다 (순환 회피).

### 예약 조건 표기는 한 가지뿐

과거 `.env`/`config.py`가 지원하던 방법 1(`dates × hours × courts`)과
방법 3(`court_schedules`)은 제거했다. 뷰어가 쓰는 형식이자 실제로 쓰이던
방법 2(1건 = 1문자열)만 남긴다. `reservation_async._build_tasks()`에는
조합형 분기가 아직 남아 있지만 설정에서 도달하지 않는다 (API 요청 본문의
`dates`/`hours`/`court_schedules` 경로에서만 쓰인다).

### 재시도 폭증 억제 (정각 서버 폭주 대응)

정각에는 병목이 클라이언트가 아니라 서버(요청 큐 포화 + IP당 제한)로 옮겨간다.
크리티컬 경로(apply/proc)의 재시도를 작게 유지해 단일 IP 요청 폭주로 인한
서버 IP 차단(자폭)을 막는다:

- 확정 실패(`이미 예약됨 …`·`예약 마감` 등)는 **재시도 없이 즉시 중단**
  (문자열 매칭이 아니라 해당 분기에서 바로 `return` 한다)
- 외부 재시도 `SUBMIT_MAX_ATTEMPTS`(3) × 요청당 `CRITICAL_MAX_RETRIES`(2)로 총량 상한
- 재시도 간격은 지수 백오프 + full jitter (`_backoff_delay`: `RETRY_BACKOFF_BASE·2^n`, 상한 `RETRY_BACKOFF_MAX`)
- 비-크리티컬(로그인·검색)은 여유 시간대라 기존 `MAX_RETRIES`(10) 유지

### 다중 PC 분산이 필요한 이유 (IP당 제한)

2026-08-25 실측: 한 IP에서 36세션(9계정×4건)을 열자 **신규 TCP 연결만 선택적으로
차단**됐다. 기존 예열 커넥션은 T+33초에도 응답을 받았지만, 신규 연결은 T+8.2초
이후 81건이 전부 실패했다. 서버가 죽은 게 아니라 IP당 동시연결·연결레이트 상한에
걸린 것이다 — 즉 **정각의 가용 창은 약 8초**다.

클라이언트 튜닝으로는 해결되지 않는다. 실효 대책은 IP당 세션 수를 줄이는 것
하나뿐이고, 회선이 물리적으로 다른 PC에 계정을 나누는 방법밖에 없다
(같은 공유기면 공인 IP가 같아 무효). 사용법 → [README](README.md#다중-pc-분산)

`[network] fire_jitter_ms`는 기본이 `0`이라 정각 즉시 발사한다. 분산 없이 한 IP에
세션이 몰릴 때만 `150` 정도로 올려 동시 폭주를 완화한다.

## API 서버 (`api-server/`)

엔드포인트 목록·요청 예시·Docker 배포는 [api-server/API_SERVER.md](api-server/API_SERVER.md)에 있다.
코드를 고칠 때 알아야 할 것만 적는다.

- 요청 본문에서 `user_id`/`user_pw`를 생략하면 `config.toml`의 **첫 계정**을 쓴다
  (`config.USER_ID`/`USER_PW`).
- `/reserve`는 요청 본문에서 **세 가지 형식**을 받는다: `reservations` /
  `dates`×`hours`×`courts` / `court_schedules`. 이건 **API 전용 경로**이고,
  `config.toml`은 `reservations` 한 가지만 지원한다
  (→ [예약 조건 표기는 한 가지뿐](#예약-조건-표기는-한-가지뿐)).
  조합형 두 형식은 `config.toml`에서 기본값을 받지 못하므로 요청 본문에
  반드시 넣어야 하고, 비어 있으면 400이다.
- `api_server.py`는 `api-server/` 하위에 있지만 `config`·`reservation_*`는 레포
  루트에 있다. 파일 상단에서 루트를 `sys.path`에 넣는다 (`config.py` 존재 여부로
  가드 — Docker 이미지는 `/app`에 평평하게 놓여 이 블록이 동작하지 않는다).
- **포트가 실행 방식에 따라 다르다.** 로컬은 `config.toml`의 `[api] port`(5000),
  Docker는 `Dockerfile`의 `gunicorn --bind 0.0.0.0:3100`이다. 컨테이너는
  `api_server.py`의 `main()`을 거치지 않아 `[api] port`를 읽지 않는다.

## 다중 계정 설정 (`main.py` 런처)

### 계정 자격증명과 예약 조건 — `config.toml`의 `[[accounts]]`

자격증명과 예약이 한 블록에 함께 있다. 자세한 형식은 위 [설정](#설정-configtoml) 참고.

### 실행 대상은 `reservation.json`이 결정한다

`accounts.txt`에 계정이 있어도 계획에 없으면 실행하지 않는다:

- 런처는 `entries`를 그대로 실행 대상으로 삼는다. 뷰어에서 체크를 해제했거나
  슬롯이 없는 계정은 애초에 저장되지 않으므로 skip 목록 자체가 사라졌다.
- `--logincheck`는 예약이 필요 없으므로 **`accounts.txt` 전 계정**을 실행한다.
- `reserve.py --account <id>`도 계획에 그 아이디가 없으면 즉시 종료한다.
  그대로 진행하면 다른 계정의 예약을 대신 잡아 1일 1건 제한을 낭비한다.
- 런처는 실행 전 계획의 `generated_at`을 출력한다 — **뷰어에서 배정하고
  저장 버튼을 안 누른 채 정각을 맞는 것이 이 구조의 최대 사고 유형**이다.

### 런처(`main.py`) 동작 방식

계정을 `config.toml`의 `[launcher] group_size`(기본 4)개씩 그룹화 →
그룹마다 새 터미널 창을 연다. `--tmux`를 주면 창마다 tmux 세션을 만든다:

```
python3 main.py --tmux 실행 (13개 계정)
  그룹 1 (계정 1~4)  → iTerm2 새 창 → tmux tennis_1 → 2×2 pane
  그룹 2 (계정 5~8)  → iTerm2 새 창 → tmux tennis_2 → 2×2 pane
  그룹 3 (계정 9~12) → iTerm2 새 창 → tmux tennis_3 → 2×2 pane
  그룹 4 (계정 13)   → iTerm2 새 창 → tmux tennis_4 → 1 pane
```

플랫폼별 동작:

| 플래그 | macOS (iTerm2) | Linux |
|--------|---------------|-------|
| (없음) | iTerm2 native split pane | 계정당 개별 창 |
| `--tmux` | tmux + 새 터미널 창 | tmux + 감지된 에뮬레이터 |
| `--background` | subprocess + 로그 파일 | 동일 |

tmux 그룹 스크립트는 `/tmp/tennis_group_N.sh`에 생성되며, tmux 절대 경로
(`TMUX="/opt/homebrew/bin/tmux"`)를 하드코딩해 non-login shell PATH 문제를 회피한다.
`--tmux`를 줬는데 tmux가 없으면 중단하지 않고 기본 모드로 내려간다 — 정각을
앞두고 "실행 안 됨"보다 "다른 방식으로라도 실행됨"이 낫기 때문이다.

`group_size`는 **1~4만 허용**하며 `config.py`가 로드 시점에 막는다.
`create_group_scripts()`(tmux)와 `build_iterm2_split_applescript()` 둘 다
분할 분기가 4개까지만 있어서, 5 이상이면 계정 스크립트는 만들어지지만
pane이 없어 5번째부터 **조용히 실행되지 않는다**.

### 진입점은 main.py 하나 — 위임과 전달

`main.py`가 모든 플래그를 받는다. 워커 플래그는 두 가지로 갈린다.

| 분류 | 플래그 | 동작 |
|------|--------|------|
| **위임** | `--account`, `--search`, `--search2` | 창을 열지 않고 `os.execv`로 `reserve.py`가 된다 |
| **전달** | `--test`, `--logincheck`, `--browser` | 전 pane의 `reserve.py`에 인자로 넘긴다 |

`--browser`는 `--only`/`--account` 로 대상을 좁혀야 실행된다. 브라우저 모드는
예약 1건마다 Chrome 을 하나 띄우고(`reservation.py` 가 작업 수만큼 `setup_browser()`),
런처는 계정 프로세스를 동시에 띄운다. `MAX_CONCURRENT` 는 프로세스 **안에서만**
적용되므로 전체 인스턴스 수에 상한이 없다. 실측 인스턴스당 약 900MB —
19계정 57건이면 약 51GB 로 물리 RAM 을 넘겨 Chrome 기동이 실패하고,
그 증상이 `login()` 의 `"로그아웃" in page_source` 검사에서 **로그인 실패로 나타난다.**
| **런처 전용** | `--only`, `--tmux`, `--background`, `--detach`, `--dry-run` | 런처가 소비. 위임 플래그와 함께 쓰면 거부 |

`--background` 는 전 계정이 끝날 때까지 `proc.wait()` 로 기다리며 결과를 요약한다
(정각에 무엇이 성공했는지 그 자리에서 보기 위해서다). 셸을 바로 돌려받으려면
`--detach` 를 쓴다 — `start_new_session=True` 로 프로세스 그룹을 분리하므로
터미널을 닫거나 SSH 가 끊겨도 워커가 살아 있다.

`--dry-run` 은 `/tmp/tennis_*.sh` 를 **만들지 않는다**. 실행하지 않을 스크립트가
남으면 나중에 손으로 실행했을 때 진짜 예약이 돌아가기 때문이다. 내용은
메모리에서 만들어 출력만 한다.

- 워커 플래그 정의는 `utils.add_worker_args()` **한 곳**이다. 두 파서가 같은
  함수를 호출하므로, 런처가 받아들인 인자를 워커가 거부하는 조합이 생기지 않는다.
- 위임을 `import reserve`가 아니라 `os.execv`로 하는 이유: `reserve.py`는
  aiohttp 등으로 300ms 넘게 걸린다. import로 하면 런처 기본 경로가 늘 그 비용을
  문다 (현재 기동 70ms). execv는 프로세스를 통째로 교체하므로 exit code·Ctrl+C·
  출력이 그대로 이어지고, 프로세스 계층도 늘지 않는다.
- `reserve.py`는 계속 직접 실행할 수 있다. pane에서 실제로 도는 실체이고,
  디버깅할 때 런처를 거치지 않아도 된다.

### 런처와 워커를 가르는 두 가지 안전장치

진입점이 둘(`main.py` 런처 / `reserve.py` 워커)이므로 서로의 인자가 섞이면
조용히 엉뚱하게 동작한다. 두 곳에서 막는다:

- **런처 파서는 `allow_abbrev=False`다.** 켜두면 argparse 접두 매칭이
  워커용 `--account 5`를 런처의 `--only=5`로 받아들여, 워커 대신 런처가
  한 겹 더 열린다 (실측 확인). 이 때문에 계정 범위 선택은 `--only`로 개명했고,
  `--account`가 실제 플래그가 된 지금은 `--accounts` 별칭도 제거했다
  (`--account`와 한 글자 차이로 동작이 완전히 달라진다).
- **`--test` 시각 변환은 `utils.parse_open_time()` 하나를 공유한다.**
  런처가 상대 초를 절대 `HH:MM`으로 한 번만 바꿔 전 계정에 같은 값을 넘기는데,
  변환 규칙이 런처와 워커에서 갈리면 계정마다 오픈 시각이 어긋난다.
  실제로 런처 쪽에만 자정 넘김 가드가 없어 23:59에 `--rehearse 90`(현 `--test 90`)을
  돌리면 전 계정이 "이미 지난 시각"으로 즉사하는 버그가 있었다 (2026-08-27 수정).

## 예약 현황 뷰어 (`viewer.py`)

```bash
python3 viewer.py          # 브라우저에서 http://127.0.0.1:8765/ 열기
python3 viewer.py 2026 7   # 특정 월 지정
# Ctrl+C로 종료
```

### 주요 기능

- **달력 뷰**: 월별 날짜 셀 × 코트(C1~C4) × 시간(06~20) 미니 그리드
- **계정 포커스**: 왼쪽 ID 카드 클릭 → 반전 표시 + 해당 계정 예약 체크 상태로 로드
- **실시간 저장**: 슬롯 클릭 → 즉시 `POST /api/save-slots` → `config.toml` 업데이트
- **중복 표시**: 같은 슬롯에 여러 계정 예약 시 황색 ⚠ 표시 + 툴팁
- **PW 마스킹**: 👁 버튼으로 토글
- **체크 = 이번 계획 포함.** 해제하면 그 계정은 사이드바 0건·달력 미표시가 되고
  저장 시 `entries`에서 빠진다. 배정 자체는 메모리에 남아 다시 체크하면 돌아온다.
  사이드바 건수는 **저장했을 때 파일에 들어갈 건수**를 보여준다 (`planCount`).
- **재배치** (배치 모드): 체크된 날짜(기본 토·일·공휴일)의 슬롯 풀을 **선택된 계정**에 계정당 N개씩 자동 배정
  - 미선택 계정은 계획에서 빠지므로 슬롯을 점유하지 않는다 — 풀은 대상 날짜의 전체 슬롯이다
  - 계정당 개수는 헤더의 "👤 계정당 N개 배정" 입력으로 조정 (`config.toml`의 `[schedule] slots_per_account`에 저장, 기본 4)
  - 하드 제약: 동일 날짜 금지 (서버가 계정당 1일 1건만 허용하므로 같은 날짜 2건은 정각에 반드시 1건 실패)
- **가능/필요 카운터** (헤더): 가능(선택 계정 수 × 계정당 배정 수) / 필요(체크된 배치 날짜 수요)를 실시간 표시, 부족 시 빨간색 강조
  - 필요 수 = 체크된 배치 날짜별 합: 토 7(6시 3+8시 3+10시 1), 그 외 — 일·평일·공휴일 — 8(6시 3+8시 4+10시 1)
  - 날짜 기본 선택은 토·일·공휴일 — 공휴일 판정은 `holidays` 패키지(KR, 대체공휴일·음력 포함), 미설치 시 주말만 기본 선택
  - 재배치 확인 대화상자도 같은 비교(용량 < 선택일 수요 시 ⚠ 경고)를 사용
- **빈자리 검색** (검색 모드): 체크된 날짜의 실제 예약 가능 여부를 사이트에서 조회하여 달력에 표시
- **헤더 설정**: 로그인 시작 시점(분)·계정당 배정 개수 입력 → 변경 즉시 `config.toml`에 저장

### 내장 HTTP 서버

`viewer.py`는 Python 내장 `http.server`로 로컬 HTTP 서버를 실행한다.

- `GET /` → HTML 페이지 서빙 (same-origin, CORS 없음, 매 요청마다 최신 `config.toml`로 재빌드)
- `POST /api/save-slots` → 해당 계정의 `reservations` 배열 교체 (날짜→시간→코트 정렬)
  - 저장 후 최신 accounts JSON 반환 → 브라우저 `ACCOUNTS` in-place 갱신
- `POST /api/redistribute` → 재배치 결과 일괄 저장 (전달된 계정만 예약 교체)
  - 저장 전 `config.toml.bak.YYYYMMDD_HHMMSS` 자동 백업 (뷰어 시작 시에도 1회)
- `POST /api/search` → 날짜별 빈자리 조회 (사이트 실시간 조회)
- `POST /api/save-login-advance` → `[schedule] login_advance_minutes` 저장
- `POST /api/save-slots-per-account` → `[schedule] slots_per_account` 저장

쓰기는 전부 `tomlkit`으로 파싱 → 값만 교체 → dump 한다. 기존 키의 값만
갈아끼우므로 주석·정렬·섹션 순서가 그대로 남는다 (`tomlkit`은 AoT 항목에
**새 서브테이블**을 추가할 때만 주변 주석이 밀리는 알려진 버그가 있는데,
뷰어는 그 경로를 쓰지 않는다).

포트: 8765~8799 범위에서 사용 가능한 포트 자동 탐색.
