# -*- coding: utf-8 -*-
"""
이번 회차 예약 계획 (reservation.json) 로드·저장

이 파일이 "무엇을 실행할지"를 결정한다. 비밀번호는 들어 있지 않고 아이디만
있으므로, 계획을 열어 보거나 다른 PC로 복사해도 자격증명이 따라가지 않는다.
비밀번호는 accounts.txt 에만 있고 config.find_account(id) 로 조회한다.

    import schedule
    plan = schedule.load()              # entries 목록 + 메타
    schedule.save(entries)              # viewer.py 저장 버튼

reservation.json 형식:

    {
      "version": 1,
      "generated_at": "2026-08-27T11:23:45+09:00",
      "entries": [
        {"no": 1, "id": "user1", "name": "홍길동",
         "slots": ["2026-09-05:8:3", "2026-09-06:8:3"]}
      ]
    }

- no    저장할 때마다 1부터 다시 부여하는 일련번호. main.py --only 가 이걸 쓴다.
        accounts.txt 행 번호와 다르다 (체크 안 된 계정은 빠지고 번호가 당겨진다).
- id    유일한 식별자. accounts.txt 에서 비밀번호를 찾는 키.
- name  사람이 읽기 위한 것. 로더는 무시한다 (진실은 accounts.txt).
"""

import json
from datetime import datetime
from pathlib import Path

import config

SCHEDULE_FILE = Path(__file__).parent / "reservation.json"
VERSION = 1


class ScheduleError(Exception):
    """예약 계획이 잘못됐다. 실행을 시작하지 않는다."""


def _require(cond, msg):
    if not cond:
        raise ScheduleError(f"[예약 계획 오류] {msg}")


def load(path=None):
    """reservation.json 을 읽어 검증한 계획을 반환한다.

    검증:
      - version 이 아는 값인가
      - slots 형식 (config.parse_slots 재사용 — 날짜·시간·코트)
      - **모든 id 가 accounts.txt 에 있는가** (위치가 아닌 이름으로 잇는 근거)
      - 같은 계정에 같은 날짜가 두 번 있는가 → 경고 목록으로 반환

    Returns:
        dict: {"generated_at": str, "entries": [...], "warnings": [str, ...]}
              entries 항목: {"no", "id", "name", "user_pw", "slots"(파싱됨), "raw_slots"}
    """
    path = Path(path) if path else SCHEDULE_FILE
    if not path.exists():
        raise ScheduleError(
            f"[예약 계획 오류] {path.name} 이 없습니다.\n"
            f"  → python3 viewer.py 에서 예약을 배정하고 [저장] 을 누르세요."
        )

    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as e:
        raise ScheduleError(f"[예약 계획 오류] {path.name} JSON 파싱 실패: {e}")

    _require(isinstance(doc, dict), f"{path.name} 최상위가 객체가 아닙니다.")
    version = doc.get("version")
    _require(
        version == VERSION,
        f"{path.name} version={version!r} 은 이 프로그램이 모르는 형식입니다 "
        f"(아는 값: {VERSION}). viewer.py 에서 다시 저장하세요.",
    )

    raw_entries = doc.get("entries")
    _require(isinstance(raw_entries, list), f"{path.name} 의 entries 가 배열이 아닙니다.")
    _require(
        raw_entries,
        f"{path.name} 에 실행할 계정이 없습니다.\n"
        f"  → viewer.py 에서 계정을 체크하고 슬롯을 배정한 뒤 [저장] 을 누르세요.",
    )

    entries, warnings, seen_ids = [], [], {}
    for i, raw in enumerate(raw_entries, start=1):
        where = f"entries[{i}]"
        _require(isinstance(raw, dict), f"{where} 가 객체가 아닙니다.")

        user_id = str(raw.get("id", "")).strip()
        _require(user_id, f"{where} 에 id 가 없습니다.")
        _require(
            user_id not in seen_ids,
            f"{where}: 아이디 '{user_id}' 가 entries[{seen_ids.get(user_id)}] 와 중복입니다.",
        )
        seen_ids[user_id] = i

        # 위치가 아니라 이름으로 잇는다. 계정을 지웠으면 여기서 즉시 드러난다.
        acct = config.find_account(user_id)
        _require(
            acct is not None,
            f"{where}: 아이디 '{user_id}' 가 {config.ACCOUNTS_FILE.name} 에 없습니다.\n"
            f"  → 계정을 지웠다면 viewer.py 에서 다시 배정하고 저장하세요.",
        )

        raw_slots = raw.get("slots") or []
        _require(isinstance(raw_slots, list), f"{where}.slots 가 배열이 아닙니다.")
        _require(raw_slots, f"{where} ('{user_id}') 의 slots 가 비어 있습니다.")
        slots = config.parse_slots(raw_slots, f"{where}.slots")

        # 서버가 계정당 하루 1건만 허용한다 — 같은 날짜 2건은 정각에 반드시 1건 실패
        dates = [s["date"] for s in slots]
        dup = sorted({d for d in dates if dates.count(d) > 1})
        if dup:
            warnings.append(
                f"계정 '{user_id}' 에 같은 날짜가 여러 건입니다: {', '.join(dup)} "
                f"(서버가 하루 1건만 허용하므로 나머지는 실패합니다)"
            )

        entries.append({
            "no": i,                       # 파일의 no 는 신뢰하지 않고 순서로 다시 매긴다
            "id": user_id,
            "name": acct["name"],          # 표시는 accounts.txt 기준
            "user_pw": acct["user_pw"],
            "slots": slots,
            "raw_slots": [str(x) for x in raw_slots],
        })

    return {
        "generated_at": str(doc.get("generated_at", "")),
        "entries": entries,
        "warnings": warnings,
    }


def find_entry(user_id, plan=None):
    """아이디로 계획 항목 하나를 찾는다. 없으면 None."""
    plan = plan or load()
    return next((e for e in plan["entries"] if e["id"] == user_id), None)


def save(entries, path=None):
    """viewer.py 의 [저장] — 계획 전체를 새로 쓴다.

    Args:
        entries: [{"id": str, "name": str, "slots": ["날짜:시간:코트", ...]}, ...]
                 slots 가 빈 항목은 실행 대상이 아니므로 제외한다.

    - no 는 여기서 1부터 다시 부여한다.
    - slots 는 날짜 → 시간 → 코트 순으로 정렬한다.
    - 기존 파일은 reservation.json.bak.YYYYMMDD_HHMMSS 로 백업한다.

    Returns:
        (ok: bool, detail: dict | str)
        detail: {"accounts": N, "slots": M, "warnings": [...]}
    """
    path = Path(path) if path else SCHEDULE_FILE

    out, warnings, total = [], [], 0
    for raw in entries:
        user_id = str(raw.get("id", "")).strip()
        if not user_id:
            return False, "id 가 없는 항목이 있습니다."
        acct = config.find_account(user_id)
        if acct is None:
            return False, f"아이디 '{user_id}' 가 {config.ACCOUNTS_FILE.name} 에 없습니다."

        raw_slots = [str(x) for x in (raw.get("slots") or [])]
        if not raw_slots:
            continue  # 배정이 없는 계정은 계획에 넣지 않는다

        try:
            parsed = config.parse_slots(raw_slots, f"'{user_id}' 의 slots")
        except config.ConfigError as e:
            return False, str(e)

        parsed.sort(key=lambda s: (s["date"], s["hour"], s["court"]))
        sorted_slots = [f"{s['date']}:{s['hour']}:{s['court']}" for s in parsed]

        dates = [s["date"] for s in parsed]
        dup = sorted({d for d in dates if dates.count(d) > 1})
        if dup:
            warnings.append(f"{acct['name'] or user_id}: 같은 날짜 중복 {', '.join(dup)}")

        total += len(sorted_slots)
        out.append({
            "no": len(out) + 1,
            "id": user_id,
            "name": acct["name"],
            "slots": sorted_slots,
        })

    if path.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak = path.parent / f"{path.name}.bak.{ts}"
        bak.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")

    doc = {
        "version": VERSION,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "entries": out,
    }
    path.write_text(
        json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return True, {"accounts": len(out), "slots": total, "warnings": warnings}
