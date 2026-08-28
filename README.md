# 고양시 테니스장 자동 예약

고양시 대화문화체육센터 테니스장 예약을 자동화하는 프로그램.
매월 25일 10시 오픈되는 예약을 asyncio 기반 HTTP 요청으로 처리한다.

- **파일 3개**로 갈린다 — 자격증명(`accounts.txt`) · 실행 파라미터(`config.toml`) · 이번 회차 예약(`reservation.json`).
- **명령은 `main.py` 하나**로 통한다. 전 계정 동시 실행부터 단일 계정·빈자리 검색까지.

## 목차

1. [빠른 시작](#1-빠른-시작)
2. [설정 (config.toml)](#2-설정-configtoml)
3. [실행](#3-실행)
4. [예약 현황 뷰어](#4-예약-현황-뷰어)
5. [API 서버](#5-api-서버)
6. [문제 해결](#6-문제-해결)

---

## 1. 빠른 시작

Python 3.11 이상이 필요하다 (`tomllib` 사용).

```bash
pip3 install -r requirements.txt

# 1. 계정 등록 — 한 줄에 하나씩 (이름은 비워도 됨)
cat > accounts.txt <<'EOF'
홍길동,user1,pass1
,user2,pass2
EOF
chmod 600 accounts.txt

# 2. 로그인 테스트 (창 없이 한 계정만)
python3 main.py --account user1 --logincheck

# 3. 예약을 달력에서 배정하고 [💾 저장] → reservation.json 생성
python3 viewer.py

# 4. 실행 내용 확인 → 실전
python3 main.py --dry-run
python3 main.py
```

> `accounts.txt`와 `reservation.json`은 `.gitignore` 대상이다.
> `config.toml`은 비밀 정보가 없어 커밋한다.

### 파일 세 개

| 파일 | 담는 것 | 민감도 | 변경 주기 | git |
|------|---------|--------|-----------|-----|
| `accounts.txt` | 이름,아이디,**비밀번호** | 높음 | 거의 안 바뀜 | ignore |
| `config.toml` | 실행 파라미터 | 없음 | 가끔 | **커밋** |
| `reservation.json` | 이번 회차 예약 (아이디 + 슬롯) | 낮음 | 매달 | ignore |

세 파일은 **아이디로 이어진다.** 위치(행 번호·목록 순서)로 잇지 않으므로
계정을 지워도 조용히 어긋나지 않고 조회 실패로 즉시 드러난다.

### 진입점 두 개

| 파일 | 역할 |
|------|------|
| `main.py` | **사용자가 쓰는 유일한 진입점.** 전 계정 런처이자, 단일 계정·검색 명령의 창구 |
| `reserve.py` | 계정 1개를 실제로 돌리는 워커. 런처가 각 창에서 띄운다. 직접 실행해도 되지만 필수는 아니다 |

---

## 2. 설정

### 계정 — `accounts.txt`

```
홍길동,user1,pass1
,user2,pass2
김철수,user3,pa,ss/3
```

- 형식은 `이름,아이디,비밀번호`. 이름은 비워도 된다.
- 앞 2개 콤마만 분리하므로 **비밀번호에 콤마·슬래시가 들어가도 된다.**
- **아이디가 식별자다.** 행 번호는 화면 표시 순서일 뿐이라 중간 행을 지워도
  안전하다 (빈 행으로 결번을 맞출 필요가 없다).
- 아이디가 중복되면 로드 시점에 중단한다.
- 비밀번호가 들어 있으므로 `chmod 600` 을 권한다.

### 예약 — `reservation.json`

`viewer.py`의 **[💾 저장]** 버튼이 만든다. 직접 편집할 일은 없다.

```json
{
  "version": 1,
  "generated_at": "2026-08-27T13:47:24+09:00",
  "entries": [
    { "no": 1, "id": "user1", "name": "홍길동",
      "slots": ["2026-09-05:8:3", "2026-09-06:8:3"] }
  ]
}
```

| 필드 | 뜻 |
|------|-----|
| `no` | 저장할 때마다 1부터 다시 부여하는 **일련번호**. `--only`가 이걸 쓴다 |
| `id` | 유일한 식별자. `accounts.txt`에서 비밀번호를 찾는 키 |
| `name` | 사람이 읽기 위한 것. 로더는 무시한다 |
| `slots` | `"날짜:시작시각:코트번호"` |

| 필드 | 형식 | 가능한 값 |
|------|------|----------|
| 날짜 | `YYYY-MM-DD` | — |
| 시작시각 | 숫자 | `6`, `8`, `10`, `12`, `14`, `16`, `18`, `20` |
| 코트번호 | 숫자 | `1`, `2`, `3`, `4` |

- **체크했고 슬롯이 1개 이상인 계정만** 들어간다. 체크를 해제하면 그 계정은 실행 대상에서 빠진다.
- `no`는 `accounts.txt` 행 번호와 **다르다** — 빠진 계정만큼 번호가 당겨진다.
- **서버가 계정당 하루 1건만 허용한다.** 같은 날짜를 두 번 넣으면 저장 시 경고가 뜨고,
  정각에 한 건은 반드시 실패한다.
- 저장 전 `reservation.json.bak.YYYYMMDD_HHMMSS`로 백업한다.

### 실행 파라미터 — `config.toml`

비밀 정보가 없어 커밋 대상이다.

#### 예약 오픈 시각

```toml
[schedule]
day    = 25   # 매월 25일 (0이면 날짜 무관 즉시 실행)
hour   = 10
minute = 0

login_advance_minutes = 10   # 오픈 몇 분 전부터 로그인할지
slots_per_account     = 4    # 뷰어 재배치 시 계정당 배정할 슬롯 수
```

| 상황 | 동작 |
|------|------|
| `day = 0` | 즉시 실행 |
| 오늘 = 예약일, 오픈 시각 **이전** | 오픈 10분 전까지 대기 → 로그인 → 정각에 제출 |
| 오늘 = 예약일, 오픈 시각 **이후** | 즉시 실행 |
| 오늘 ≠ 예약일 | 에러 종료 (`--test`는 예외 — 아래 참조) |

**10분 전 대기**: 오픈 10분 이상 전에 시작하면 로그인 없이 기다리다가, 10분 전에 로그인을 시작한다.

#### 시작 시각 표

| 값 | 예약 시간 | | 값 | 예약 시간 |
|----|----------|-|----|----------|
| 6  | 06:00~08:00 | | 14 | 14:00~16:00 |
| 8  | 08:00~10:00 | | 16 | 16:00~18:00 |
| 10 | 10:00~12:00 | | 18 | 18:00~20:00 |
| 12 | 12:00~14:00 | | 20 | 20:00~22:00 |

#### 런처와 네트워크

```toml
[launcher]
group_size = 4        # 터미널 창 하나에 넣을 계정 수 (1~4)

[network]
max_concurrent = 10   # 최대 동시 예약 세션 수
fire_jitter_ms = 0    # 정각 발사 지터 상한 ms (0 = 지터 없이 즉시 발사)
```

`group_size`가 **1~4로 제한되는 이유**: tmux 2×2와 iTerm2 split 레이아웃이
4분할까지만 지원한다. 5 이상을 넣으면 5번째부터 조용히 실행되지 않으므로 로드 시점에 막는다.

#### 인증 정보 우선순위

1. API 요청 파라미터 (`user_id`, `user_pw`) — API 서버만 해당
2. `accounts.txt`
3. 실행 시 직접 입력 — CLI만 해당

`--account` 없이 실행하면 `accounts.txt`의 첫 계정으로 동작한다.

---

## 3. 실행

### 전 계정 실행

```bash
python3 main.py                # 터미널 네이티브 분할 (기본)
python3 main.py --tmux         # tmux 세션 + 새 터미널 창
python3 main.py --background   # 창 없이 백그라운드 + logs/<아이디>.log (끝까지 대기)
python3 main.py --detach       # 위와 같되 기다리지 않고 셸을 돌려준다
python3 main.py --only 1-10    # 일련번호 부분 선택 (다중 PC 분산)
python3 main.py --test         # 테스트 모드 (즉시 실행, 대관신청 전 중단)
python3 main.py --test 300     # 오픈을 300초 후로 강제 (전 계정 공통)
python3 main.py --logincheck   # 전 계정 로그인 테스트
python3 main.py --dry-run      # 실행될 명령만 출력 (창 미생성)
```

실행 대상은 **`reservation.json`의 `entries`**다. 뷰어에서 체크하지 않았거나
슬롯이 없는 계정은 애초에 계획에 들어가지 않는다. 계정을 `group_size`개씩 그룹으로
묶어 그룹마다 새 터미널 창을 연다.

`--logincheck`만 예외다 — 로그인 확인에는 예약이 필요 없으므로 `accounts.txt`
전 계정을 실행한다.

| 플래그 | macOS (iTerm2) | Linux |
|--------|---------------|-------|
| (없음) | native split pane | 계정당 개별 창 |
| `--tmux` | tmux + 새 터미널 창 | tmux + 감지된 에뮬레이터 |
| `--background` | subprocess + 로그 파일 | 동일 |

`--tmux`를 줬는데 tmux가 없으면 중단하지 않고 경고 후 기본 모드로 실행한다.

### 계정 1개만 / 빈자리 검색 — 창이 열리지 않는다

`--account`·`--search`·`--search2` 중 하나라도 주면 창을 열지 않고 현재 터미널에서
바로 실행된다 (내부적으로 `reserve.py`로 넘어간다).

```bash
# 로그인 테스트
python3 main.py --account user1 --logincheck

# 테스트 모드 (대관신청 직전에 중단)
python3 main.py --account user1 --test

# 실제 예약 (한 계정만)
python3 main.py --account user1

# 브라우저 모드 (Selenium)
python3 main.py --account user1 --browser
python3 main.py --account user1 --browser --test

# 주말 빈자리 검색 — 계정과 무관하게 결과가 같으므로 1회만 조회한다
python3 main.py --search 3        # 3월
python3 main.py --search 2026-03  # 2026년 3월

# 전체 날짜 빈자리 검색
python3 main.py --search2 3
```

### 파라미터 레퍼런스

**공통** — 런처·단일 계정 양쪽에서 동작한다.

| 파라미터 | 값 | 설명 |
|---|---|---|
| `--test` | 없음 / `초` / `HH:MM` | 대관신청 직전 중단. **값이 없으면 오픈을 기다리지 않고 즉시 실행**, 값을 주면 그 시각을 오픈으로 강제해 대기→예열→정각 발사를 재현 |
| `--logincheck` | — | 로그인 + 예약 페이지 접근 테스트 |

**런처 전용** — 아래 위임 플래그와 함께 쓰면 거부된다.

| 파라미터 | 값 | 설명 |
|---|---|---|
| `--only` | `1-10` / `1,3,5` / `1-5,8` | `reservation.json`의 **일련번호**로 선택 (다중 PC 분산) |
| `--tmux` | — | tmux 세션으로 실행 |
| `--background` | — | 창 없이 subprocess + `logs/<아이디>.log`. **전 계정이 끝날 때까지 기다리며** 결과를 요약 |
| `--detach` | — | `--background` 로 시작하고 **기다리지 않고 셸을 돌려준다**. 터미널을 닫아도 계속 돈다 |
| `--dry-run` | — | 실행될 명령만 출력. 창도 안 열고 `/tmp` 스크립트도 만들지 않는다 |

**단일 계정** — 창이 열리지 않는다.

| 파라미터 | 값 | 처리 |
|---|---|---|
| `--account` | **아이디** | **위임** — `reserve.py`로 프로세스 교체 |
| `--search` | `3` / `2026-03` | **위임** — 주말 빈자리, 1회만 조회 |
| `--search2` | `3` / `2026-03` | **위임** — 전체 날짜 빈자리 |
| `--browser` | — | **전달** — 각 창의 `reserve.py`에 넘긴다 (위임 아님) |

**`--browser`는 `--only` 또는 `--account` 로 대상을 좁혀야 실행된다.** 브라우저 모드는
"예약 1건 = Chrome 1개"이고 런처가 계정 프로세스를 동시에 띄우므로, 좁히지 않으면
예약 건수 총합만큼 Chrome이 한꺼번에 뜬다. 인스턴스당 약 900MB라 메모리가 먼저
고갈되고, 그 결과가 "로그인 실패"로 나타난다. 좁히지 않으면 예상 개수를 알리고 중단한다.

```bash
python3 main.py --account user1 --browser --test   # Chrome = 그 계정의 예약 건수
python3 main.py --only 1-2 --browser --test        # 좁혀서
```

**우선순위**

- `main.py`: 위임(`--account`/`--search`/`--search2`) → 런처
- `reserve.py`: `--search`/`--search2` → `--logincheck` → `--test` → 실제 예약

**CLI에 없고 `config.toml`에 있는 것**: 창당 계정 수(`[launcher] group_size`),
오픈 시각(`[schedule]`), 로그인 선행 시간(`login_advance_minutes`).
**CLI에 없고 `reservation.json`에 있는 것**: 누가 무엇을 예약할지 — `viewer.py`에서 정한다.

### 다중 PC 분산

단일 IP에서 전 계정을 실행하면 IP당 동시연결·요청 상한에 걸려 전 계정이 동시에
차단될 수 있다. 회선이 물리적으로 다른 여러 PC에 계정을 나눈다.

```bash
# 두 PC가 같은 reservation.json 을 써야 번호가 일치한다
# PC-A
python3 main.py --only 1-10
# PC-B (회선이 물리적으로 달라야 함 — 같은 공유기면 IP가 같아 무효)
python3 main.py --only 11-19
```

`fire_jitter_ms`는 기본이 `0`(지터 없이 정각 즉시 발사)이다. 분산 없이 한 IP에서
세션을 많이 열어야 한다면 `150` 정도로 올려 동시 폭주를 완화한다.

---

## 4. 예약 현황 뷰어

```bash
python3 viewer.py          # http://127.0.0.1:8765/ 브라우저에서 열림
python3 viewer.py 2026 7   # 특정 월 지정
# Ctrl+C로 종료
```

- **달력 뷰**: 날짜 셀 × 코트(C1~C4) × 시간(06~20) 미니 그리드
- **계정 체크**: 체크된 계정만 계획에 들어간다. 해제하면 **건수가 0건으로 바뀌고**
  달력에서도 사라진다 (배정은 메모리에 남아 다시 체크하면 돌아온다)
- **계정 포커스**: 왼쪽 ID 카드 클릭 → 반전 표시 + 해당 계정 예약 로드
- **명시적 저장**: 슬롯 클릭은 브라우저 메모리에만 반영된다. 헤더의 **[💾 저장]** 을
  눌러야 `reservation.json`에 쓰인다. 미저장 상태면 버튼이 주황색으로 깜박이고,
  저장하지 않고 창을 닫으려 하면 경고가 뜬다
- **중복 표시**: 같은 슬롯에 여러 계정 예약 시 황색 ⚠ + 툴팁
- **재배치**: 체크된 날짜(기본 토·일·공휴일)의 슬롯을 선택된 계정에 계정당 N개씩 자동 배정.
  같은 계정에 동일 날짜를 배정하지 않는다 (서버가 하루 1건만 허용하므로).
  결과는 계정 카드에 `▲N`(늘어남) `▼N`(줄어듦) `↻`(건수는 같고 슬롯만 교체) `=`(그대로)로 표시된다
- **가능/필요 카운터**: 선택 계정 수 × 계정당 배정 수 vs 체크된 날짜 수요를 실시간 비교
- **빈자리 검색**: 체크된 날짜의 실제 예약 가능 여부를 사이트에서 조회해 달력에 표시

저장할 때마다 `reservation.json.bak.YYYYMMDD_HHMMSS`로 백업한다.
헤더의 로그인 시작 시점·계정당 배정 개수는 `config.toml`에 바로 저장된다.

---

## 5. API 서버

n8n, cron 등 외부 시스템에서 HTTP로 예약을 자동화할 때 쓰는 REST API 서버가 있다.
실행·엔드포인트·Docker 배포는 별도 문서로 분리했다.

```bash
python3 api-server/api_server.py            # 로컬 (포트 5000)
cd api-server && docker-compose up -d       # Docker (포트 3100)
```

→ **[API 서버 운영 (Docker)](api-server/API_SERVER.md)**

---

## 6. 문제 해결

### 설정이 로드되지 않음

```
config.ConfigError: [설정 오류] accounts.txt 이 없습니다.
```

`accounts.txt`에 `이름,아이디,비밀번호`를 한 줄씩 적는다.
`[설정 오류]`는 어느 섹션·어느 항목인지 함께 찍으므로 그 위치를 고치면 된다.

### 계정이 실행되지 않음

`reservation.json`의 `entries`에 없는 계정은 실행되지 않는다. `python3 viewer.py`에서
그 계정을 **체크**하고 슬롯을 배정한 뒤 **[💾 저장]** 을 누른다.

### 배정했는데 반영되지 않음

**저장 버튼을 눌렀는지 확인한다.** 슬롯 클릭만으로는 파일이 바뀌지 않는다.
`main.py` 실행 시 첫 줄에 계획의 저장 시각이 찍히므로 최신인지 볼 수 있다.

```
계획: reservation.json (저장 시각 2026-08-27T13:47:24+09:00)
```

### 없어진 플래그

| 옛 플래그 | 지금 |
|---|---|
| `--rehearse N` | `--test N` |
| `--check` | `--logincheck` |
| `--no-tmux` | (기본 동작) — tmux를 쓰려면 `--tmux` |
| `--accounts 1-10` | `--only 1-10` |
| `--group-size N` | `config.toml`의 `[launcher] group_size` |
| `--account 3` (번호) | `--account user1` (아이디) |

별칭을 남기지 않았으므로 옛 플래그는 `unrecognized arguments`로 실패한다.

### 브라우저 모드 (ChromeDriver)

```bash
# 1. ChromeDriver 캐시 삭제
rm -rf ~/.wdm

# 2. 패키지 재설치
pip3 install --upgrade selenium webdriver-manager

# 3. 테스트
python3 main.py --account user1 --logincheck --browser
```

**"chromedriver not found"**
```bash
brew install chromedriver
xattr -d com.apple.quarantine $(which chromedriver)
```

**"This version of ChromeDriver only supports Chrome version XX"**
```bash
rm -rf ~/.wdm
python3 main.py --account user1 --logincheck --browser  # 자동으로 맞는 버전 다운로드
```

**"developer cannot be verified" (macOS 보안)**
```bash
CHROMEDRIVER_PATH=$(python3 -c "from webdriver_manager.chrome import ChromeDriverManager; print(ChromeDriverManager().install())")
xattr -d com.apple.quarantine "$CHROMEDRIVER_PATH"
```

**Segmentation fault** — `config.toml`에서 창을 표시하도록 바꾼다.
```toml
[browser]
headless = false
```

브라우저 모드 문제가 지속되면 HTTP 모드를 쓴다. 더 빠르고 안정적이다.

```bash
python3 main.py --account user1 --test        # HTTP 모드 (브라우저 불필요)
python3 main.py --account user1 --logincheck  # HTTP 로그인 테스트
```

---

## 관련 문서

- [API 서버 운영 (Docker)](api-server/API_SERVER.md) — REST API 엔드포인트, Docker 배포, n8n 연동
- [CLAUDE.md](CLAUDE.md) — 아키텍처 상세, 정각 크리티컬 경로, 뷰어 설계
- [POSTMORTEM_20260825.md](POSTMORTEM_20260825.md) — 2026-08-25 전건 실패 원인 분석
