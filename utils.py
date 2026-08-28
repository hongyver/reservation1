#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
공통 유틸리티: 예약 타이밍 대기 함수 (동기 + 비동기), CLI 인자 공유 정의
"""

import asyncio
import time
import config
from datetime import datetime, timedelta


def add_worker_args(container):
    """단일 계정 실행 플래그를 파서(또는 argument group)에 추가한다.

    main.py(런처)와 reserve.py(워커)가 같은 정의를 쓴다. 정의가 갈리면
    런처가 받아들인 인자를 워커가 거부하는 조합이 생긴다.

    런처는 --account/--search/--search2 가 오면 reserve.py 로 프로세스를
    넘기고(위임), --browser 만 각 창의 reserve.py 에 그대로 전달한다.
    """
    container.add_argument("--account", metavar="ID",
                           help="실행할 계정 아이디 (미지정 시 accounts.txt 첫 계정)")
    container.add_argument("--browser", action="store_true",
                           help="브라우저 모드로 실행 (Selenium)")
    container.add_argument("--search", metavar="MONTH",
                           help="주말 예약 가능 시간 검색 (예: 2 또는 2026-02)")
    container.add_argument("--search2", metavar="MONTH",
                           help="전체 날짜/시간 예약 가능 시간 검색 (예: 2 또는 2026-02)")


def parse_open_time(value):
    """--test 값(초 또는 HH:MM)을 오늘의 오픈 시각으로 변환한다.

    오픈 시각은 분 경계만 지원하므로 상대 초는 다음 분 경계로 올림한다.
    main.py(런처)와 reserve.py(워커)가 같은 규칙을 써야 한다 — 규칙이 갈리면
    런처가 넘긴 시각을 워커가 다르게 해석해 계정마다 오픈 시각이 어긋난다.

    Raises:
        ValueError: 이미 지난 시각이거나 자정을 넘기는 경우
    """
    now = datetime.now()
    if ":" in value:
        h, m = value.split(":", 1)
        if not (h.isdigit() and m.isdigit() and int(h) < 24 and int(m) < 60):
            raise ValueError(f"HH:MM 형식이 아닙니다: {value!r}")
        target = now.replace(hour=int(h), minute=int(m), second=0, microsecond=0)
        if target <= now:
            raise ValueError(f"이미 지난 시각입니다: {value}")
    else:
        if not value.isdigit():
            raise ValueError(f"초(숫자) 또는 HH:MM 이어야 합니다: {value!r}")
        target = now + timedelta(seconds=int(value))
        if target.second or target.microsecond:
            target = target.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if target.day != now.day:
            raise ValueError("자정을 넘기는 시각은 지원하지 않습니다")
    return target


def wait_before_login():
    """예약 오픈 N분(LOGIN_ADVANCE_MINUTES) 전까지 로그인 없이 대기.

    - RESERVATION_DAY == 0: 즉시 통과
    - 오늘이 예약일이 아닌 경우: 즉시 통과 (기존 검증 함수가 처리)
    - 남은 시간 > N분: N분 전 시각까지 슬립 대기
    - 남은 시간 <= N분: 즉시 통과
    """
    if config.RESERVATION_DAY == 0:
        return

    now = datetime.now()
    if now.day != config.RESERVATION_DAY:
        return

    target = now.replace(
        hour=config.RESERVATION_HOUR,
        minute=config.RESERVATION_MINUTE,
        second=0,
        microsecond=0
    )
    login_time = target - timedelta(minutes=config.LOGIN_ADVANCE_MINUTES)
    remaining = (login_time - now).total_seconds()

    if remaining <= 0:
        return

    print(f"[PRE-WAIT] 예약 오픈({target.strftime('%H:%M')})까지 {(target - now).total_seconds():.0f}초 남았습니다.")
    print(f"[PRE-WAIT] 로그인은 {config.LOGIN_ADVANCE_MINUTES}분 전({login_time.strftime('%H:%M:%S')})부터 시작합니다.")

    while True:
        now = datetime.now()
        remaining = (login_time - now).total_seconds()
        if remaining <= 0:
            break
        if remaining > 60:
            print(f"\r[PRE-WAIT] 로그인까지 {remaining:.0f}초 남음 (슬립 중)", end="", flush=True)
            time.sleep(30)
        else:
            print(f"\r[PRE-WAIT] 로그인까지 {remaining:.0f}초 남음", end="", flush=True)
            time.sleep(1)

    print()
    print(f"[INFO] {config.LOGIN_ADVANCE_MINUTES}분 전 도달. 로그인을 시작합니다.")


def wait_for_reservation_open():
    """예약 오픈 시간까지 대기.

    RESERVATION_DAY = 0이면 바로 실행
    RESERVATION_DAY != 0이면:
      - 오늘이 예약일과 같으면: 예약 시간까지 대기
      - 오늘이 예약일보다 크면: 에러 (이미 지남)
      - 오늘이 예약일보다 작으면: 에러 (아직 예약일이 아님)

    Returns:
        bool: 성공 시 True, 실행 불가 시 False
    """
    if config.RESERVATION_DAY == 0:
        print("[INFO] 즉시 실행 모드")
        return True

    now = datetime.now()

    if now.day != config.RESERVATION_DAY:
        if now.day > config.RESERVATION_DAY:
            print(f"[ERROR] 이번 달 예약일({config.RESERVATION_DAY}일)이 이미 지났습니다.")
            print(f"[ERROR] 오늘은 {now.month}월 {now.day}일입니다.")
            return False
        else:
            print(f"[ERROR] 아직 예약일이 아닙니다.")
            print(f"[ERROR] 오늘은 {now.month}월 {now.day}일이고, 예약일은 매월 {config.RESERVATION_DAY}일입니다.")
            return False

    target = now.replace(
        hour=config.RESERVATION_HOUR,
        minute=config.RESERVATION_MINUTE,
        second=0,
        microsecond=0
    )

    if now >= target:
        print(f"[INFO] 예약 시간({config.RESERVATION_HOUR}:{config.RESERVATION_MINUTE:02d})이 지났습니다. 바로 진행합니다.")
        return True

    wait_seconds = (target - now).total_seconds()
    print(f"[INFO] 예약 오픈까지 {wait_seconds:.0f}초 남았습니다.")
    print(f"[INFO] 목표 시간: {target.strftime('%Y-%m-%d %H:%M:%S')}")

    while True:
        now = datetime.now()
        remaining = (target - now).total_seconds()

        if remaining <= 0:
            break

        if remaining > 10:
            print(f"\r[WAIT] 남은 시간: {remaining:.0f}초", end="", flush=True)
            time.sleep(1)
        else:
            print(f"\r[READY] {remaining:.1f}초 후 시작!", end="", flush=True)
            time.sleep(0.05)

    print("[GO!] 예약을 시작합니다!")
    return True


# ============================================================
# 비동기 버전 (asyncio + aiohttp 모드용)
# ============================================================

async def wait_before_login_async():
    """예약 오픈 전 N분(LOGIN_ADVANCE_MINUTES)까지 비동기 대기 (이벤트 루프 블로킹 없음).

    - RESERVATION_DAY == 0: 즉시 통과
    - 오늘이 예약일이 아닌 경우: 즉시 통과
    - 남은 시간 > N분: 30초 단위 asyncio.sleep
    - 남은 시간 <= N분: 1초 단위 asyncio.sleep
    """
    if config.RESERVATION_DAY == 0:
        return

    now = datetime.now()
    if now.day != config.RESERVATION_DAY:
        return

    target = now.replace(
        hour=config.RESERVATION_HOUR,
        minute=config.RESERVATION_MINUTE,
        second=0,
        microsecond=0
    )
    login_time = target - timedelta(minutes=config.LOGIN_ADVANCE_MINUTES)
    remaining = (login_time - now).total_seconds()

    if remaining <= 0:
        return

    print(f"[PRE-WAIT] 예약 오픈({target.strftime('%H:%M')})까지 {(target - now).total_seconds():.0f}초 남았습니다.")
    print(f"[PRE-WAIT] 로그인은 {config.LOGIN_ADVANCE_MINUTES}분 전({login_time.strftime('%H:%M:%S')})부터 시작합니다.")

    while True:
        now = datetime.now()
        remaining = (login_time - now).total_seconds()
        if remaining <= 0:
            break
        if remaining > 60:
            print(f"\r[PRE-WAIT] 로그인까지 {remaining:.0f}초 남음 (대기 중)", end="", flush=True)
            await asyncio.sleep(min(remaining - 60, 30))
        else:
            print(f"\r[PRE-WAIT] 로그인까지 {remaining:.0f}초 남음", end="", flush=True)
            await asyncio.sleep(1)

    print()
    print(f"[INFO] {config.LOGIN_ADVANCE_MINUTES}분 전 도달. 로그인을 시작합니다.")


async def wait_for_reservation_open_async(warmup=None):
    """예약 오픈 시간까지 비동기 정밀 대기.

    RESERVATION_DAY = 0이면 바로 실행
    RESERVATION_DAY != 0이면:
      - 오늘이 예약일과 같으면: 예약 시간까지 대기 (10ms 정밀도)
      - 그 외: False 반환

    Args:
        warmup: 오픈 직전 연결 재예열용 async 콜백.
                남은 시간이 20초/4초 이하가 되는 시점에 각 1회,
                fire-and-forget 태스크로 실행되어 정각 시작을 지연시키지 않는다.

    Returns:
        bool: 성공 시 True, 실행 불가 시 False
    """
    if config.RESERVATION_DAY == 0:
        print("[INFO] 즉시 실행 모드")
        return True

    now = datetime.now()

    if now.day != config.RESERVATION_DAY:
        if now.day > config.RESERVATION_DAY:
            print(f"[ERROR] 이번 달 예약일({config.RESERVATION_DAY}일)이 이미 지났습니다.")
            print(f"[ERROR] 오늘은 {now.month}월 {now.day}일입니다.")
        else:
            print(f"[ERROR] 아직 예약일이 아닙니다.")
            print(f"[ERROR] 오늘은 {now.month}월 {now.day}일이고, 예약일은 매월 {config.RESERVATION_DAY}일입니다.")
        return False

    target = now.replace(
        hour=config.RESERVATION_HOUR,
        minute=config.RESERVATION_MINUTE,
        second=0,
        microsecond=0
    )

    if now >= target:
        print(f"[INFO] 예약 시간({config.RESERVATION_HOUR}:{config.RESERVATION_MINUTE:02d})이 지났습니다. 바로 진행합니다.")
        return True

    wait_seconds = (target - now).total_seconds()
    print(f"[INFO] 예약 오픈까지 {wait_seconds:.0f}초 남았습니다.")
    print(f"[INFO] 목표 시간: {target.strftime('%Y-%m-%d %H:%M:%S')}")

    # keepalive(클라 30초, 서버는 보통 수 초)가 끊기지 않은 상태로 정각을 맞도록
    # 남은 20초(TLS 세션 확보)·4초(최종 예열) 시점에 재예열을 트리거한다.
    warmup_marks = [20, 4]
    warmup_tasks = []  # fire-and-forget 태스크 GC 방지용 참조

    while True:
        now = datetime.now()
        remaining = (target - now).total_seconds()

        if remaining <= 0:
            break

        if warmup and warmup_marks and remaining <= warmup_marks[0]:
            warmup_marks.pop(0)
            print(f"\n[WARMUP] 연결 재예열 (남은 {remaining:.1f}초)")
            warmup_tasks.append(asyncio.create_task(warmup()))

        if remaining > 10:
            print(f"\r[WAIT] 남은 시간: {remaining:.0f}초", end="", flush=True)
            await asyncio.sleep(1)
        else:
            # 마지막 10초: 10ms 정밀도로 대기
            print(f"\r[READY] {remaining:.3f}초 후 시작!", end="", flush=True)
            await asyncio.sleep(0.01)

    print("\n[GO!] 예약을 시작합니다!")
    return True
