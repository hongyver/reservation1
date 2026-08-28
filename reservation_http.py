#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
고양시 테니스장 예약 - HTTP 요청 기반 모듈
Chrome 브라우저 없이 requests로 직접 HTTP 요청
접속 폭주 상황 대응: 재시도, 타임아웃, 세션 복구
"""

import time
import random
import calendar
from datetime import datetime, date
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup

import config
from utils import wait_before_login, wait_for_reservation_open


class TennisReservationHTTP:
    """HTTP 요청 기반 테니스장 예약 (접속 폭주 대응)"""

    def __init__(self, worker_id=None):
        self.worker_id = worker_id or 0
        self.prefix = f"[W{self.worker_id}]" if worker_id is not None else ""
        self.logged_in = False
        self.session = None
        self._create_session()

    def _create_session(self):
        """재시도 로직이 포함된 세션 생성"""
        self.session = requests.Session()

        # SSL 인증서 검증 비활성화
        self.session.verify = False

        # 재시도 전략 설정
        retry_strategy = Retry(
            total=config.SESSION_RETRY_TOTAL,
            backoff_factor=config.SESSION_RETRY_BACKOFF,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST"]
        )

        adapter = HTTPAdapter(
            max_retries=retry_strategy,
            pool_connections=config.SESSION_POOL_SIZE,
            pool_maxsize=config.SESSION_POOL_SIZE
        )

        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)

        # 브라우저처럼 보이도록 헤더 설정
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
            "Accept-Encoding": "gzip, deflate, br",
            "Connection": "keep-alive",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        })

    def _reset_session(self):
        """세션 초기화 (연결 문제 시)"""
        self.log("[WARN] 세션 재생성...")
        if self.session:
            self.session.close()
        self._create_session()
        self.logged_in = False

    def log(self, msg):
        """로그 출력 (타임스탬프 포함)"""
        timestamp = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        print(f"{timestamp} {self.prefix} {msg}")

    def _request_with_retry(self, method, url, max_retries=None, **kwargs):
        """재시도 로직이 포함된 HTTP 요청"""
        if max_retries is None:
            max_retries = config.MAX_RETRIES
        kwargs.setdefault("timeout", (config.CONNECTION_TIMEOUT, config.READ_TIMEOUT))

        last_error = None

        for attempt in range(max_retries):
            try:
                if method == "GET":
                    resp = self.session.get(url, **kwargs)
                else:
                    resp = self.session.post(url, **kwargs)

                resp.raise_for_status()
                return resp

            except requests.exceptions.Timeout as e:
                last_error = e
                self.log(f"[RETRY {attempt+1}/{max_retries}] 타임아웃: {url}")

            except requests.exceptions.ConnectionError as e:
                last_error = e
                self.log(f"[RETRY {attempt+1}/{max_retries}] 연결 오류: {url}")
                # 연결 오류 시 세션 재생성
                if attempt > 2:
                    self._reset_session()

            except requests.exceptions.HTTPError as e:
                last_error = e
                status_code = e.response.status_code if e.response else "?"
                self.log(f"[RETRY {attempt+1}/{max_retries}] HTTP {status_code}: {url}")

                # 5xx 에러는 재시도, 4xx는 중단
                if e.response and 400 <= e.response.status_code < 500:
                    raise

            except Exception as e:
                last_error = e
                self.log(f"[RETRY {attempt+1}/{max_retries}] 오류: {e}")

            # 재시도 대기 (랜덤 지터 추가)
            if attempt < max_retries - 1:
                delay = random.uniform(config.RETRY_DELAY_MIN, config.RETRY_DELAY_MAX)
                time.sleep(delay)

        raise last_error or Exception("최대 재시도 횟수 초과")

    def login(self, user_id=None, user_pw=None):
        """로그인 (재시도 포함)"""
        user_id = user_id or config.USER_ID
        user_pw = user_pw or config.USER_PW

        for attempt in range(config.MAX_RETRIES):
            try:
                self.log("[INFO] 로그인 시도...")

                # 메인 페이지 접근 (쿠키 획득)
                self._request_with_retry("GET", config.MAIN_URL, max_retries=3)

                # 로그인 요청
                login_url = urljoin(config.MAIN_URL, "/member/login_process.php")
                login_data = {"id": user_id, "pw": user_pw}

                self._request_with_retry("POST", login_url, data=login_data, max_retries=3)

                # 로그인 확인
                resp = self._request_with_retry("GET", config.MAIN_URL, max_retries=3)

                if "로그아웃" in resp.text:
                    self.logged_in = True
                    self.log("[SUCCESS] 로그인 성공!")
                    return True

                self.log(f"[WARN] 로그인 확인 실패, 재시도 {attempt+1}/{config.MAX_RETRIES}")

            except Exception as e:
                self.log(f"[ERROR] 로그인 오류: {e}")
                if attempt < config.MAX_RETRIES - 1:
                    self._reset_session()
                    time.sleep(random.uniform(0.5, 1.5))

        self.log("[ERROR] 로그인 최종 실패")
        return False

    def warmup_connection(self):
        """연결 예열 (예약 시간 직전 호출)"""
        self.log("[INFO] 연결 예열 중...")
        try:
            # 가벼운 요청으로 연결 유지
            self._request_with_retry("GET", config.MAIN_URL, max_retries=2)
            self.log("[INFO] 연결 예열 완료")
            return True
        except Exception as e:
            self.log(f"[WARN] 연결 예열 실패: {e}")
            return False

    def get_reservation_page(self, court_number, year, month, day):
        """예약 페이지 조회 (재시도 포함) - GET URL 파라미터 방식"""
        court_value = config.COURT_VALUE_MAP.get(court_number)
        if not court_value:
            self.log(f"[ERROR] 잘못된 코트 번호: {court_number}")
            return None

        # URL 파라미터로 조회 (GET 사용)
        # 형식: tennis_rent.php?place_opt=7&nyear=2026&nmonth=02&nday=01
        params = {
            "place_opt": court_value,
            "nyear": str(year),
            "nmonth": str(month).zfill(2),
            "nday": str(day).zfill(2),
        }

        try:
            resp = self._request_with_retry(
                "GET", config.TENNIS_RESERVATION_URL,
                params=params, max_retries=config.MAX_RETRIES
            )
            return resp.text

        except Exception as e:
            self.log(f"[ERROR] 예약 페이지 조회 실패: {e}")
            return None

    def get_available_slots(self, html_content):
        """예약 가능한 시간대 파싱"""
        available = []

        try:
            soup = BeautifulSoup(html_content, "html.parser")

            # 테이블 행에서 체크박스 찾기
            rows = soup.find_all("tr")

            for row in rows:
                checkbox = row.find("input", {"name": "rent_chk[]"})
                if not checkbox:
                    continue

                # disabled 체크
                if checkbox.get("disabled"):
                    continue

                # "일정있음" 텍스트가 있으면 이미 예약된 것
                row_text = row.get_text()
                if "일정있음" in row_text:
                    continue

                value = checkbox.get("value", "")
                if len(value) >= 8:
                    start_time = value[:4]
                    end_time = value[4:8]
                    available.append({
                        "value": value,
                        "start": f"{start_time[:2]}:{start_time[2:]}",
                        "end": f"{end_time[:2]}:{end_time[2:]}",
                        "start_hour": int(start_time[:2])
                    })

            return available

        except Exception as e:
            self.log(f"[ERROR] 시간대 파싱 오류: {e}")
            return []

    def submit_reservation(self, court_number, year, month, day, time_value, test_mode=False):
        """예약 신청 - 2단계 프로세스

        1. rent_period_apply.php로 신청서 폼 조회 (사용자 정보 포함)
        2. rent_period_proc.php로 최종 예약 제출
        """
        self.log(f"[INFO] 예약 신청: {year}-{month:02d}-{day:02d} {time_value[:2]}:00")

        if test_mode:
            self.log("[TEST] 테스트 모드 - 실제 신청하지 않음")
            return True, "테스트 모드 - 신청 건너뜀"

        for attempt in range(config.MAX_RETRIES):
            try:
                # Step 1: 예약 페이지에서 DocumentForm 필드 가져오기
                html = self.get_reservation_page(court_number, year, month, day)
                if not html:
                    continue

                soup = BeautifulSoup(html, "html.parser")
                doc_form = soup.find("form", {"name": "DocumentForm"})
                if not doc_form:
                    self.log("[WARN] DocumentForm을 찾을 수 없음")
                    continue

                # DocumentForm의 모든 hidden 필드 수집
                form_data = {}
                for inp in doc_form.find_all("input"):
                    name = inp.get("name")
                    if name:
                        form_data[name] = inp.get("value", "")

                # select 요소 처리 (place_opt 등)
                for select in doc_form.find_all("select"):
                    name = select.get("name")
                    if name:
                        # selected option 찾기
                        selected = select.find("option", selected=True)
                        if selected:
                            form_data[name] = selected.get("value", "")

                # place_opt 강제 설정 (코트 번호)
                court_value = config.COURT_VALUE_MAP.get(court_number)
                form_data["place_opt"] = court_value

                # 선택한 시간 추가
                form_data["rent_chk[]"] = time_value
                form_data["use_time"] = "2"

                # Step 2: rent_period_apply.php로 신청서 폼 조회
                apply_url = urljoin(config.MAIN_URL, "/rent/rent_period_apply.php")
                resp = self._request_with_retry("POST", apply_url, data=form_data, max_retries=3)

                # useForm에서 사용자 정보 추출
                soup2 = BeautifulSoup(resp.text, "html.parser")
                use_form = soup2.find("form", {"name": "useForm"})

                if not use_form:
                    self.log("[WARN] 신청서 폼(useForm)을 찾을 수 없음")
                    continue

                # useForm 필드 수집
                for inp in use_form.find_all(["input", "textarea"]):
                    name = inp.get("name")
                    if name:
                        value = inp.get("value", "") if inp.name != "textarea" else inp.get_text()
                        if value:  # 값이 있으면 덮어쓰기
                            form_data[name] = value

                # 필수 필드 설정 (JavaScript checkIt() 함수 참조)
                form_data["regno"] = "0000000000000"
                form_data["com_nm"] = form_data.get("com_nm") or "개인"
                form_data["apply_chk2"] = "1"
                form_data["apply_chk"] = "1"

                # 시간 필드 설정 (rent_chk 값에서 시간 부분만 추출)
                # time_value 예시: "1000120066" → stime=10, etime=12
                form_data["stime"] = time_value[:2]
                form_data["etime"] = time_value[4:6]
                form_data["rent_stime"] = time_value[:4]
                form_data["rent_etime"] = time_value[4:8]
                form_data["rent_p_stime"] = ""
                form_data["rent_p_etime"] = ""
                # rent_chk[]는 전체 값 유지 (DB 레코드 ID 포함)
                # 정규화하면 "존재하지않는 시간데이터" 오류 발생

                # Step 3: rent_period_proc.php로 최종 제출
                proc_url = urljoin(config.MAIN_URL, "/rent/rent_period_proc.php")
                self.log(f"[INFO] 최종 제출 중...")

                resp2 = self._request_with_retry("POST", proc_url, data=form_data, max_retries=3)

                # 결과 확인
                response_text = resp2.text

                if "정상적으로 완료" in response_text:
                    self.log("[SUCCESS] 대관접수 완료!")
                    return True, "대관접수 완료"
                elif "완료" in response_text or ", 0)" in response_text:
                    self.log("[SUCCESS] 예약 완료!")
                    return True, "예약 완료"
                elif "한 건 이상 예약" in response_text:
                    self.log("[WARN] 이미 예약됨 (1일 1건 제한)")
                    return False, "이미 예약됨 (1일 1건 제한)"
                elif "이미" in response_text or "중복" in response_text:
                    self.log("[WARN] 이미 예약됨 (다른 사람이 선점)")
                    return False, "이미 예약됨 (다른 사람이 선점)"
                elif "마감" in response_text:
                    self.log("[WARN] 예약 마감")
                    return False, "예약 마감"
                elif "존재하지않는" in response_text:
                    self.log("[ERROR] 슬롯 값 오류 (존재하지않는 시간)")
                    return False, "슬롯 값 오류 (존재하지않는 시간)"
                elif "alert" in response_text and "submit" in response_text:
                    # 폼 제출 후 리다이렉트 - 성공으로 간주
                    self.log("[SUCCESS] 예약 완료!")
                    return True, "예약 완료"
                else:
                    self.log("[INFO] 예약 제출 완료")
                    return True, "예약 제출 완료"

            except Exception as e:
                self.log(f"[RETRY {attempt+1}/{config.MAX_RETRIES}] 예약 신청 오류: {e}")
                if attempt < config.MAX_RETRIES - 1:
                    time.sleep(random.uniform(0.1, 0.5))

        return False, "예약 신청 실패 (재시도 초과)"

    def reserve(self, target_date, target_hour, court_number, test_mode=False):
        """예약 실행 (전체 플로우)"""
        self.log(f"[START] {target_date} {target_hour:02d}:00 예약 시작")

        try:
            dt = datetime.strptime(target_date, "%Y-%m-%d")
            year = dt.year
            month = dt.month
            day = dt.day

            # 예약 페이지 조회 (재시도 포함)
            html = None
            for attempt in range(config.MAX_RETRIES):
                html = self.get_reservation_page(court_number, year, month, day)
                if html:
                    break
                self.log(f"[RETRY {attempt+1}/{config.MAX_RETRIES}] 예약 페이지 재조회")
                time.sleep(random.uniform(0.2, 0.8))

            if not html:
                return False, "예약 페이지 조회 실패"

            # 시간대 확인
            available = self.get_available_slots(html)
            if not available:
                self.log("[WARN] 예약 가능 시간대 없음")
                return False, "예약 가능 시간대 없음"

            self.log(f"[INFO] 예약 가능: {[s['start'] for s in available]}")

            # 목표 시간대 찾기
            target_slot = None
            for slot in available:
                if slot["start_hour"] == target_hour:
                    target_slot = slot
                    break

            if not target_slot:
                self.log(f"[WARN] {target_hour:02d}:00 예약 불가")
                return False, f"{target_hour:02d}:00 예약 불가"

            # 예약 신청
            success, message = self.submit_reservation(
                court_number, year, month, day,
                target_slot["value"], test_mode
            )

            if success:
                self.log(f"[SUCCESS] {target_date} {target_hour:02d}:00 예약 완료!")

            return success, message

        except Exception as e:
            self.log(f"[ERROR] 예약 중 오류: {e}")
            return False, str(e)

    def close(self):
        """세션 종료"""
        if self.session:
            self.session.close()


def run_reservation_http(test_mode=False, dates=None, hours=None, court=None, courts=None, reservations=None, user_id=None, user_pw=None, wait_for_open=True):
    """HTTP 기반 예약 실행

    Args:
        wait_for_open: 예약 오픈 시간까지 대기 여부 (기본값: True)
                      API 호출 시에는 False로 설정하여 즉시 실행
    """

    # reservations가 직접 지정된 경우 (API 호출 또는 방법 2)
    if reservations is not None:
        tasks = [(r["date"], r["hour"], r["court"]) for r in reservations]
    else:
        # config.py에서 설정 읽기
        cfg = config.RESERVATION_CONFIG

        # 방법 2: reservations 직접 지정 (config.py)
        if "reservations" in cfg:
            tasks = [(r["date"], r["hour"], r["court"]) for r in cfg["reservations"]]

        # 방법 3: court_schedules로 코트별 시간 지정
        elif "court_schedules" in cfg:
            tasks = []
            dates = dates or cfg["dates"]
            for schedule in cfg["court_schedules"]:
                court_num = schedule["court"]
                hours_list = schedule["hours"]
                for d in dates:
                    for h in hours_list:
                        tasks.append((d, h, court_num))

        # 방법 1: 기본 방식 (모든 코트에 같은 시간)
        else:
            dates = dates or cfg["dates"]
            hours = hours or cfg["hours"]

            # court / courts 처리
            if courts is not None:
                court_list = courts if isinstance(courts, list) else [courts]
            elif court is not None:
                court_list = [court]
            elif "courts" in cfg:
                court_list = cfg["courts"]
            else:
                court_list = [cfg["court_number"]]

            tasks = [(d, h, c) for d in dates for h in hours for c in court_list]

    print("=" * 60)
    print("고양시 테니스장 자동 예약 (HTTP - 접속 폭주 대응)")
    print("=" * 60)
    print(f"총 {len(tasks)}개 예약 작업")
    for i, (d, h, c) in enumerate(tasks):
        print(f"  [{i+1}] {d} {h:02d}:00~{h+2:02d}:00 / {c}번 코트")
    print(f"설정: 최대 {config.MAX_RETRIES}회 재시도, 타임아웃 ({config.CONNECTION_TIMEOUT},{config.READ_TIMEOUT})초")
    print()

    results = []

    # 로그인 전 10분 전까지 대기
    if wait_for_open:
        wait_before_login()

    if len(tasks) == 1:
        # 단일 작업
        bot = TennisReservationHTTP()

        if not bot.login(user_id, user_pw):
            return {"success": False, "results": [], "message": "로그인 실패"}

        # 연결 예열
        bot.warmup_connection()

        # 예약 오픈 시간까지 대기
        if wait_for_open:
            if not wait_for_reservation_open():
                bot.close()
                return {"success": False, "results": [], "message": "예약일이 아니거나 이미 지났습니다"}

        date, hour, court_num = tasks[0]
        success, message = bot.reserve(date, hour, court_num, test_mode)
        results.append({
            "date": date,
            "hour": hour,
            "court": court_num,
            "success": success,
            "message": message
        })
        bot.close()

    else:
        # 병렬 실행 - 먼저 모든 봇 로그인 후 대기
        bots = []
        for i, (date, hour, court_num) in enumerate(tasks):
            bot = TennisReservationHTTP(worker_id=i+1)
            if bot.login(user_id, user_pw):
                bot.warmup_connection()
                bots.append((bot, date, hour, court_num))
            else:
                print(f"[W{i+1}] 로그인 실패")

        if not bots:
            return {"success": False, "results": [], "message": "모든 로그인 실패"}

        print(f"[INFO] {len(bots)}개 세션 준비 완료")

        # 동시 접속 제한 표시
        max_concurrent = config.MAX_CONCURRENT
        if len(bots) > max_concurrent:
            print(f"[INFO] 동시 접속 제한: {max_concurrent}개씩 실행")

        # 예약 오픈 시간까지 대기
        if wait_for_open:
            if not wait_for_reservation_open():
                # 모든 봇 종료
                for bot, _, _, _ in bots:
                    bot.close()
                return {"success": False, "results": [], "message": "예약일이 아니거나 이미 지났습니다"}

        # 동시 접속 수 제한하여 예약 실행
        def worker(bot, date, hour, court_num):
            try:
                success, message = bot.reserve(date, hour, court_num, test_mode)
                return {
                    "date": date,
                    "hour": hour,
                    "court": court_num,
                    "success": success,
                    "message": message
                }
            finally:
                bot.close()

        # ThreadPoolExecutor로 동시 접속 수 제한
        with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
            futures = {
                executor.submit(worker, bot, date, hour, court_num): (date, hour, court_num)
                for bot, date, hour, court_num in bots
            }

            for future in as_completed(futures):
                result = future.result()
                results.append(result)

    # 결과 집계
    success_count = sum(1 for r in results if r["success"])
    total_count = len(results)

    print()
    print("=" * 60)
    print("[결과]")
    for r in results:
        status = "성공" if r["success"] else "실패"
        print(f"  {r['date']} {r['hour']:02d}:00 {r['court']}번 코트 - {status} ({r['message']})")
    print(f"총 {success_count}/{total_count}건 성공")
    print("=" * 60)

    return {
        "success": success_count > 0,
        "results": results,
        "summary": f"{success_count}/{total_count}건 성공"
    }


def get_weekends_in_month(year, month):
    """해당 월의 모든 토요일, 일요일 반환"""
    weekends = []
    cal = calendar.Calendar()

    for day in cal.itermonthdays2(year, month):
        day_num, weekday = day
        if day_num == 0:
            continue
        # 5 = Saturday, 6 = Sunday
        if weekday in (5, 6):
            weekends.append(date(year, month, day_num))

    return weekends


def get_all_days_in_month(year, month):
    """해당 월의 모든 날짜 반환"""
    all_days = []
    cal = calendar.Calendar()

    for day in cal.itermonthdays2(year, month):
        day_num, weekday = day
        if day_num == 0:
            continue
        all_days.append((date(year, month, day_num), weekday))

    return all_days


def is_likely_closure(slots):
    """
    휴장일 여부 추정
    모든 슬롯(8개: 06:00~22:00)이 비어있으면 휴장일로 추정
    (정상 운영일에는 인기 시설이라 일부 예약이 있음)
    """
    if len(slots) >= 8:
        available_hours = set(s["start_hour"] for s in slots)
        if set(config.AVAILABLE_HOURS).issubset(available_hours):
            return True
    return False


def search_available_slots(year, month, courts=None, hours=None, verbose=True, user_id=None, user_pw=None):
    """
    해당 월의 토/일 예약 가능 시간대 검색

    Args:
        year: 연도
        month: 월
        courts: 검색할 코트 목록 (기본: [1,2,3,4])
        hours: 검색할 시간 목록 (기본: [6,8,10])
        verbose: 상세 출력 여부
        user_id: 로그인 ID (없으면 config.USER_ID 사용)
        user_pw: 로그인 PW (없으면 config.USER_PW 사용)

    Returns:
        dict: 검색 결과
    """
    courts = courts or config.ALL_COURTS
    hours = hours or config.SEARCH_DEFAULT_HOURS

    weekends = get_weekends_in_month(year, month)

    if not weekends:
        return {"error": "해당 월에 주말이 없습니다.", "results": []}

    print("=" * 70)
    print(f"  {year}년 {month}월 주말 빈자리 검색")
    print("=" * 70)
    print(f"  검색 대상: 토/일 {len(weekends)}일")
    print(f"  코트: {courts}")
    print(f"  시간: {[f'{h:02d}:00' for h in hours]}")
    print("  ※ 예약을 하지 않고 빈자리만 조회합니다")
    print("=" * 70)
    print()

    # 로그인
    bot = TennisReservationHTTP()
    if not bot.login(user_id, user_pw):
        return {"error": "로그인 실패", "results": []}

    results = []
    skipped_dates = set()  # 휴장일로 추정되어 건너뛴 날짜
    day_names = ["월", "화", "수", "목", "금", "토", "일"]

    for d in weekends:
        day_name = day_names[d.weekday()]
        date_str = f"{d.year}-{d.month:02d}-{d.day:02d}"

        if verbose:
            print(f"[검색] {date_str} ({day_name})")

        date_skipped = False

        for court in courts:
            html = bot.get_reservation_page(court, d.year, d.month, d.day)
            if not html:
                if verbose:
                    print(f"  {court}코트: 페이지 조회 실패")
                continue

            slots = bot.get_available_slots(html)

            # 휴장일 감지: 모든 슬롯이 비어있으면 휴장일로 추정
            if is_likely_closure(slots):
                if verbose and not date_skipped:
                    print(f"  ※ 휴장일 추정 (모든 시간 예약가능 표시) - 제외")
                    date_skipped = True
                    skipped_dates.add(date_str)
                continue

            available_hours = [s["start_hour"] for s in slots]

            for hour in hours:
                if hour in available_hours:
                    result = {
                        "date": date_str,
                        "day": day_name,
                        "court": court,
                        "hour": hour,
                        "time": f"{hour:02d}:00~{hour+2:02d}:00"
                    }
                    results.append(result)

                    if verbose:
                        print(f"  ○ {court}코트 {hour:02d}:00~{hour+2:02d}:00 빈자리")

            # 요청 간 짧은 대기
            time.sleep(0.1)

        if verbose:
            print()

    bot.close()

    # 결과 정리
    print("=" * 70)
    if skipped_dates:
        print(f"  검색 결과: 총 {len(results)}건 빈자리 (휴장일 {len(skipped_dates)}일 제외)")
    else:
        print(f"  검색 결과: 총 {len(results)}건 빈자리 발견")
    print("=" * 70)

    if results:
        # 날짜별로 그룹화하여 출력
        current_date = None
        for r in results:
            if r["date"] != current_date:
                current_date = r["date"]
                print(f"  [{r['date']} ({r['day']})]")

            print(f"    - {r['court']}코트 {r['time']}")

    print()
    print("=" * 70)

    return {
        "year": year,
        "month": month,
        "total": len(results),
        "results": results
    }


def search_all_slots(year, month, courts=None, verbose=True, user_id=None, user_pw=None):
    """
    해당 월의 모든 날짜/시간 예약 가능 시간대 검색

    Args:
        year: 연도
        month: 월
        courts: 검색할 코트 목록 (기본: [1,2,3,4])
        verbose: 상세 출력 여부
        user_id: 로그인 ID (없으면 config.USER_ID 사용)
        user_pw: 로그인 PW (없으면 config.USER_PW 사용)

    Returns:
        dict: 검색 결과
    """
    courts = courts or config.ALL_COURTS
    all_hours = config.AVAILABLE_HOURS

    all_days = get_all_days_in_month(year, month)

    if not all_days:
        return {"error": "해당 월에 날짜가 없습니다.", "results": []}

    # ANSI 컬러 코드
    BLUE = "\033[94m"   # 주말 색상
    RESET = "\033[0m"

    print("=" * 70)
    print(f"  {year}년 {month}월 전체 빈자리 검색")
    print("=" * 70)
    print(f"  검색 대상: 전체 {len(all_days)}일")
    print(f"  코트: {courts}")
    print(f"  시간: 모든 시간대 (06:00~22:00)")
    print(f"  {BLUE}■ 주말{RESET} / □ 평일")
    print("  ※ 예약을 하지 않고 빈자리만 조회합니다")
    print("=" * 70)
    print()

    # 로그인
    bot = TennisReservationHTTP()
    if not bot.login(user_id, user_pw):
        return {"error": "로그인 실패", "results": []}

    results = []
    skipped_dates = set()  # 휴장일로 추정되어 건너뛴 날짜
    day_names = ["월", "화", "수", "목", "금", "토", "일"]

    for d, weekday in all_days:
        day_name = day_names[weekday]
        date_str = f"{d.year}-{d.month:02d}-{d.day:02d}"
        is_weekend = weekday in (5, 6)  # 토요일(5), 일요일(6)

        if verbose:
            color = BLUE if is_weekend else ""
            reset = RESET if is_weekend else ""
            print(f"{color}[검색] {date_str} ({day_name}){reset}")

        date_skipped = False

        for court in courts:
            html = bot.get_reservation_page(court, d.year, d.month, d.day)
            if not html:
                if verbose:
                    print(f"  {court}코트: 페이지 조회 실패")
                continue

            slots = bot.get_available_slots(html)

            # 휴장일 감지: 모든 슬롯이 비어있으면 휴장일로 추정
            if is_likely_closure(slots):
                if verbose and not date_skipped:
                    print(f"  ※ 휴장일 추정 (모든 시간 예약가능 표시) - 제외")
                    date_skipped = True
                    skipped_dates.add(date_str)
                break  # 이 날짜는 모든 코트 건너뜀

            available_hours = [s["start_hour"] for s in slots]

            for hour in all_hours:
                if hour in available_hours:
                    result = {
                        "date": date_str,
                        "day": day_name,
                        "court": court,
                        "hour": hour,
                        "time": f"{hour:02d}:00~{hour+2:02d}:00",
                        "is_weekend": is_weekend
                    }
                    results.append(result)

                    if verbose:
                        color = BLUE if is_weekend else ""
                        reset = RESET if is_weekend else ""
                        print(f"  {color}○ {court}코트 {hour:02d}:00~{hour+2:02d}:00 빈자리{reset}")

            # 요청 간 짧은 대기
            time.sleep(0.1)

        if verbose and not date_skipped:
            print()

    bot.close()

    # 결과 정리
    print("=" * 70)
    if skipped_dates:
        print(f"  검색 결과: 총 {len(results)}건 빈자리 (휴장일 {len(skipped_dates)}일 제외)")
    else:
        print(f"  검색 결과: 총 {len(results)}건 빈자리 발견")
    print("=" * 70)

    if results:
        # 날짜별로 그룹화하여 출력
        current_date = None
        for r in results:
            if r["date"] != current_date:
                current_date = r["date"]
                color = BLUE if r["is_weekend"] else ""
                reset = RESET if r["is_weekend"] else ""
                print(f"  {color}[{r['date']} ({r['day']})]{reset}")

            color = BLUE if r["is_weekend"] else ""
            reset = RESET if r["is_weekend"] else ""
            print(f"    {color}- {r['court']}코트 {r['time']}{reset}")

    print()
    print("=" * 70)

    return {
        "year": year,
        "month": month,
        "total": len(results),
        "results": results,
        "skipped_dates": list(skipped_dates)
    }


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = run_reservation_http(test_mode=True)
    print(result)
