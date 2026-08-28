# -*- coding: utf-8 -*-
"""
고양시 체육시설 예약 프로그램 설정

설정은 세 파일로 나뉜다. 민감도와 변경 주기가 다르기 때문이다.

    accounts.txt      이름,아이디,비밀번호  — 비밀번호는 여기에만 있다. 거의 안 바뀜
    config.toml       실행 파라미터         — 비밀 없음. 커밋 대상
    reservation.json  이번 회차 예약 계획   — schedule.py 가 다룬다. 매달 바뀜

세 파일은 **아이디로 이어진다.** 위치(행 번호·목록 순서)로 잇지 않으므로
계정을 지워도 조용히 어긋나지 않고 조회 실패로 즉시 드러난다.

    import config
    config.load_accounts()      # [{"num","name","user_id","user_pw"}, ...]
    config.find_account("id")   # pw 조회. 없으면 None
    config.RESERVATION_CONFIG   # 기본은 빈 목록 — reserve.py 가 schedule 에서 주입한다
    config.reload()             # config.toml·accounts.txt 를 다시 읽는다
"""

import tomllib
from datetime import datetime as _dt
from pathlib import Path

CONFIG_FILE = Path(__file__).parent / "config.toml"
ACCOUNTS_FILE = Path(__file__).parent / "accounts.txt"


class ConfigError(Exception):
    """설정이 잘못됐다. 실행을 시작하지 않는다."""


# ============================================
# 검증 헬퍼
# ============================================

def _get(table, key, where):
    """필수 키를 꺼낸다. 없으면 어디를 고쳐야 하는지 알려주고 중단한다."""
    if key not in table:
        raise ConfigError(
            f"[설정 오류] config.toml [{where}] 에 {key} 가 없습니다.\n"
            f"  → config.example.toml 의 [{where}] 항목을 참고해 추가하세요."
        )
    return table[key]


def _section(doc, name):
    section = doc.get(name)
    if not isinstance(section, dict):
        raise ConfigError(
            f"[설정 오류] config.toml 에 [{name}] 섹션이 없습니다.\n"
            f"  → config.example.toml 을 참고해 추가하세요."
        )
    return section


def parse_slots(raw, where):
    """["YYYY-MM-DD:시작시각:코트번호", ...] → [{"date","hour","court"}, ...]

    reservation.json 의 slots 도 같은 형식이라 schedule.py 가 이 함수를 쓴다.
    """
    reservations = []
    for i, item in enumerate(raw, start=1):
        key = f'{where}[{i}] = "{item}"'
        parts = str(item).split(":")
        if len(parts) != 3:
            raise ConfigError(
                f"[설정 오류] {key}\n"
                f'  → 올바른 형식: "날짜:시간:코트번호"  (예: "2026-06-07:10:1")'
            )

        date_str, hour_str, court_str = (p.strip() for p in parts)
        try:
            _dt.strptime(date_str, "%Y-%m-%d")
        except ValueError:
            raise ConfigError(
                f"[설정 오류] {key}: 날짜 형식 오류 '{date_str}'\n"
                f"  → 올바른 형식: YYYY-MM-DD  (예: 2026-06-07)"
            )
        try:
            hour, court = int(hour_str), int(court_str)
        except ValueError:
            raise ConfigError(
                f"[설정 오류] {key}: 시간·코트는 숫자여야 합니다."
            )
        if hour not in AVAILABLE_HOURS:
            raise ConfigError(
                f"[설정 오류] {key}: 잘못된 시간 '{hour}'\n"
                f"  → 가능한 시작 시각: {AVAILABLE_HOURS}"
            )
        if court not in ALL_COURTS:
            raise ConfigError(
                f"[설정 오류] {key}: 잘못된 코트번호 '{court}'\n"
                f"  → 가능한 코트: {ALL_COURTS}"
            )
        reservations.append({"date": date_str, "hour": hour, "court": court})
    return reservations


def _parse_accounts_file(path):
    """accounts.txt 를 파싱한다. 형식: 이름,아이디,비밀번호 (이름은 비워도 됨)

    - 비밀번호에 콤마·슬래시가 들어갈 수 있으므로 split(",", 2) 로 앞 2개만
      분리한다 (셋째 필드 전체 = 비밀번호).
    - 2필드 행(아이디,비밀번호)은 이름 생략으로 허용한다.
    - 빈 행·주석(#)은 건너뛴다. **행 번호는 식별자가 아니라 표시 순서일 뿐이라
      결번을 맞출 필요가 없다** — 예약은 reservation.json 이 아이디로 잇는다.

    Returns:
        list of dict: [{"num": 표시순번, "name", "user_id", "user_pw"}, ...]
    """
    if not path.exists():
        raise ConfigError(
            f"[설정 오류] {path.name} 이 없습니다.\n"
            f"  → 한 줄에 하나씩 계정을 적으세요:  이름,아이디,비밀번호"
        )

    accounts = []
    seen = {}
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(",", 2)
        if len(parts) == 2:
            parts = [""] + parts
        if len(parts) != 3:
            raise ConfigError(
                f"[설정 오류] {path.name} {lineno}행 형식 오류: {raw!r}\n"
                f"  → 올바른 형식: 이름,아이디,비밀번호"
            )
        name, uid, upw = (p.strip() for p in parts)
        if not uid or not upw:
            raise ConfigError(
                f"[설정 오류] {path.name} {lineno}행: 아이디 또는 비밀번호가 비어 있습니다."
            )
        if uid in seen:
            raise ConfigError(
                f"[설정 오류] {path.name} {lineno}행: 아이디 '{uid}' 가 "
                f"{seen[uid]}행과 중복입니다. 아이디가 식별자이므로 중복될 수 없습니다."
            )
        seen[uid] = lineno
        accounts.append({
            "num": len(accounts) + 1,   # 표시 순번 — 식별자가 아니다
            "name": name,
            "user_id": uid,
            "user_pw": upw,
        })
    return accounts


# ============================================
# 로드
# ============================================

def _apply(doc):
    """파싱한 TOML을 모듈 상수에 반영한다."""
    g = globals()

    # ── 사이트 상수 ──────────────────────────────────────────────────────
    # court_value_map은 정각에 그대로 서버로 전송되는 값이다. 합성·정규화 없이
    # 파일에 적힌 문자열을 그대로 쓴다 (POSTMORTEM_20260825.md 참고).
    site = _section(doc, "site")
    g["MAIN_URL"] = _get(site, "main_url", "site")
    g["TENNIS_RESERVATION_URL"] = _get(site, "reservation_url", "site")
    g["ALL_COURTS"] = list(_get(site, "all_courts", "site"))
    g["AVAILABLE_HOURS"] = list(_get(site, "available_hours", "site"))
    g["SEARCH_DEFAULT_HOURS"] = list(_get(site, "search_default_hours", "site"))
    g["COURT_VALUE_MAP"] = {
        int(k): str(v)
        for k, v in _get(site, "court_value_map", "site").items()
    }

    # ── 예약 오픈 시각 ───────────────────────────────────────────────────
    schedule = _section(doc, "schedule")
    g["RESERVATION_DAY"] = int(_get(schedule, "day", "schedule"))
    g["RESERVATION_HOUR"] = int(_get(schedule, "hour", "schedule"))
    g["RESERVATION_MINUTE"] = int(_get(schedule, "minute", "schedule"))
    g["LOGIN_ADVANCE_MINUTES"] = int(_get(schedule, "login_advance_minutes", "schedule"))
    g["SLOTS_PER_ACCOUNT"] = int(_get(schedule, "slots_per_account", "schedule"))

    # ── HTTP / 재시도 ────────────────────────────────────────────────────
    net = _section(doc, "network")
    g["MAX_CONCURRENT"] = int(_get(net, "max_concurrent", "network"))
    g["CONNECTION_TIMEOUT"] = int(_get(net, "connection_timeout", "network"))
    g["READ_TIMEOUT"] = int(_get(net, "read_timeout", "network"))
    g["MAX_RETRIES"] = int(_get(net, "max_retries", "network"))
    g["RETRY_DELAY_MIN"] = float(_get(net, "retry_delay_min", "network"))
    g["RETRY_DELAY_MAX"] = float(_get(net, "retry_delay_max", "network"))
    g["SUBMIT_MAX_ATTEMPTS"] = int(_get(net, "submit_max_attempts", "network"))
    g["CRITICAL_MAX_RETRIES"] = int(_get(net, "critical_max_retries", "network"))
    g["RETRY_BACKOFF_BASE"] = float(_get(net, "retry_backoff_base", "network"))
    g["RETRY_BACKOFF_MAX"] = float(_get(net, "retry_backoff_max", "network"))
    g["FIRE_JITTER_MS"] = int(_get(net, "fire_jitter_ms", "network"))
    g["SESSION_RETRY_TOTAL"] = int(_get(net, "session_retry_total", "network"))
    g["SESSION_RETRY_BACKOFF"] = float(_get(net, "session_retry_backoff", "network"))
    g["SESSION_POOL_SIZE"] = int(_get(net, "session_pool_size", "network"))

    # ── 브라우저 모드 ────────────────────────────────────────────────────
    browser = _section(doc, "browser")
    g["HEADLESS"] = bool(_get(browser, "headless", "browser"))
    g["PAGE_LOAD_TIMEOUT"] = int(_get(browser, "page_load_timeout", "browser"))
    g["ELEMENT_WAIT_TIMEOUT"] = int(_get(browser, "element_wait_timeout", "browser"))

    # ── API 서버 ─────────────────────────────────────────────────────────
    api = _section(doc, "api")
    g["API_HOST"] = _get(api, "host", "api")
    g["API_PORT"] = int(_get(api, "port", "api"))

    # ── 다중 계정 런처 ───────────────────────────────────────────────────
    # tmux 2×2 / iTerm2 split 레이아웃이 4분할까지만 있어서, 5 이상이면
    # 스크립트는 만들어지지만 5번째부터는 pane이 없어 실행되지 않는다.
    # 계정이 조용히 누락되는 것을 막으려고 여기서 막는다.
    launcher = _section(doc, "launcher")
    group_size = int(_get(launcher, "group_size", "launcher"))
    if not 1 <= group_size <= 4:
        raise ConfigError(
            f"[설정 오류] config.toml [launcher] group_size = {group_size}\n"
            f"  → 1~4 만 가능합니다 (터미널 분할이 4개까지만 지원됩니다)."
        )
    g["GROUP_SIZE"] = group_size

    # ── 계정 (accounts.txt) ──────────────────────────────────────────────
    # --account 없이 실행하면 첫 계정으로 동작한다.
    accounts = _parse_accounts_file(ACCOUNTS_FILE)
    g["_ACCOUNTS"] = accounts
    default = accounts[0] if accounts else None
    g["USER_ID"] = default["user_id"] if default else None
    g["USER_PW"] = default["user_pw"] if default else None

    # 예약은 reservation.json 에 있다. reserve.py 가 실행할 계정의 것을 주입한다.
    # 여기서 schedule 을 import 하면 순환이 되므로 빈 목록으로 둔다.
    g["RESERVATION_CONFIG"] = {"reservations": []}


def reload():
    """config.toml·accounts.txt 를 다시 읽어 모듈 상수를 갱신한다."""
    if not CONFIG_FILE.exists():
        raise ConfigError(
            f"[설정 오류] {CONFIG_FILE.name} 이 없습니다.\n"
            f"  → 저장소에 커밋된 {CONFIG_FILE.name} 을 복원하세요 (비밀 정보 없음)."
        )
    with open(CONFIG_FILE, "rb") as f:
        _apply(tomllib.load(f))


def load_accounts():
    """accounts.txt 의 계정 목록을 반환한다.

    Returns:
        list of dict: [{"num": 표시순번, "name", "user_id", "user_pw"}, ...]
    """
    return list(_ACCOUNTS)


def find_account(user_id):
    """아이디로 계정을 찾는다. 비밀번호를 얻는 유일한 경로다.

    Returns:
        dict or None
    """
    return next((a for a in _ACCOUNTS if a["user_id"] == user_id), None)


reload()
