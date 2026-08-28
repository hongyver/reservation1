#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
단일 계정 예약 실행 (HTTP 기반 - 브라우저 불필요)
매월 25일 10시 예약용.

전 계정을 한 번에 돌리려면 main.py(런처)를 쓴다. main.py가 계정마다
이 파일을 `--account N` 으로 띄운다.

사용법:
    python3 reserve.py --logincheck  # 로그인 테스트
    python3 reserve.py --test        # 테스트 모드 (즉시 실행, 대관신청 전 멈춤)
    python3 reserve.py               # 실제 예약 (1번 계정, 대관신청까지 진행)
    python3 reserve.py --account 3   # 3번 계정으로 실행
    python3 reserve.py --browser     # 브라우저 모드로 실행 (Selenium)
    python3 reserve.py --search 2    # 2월 주말 예약 가능 시간 검색
    python3 reserve.py --search 2026-02  # 2026년 2월 검색
    python3 reserve.py --test 300    # 오픈을 300초 후로 강제 (대기→예열→정각 발사 재현)
    python3 reserve.py --test 14:30  # 오픈 시각 직접 지정
"""

import asyncio
import sys
import argparse
from datetime import datetime
import urllib3
import getpass

# SSL 경고 비활성화
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

import config
from reservation_async import (
    run_reservation_async,
    search_available_slots_async,
    search_all_slots_async,
)
from reservation_http import TennisReservationHTTP
import schedule
from utils import add_worker_args, parse_open_time


def get_credentials():
    """로그인 정보 가져오기 (환경변수 또는 입력)

    Returns:
        tuple: (user_id, user_pw)
    """
    user_id = config.USER_ID
    user_pw = config.USER_PW

    # ID/PW가 없으면 입력받기
    if not user_id or not user_pw:
        print()
        print("=" * 60)
        print("로그인 정보 입력")
        print("=" * 60)
        print()
        print("[INFO] accounts.txt 에 로그인 정보가 없습니다.")
        print("[INFO] 직접 입력하시거나, accounts.txt 에 한 줄 추가하세요:")
        print("[INFO]   이름,아이디,비밀번호")
        print()

        if not user_id:
            user_id = input("아이디: ").strip()

        if not user_pw:
            user_pw = getpass.getpass("비밀번호: ")

        print()

    if not user_id or not user_pw:
        print("[ERROR] 아이디 또는 비밀번호가 입력되지 않았습니다.")
        return None, None

    return user_id, user_pw


def print_config(require_reservations=True):
    """설정 출력.

    --logincheck 는 예약이 필요 없다. 그런데도 여기서 막고 있었다 —
    예전에는 config.RESERVATION_CONFIG 가 항상 "첫 계정의 예약"으로 채워져
    우연히 통과했지만, 예약이 reservation.json 으로 빠진 뒤로는 로그인 테스트가
    이 관문에 걸려 실행되지 않았다.
    """
    print("=" * 60)
    print("고양시 테니스장 자동 예약 프로그램 (HTTP)")
    print("=" * 60)
    print()

    cfg = config.RESERVATION_CONFIG

    # 로그인 정보 마스킹
    user_id = config.USER_ID
    if user_id and len(user_id) > 2:
        masked_id = user_id[0] + "*" * (len(user_id) - 2) + user_id[-1]
    elif user_id:
        masked_id = user_id[0] + "*"
    else:
        masked_id = "(입력 필요)"

    print("[설정 확인]")
    print(f"  사이트: {config.MAIN_URL}")
    print(f"  로그인 ID: {masked_id}")
    print()

    reservations = cfg["reservations"]
    if not reservations:
        if require_reservations:
            print(f"[ERROR] {schedule.SCHEDULE_FILE.name} 에 이 계정의 예약이 없습니다.")
            print("  → python3 viewer.py 에서 계정을 체크해 배정하고 [저장] 을 누르세요.")
            return False
        print("  예약: 없음 (로그인 테스트 모드)")
        print()
        return True

    print(f"  예약 {len(reservations)}건:")
    for i, res in enumerate(reservations, 1):
        print(f"    [{i}] {res['date']} {res['hour']:02d}:00~{res['hour']+2:02d}:00 / {res['court']}번 코트")
    task_count = len(reservations)

    print()
    if task_count > 1:
        print(f"  → 총 {task_count}개 병렬 실행 (동시 접속 제한: {config.MAX_CONCURRENT}개)")
    print()

    if config.RESERVATION_DAY == 0:
        print("  예약 오픈: 즉시 실행")
    else:
        print(f"  예약 오픈: 매월 {config.RESERVATION_DAY}일 {config.RESERVATION_HOUR}시 {config.RESERVATION_MINUTE}분")
        print("  → 로그인 후 해당 시간까지 대기")
    print()

    print("[접속 폭주 대응 설정]")
    print(f"  최대 재시도: {config.MAX_RETRIES}회")
    print(f"  타임아웃: 연결 {config.CONNECTION_TIMEOUT}초, 읽기 {config.READ_TIMEOUT}초")
    print()

    return True


def test_login(user_id, user_pw):
    """로그인 테스트 (HTTP)"""
    print("[TEST] 로그인 테스트 (HTTP)")
    print()

    bot = TennisReservationHTTP()

    if not bot.login(user_id, user_pw):
        print("[FAIL] 로그인 실패")
        return False

    print()

    # 예약 페이지 접근 테스트
    # 예약 조건이 없는 계정(--logincheck 전용)도 페이지 접근은 확인할 수 있어야 한다
    reservations = config.RESERVATION_CONFIG["reservations"]
    if reservations:
        court, date = reservations[0]["court"], reservations[0]["date"]
    else:
        court, date = config.ALL_COURTS[0], datetime.now().strftime("%Y-%m-%d")

    dt = datetime.strptime(date, "%Y-%m-%d")
    html = bot.get_reservation_page(court, dt.year, dt.month, dt.day)

    if not html:
        print("[FAIL] 예약 페이지 접근 실패")
        return False

    slots = bot.get_available_slots(html)
    print(f"[INFO] 예약 가능 시간대: {[s['start'] for s in slots]}")
    print()

    print("[SUCCESS] 모든 테스트 통과!")
    bot.close()
    return True


def test_login_browser(user_id, user_pw):
    """로그인 테스트 (브라우저)"""
    try:
        from reservation import TennisReservationBot
    except ImportError:
        print("[ERROR] Selenium 모듈이 없습니다.")
        print("[INFO] pip3 install selenium webdriver-manager")
        return False

    print("[TEST] 로그인 및 페이지 접근 테스트 (브라우저)")
    print()

    bot = TennisReservationBot()
    try:
        bot.setup_browser()

        if not bot.login(user_id, user_pw):
            print("[FAIL] 로그인 실패")
            return False

        print()
        if not bot.go_to_reservation_page():
            print("[FAIL] 예약 페이지 접근 실패")
            return False

        reservations = config.RESERVATION_CONFIG["reservations"]
        court = reservations[0]["court"] if reservations else config.ALL_COURTS[0]
        if not bot.select_court(court):
            print(f"[FAIL] {court}번 코트 선택 실패")
            return False

        print()
        print("[SUCCESS] 모든 테스트 통과!")
        print("[INFO] 브라우저를 확인하세요. (30초 후 종료)")

        import time
        time.sleep(30)

        return True

    finally:
        bot.close()


def check_reservation_day():
    """예약 가능한 날인지 확인

    RESERVATION_DAY가 0이면 바로 실행
    0이 아니면 해당 날짜에만 실행
    """
    # RESERVATION_DAY가 0이면 바로 실행
    if config.RESERVATION_DAY == 0:
        return True

    today = datetime.now()

    if today.day != config.RESERVATION_DAY:
        print(f"[INFO] 오늘은 {today.month}월 {today.day}일입니다.")
        print(f"[INFO] 예약 오픈은 매월 {config.RESERVATION_DAY}일입니다.")
        print("[INFO] 예약일이 아니므로 프로그램을 종료합니다.")
        return False

    return True


def run_browser_mode(test_mode=False, user_id=None, user_pw=None):
    """브라우저 모드 실행"""
    try:
        from reservation import run_reservation
    except ImportError:
        print("[ERROR] Selenium 모듈이 없습니다.")
        print("[INFO] pip3 install selenium webdriver-manager")
        return False

    return run_reservation(test_mode=test_mode, user_id=user_id, user_pw=user_pw)


def parse_search_month(search_arg):
    """검색 월 파싱

    Args:
        search_arg: "2" (현재 연도 2월) 또는 "2026-02" (2026년 2월)

    Returns:
        tuple: (year, month)
    """
    now = datetime.now()

    if "-" in search_arg:
        # 2026-02 형식
        parts = search_arg.split("-")
        year = int(parts[0])
        month = int(parts[1])
    else:
        # 2 형식 (현재 연도)
        month = int(search_arg)
        year = now.year

        # 이미 지난 달이면 다음 연도로
        if month < now.month:
            year += 1

    return year, month


def main():
    parser = argparse.ArgumentParser(description="고양시 테니스장 자동 예약")
    parser.add_argument("--test", nargs="?", const=True, default=None,
                        metavar="초|HH:MM",
                        help="테스트 모드 (대관신청 전 멈춤). 값을 주면 그 시각을 "
                             "오픈으로 강제해 대기→예열→정각 발사까지 재현한다. "
                             "값이 없으면 오픈을 기다리지 않고 즉시 실행")
    parser.add_argument("--logincheck", action="store_true",
                        help="로그인 테스트")
    add_worker_args(parser)
    args = parser.parse_args()

    # --account ID: 비밀번호는 accounts.txt, 예약은 reservation.json 에서 가져온다
    if args.account is not None:
        acct = config.find_account(args.account)
        if acct is None:
            ids = ", ".join(a["user_id"] for a in config.load_accounts())
            print(f"[ERROR] 아이디 '{args.account}' 가 "
                  f"{config.ACCOUNTS_FILE.name} 에 없습니다.")
            print(f"  → 가능한 아이디: {ids}")
            sys.exit(1)
        config.USER_ID = acct["user_id"]
        config.USER_PW = acct["user_pw"]
        label = f" {acct['name']}" if acct["name"] else ""
        print(f"[계정 {acct['user_id']}{label}] 로 실행합니다.")

    # 예약 계획 주입 — 로그인·검색은 예약이 없어도 되므로 건너뛴다
    if not (args.logincheck or args.search or args.search2):
        try:
            entry = schedule.find_entry(config.USER_ID)
        except schedule.ScheduleError as e:
            print(f"[ERROR] {e}")
            sys.exit(1)
        if entry is None:
            print(f"[ERROR] 계정 '{config.USER_ID}' 가 "
                  f"{schedule.SCHEDULE_FILE.name} 에 없습니다.")
            print("  → python3 viewer.py 에서 이 계정을 체크해 예약을 배정하고 저장하세요.")
            sys.exit(1)
        config.RESERVATION_CONFIG = {"reservations": entry["slots"]}

    # --test: 항상 대관신청 직전에 멈춘다. 오픈 시각만 값 유무로 갈린다.
    #   값 없음 → 즉시 실행 (RESERVATION_DAY=0). 예약일이 아니어도 항상 동작한다
    #   값 있음 → 그 시각을 오픈으로 강제해 대기→예열→정각 발사를 실전대로 재현
    if args.test is not None:
        if args.test is True:
            config.RESERVATION_DAY = 0
            print("[TEST] 테스트 모드: 오픈을 기다리지 않고 즉시 실행합니다.")
        else:
            try:
                target = parse_open_time(args.test)
            except ValueError as e:
                print(f"[ERROR] --test: {e}")
                sys.exit(1)
            config.RESERVATION_DAY = target.day
            config.RESERVATION_HOUR = target.hour
            config.RESERVATION_MINUTE = target.minute
            print(f"[TEST] 테스트 모드: 오픈 {target.strftime('%H:%M')} 강제 "
                  f"({(target - datetime.now()).total_seconds():.0f}초 후)")
            print("[TEST] 로그인 → T-20초 예열 → T-4초 재예열 → 정각 발사까지 검증")
        print("[TEST] 최종 대관신청 직전에 멈춥니다.")

    # 검색 모드는 설정 출력 생략하지만 로그인 정보는 필요
    if args.search or args.search2:
        # 로그인 정보 가져오기
        user_id, user_pw = get_credentials()
        if not user_id or not user_pw:
            sys.exit(1)

    # 주말 검색 모드
    if args.search:
        try:
            year, month = parse_search_month(args.search)
            result = asyncio.run(
                search_available_slots_async(year, month, user_id=user_id, user_pw=user_pw)
            )
            sys.exit(0 if result.get("total", 0) > 0 else 1)
        except ValueError:
            print(f"[ERROR] 잘못된 월 형식: {args.search}")
            print("[INFO] 사용법: --search 2 또는 --search 2026-02")
            sys.exit(1)

    # 전체 검색 모드
    if args.search2:
        try:
            year, month = parse_search_month(args.search2)
            result = asyncio.run(
                search_all_slots_async(year, month, user_id=user_id, user_pw=user_pw)
            )
            sys.exit(0 if result.get("total", 0) > 0 else 1)
        except ValueError:
            print(f"[ERROR] 잘못된 월 형식: {args.search2}")
            print("[INFO] 사용법: --search2 2 또는 --search2 2026-02")
            sys.exit(1)

    if not print_config(require_reservations=not args.logincheck):
        sys.exit(1)

    # 로그인 정보 가져오기
    user_id, user_pw = get_credentials()
    if not user_id or not user_pw:
        sys.exit(1)

    # 하위 모듈이 config.USER_ID/PW를 기본값으로 쓴다
    config.USER_ID = user_id
    config.USER_PW = user_pw

    # 로그인 테스트
    if args.logincheck:
        if args.browser:
            success = test_login_browser(user_id, user_pw)
        else:
            success = test_login(user_id, user_pw)
        sys.exit(0 if success else 1)

    # 테스트 모드 (오픈 시각 설정은 위에서 이미 끝났다)
    if args.test is not None:
        if args.browser:
            success = run_browser_mode(test_mode=True, user_id=user_id, user_pw=user_pw)
        else:
            result = asyncio.run(
                run_reservation_async(test_mode=True, user_id=user_id, user_pw=user_pw)
            )
            success = result.get("success", False)

        sys.exit(0 if success else 1)

    # 일반 실행
    if not check_reservation_day():
        sys.exit(0)

    print("[INFO] 예약 프로그램을 시작합니다.")
    print("[INFO] 최종 대관신청까지 자동 진행됩니다.")
    print("[INFO] Ctrl+C를 눌러 중단할 수 있습니다.")
    print()

    if args.browser:
        success = run_browser_mode(test_mode=False, user_id=user_id, user_pw=user_pw)
    else:
        result = asyncio.run(
            run_reservation_async(test_mode=False, user_id=user_id, user_pw=user_pw)
        )
        success = result.get("success", False)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
