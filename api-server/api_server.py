#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
고양시 테니스장 예약 API 서버
n8n에서 HTTP Request로 호출하여 사용

사용법:
    python3 api_server.py                    # 기본 포트 5000
    python3 api_server.py --port 8080        # 포트 지정
    python3 api_server.py --host 0.0.0.0     # 외부 접속 허용

n8n 호출 예시:
    POST http://your-server:5000/reserve
    {
        "dates": ["2026-02-02"],
        "hours": [6, 8],
        "court": 2,
        "test_mode": true
    }
"""

import argparse
import asyncio
import sys
from pathlib import Path

# config·reservation_* 는 레포 루트에 있는 공유 모듈이다. 이 파일은 api-server/
# 하위에 있으므로 루트를 import 경로에 넣어 준다.
# Docker 이미지는 파일을 /app 에 평평하게 놓으므로 이 블록이 아무것도 하지 않는다.
_ROOT = Path(__file__).resolve().parent.parent
if (_ROOT / "config.py").exists():
    sys.path.insert(0, str(_ROOT))

import urllib3
from flask import Flask, request, jsonify
from datetime import datetime

import config
from reservation_async import (
    TennisReservationAsync,
    run_reservation_async,
    search_available_slots_async,
    search_all_slots_async,
)
from reservation_http import TennisReservationHTTP

# SSL 경고 비활성화
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

app = Flask(__name__)


@app.route("/health", methods=["GET"])
def health():
    """헬스 체크"""
    return jsonify({
        "status": "ok",
        "timestamp": datetime.now().isoformat()
    })


@app.route("/config", methods=["GET"])
def get_config():
    """현재 설정 조회"""
    return jsonify({
        "config": config.RESERVATION_CONFIG,
        "site_url": config.MAIN_URL,
        "reservation_day": config.RESERVATION_DAY,
        "reservation_hour": config.RESERVATION_HOUR,
        "reservation_minute": config.RESERVATION_MINUTE,
    })


@app.route("/check-login", methods=["POST"])
def check_login():
    """로그인 테스트"""
    data = request.get_json() or {}

    user_id = data.get("user_id") or config.USER_ID
    user_pw = data.get("user_pw") or config.USER_PW

    if not user_id or not user_pw:
        return jsonify({
            "error": "user_id 또는 user_pw 필요 (요청 본문 또는 환경변수)"
        }), 400

    bot = TennisReservationHTTP()
    success = bot.login(user_id, user_pw)

    return jsonify({
        "success": success,
        "message": "로그인 성공" if success else "로그인 실패"
    })


@app.route("/check-slots", methods=["POST"])
def check_slots():
    """예약 가능 시간대 조회"""
    data = request.get_json() or {}

    date_str = data.get("date")
    _reservations = config.RESERVATION_CONFIG["reservations"]
    _default_court = _reservations[0]["court"] if _reservations else config.ALL_COURTS[0]
    court = data.get("court", _default_court)

    if not date_str:
        return jsonify({"error": "date 필드 필요 (YYYY-MM-DD)"}), 400

    try:
        dt = datetime.strptime(date_str, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "잘못된 날짜 형식 (YYYY-MM-DD)"}), 400

    # 로그인 정보
    user_id = data.get("user_id") or config.USER_ID
    user_pw = data.get("user_pw") or config.USER_PW

    if not user_id or not user_pw:
        return jsonify({
            "error": "user_id 또는 user_pw 필요 (요청 본문 또는 환경변수 TENNIS_USER_ID, TENNIS_USER_PW)"
        }), 400

    async def _check():
        async with TennisReservationAsync() as bot:
            if not await bot.login(user_id, user_pw):
                return None, None
            html = await bot.get_reservation_page(court, dt.year, dt.month, dt.day)
            if not html:
                return None, None
            return bot.get_available_slots(html), None

    slots, _ = asyncio.run(_check())

    if slots is None:
        return jsonify({"error": "로그인 실패 또는 예약 페이지 조회 실패"}), 500

    return jsonify({
        "date": date_str,
        "court": court,
        "available_slots": slots
    })


@app.route("/reserve", methods=["POST"])
def reserve():
    """예약 실행

    요청 본문 (방법 1 - 기존 방식):
    {
        "dates": ["2026-02-02", "2026-02-03"],  # 방법 1/3에서 필수
        "hours": [6, 8, 10],                    # 방법 1에서 필수
        "court": 2,                             # 선택사항, 단일 코트
        "courts": [1, 2, 3],                    # 선택사항, 복수 코트 (court와 둘 중 하나)
        "test_mode": false                      # 선택사항, 기본값: false
    }

    요청 본문 (방법 2 - 상세 지정):
    {
        "reservations": [                       # 예약 목록을 직접 지정
            {"date": "2026-02-09", "hour": 8, "court": 1},
            {"date": "2026-02-09", "hour": 10, "court": 1},
            {"date": "2026-02-09", "hour": 6, "court": 2}
        ],
        "test_mode": false
    }

    요청 본문 (방법 3 - 코트별 시간 지정):
    {
        "dates": ["2026-02-09"],
        "court_schedules": [
            {"court": 1, "hours": [8, 10]},
            {"court": 2, "hours": [6, 8]},
            {"court": 3, "hours": [10, 12]}
        ],
        "test_mode": false
    }
    """
    data = request.get_json() or {}
    test_mode = data.get("test_mode", False)

    # 로그인 정보 확인
    user_id = data.get("user_id") or config.USER_ID
    user_pw = data.get("user_pw") or config.USER_PW

    if not user_id or not user_pw:
        return jsonify({
            "error": "user_id 또는 user_pw 필요 (요청 본문 또는 환경변수 TENNIS_USER_ID, TENNIS_USER_PW)"
        }), 400

    # 방법 2: reservations 배열 직접 지정
    if "reservations" in data:
        reservations = data["reservations"]
        if not isinstance(reservations, list):
            return jsonify({"error": "reservations는 배열이어야 합니다."}), 400

        # 유효성 검사
        for i, res in enumerate(reservations):
            if "date" not in res:
                return jsonify({"error": f"reservations[{i}]에 date 필드 필요"}), 400
            if "hour" not in res:
                return jsonify({"error": f"reservations[{i}]에 hour 필드 필요"}), 400
            if "court" not in res:
                return jsonify({"error": f"reservations[{i}]에 court 필드 필요"}), 400

            # 날짜 검증
            try:
                datetime.strptime(res["date"], "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": f"잘못된 날짜 형식: {res['date']} (YYYY-MM-DD 필요)"}), 400

            # 시간 검증
            if res["hour"] not in config.AVAILABLE_HOURS:
                return jsonify({"error": f"잘못된 시간: {res['hour']} (6, 8, 10 등 2시간 단위)"}), 400

            # 코트 검증
            if res["court"] not in config.ALL_COURTS:
                return jsonify({"error": f"잘못된 코트 번호: {res['court']} (1-4)"}), 400

        print(f"[API] 예약 요청 수신 (상세 지정)")
        print(f"  총 {len(reservations)}건")
        for res in reservations:
            print(f"    - {res['date']} {res['hour']:02d}:00 {res['court']}번 코트")

        # 예약 실행
        result = asyncio.run(run_reservation_async(
            test_mode=test_mode,
            reservations=reservations,
            user_id=user_id,
            user_pw=user_pw,
            wait_for_open=False,
        ))
        return jsonify(result)

    # 방법 3: court_schedules로 코트별 시간 지정
    if "court_schedules" in data:
        court_schedules = data["court_schedules"]
        dates = data.get("dates") or []

        if not isinstance(court_schedules, list):
            return jsonify({"error": "court_schedules는 배열이어야 합니다."}), 400
        if not dates:
            return jsonify({"error": "dates 필드가 필요합니다."}), 400

        # 날짜 검증
        for d in dates:
            try:
                datetime.strptime(d, "%Y-%m-%d")
            except ValueError:
                return jsonify({"error": f"잘못된 날짜 형식: {d} (YYYY-MM-DD 필요)"}), 400

        # court_schedules → reservations 변환
        reservations = []
        for schedule in court_schedules:
            if "court" not in schedule or "hours" not in schedule:
                return jsonify({"error": "court_schedules 항목에 court, hours 필드 필요"}), 400

            court = schedule["court"]
            hours = schedule["hours"]

            # 코트 검증
            if court not in config.ALL_COURTS:
                return jsonify({"error": f"잘못된 코트 번호: {court} (1-4)"}), 400

            # 시간 검증
            for h in hours:
                if h not in config.AVAILABLE_HOURS:
                    return jsonify({"error": f"잘못된 시간: {h} (6, 8, 10 등 2시간 단위)"}), 400

            # 날짜 × 시간 조합
            for date in dates:
                for hour in hours:
                    reservations.append({
                        "date": date,
                        "hour": hour,
                        "court": court
                    })

        print(f"[API] 예약 요청 수신 (코트별 시간 지정)")
        print(f"  총 {len(reservations)}건")
        for res in reservations:
            print(f"    - {res['date']} {res['hour']:02d}:00 {res['court']}번 코트")

        # 예약 실행
        result = asyncio.run(run_reservation_async(
            test_mode=test_mode,
            reservations=reservations,
            user_id=user_id,
            user_pw=user_pw,
            wait_for_open=False,
        ))
        return jsonify(result)

    # 방법 1: 기존 방식 (dates × hours × courts)
    # 방법 1은 조합형이라 config.toml에 대응하는 기본값이 없다 — 요청 본문에서 받는다
    dates = data.get("dates") or []
    hours = data.get("hours") or []

    # court / courts 처리
    if "courts" in data:
        courts = data["courts"]
        if not isinstance(courts, list):
            return jsonify({"error": "courts는 배열이어야 합니다."}), 400
    elif "court" in data:
        courts = [data["court"]]
    else:
        courts = []

    # 유효성 검사
    if not dates:
        return jsonify({"error": "dates 필드가 비어있습니다."}), 400

    if not hours:
        return jsonify({"error": "hours 필드가 비어있습니다."}), 400

    if not courts:
        return jsonify({"error": "courts 필드가 비어있습니다."}), 400

    # 날짜 형식 검증
    for d in dates:
        try:
            datetime.strptime(d, "%Y-%m-%d")
        except ValueError:
            return jsonify({"error": f"잘못된 날짜 형식: {d} (YYYY-MM-DD 필요)"}), 400

    # 시간 검증
    for h in hours:
        if h not in config.AVAILABLE_HOURS:
            return jsonify({"error": f"잘못된 시간: {h} (6, 8, 10 등 2시간 단위)"}), 400

    # 코트 검증
    for court in courts:
        if court not in config.ALL_COURTS:
            return jsonify({"error": f"잘못된 코트 번호: {court} (1-4)"}), 400

    print(f"[API] 예약 요청 수신")
    print(f"  날짜: {dates}")
    print(f"  시간: {hours}")
    print(f"  코트: {courts}")
    print(f"  테스트: {test_mode}")

    # 예약 실행
    result = asyncio.run(run_reservation_async(
        test_mode=test_mode,
        dates=dates,
        hours=hours,
        courts=courts,
        user_id=user_id,
        user_pw=user_pw,
        wait_for_open=False,
    ))

    return jsonify(result)


@app.route("/reserve-single", methods=["POST"])
def reserve_single():
    """단일 예약 실행

    요청 본문:
    {
        "date": "2026-02-02",
        "hour": 6,
        "court": 2,
        "test_mode": false,
        "user_id": "your_id",    # 선택사항 (없으면 config.toml의 첫 계정)
        "user_pw": "your_pw"     # 선택사항 (없으면 config.toml의 첫 계정)
    }
    """
    data = request.get_json() or {}

    date = data.get("date")
    hour = data.get("hour")
    _reservations = config.RESERVATION_CONFIG["reservations"]
    _default_court = _reservations[0]["court"] if _reservations else config.ALL_COURTS[0]
    court = data.get("court", _default_court)
    test_mode = data.get("test_mode", False)

    if not date:
        return jsonify({"error": "date 필드 필요"}), 400

    if hour is None:
        return jsonify({"error": "hour 필드 필요"}), 400

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        return jsonify({"error": "잘못된 날짜 형식 (YYYY-MM-DD)"}), 400

    # 로그인 정보
    user_id = data.get("user_id") or config.USER_ID
    user_pw = data.get("user_pw") or config.USER_PW

    if not user_id or not user_pw:
        return jsonify({
            "error": "user_id 또는 user_pw 필요 (요청 본문 또는 환경변수 TENNIS_USER_ID, TENNIS_USER_PW)"
        }), 400

    print(f"[API] 단일 예약 요청: {date} {hour:02d}:00 / {court}번 코트")

    async def _reserve():
        async with TennisReservationAsync() as bot:
            if not await bot.login(user_id, user_pw):
                return False, "로그인 실패"
            return await bot.reserve(date, hour, court, test_mode)

    success, message = asyncio.run(_reserve())

    return jsonify({
        "success": success,
        "message": message,
        "date": date,
        "hour": hour,
        "court": court
    })


@app.route("/search-weekend", methods=["POST"])
def search_weekend():
    """주말 빈자리 검색 (토/일, 6시~12시)

    요청 본문:
    {
        "year": 2026,
        "month": 2,
        "courts": [1, 2, 3, 4],  # 선택사항
        "hours": [6, 8, 10],     # 선택사항
        "user_id": "your_id",    # 선택사항 (없으면 config.toml의 첫 계정)
        "user_pw": "your_pw"     # 선택사항 (없으면 config.toml의 첫 계정)
    }
    """
    data = request.get_json() or {}

    year = data.get("year")
    month = data.get("month")

    if not year or not month:
        return jsonify({"error": "year, month 필드 필요"}), 400

    courts = data.get("courts", config.ALL_COURTS)
    hours = data.get("hours", config.SEARCH_DEFAULT_HOURS)

    # 로그인 정보
    user_id = data.get("user_id") or config.USER_ID
    user_pw = data.get("user_pw") or config.USER_PW

    if not user_id or not user_pw:
        return jsonify({
            "error": "user_id 또는 user_pw 필요 (요청 본문 또는 환경변수 TENNIS_USER_ID, TENNIS_USER_PW)"
        }), 400

    print(f"[API] 주말 빈자리 검색: {year}년 {month}월")

    result = asyncio.run(search_available_slots_async(
        year, month, courts=courts, hours=hours, verbose=False,
        user_id=user_id, user_pw=user_pw,
    ))

    return jsonify(result)


@app.route("/search-all", methods=["POST"])
def search_all():
    """전체 날짜/시간 빈자리 검색

    요청 본문:
    {
        "year": 2026,
        "month": 2,
        "courts": [1, 2, 3, 4],  # 선택사항
        "user_id": "your_id",    # 선택사항 (없으면 config.toml의 첫 계정)
        "user_pw": "your_pw"     # 선택사항 (없으면 config.toml의 첫 계정)
    }
    """
    data = request.get_json() or {}

    year = data.get("year")
    month = data.get("month")

    if not year or not month:
        return jsonify({"error": "year, month 필드 필요"}), 400

    courts = data.get("courts", config.ALL_COURTS)

    # 로그인 정보
    user_id = data.get("user_id") or config.USER_ID
    user_pw = data.get("user_pw") or config.USER_PW

    if not user_id or not user_pw:
        return jsonify({
            "error": "user_id 또는 user_pw 필요 (요청 본문 또는 환경변수 TENNIS_USER_ID, TENNIS_USER_PW)"
        }), 400

    print(f"[API] 전체 빈자리 검색: {year}년 {month}월")

    result = asyncio.run(search_all_slots_async(
        year, month, courts=courts, verbose=False,
        user_id=user_id, user_pw=user_pw,
    ))

    return jsonify(result)


def main():
    parser = argparse.ArgumentParser(description="테니스장 예약 API 서버")
    parser.add_argument("--host", default=config.API_HOST, help=f"바인딩 호스트 (기본: {config.API_HOST})")
    parser.add_argument("--port", type=int, default=config.API_PORT, help=f"포트 번호 (기본: {config.API_PORT})")
    parser.add_argument("--debug", action="store_true", help="디버그 모드")
    args = parser.parse_args()

    print("=" * 50)
    print("고양시 테니스장 예약 API 서버")
    print("=" * 50)
    print(f"서버 주소: http://{args.host}:{args.port}")
    print()
    print("API 엔드포인트:")
    print(f"  GET  /health         - 헬스 체크")
    print(f"  GET  /config         - 설정 조회")
    print(f"  POST /check-login    - 로그인 테스트")
    print(f"  POST /check-slots    - 예약 가능 시간대 조회")
    print(f"  POST /reserve        - 예약 실행 (복수)")
    print(f"  POST /reserve-single - 예약 실행 (단일)")
    print(f"  POST /search-weekend - 주말 빈자리 검색")
    print(f"  POST /search-all     - 전체 빈자리 검색")
    print("=" * 50)
    print()

    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
