#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
고양시 테니스장 예약 자동화 모듈
실제 사이트 구조에 맞춘 Selenium 자동화
병렬 실행 지원
"""

import time
import threading
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait, Select
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.common.exceptions import (
    TimeoutException, NoSuchElementException,
    UnexpectedAlertPresentException, NoAlertPresentException
)
from webdriver_manager.chrome import ChromeDriverManager

import config
from utils import wait_before_login, wait_for_reservation_open

# ChromeDriver 다운로드 동시 접근 방지용 Lock
_chromedriver_lock = threading.Lock()


class TennisReservationBot:
    """테니스장 예약 자동화 봇"""

    def __init__(self, test_mode=False, worker_id=None):
        self.driver = None
        self.wait = None
        self.test_mode = test_mode
        self.worker_id = worker_id or 0
        self.prefix = f"[W{self.worker_id}]" if worker_id is not None else ""

    def log(self, msg):
        """로그 출력 (워커 ID 포함)"""
        print(f"{self.prefix} {msg}")

    def setup_browser(self):
        """브라우저 초기화"""
        self.log("[INFO] 브라우저를 시작합니다...")

        chrome_options = Options()

        if config.HEADLESS:
            chrome_options.add_argument("--headless=new")  # 새로운 headless 모드

        # 안정성 향상을 위한 옵션
        chrome_options.add_argument("--no-sandbox")
        chrome_options.add_argument("--disable-dev-shm-usage")
        chrome_options.add_argument("--disable-gpu")
        chrome_options.add_argument("--disable-software-rasterizer")
        chrome_options.add_argument("--window-size=1920,1080")
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument("--disable-extensions")
        chrome_options.add_argument("--disable-plugins")
        chrome_options.add_argument("--disable-infobars")
        chrome_options.add_argument("--ignore-certificate-errors")
        chrome_options.add_argument("--allow-running-insecure-content")
        chrome_options.add_argument("--disable-web-security")
        chrome_options.add_argument("--disable-features=IsolateOrigins,site-per-process")

        # 병렬 실행 시 포트 충돌 방지 (각 워커마다 다른 포트)
        debug_port = 9222 + self.worker_id
        chrome_options.add_argument(f"--remote-debugging-port={debug_port}")

        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation", "enable-logging"])
        chrome_options.add_experimental_option("useAutomationExtension", False)
        chrome_options.add_experimental_option("prefs", {
            "profile.default_content_setting_values.notifications": 2,
            "profile.default_content_settings.popups": 0,
        })

        try:
            # ChromeDriverManager로 자동 설치 (병렬 실행 시 충돌 방지를 위해 lock 사용)
            with _chromedriver_lock:
                chromedriver_path = ChromeDriverManager().install()

            service = Service(chromedriver_path)
            self.driver = webdriver.Chrome(service=service, options=chrome_options)
        except Exception as e:
            # webdriver-manager 실패 시 시스템 chromedriver 사용
            self.log(f"[WARN] ChromeDriverManager 실패, 시스템 chromedriver 사용: {e}")
            try:
                self.driver = webdriver.Chrome(options=chrome_options)
            except Exception as e2:
                self.log(f"[ERROR] Chrome 브라우저 시작 실패: {e2}")
                raise

        self.driver.set_page_load_timeout(config.PAGE_LOAD_TIMEOUT)
        self.wait = WebDriverWait(self.driver, config.ELEMENT_WAIT_TIMEOUT)

        self.log("[INFO] 브라우저 준비 완료")

    def login(self, user_id=None, user_pw=None):
        """로그인 수행"""
        user_id = user_id or config.USER_ID
        user_pw = user_pw or config.USER_PW

        self.log("[INFO] 로그인 페이지로 이동합니다...")

        self.driver.get(config.MAIN_URL)
        time.sleep(2)

        try:
            id_input = self.driver.find_element(By.CSS_SELECTOR, "input[name='id']")
            pw_input = self.driver.find_element(By.CSS_SELECTOR, "input[type='password']")

            id_input.clear()
            id_input.send_keys(user_id)
            time.sleep(0.2)

            pw_input.clear()
            pw_input.send_keys(user_pw)
            time.sleep(0.2)

            pw_input.submit()
            time.sleep(3)

            if "로그아웃" in self.driver.page_source:
                self.log("[SUCCESS] 로그인 성공!")
                return True
            else:
                self.log("[ERROR] 로그인 실패. ID/PW를 확인해주세요.")
                return False

        except Exception as e:
            self.log(f"[ERROR] 로그인 중 오류 발생: {e}")
            return False

    def go_to_reservation_page(self):
        """테니스 예약 페이지로 이동"""
        self.log("[INFO] 테니스 예약 페이지로 이동합니다...")

        try:
            self.driver.get(config.TENNIS_RESERVATION_URL)
            time.sleep(2)

            self.wait.until(
                EC.presence_of_element_located((By.NAME, "place_opt"))
            )

            self.log("[INFO] 테니스 예약 페이지 준비 완료")
            return True

        except Exception as e:
            self.log(f"[ERROR] 예약 페이지 이동 중 오류: {e}")
            return False

    def select_court(self, court_number):
        """코트 선택"""
        court_value = config.COURT_VALUE_MAP.get(court_number)
        if not court_value:
            self.log(f"[ERROR] 잘못된 코트 번호: {court_number}")
            return False

        try:
            place_select = Select(self.driver.find_element(By.NAME, "place_opt"))
            place_select.select_by_value(court_value)
            self.log(f"[INFO] {court_number}코트 선택 완료")
            time.sleep(2)
            return True

        except Exception as e:
            self.log(f"[ERROR] 코트 선택 중 오류: {e}")
            return False

    def select_date(self, year, month, day):
        """날짜 선택"""
        try:
            month_str = str(month).zfill(2)
            day_str = str(day).zfill(2)

            for _ in range(10):
                try:
                    is_defined = self.driver.execute_script(
                        "return typeof onChangeDay === 'function'"
                    )
                    if is_defined:
                        break
                except Exception:
                    pass
                time.sleep(0.5)

            script = f"onChangeDay('{year}', '{month_str}', '{day_str}')"
            self.driver.execute_script(script)

            self.log(f"[INFO] 날짜 선택: {year}-{month_str}-{day_str}")
            time.sleep(2)

            self._handle_alert()
            return True

        except Exception as e:
            self.log(f"[ERROR] 날짜 선택 중 오류: {e}")
            return False

    def _handle_alert(self):
        """JavaScript alert 처리"""
        try:
            alert = self.driver.switch_to.alert
            alert_text = alert.text
            self.log(f"[ALERT] {alert_text}")
            alert.accept()
            return alert_text
        except NoAlertPresentException:
            return None

    def get_available_time_slots(self):
        """예약 가능한 시간대 조회"""
        available = []

        try:
            checkboxes = self.driver.find_elements(By.NAME, "rent_chk[]")

            for cb in checkboxes:
                if cb.is_enabled() and cb.is_displayed():
                    value = cb.get_attribute("value")
                    if len(value) >= 8:
                        start_time = value[:4]
                        end_time = value[4:8]
                        available.append({
                            "checkbox": cb,
                            "value": value,
                            "start": f"{start_time[:2]}:{start_time[2:]}",
                            "end": f"{end_time[:2]}:{end_time[2:]}"
                        })

            return available

        except Exception as e:
            self.log(f"[ERROR] 시간대 조회 중 오류: {e}")
            return []

    def select_single_time_slot(self, target_hour):
        """단일 시간대 선택"""
        try:
            checkboxes = self.driver.find_elements(By.NAME, "rent_chk[]")

            for cb in checkboxes:
                if not cb.is_enabled() or not cb.is_displayed():
                    continue

                value = cb.get_attribute("value")

                if len(value) >= 4:
                    start_hour = int(value[:2])

                    if start_hour == target_hour:
                        # JavaScript로 직접 클릭 (element click intercepted 방지)
                        try:
                            self.driver.execute_script("arguments[0].click();", cb)
                            self.log(f"[INFO] 시간 선택: {start_hour:02d}:00~{start_hour+2:02d}:00")
                            time.sleep(0.3)
                            return True
                        except Exception as click_error:
                            # JavaScript 클릭도 실패하면 일반 클릭 시도
                            self.log(f"[WARN] JavaScript 클릭 실패, 일반 클릭 시도: {click_error}")
                            cb.click()
                            self.log(f"[INFO] 시간 선택: {start_hour:02d}:00~{start_hour+2:02d}:00")
                            time.sleep(0.3)
                            return True

            return False

        except Exception as e:
            self.log(f"[ERROR] 시간 선택 중 오류: {e}")
            return False

    def submit_reservation(self, test_mode=False):
        """예약 신청"""
        try:
            self.driver.execute_script("Commit()")
            self.log("[INFO] 허가 신청서 팝업 열기...")
            time.sleep(2)

            windows = self.driver.window_handles
            if len(windows) > 1:
                self.driver.switch_to.window(windows[-1])
                self.log("[INFO] 허가 신청서 팝업으로 전환")
                time.sleep(1)

                # 1. 개인정보 동의 체크
                try:
                    chk1 = self.driver.find_element(By.NAME, "apply_chk2")
                    if not chk1.is_selected():
                        chk1.click()
                        self.log("[INFO] 개인정보 동의 체크")
                    time.sleep(0.3)
                except NoSuchElementException:
                    pass

                # 2. 준수사항 동의 체크
                try:
                    chk2 = self.driver.find_element(By.NAME, "apply_chk")
                    if not chk2.is_selected():
                        chk2.click()
                        self.log("[INFO] 준수사항 동의 체크")
                    time.sleep(0.3)
                except NoSuchElementException:
                    pass

                if test_mode:
                    self.log("[TEST] 테스트 모드 - 대관신청 전 멈춤")
                    return True

                # 3. 최종 대관신청
                self.log("[INFO] 최종 대관신청 진행...")
                self.driver.execute_script("checkIt()")
                time.sleep(1)

                # confirm 처리 (여러 번 시도)
                for i in range(3):
                    try:
                        alert = self.driver.switch_to.alert
                        self.log(f"[CONFIRM] {alert.text}")
                        alert.accept()
                        self.log("[INFO] 확인 버튼 클릭")
                        time.sleep(1)
                    except NoAlertPresentException:
                        break
                    except Exception:
                        break

                # 팝업 창이 닫혔을 수 있으므로 안전하게 메인 윈도우로 복귀
                time.sleep(2)
                try:
                    windows = self.driver.window_handles
                    if len(windows) >= 1:
                        self.driver.switch_to.window(windows[0])
                        time.sleep(1)

                        # 메인 창에서 추가 alert 처리
                        try:
                            alert = self.driver.switch_to.alert
                            self.log(f"[ALERT] {alert.text}")
                            alert.accept()
                        except NoAlertPresentException:
                            pass
                except Exception as e:
                    # 창이 이미 닫힌 경우 무시 (예약은 성공한 것)
                    self.log(f"[INFO] 팝업 창 자동 종료됨")

                self.log("[INFO] 대관신청 완료")
                return True

            else:
                self.log("[ERROR] 팝업이 열리지 않음")
                return False

        except UnexpectedAlertPresentException:
            self._handle_alert()
            return True

        except Exception as e:
            self.log(f"[ERROR] 예약 신청 중 오류: {e}")
            return False

    def reserve_single(self, target_date, target_hour, court):
        """단일 날짜/시간 예약"""
        self.log(f"[TRY] {target_date} {target_hour:02d}:00 예약 시도")

        try:
            # 예약 페이지로 이동
            self.driver.get(config.TENNIS_RESERVATION_URL)
            time.sleep(1)

            # 코트 선택
            if not self.select_court(court):
                return False

            # 날짜 선택
            dt = datetime.strptime(target_date, "%Y-%m-%d")
            if not self.select_date(dt.year, dt.month, dt.day):
                return False

            # 시간 선택
            available = self.get_available_time_slots()
            if not available:
                self.log("[WARN] 예약 가능한 시간대 없음")
                return False

            self.log(f"[INFO] 예약 가능: {[s['start'] for s in available]}")

            if not self.select_single_time_slot(target_hour):
                self.log("[WARN] 목표 시간대를 선택할 수 없음")
                return False

            # 예약 신청
            if self.submit_reservation(test_mode=self.test_mode):
                self.log(f"[SUCCESS] {target_date} {target_hour:02d}:00 예약 완료!")
                return True
            else:
                return False

        except Exception as e:
            self.log(f"[ERROR] 예약 중 오류: {e}")
            return False

    def close(self):
        """브라우저 종료"""
        if self.driver:
            self.driver.quit()
            self.log("[INFO] 브라우저 종료")


def run_reservation(test_mode=False, user_id=None, user_pw=None):
    """예약 실행 메인 함수"""
    cfg = config.RESERVATION_CONFIG

    # reservations가 직접 지정된 경우 (방법 2)
    if "reservations" in cfg:
        tasks = [(r["date"], r["hour"], r["court"]) for r in cfg["reservations"]]
    # court_schedules로 코트별 시간 지정 (방법 3)
    elif "court_schedules" in cfg:
        tasks = []
        dates = cfg["dates"]
        for schedule in cfg["court_schedules"]:
            court = schedule["court"]
            hours = schedule["hours"]
            for date in dates:
                for hour in hours:
                    tasks.append((date, hour, court))
    # 기본 방식 (방법 1)
    else:
        dates = cfg["dates"]
        hours = cfg["hours"]
        court = cfg.get("court_number")

        # courts 배열 지원
        if "courts" in cfg:
            courts = cfg["courts"]
            tasks = [(d, h, c) for d in dates for h in hours for c in courts]
        else:
            tasks = [(d, h, court) for d in dates for h in hours]

    print("=" * 50)
    print("고양시 테니스장 자동 예약")
    print("=" * 50)
    print(f"총 {len(tasks)}개 예약 작업")
    for i, (d, h, c) in enumerate(tasks):
        print(f"  [{i+1}] {d} {h:02d}:00~{h+2:02d}:00 / {c}번 코트")
    print()

    # 로그인 전 10분 전까지 대기
    if config.RESERVATION_DAY != 0:
        wait_before_login()

    # 단일 작업이면 순차 실행
    if len(tasks) == 1:
        print("[INFO] 단일 작업 - 순차 실행")
        bot = TennisReservationBot(test_mode=test_mode)

        try:
            bot.setup_browser()

            if not bot.login(user_id, user_pw):
                return False

            if not bot.go_to_reservation_page():
                return False

            if not wait_for_reservation_open():
                return False

            date, hour, court = tasks[0]
            success = bot.reserve_single(date, hour, court)

            if test_mode and success:
                print("[TEST] 브라우저 유지 중... (30초)")
                time.sleep(30)

            return success

        finally:
            if not test_mode:
                time.sleep(5)
            bot.close()

    # 복수 작업이면 병렬 실행
    print(f"[INFO] {len(tasks)}개 브라우저 병렬 실행")
    print()

    # 먼저 모든 브라우저를 준비하고 로그인
    bots = []
    for i, (date, hour, court) in enumerate(tasks):
        bot = TennisReservationBot(test_mode=test_mode, worker_id=i+1)
        bot.setup_browser()

        if bot.login(user_id, user_pw) and bot.go_to_reservation_page():
            bots.append((bot, date, hour, court))
        else:
            bot.close()

    if not bots:
        print("[ERROR] 로그인 성공한 브라우저가 없습니다.")
        return False

    print(f"[INFO] {len(bots)}개 브라우저 준비 완료")

    # 동시 접속 제한 표시
    max_concurrent = config.MAX_CONCURRENT
    if len(bots) > max_concurrent:
        print(f"[INFO] 동시 접속 제한: {max_concurrent}개씩 실행")

    # 예약 오픈 시간까지 대기
    if not wait_for_reservation_open():
        # 모든 브라우저 종료
        for bot, _, _, _ in bots:
            bot.close()
        return False

    # 동시 접속 수 제한하여 예약 실행
    results = {}

    def reserve_task(bot, date, hour, court, idx):
        success = bot.reserve_single(date, hour, court)
        return idx, success

    # ThreadPoolExecutor로 동시 접속 수 제한
    with ThreadPoolExecutor(max_workers=max_concurrent) as executor:
        futures = {
            executor.submit(reserve_task, bot, date, hour, court, i): i
            for i, (bot, date, hour, court) in enumerate(bots)
        }

        for future in as_completed(futures):
            idx, success = future.result()
            results[idx] = success

    # 결과 출력
    print()
    print("=" * 50)
    print("[결과]")
    success_count = 0
    for i, (bot, date, hour, court) in enumerate(bots):
        status = "성공" if results.get(i) else "실패"
        print(f"  [{i+1}] {date} {hour:02d}:00 {court}번 코트 - {status}")
        if results.get(i):
            success_count += 1
    print(f"총 {success_count}/{len(bots)}건 성공")
    print("=" * 50)

    # 테스트 모드면 브라우저 유지
    if test_mode:
        print("[TEST] 브라우저 유지 중... (30초)")
        time.sleep(30)

    # 브라우저 종료
    for bot, _, _, _ in bots:
        bot.close()

    return success_count > 0


if __name__ == "__main__":
    run_reservation()
