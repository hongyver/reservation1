#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
고양시 테니스장 예약 - asyncio + aiohttp 기반
단일 세션으로 1회 로그인 후 N건 동시 예약 처리.

기존 reservation_http.py 대비 개선사항:
- 로그인 HTTP 요청: 4N번 → 4번 (고정)
- TCP 커넥션 풀: N개 독립 풀 → 1개 공유 풀
- 대기 중 스레드 블로킹 없음 (asyncio.sleep)
- GIL 없는 진정한 I/O 동시성 (asyncio.gather)
"""

import asyncio
import calendar
import json
import random
import time
from datetime import date, datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

import aiohttp
from bs4 import BeautifulSoup

import config
from utils import wait_before_login_async, wait_for_reservation_open_async

LOGS_DIR = Path(__file__).resolve().parent / "logs"


def _backoff_delay(attempt):
    """지수 백오프 + full jitter 대기 시간(초).

    base * 2^attempt 를 상한까지 키운 뒤 [0, 상한] 균등 지터를 적용한다.
    동시 재시도가 같은 순간에 몰리는 thundering herd를 분산해
    단일 IP 요청 폭주(서버 IP 차단)를 억제한다.
    """
    ceiling = min(config.RETRY_BACKOFF_BASE * (2 ** attempt),
                  config.RETRY_BACKOFF_MAX)
    return random.uniform(0, ceiling)


def _dump_timing(log_path, record):
    """타이밍 분석 로그를 JSONL로 추가 기록. 실패해도 예약 흐름에 영향 없음."""
    try:
        LOGS_DIR.mkdir(exist_ok=True)
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"[WARN] 타이밍 로그 기록 실패: {e}")


class TennisReservationAsync:
    """단일 aiohttp.ClientSession으로 비동기 테니스장 예약.

    컨텍스트 매니저로 사용:
        async with TennisReservationAsync() as bot:
            await bot.login(user_id, user_pw)
            ...
    """

    def __init__(self):
        self.session: aiohttp.ClientSession | None = None
        self.logged_in = False
        self.timing = []  # 요청 단위 타이밍 이벤트 (정각 지연 분석용)

    async def __aenter__(self):
        await self._create_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        await self.close()

    async def _create_session(self):
        connector = aiohttp.TCPConnector(
            ssl=False,
            limit=config.SESSION_POOL_SIZE * 4,
            limit_per_host=config.SESSION_POOL_SIZE,
            keepalive_timeout=30,
            enable_cleanup_closed=True,
        )
        timeout = aiohttp.ClientTimeout(
            connect=config.CONNECTION_TIMEOUT,
            total=config.CONNECTION_TIMEOUT + config.READ_TIMEOUT,
        )
        self.session = aiohttp.ClientSession(
            connector=connector,
            timeout=timeout,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "Connection": "keep-alive",
                "Cache-Control": "no-cache",
                "Pragma": "no-cache",
            },
        )

    def _log(self, msg, worker_id=None):
        ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
        prefix = f"[W{worker_id}]" if worker_id is not None else ""
        print(f"{ts} {prefix} {msg}")

    def _record(self, start_mono, method, url, attempt, outcome,
                status=None, size=None):
        """요청 1회 시도의 타이밍 이벤트를 기록한다 (JSONL 분석 로그용)."""
        try:
            event = {
                "ts": datetime.now().isoformat(timespec="milliseconds"),
                "elapsed_ms": round((time.monotonic() - start_mono) * 1000, 1),
                "method": method,
                "path": urlparse(url).path or url,
                "attempt": attempt,
                "outcome": outcome,
            }
            if status is not None:
                event["status"] = status
            if size is not None:
                event["bytes"] = size
            self.timing.append(event)
        except Exception:
            pass

    async def _request_with_retry(self, method, url, max_retries=None, **kwargs):
        """재시도 포함 비동기 HTTP 요청. 성공 시 응답 텍스트 반환."""
        if max_retries is None:
            max_retries = config.MAX_RETRIES

        last_error = None
        for attempt in range(max_retries):
            t_start = time.monotonic()
            try:
                async with self.session.request(method, url, **kwargs) as resp:
                    resp.raise_for_status()
                    # 서버가 EUC-KR 선언이지만 UTF-8 바이트를 혼용하는 경우 대응:
                    # 바이트를 직접 읽어 euc-kr → cp949 → utf-8 → replace 순으로 시도
                    raw = await resp.read()
                    self._record(t_start, method, url, attempt + 1, "ok",
                                 status=resp.status, size=len(raw))
                    for enc in ("euc-kr", "cp949", "utf-8"):
                        try:
                            return raw.decode(enc)
                        except UnicodeDecodeError:
                            continue
                    return raw.decode("utf-8", errors="replace")

            except aiohttp.ClientResponseError as e:
                last_error = e
                self._record(t_start, method, url, attempt + 1, f"http_{e.status}")
                self._log(f"[RETRY {attempt+1}/{max_retries}] HTTP {e.status}: {url}")
                if 400 <= e.status < 500:
                    raise

            except asyncio.TimeoutError as e:
                last_error = e
                self._record(t_start, method, url, attempt + 1, "timeout")
                self._log(f"[RETRY {attempt+1}/{max_retries}] 타임아웃: {url}")

            except aiohttp.ClientConnectionError as e:
                last_error = e
                self._record(t_start, method, url, attempt + 1, "conn_error")
                self._log(f"[RETRY {attempt+1}/{max_retries}] 연결 오류: {url}")

            except Exception as e:
                last_error = e
                self._record(t_start, method, url, attempt + 1, "error")
                self._log(f"[RETRY {attempt+1}/{max_retries}] 오류: {e}")

            if attempt < max_retries - 1:
                await asyncio.sleep(_backoff_delay(attempt))

        raise last_error or Exception("최대 재시도 횟수 초과")

    async def login(self, user_id=None, user_pw=None):
        """로그인 (재시도 포함). 성공 시 True."""
        user_id = user_id or config.USER_ID
        user_pw = user_pw or config.USER_PW

        for attempt in range(config.MAX_RETRIES):
            try:
                self._log("[INFO] 로그인 시도...")

                await self._request_with_retry("GET", config.MAIN_URL, max_retries=3)

                login_url = urljoin(config.MAIN_URL, "/member/login_process.php")
                await self._request_with_retry(
                    "POST", login_url,
                    data={"id": user_id, "pw": user_pw},
                    max_retries=3,
                )

                text = await self._request_with_retry("GET", config.MAIN_URL, max_retries=3)
                if "로그아웃" in text:
                    self.logged_in = True
                    self._log("[SUCCESS] 로그인 성공!")
                    return True

                self._log(f"[WARN] 로그인 확인 실패, 재시도 {attempt+1}/{config.MAX_RETRIES}")

            except Exception as e:
                self._log(f"[ERROR] 로그인 오류: {e}")
                if attempt < config.MAX_RETRIES - 1:
                    await asyncio.sleep(random.uniform(0.5, 1.5))

        self._log("[ERROR] 로그인 최종 실패")
        return False

    async def warmup_connection(self, max_retries=2, total_timeout=None):
        """연결 예열 (로그인 직후 + 오픈 직전 재예열 호출).

        Args:
            total_timeout: 요청 전체 타임아웃(초). 오픈 직전 재예열처럼
                           정각 전에 반드시 끝나야 하는 호출에 짧게 지정.
        """
        self._log("[INFO] 연결 예열 중...")
        kwargs = {}
        if total_timeout is not None:
            kwargs["timeout"] = aiohttp.ClientTimeout(total=total_timeout)
        try:
            await self._request_with_retry(
                "GET", config.MAIN_URL, max_retries=max_retries, **kwargs
            )
            self._log("[INFO] 연결 예열 완료")
            return True
        except Exception as e:
            self._log(f"[WARN] 연결 예열 실패: {e}")
            return False

    async def get_reservation_page(self, court_number, year, month, day,
                                    max_retries=None):
        """예약 페이지 HTML 조회 (GET URL 파라미터 방식)."""
        court_value = config.COURT_VALUE_MAP.get(court_number)
        if not court_value:
            self._log(f"[ERROR] 잘못된 코트 번호: {court_number}")
            return None

        params = {
            "place_opt": court_value,
            "nyear": str(year),
            "nmonth": str(month).zfill(2),
            "nday": str(day).zfill(2),
        }
        try:
            return await self._request_with_retry(
                "GET", config.TENNIS_RESERVATION_URL, params=params,
                max_retries=max_retries
            )
        except Exception as e:
            self._log(f"[ERROR] 예약 페이지 조회 실패: {e}")
            return None

    def get_available_slots(self, html_content):
        """예약 가능 시간대 파싱. BeautifulSoup은 동기 유지 (빠른 CPU 작업)."""
        available = []
        try:
            soup = BeautifulSoup(html_content, "html.parser")
            for row in soup.find_all("tr"):
                checkbox = row.find("input", {"name": "rent_chk[]"})
                if not checkbox or checkbox.get("disabled"):
                    continue
                if "일정있음" in row.get_text():
                    continue
                value = checkbox.get("value", "")
                # 신청 가능한 슬롯만 "HHMMHHMM" 뒤에 슬롯 ID가 붙는다
                # (예: 1400160076). 8자리 그대로면 이미 예약된 시간이라
                # 그대로 제출하면 서버가 "존재하지않는 시간" 오류를 낸다.
                if len(value) > 8:
                    available.append({
                        "value": value,
                        "start": f"{value[:2]}:{value[2:4]}",
                        "end": f"{value[4:6]}:{value[6:8]}",
                        "start_hour": int(value[:2]),
                    })
        except Exception as e:
            self._log(f"[ERROR] 시간대 파싱 오류: {e}")
        return available

    def _unavailable_reason(self, html, target_hour):
        """예약할 수 없는 이유를 페이지에서 판별해 정확한 메시지를 만든다.

        실패 분기에서만 부른다 — 정각 성공 경로에는 비용이 들지 않는다.
        rent_chk[] 값의 자릿수가 상태를 그대로 말해준다 (CLAUDE.md 슬롯 값 형식):

            필드 없음   → 아직 열리지 않은 날짜 (미오픈)
            8자리       → 그 시간은 이미 예약됨
            8자리 초과  → 신청 가능

        예전에는 이 셋을 "예약 가능 시간대 없음" 하나로 뭉뚱그려, 만석인지
        날짜가 안 열린 건지 로그만 보고는 구분할 수 없었다.
        """
        try:
            values = [
                b.get("value", "") for b in
                BeautifulSoup(html, "html.parser").find_all("input", {"name": "rent_chk[]"})
            ]
        except Exception:
            return "예약 상태 판별 실패"

        hh = f"{target_hour:02d}"
        if not values:
            return "미오픈 날짜 (신청 폼 없음)"
        if all(len(v) == 8 for v in values):
            return "이미 예약됨 (전 시간대 마감)"
        if any(v.startswith(hh) and len(v) == 8 for v in values):
            return f"이미 예약됨 ({hh}:00)"
        if not any(v.startswith(hh) for v in values):
            return f"운영하지 않는 시간 ({hh}:00)"
        return f"예약 불가 ({hh}:00)"

    def _collect_document_form(self, html, worker_id=None):
        """페이지 HTML에서 DocumentForm 필드를 수집한다. 실패 시 None."""
        soup = BeautifulSoup(html, "html.parser")
        doc_form = soup.find("form", {"name": "DocumentForm"})
        if not doc_form:
            self._log("[WARN] DocumentForm을 찾을 수 없음", worker_id)
            return None

        form_data = {
            inp["name"]: inp.get("value", "")
            for inp in doc_form.find_all("input")
            if inp.get("name")
        }
        for select in doc_form.find_all("select"):
            if select.get("name"):
                selected = select.find("option", selected=True)
                if selected:
                    form_data[select["name"]] = selected.get("value", "")
        return form_data

    async def warmup_target_page(self, court_number, year, month, day, worker_id=None,
                                 max_retries=2, total_timeout=8):
        """정각 직전에 대상 페이지를 조회해 연결을 예열한다.

        폼을 캐시해 정각에 apply부터 시작하는 최적화는 쓸 수 없다:
        신청 가능한 슬롯 값은 "HHMMHHMM"에 코트×시간대 슬롯 ID가 붙은
        형태(예: 1400160076)인데, 미오픈 페이지에는 rent_chk[] 자체가 없어
        그 ID를 미리 알 방법이 없다. 8자리로 예측 구성하면 "이미 예약된 시간"
        값과 같아져 서버가 "존재하지않는 시간 데이터"로 거부한다.
        따라서 슬롯 값은 정각에 조회한 실제 페이지에서만 얻는다.

        재시도·타임아웃을 짧게 잡아 정각 전에 반드시 끝나게 한다.
        """
        court_value = config.COURT_VALUE_MAP.get(court_number)
        if not court_value:
            return False
        params = {
            "place_opt": court_value,
            "nyear": str(year),
            "nmonth": str(month).zfill(2),
            "nday": str(day).zfill(2),
        }
        try:
            await self._request_with_retry(
                "GET", config.TENNIS_RESERVATION_URL, params=params,
                max_retries=max_retries,
                timeout=aiohttp.ClientTimeout(total=total_timeout),
            )
        except Exception as e:
            self._log(f"[WARN] 대상 페이지 예열 실패: {e}", worker_id)
            return False

        self._log("[INFO] 대상 페이지 예열 완료", worker_id)
        return True

    async def submit_reservation(self, court_number, year, month, day,
                                  time_value, test_mode=False, worker_id=None,
                                  page_html=None):
        """예약 신청 - 3단계 프로세스.

        1. get_reservation_page() → DocumentForm 필드 수집
           (page_html이 주어지면 첫 시도는 재조회 없이 재사용 → 정각 1왕복 절약)
        2. rent_period_apply.php → useForm 사용자 정보 수집
        3. rent_period_proc.php → 최종 제출
        """
        self._log(
            f"[INFO] 예약 신청: {year}-{month:02d}-{day:02d} {time_value[:2]}:00",
            worker_id
        )

        if test_mode:
            self._log("[TEST] 테스트 모드 - 실제 신청하지 않음", worker_id)
            return True, "테스트 모드 - 신청 건너뜀"

        for attempt in range(config.SUBMIT_MAX_ATTEMPTS):
            try:
                # Step 1: DocumentForm 필드 수집
                # 우선순위: reserve()가 조회한 HTML → 재조회
                # (재사용은 1회용 — 실패 후 재시도부터는 항상 재조회)
                if page_html is not None:
                    html, page_html = page_html, None
                    form_data = self._collect_document_form(html, worker_id)
                else:
                    html = await self.get_reservation_page(
                        court_number, year, month, day,
                        max_retries=config.CRITICAL_MAX_RETRIES
                    )
                    form_data = self._collect_document_form(html, worker_id) if html else None
                if not form_data:
                    continue

                form_data["place_opt"] = config.COURT_VALUE_MAP[court_number]
                form_data["rent_chk[]"] = time_value
                form_data["use_time"] = "2"

                # Step 2: useForm 사용자 정보 수집
                apply_url = urljoin(config.MAIN_URL, "/rent/rent_period_apply.php")
                apply_text = await self._request_with_retry(
                    "POST", apply_url, data=form_data,
                    max_retries=config.CRITICAL_MAX_RETRIES
                )

                soup2 = BeautifulSoup(apply_text, "html.parser")
                use_form = soup2.find("form", {"name": "useForm"})
                if not use_form:
                    self._log("[WARN] 신청서 폼(useForm)을 찾을 수 없음", worker_id)
                    continue

                for inp in use_form.find_all(["input", "textarea"]):
                    if inp.get("name"):
                        val = (inp.get("value", "")
                               if inp.name != "textarea" else inp.get_text())
                        if val:
                            form_data[inp["name"]] = val

                # 필수 필드 설정
                form_data["regno"] = "0000000000000"
                form_data["com_nm"] = form_data.get("com_nm") or "개인"
                form_data["apply_chk2"] = "1"
                form_data["apply_chk"] = "1"
                form_data["stime"] = time_value[:2]
                form_data["etime"] = time_value[4:6]
                form_data["rent_stime"] = time_value[:4]
                form_data["rent_etime"] = time_value[4:8]
                form_data["rent_p_stime"] = ""
                form_data["rent_p_etime"] = ""

                # Step 3: 최종 제출
                proc_url = urljoin(config.MAIN_URL, "/rent/rent_period_proc.php")
                self._log("[INFO] 최종 제출 중...", worker_id)
                result_text = await self._request_with_retry(
                    "POST", proc_url, data=form_data,
                    max_retries=config.CRITICAL_MAX_RETRIES
                )

                # ── 실패 조건 (먼저 검사) ──────────────────────────────
                # 실패 메시지에도 "완료"가 포함되므로 반드시 성공 조건보다 앞에 둔다.
                # 실제 서버 응답 확인:
                #   실패(중복): alert("예약이 완료된 시간입니다.(3)")
                #   실패(1건): alert("한 건 이상 예약이 완료되어 있습니다.")
                if "한 건 이상 예약" in result_text:
                    self._log("[WARN] 이미 예약됨 (1일 1건 제한)", worker_id)
                    return False, "이미 예약됨 (1일 1건 제한)"
                if "예약이 완료된 시간" in result_text:
                    self._log("[WARN] 이미 예약됨 (다른 사람이 선점)", worker_id)
                    return False, "이미 예약됨 (다른 사람이 선점)"
                if "이미 예약" in result_text or "중복" in result_text:
                    self._log("[WARN] 이미 예약됨 (중복)", worker_id)
                    return False, "이미 예약됨 (중복)"
                if "마감" in result_text:
                    self._log("[WARN] 예약 마감", worker_id)
                    return False, "예약 마감"
                if "존재하지않는" in result_text:
                    self._log("[ERROR] 슬롯 값 오류 (존재하지않는 시간)", worker_id)
                    return False, "슬롯 값 오류 (존재하지않는 시간)"

                # ── 성공 조건 ──────────────────────────────────────────
                # 실제 서버 응답: alert("대관접수가 정상적으로 완료되었습니다..")
                if "정상적으로 완료" in result_text:
                    self._log("[SUCCESS] 대관접수 완료!", worker_id)
                    return True, "대관접수 완료"

                # ── 알 수 없는 응답: 실패로 처리 후 전체 내용 로깅 ────────
                self._log(
                    f"[WARN] proc.php 알 수 없는 응답 — 실패로 처리\n"
                    f"       응답 내용: {result_text[:500]}",
                    worker_id
                )
                return False, "알 수 없는 서버 응답"

            except Exception as e:
                self._log(f"[RETRY {attempt+1}/{config.SUBMIT_MAX_ATTEMPTS}] 예약 신청 오류: {e}", worker_id)
                if attempt < config.SUBMIT_MAX_ATTEMPTS - 1:
                    await asyncio.sleep(_backoff_delay(attempt))

        return False, "예약 신청 실패 (재시도 초과)"

    async def reserve(self, target_date, target_hour, court_number,
                      test_mode=False, worker_id=None):
        """예약 실행 전체 플로우."""
        self._log(f"[START] {target_date} {target_hour:02d}:00 예약 시작", worker_id)
        try:
            dt = datetime.strptime(target_date, "%Y-%m-%d")

            # 슬롯 값(rent_chk[])에는 코트×시간대 슬롯 ID가 붙어 있고 미오픈
            # 페이지에는 존재하지 않으므로, 정각에 실제 페이지를 조회해야만
            # 신청 가능한 값을 얻을 수 있다.
            html = None
            for attempt in range(config.SUBMIT_MAX_ATTEMPTS):
                html = await self.get_reservation_page(
                    court_number, dt.year, dt.month, dt.day,
                    max_retries=config.CRITICAL_MAX_RETRIES
                )
                if html:
                    break
                self._log(f"[RETRY {attempt+1}/{config.SUBMIT_MAX_ATTEMPTS}] 예약 페이지 재조회", worker_id)
                await asyncio.sleep(_backoff_delay(attempt))

            if not html:
                return False, "예약 페이지 조회 실패"

            available = self.get_available_slots(html)
            if not available:
                reason = self._unavailable_reason(html, target_hour)
                self._log(f"[WARN] {reason}", worker_id)
                return False, reason

            self._log(f"[INFO] 예약 가능: {[s['start'] for s in available]}", worker_id)

            target_slot = next(
                (s for s in available if s["start_hour"] == target_hour), None
            )
            if not target_slot:
                reason = self._unavailable_reason(html, target_hour)
                self._log(f"[WARN] {reason}", worker_id)
                return False, reason

            success, message = await self.submit_reservation(
                court_number, dt.year, dt.month, dt.day,
                target_slot["value"], test_mode, worker_id,
                page_html=html,
            )

            if success:
                self._log(f"[SUCCESS] {target_date} {target_hour:02d}:00 예약 완료!", worker_id)

            return success, message

        except Exception as e:
            self._log(f"[ERROR] 예약 중 오류: {e}", worker_id)
            return False, str(e)

    async def close(self):
        if self.session and not self.session.closed:
            await self.session.close()


def _build_tasks(dates=None, hours=None, court=None, courts=None, reservations=None):
    """예약 작업 목록 조립 (config.py 방법 1/2/3 지원)."""
    if reservations is not None:
        return [(r["date"], r["hour"], r["court"]) for r in reservations]

    cfg = config.RESERVATION_CONFIG

    if "reservations" in cfg:
        return [(r["date"], r["hour"], r["court"]) for r in cfg["reservations"]]

    if "court_schedules" in cfg:
        return [
            (d, h, schedule["court"])
            for schedule in cfg["court_schedules"]
            for d in (dates or cfg["dates"])
            for h in schedule["hours"]
        ]

    # 방법 1: dates × hours × courts
    dates = dates or cfg["dates"]
    hours = hours or cfg["hours"]
    if courts is not None:
        court_list = courts if isinstance(courts, list) else [courts]
    elif court is not None:
        court_list = [court]
    elif "courts" in cfg:
        court_list = cfg["courts"]
    else:
        court_list = [cfg["court_number"]]

    return [(d, h, c) for d in dates for h in hours for c in court_list]


async def run_reservation_async(
    test_mode=False, dates=None, hours=None, court=None, courts=None,
    reservations=None, user_id=None, user_pw=None, wait_for_open=True
):
    """asyncio 기반 예약 실행.

    예약 1건 = 독립 세션(PHPSESSID) 사용.
    같은 계정으로 동시에 apply.php → proc.php 를 병렬 요청하면
    서버 PHP 세션 상태가 덮어쓰여져 "(1-1) 정상적인 방법으로 신청" 오류 발생.
    따라서 각 예약 작업에 독립 세션을 부여하고, 로그인만 asyncio.gather로 병렬화한다.

    흐름:
      Phase 1 (pre-login) : N개 봇 생성 → 모두 비동기 병렬 로그인 — O(1) 시간
      Phase 2 (wait)      : 예약 오픈 시간까지 비동기 대기
      Phase 3 (reserve)   : Semaphore로 동시 접속 제한하며 asyncio.gather 동시 실행

    Args:
        wait_for_open: 예약 오픈 시간까지 대기 여부 (API 호출 시 False)
    """
    tasks = _build_tasks(dates, hours, court, courts, reservations)
    uid = user_id or config.USER_ID
    upw = user_pw or config.USER_PW

    print("=" * 60)
    print("고양시 테니스장 자동 예약 (asyncio - 독립 세션)")
    print("=" * 60)
    print(f"총 {len(tasks)}개 예약 작업 | 동시 접속 제한: {config.MAX_CONCURRENT}개")
    for i, (d, h, c) in enumerate(tasks):
        print(f"  [{i+1}] {d} {h:02d}:00~{h+2:02d}:00 / {c}번 코트")
    print(f"설정: 최대 {config.MAX_RETRIES}회 재시도, 타임아웃 ({config.CONNECTION_TIMEOUT},{config.READ_TIMEOUT})초")
    print()

    # ── Phase 1: 로그인 전 대기 ──────────────────────────────────
    if wait_for_open:
        await wait_before_login_async()

    # ── Phase 2: N개 봇 생성 + 병렬 로그인 (O(1)) ───────────────
    async def create_bot(task_idx):
        bot = TennisReservationAsync()
        await bot._create_session()
        bot.worker_id = task_idx
        if await bot.login(uid, upw):
            await bot.warmup_connection()
            return bot
        await bot.close()
        return None

    print(f"[INFO] {len(tasks)}개 세션 병렬 로그인 시작...")
    bot_list = await asyncio.gather(*[create_bot(i + 1) for i in range(len(tasks))])
    bots = [(bot, d, h, c)
            for bot, (d, h, c) in zip(bot_list, tasks)
            if bot is not None]

    failed_login = len(tasks) - len(bots)
    if failed_login:
        print(f"[WARN] {failed_login}개 세션 로그인 실패")
    if not bots:
        return {"success": False, "results": [], "message": "모든 로그인 실패"}
    print(f"[INFO] {len(bots)}개 세션 준비 완료")

    # ── Phase 3: 예약 오픈 시간까지 비동기 대기 ──────────────────
    # 로그인 직후 예열한 연결은 keepalive(클라 30초, 서버 수 초)로 정각 전에
    # 끊기므로, 대기 루프가 오픈 직전(남은 20초/4초)에 재예열을 트리거한다.
    # 1차(20초)·2차(4초) 모두 대상 페이지를 조회해 연결을 예열한다.
    #   메인 페이지 GET은 예열이 만든 PHP 세션 상태(마지막 조회 페이지)를
    #   덮어쓸 수 있어 쓰지 않는다. 봇별 지터로 동시 핸드셰이크 폭주를 분산한다.
    if wait_for_open:
        rewarm_count = 0

        async def _warmup(bot, idx, d, c, jitter, **kw):
            await asyncio.sleep(random.uniform(0, jitter))
            pdt = datetime.strptime(d, "%Y-%m-%d")
            await bot.warmup_target_page(c, pdt.year, pdt.month, pdt.day,
                                         worker_id=idx, **kw)

        async def rewarm_all():
            nonlocal rewarm_count
            rewarm_count += 1
            if rewarm_count == 1:
                await asyncio.gather(
                    *[_warmup(bot, i + 1, d, c, jitter=1.0)
                      for i, (bot, d, h, c) in enumerate(bots)],
                    return_exceptions=True,
                )
            else:
                # 정각 전에 반드시 끝나야 하므로 짧게 1회만 시도
                await asyncio.gather(
                    *[_warmup(bot, i + 1, d, c, jitter=0.3,
                                max_retries=1, total_timeout=3)
                      for i, (bot, d, h, c) in enumerate(bots)],
                    return_exceptions=True,
                )

        if not await wait_for_reservation_open_async(warmup=rewarm_all):
            for bot, *_ in bots:
                await bot.close()
            return {"success": False, "results": [],
                    "message": "예약일이 아니거나 이미 지났습니다"}

    # ── Phase 4: 동시 예약 실행 (독립 세션) ─────────────────────
    sem = asyncio.Semaphore(config.MAX_CONCURRENT)
    log_path = LOGS_DIR / f"timing_{datetime.now():%Y%m%d_%H%M%S}_{uid}.jsonl"

    async def worker(bot, task_idx, d, h, c):
        t_queued = time.monotonic()
        async with sem:
            sem_wait_ms = round((time.monotonic() - t_queued) * 1000, 1)
            # 발사 지터: 동일 IP 동시 폭주로 인한 서버 큐잉·차단 완화
            if config.FIRE_JITTER_MS > 0:
                await asyncio.sleep(random.uniform(0, config.FIRE_JITTER_MS / 1000))
            fire_ts = datetime.now().isoformat(timespec="milliseconds")
            t_fire = time.monotonic()
            success, message = False, "예외 발생"
            try:
                success, message = await bot.reserve(d, h, c, test_mode, worker_id=task_idx)
                return {"date": d, "hour": h, "court": c,
                        "success": success, "message": message}
            finally:
                await bot.close()
                # 예약 1건 요약 + 로그인부터의 전체 요청 이벤트 (분석용)
                _dump_timing(log_path, {
                    "worker": task_idx, "user_id": uid,
                    "date": d, "hour": h, "court": c,
                    "fire_ts": fire_ts, "sem_wait_ms": sem_wait_ms,
                    "total_ms": round((time.monotonic() - t_fire) * 1000, 1),
                    "success": success, "message": message,
                    "events": bot.timing,
                })

    results = list(await asyncio.gather(
        *[worker(bot, i + 1, d, h, c) for i, (bot, d, h, c) in enumerate(bots)]
    ))

    success_count = sum(1 for r in results if r["success"])
    print()
    print("=" * 60)
    print("[결과]")
    for r in results:
        status = "성공" if r["success"] else "실패"
        print(f"  {r['date']} {r['hour']:02d}:00 {r['court']}번 코트 - {status} ({r['message']})")
    print(f"총 {success_count}/{len(results)}건 성공")
    print("=" * 60)

    return {
        "success": success_count > 0,
        "results": results,
        "summary": f"{success_count}/{len(results)}건 성공",
    }


def get_weekends_in_month(year, month):
    """해당 월의 모든 토요일, 일요일 반환."""
    cal = calendar.Calendar()
    return [
        date(year, month, day_num)
        for day_num, weekday in cal.itermonthdays2(year, month)
        if day_num != 0 and weekday in (5, 6)
    ]


def get_all_days_in_month(year, month):
    """해당 월의 모든 날짜와 요일 반환."""
    cal = calendar.Calendar()
    return [
        (date(year, month, day_num), weekday)
        for day_num, weekday in cal.itermonthdays2(year, month)
        if day_num != 0
    ]


def is_likely_closure(slots):
    """모든 슬롯(8개: 06:00~22:00)이 비어있으면 휴장일로 추정."""
    if len(slots) >= 8:
        available_hours = {s["start_hour"] for s in slots}
        if set(config.AVAILABLE_HOURS).issubset(available_hours):
            return True
    return False


async def search_available_slots_async(
    year, month, courts=None, hours=None, verbose=True, user_id=None, user_pw=None
):
    """해당 월 토/일 예약 가능 시간대 검색 (비동기)."""
    courts = courts or config.ALL_COURTS
    hours = hours or config.SEARCH_DEFAULT_HOURS
    weekends = get_weekends_in_month(year, month)

    if not weekends:
        return {"error": "해당 월에 주말이 없습니다.", "results": []}

    print("=" * 70)
    print(f"  {year}년 {month}월 주말 빈자리 검색")
    print("=" * 70)
    print(f"  검색 대상: 토/일 {len(weekends)}일 | 코트: {courts} | 시간: {[f'{h:02d}:00' for h in hours]}")
    print("  ※ 예약을 하지 않고 빈자리만 조회합니다")
    print("=" * 70)
    print()

    async with TennisReservationAsync() as bot:
        if not await bot.login(user_id, user_pw):
            return {"error": "로그인 실패", "results": []}

        results = []
        skipped_dates = set()
        day_names = ["월", "화", "수", "목", "금", "토", "일"]

        for d in weekends:
            day_name = day_names[d.weekday()]
            date_str = f"{d.year}-{d.month:02d}-{d.day:02d}"

            if verbose:
                print(f"[검색] {date_str} ({day_name})")

            date_skipped = False
            for court in courts:
                html = await bot.get_reservation_page(court, d.year, d.month, d.day)
                if not html:
                    if verbose:
                        print(f"  {court}코트: 페이지 조회 실패")
                    continue

                slots = bot.get_available_slots(html)

                if is_likely_closure(slots):
                    if verbose and not date_skipped:
                        print(f"  ※ 휴장일 추정 - 제외")
                        date_skipped = True
                        skipped_dates.add(date_str)
                    continue

                for hour in hours:
                    if hour in [s["start_hour"] for s in slots]:
                        results.append({
                            "date": date_str, "day": day_name,
                            "court": court, "hour": hour,
                            "time": f"{hour:02d}:00~{hour+2:02d}:00",
                        })
                        if verbose:
                            print(f"  ○ {court}코트 {hour:02d}:00~{hour+2:02d}:00 빈자리")

                await asyncio.sleep(0.1)

            if verbose:
                print()

    print("=" * 70)
    if skipped_dates:
        print(f"  검색 결과: 총 {len(results)}건 빈자리 (휴장일 {len(skipped_dates)}일 제외)")
    else:
        print(f"  검색 결과: 총 {len(results)}건 빈자리 발견")
    print("=" * 70)

    if results:
        current_date = None
        for r in results:
            if r["date"] != current_date:
                current_date = r["date"]
                print(f"  [{r['date']} ({r['day']})]")
            print(f"    - {r['court']}코트 {r['time']}")

    print()
    print("=" * 70)

    return {"year": year, "month": month, "total": len(results), "results": results}


async def search_all_slots_async(
    year, month, courts=None, verbose=True, user_id=None, user_pw=None
):
    """해당 월 전체 날짜/시간 예약 가능 시간대 검색 (비동기)."""
    courts = courts or config.ALL_COURTS
    all_hours = config.AVAILABLE_HOURS
    all_days = get_all_days_in_month(year, month)

    if not all_days:
        return {"error": "해당 월에 날짜가 없습니다.", "results": []}

    BLUE = "\033[94m"
    RESET = "\033[0m"

    print("=" * 70)
    print(f"  {year}년 {month}월 전체 빈자리 검색")
    print("=" * 70)
    print(f"  검색 대상: 전체 {len(all_days)}일 | 코트: {courts} | 시간: 06:00~22:00")
    print("  ※ 예약을 하지 않고 빈자리만 조회합니다")
    print("=" * 70)
    print()

    async with TennisReservationAsync() as bot:
        if not await bot.login(user_id, user_pw):
            return {"error": "로그인 실패", "results": []}

        results = []
        skipped_dates = set()
        day_names = ["월", "화", "수", "목", "금", "토", "일"]

        for d, weekday in all_days:
            day_name = day_names[weekday]
            date_str = f"{d.year}-{d.month:02d}-{d.day:02d}"
            is_weekend = weekday in (5, 6)

            if verbose:
                color = BLUE if is_weekend else ""
                reset = RESET if is_weekend else ""
                print(f"{color}[검색] {date_str} ({day_name}){reset}")

            date_skipped = False
            for court in courts:
                html = await bot.get_reservation_page(court, d.year, d.month, d.day)
                if not html:
                    if verbose:
                        print(f"  {court}코트: 페이지 조회 실패")
                    continue

                slots = bot.get_available_slots(html)

                if is_likely_closure(slots):
                    if verbose and not date_skipped:
                        print(f"  ※ 휴장일 추정 - 제외")
                        date_skipped = True
                        skipped_dates.add(date_str)
                    break

                for hour in all_hours:
                    if hour in [s["start_hour"] for s in slots]:
                        results.append({
                            "date": date_str, "day": day_name,
                            "court": court, "hour": hour,
                            "time": f"{hour:02d}:00~{hour+2:02d}:00",
                            "is_weekend": is_weekend,
                        })
                        if verbose:
                            color = BLUE if is_weekend else ""
                            reset = RESET if is_weekend else ""
                            print(f"  {color}○ {court}코트 {hour:02d}:00~{hour+2:02d}:00 빈자리{reset}")

                await asyncio.sleep(0.1)

            if verbose and not date_skipped:
                print()

    print("=" * 70)
    if skipped_dates:
        print(f"  검색 결과: 총 {len(results)}건 빈자리 (휴장일 {len(skipped_dates)}일 제외)")
    else:
        print(f"  검색 결과: 총 {len(results)}건 빈자리 발견")
    print("=" * 70)

    if results:
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
        "year": year, "month": month,
        "total": len(results), "results": results,
        "skipped_dates": list(skipped_dates),
    }


if __name__ == "__main__":
    import urllib3
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    result = asyncio.run(run_reservation_async(test_mode=True))
    print(result)
