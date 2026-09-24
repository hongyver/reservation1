#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
테니스장 예약 현황 뷰어

config.toml의 다중 계정 예약 정보를 달력 형태로 브라우저에 표시한다.
  - 달력 그리드: 날짜 셀마다 코트(1-4) × 시간 미니 그리드
  - 왼쪽 패널: 계정 체크박스, PW 마스킹, 예약 건수
  - 중복 슬롯: 황색 + ⚠ 표시 + 툴팁

사용법:
    python3 viewer.py
    python3 viewer.py 2026 7   # 특정 월 지정
"""

import json
import sys
import tempfile
import threading
import webbrowser
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import tomlkit

import config
import schedule

CONFIG_PATH = config.CONFIG_FILE

ACCOUNT_COLORS = [
    '#3B82F6', '#EF4444', '#10B981', '#F59E0B', '#8B5CF6',
    '#EC4899', '#14B8A6', '#F97316', '#6366F1', '#84CC16',
    '#06B6D4', '#D946EF', '#78716C',
]

ALL_COURTS = [1, 2, 3, 4]


# ─── config.toml 업데이트 ────────────────────────────────────────────────────

# ThreadingHTTPServer 전환으로 동시 요청이 가능해져 config.toml 쓰기 경합을 막는다
_CONFIG_LOCK = threading.Lock()


def _load_doc():
    """config.toml을 tomlkit 문서로 읽는다 (주석·서식 보존 편집용)."""
    return tomlkit.parse(CONFIG_PATH.read_text(encoding="utf-8"))


def save_schedule_entries(entries):
    """[저장] 버튼 — reservation.json 을 통째로 새로 쓴다.

    슬롯 클릭마다 저장하던 것을 명시적 저장으로 바꿨다. 브라우저가 전체
    스냅샷(체크된 계정 × 배정 슬롯)을 보내고, 여기서는 그대로 넘긴다.
    일련번호(no) 부여·정렬·백업은 schedule.save() 가 담당한다.

    Returns: (ok: bool, detail: dict|str)
    """
    with _CONFIG_LOCK:
        try:
            return schedule.save(entries)
        except (config.ConfigError, schedule.ScheduleError) as e:
            return False, str(e)


def _update_config_int(section, key, value, lo, hi):
    """config.toml의 정수 설정값을 교체한다.

    Returns: (ok: bool, detail: int|str)
    """
    try:
        value = int(value)
    except (TypeError, ValueError):
        return False, f"정수가 아님: {value!r}"
    if not (lo <= value <= hi):
        return False, f"허용 범위({lo}~{hi}) 초과: {value}"

    with _CONFIG_LOCK:
        if not CONFIG_PATH.exists():
            return False, "config.toml 파일 없음"

        doc = _load_doc()
        if section not in doc:
            return False, f"[{section}] 섹션 없음"
        doc[section][key] = value

        CONFIG_PATH.write_text(tomlkit.dumps(doc), encoding="utf-8")
        return True, value


def update_config_login_advance(minutes):
    """config.toml의 [schedule] login_advance_minutes 값을 교체한다."""
    return _update_config_int("schedule", "login_advance_minutes", minutes, 1, 120)


def update_config_slots_per_account(count):
    """config.toml의 [schedule] slots_per_account 값을 교체한다."""
    return _update_config_int("schedule", "slots_per_account", count, 1, 10)


def backup_config():
    """config.toml을 타임스탬프 파일명으로 백업한다. (config.toml.bak.YYYYMMDD_HHMMSS)"""
    if CONFIG_PATH.exists():
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        bak_path = CONFIG_PATH.parent / f"config.toml.bak.{ts}"
        bak_path.write_text(CONFIG_PATH.read_text(encoding="utf-8"), encoding="utf-8")
        print(f"[viewer] config.toml 백업: {bak_path.name}")


# ─── HTTP API 서버 ────────────────────────────────────────────────────────────

# HTML을 HTTP로 서빙하기 위해 모듈 레벨에 보관
# (file:// 프로토콜에서 fetch → CORS null-origin 차단 우회)
# _HTML_CONTENT: 재빌드 실패 시 fallback용 시작 시점 스냅샷
# _BUILD_PARAMS: do_GET이 매 요청마다 최신 config.toml로 재빌드할 때 쓰는 파라미터
_HTML_CONTENT: str = ""
_BUILD_PARAMS: dict = {}


class _APIHandler(BaseHTTPRequestHandler):
    """브라우저 → Python config.toml 업데이트를 처리하는 로컬 HTTP 핸들러.

    GET /          → HTML 페이지 서빙 (same-origin으로 CORS 완전 해소)
    POST /api/save-schedule → reservation.json 전체 교체 ([저장] 버튼)
    """

    def log_message(self, *_):
        pass  # 콘솔 로그 억제

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")

    def do_OPTIONS(self):
        self.send_response(200)
        self._cors()
        self.end_headers()

    def do_GET(self):
        if self.path in ("/", "/index.html"):
            # 매 요청마다 최신 config.toml 기준으로 재빌드 → refresh 시 변경값 반영.
            # config validation 예외 시 시작 시점 스냅샷(_HTML_CONTENT)으로 폴백.
            try:
                html = build_html(
                    load_data(),
                    _BUILD_PARAMS["init_year"],
                    _BUILD_PARAMS["init_month"],
                    _BUILD_PARAMS["api_port"],
                    load_settings(),
                )
            except Exception as e:
                print(f"[viewer] HTML 재빌드 실패, 캐시 사용: {e}")
                html = _HTML_CONTENT
            content = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(content))
            # 매 요청마다 새로 빌드하므로 브라우저가 캐시하면 안 된다.
            # 헤더가 없으면 브라우저 휴리스틱 캐싱이 걸려 viewer.py 를 고쳐도
            # 새로고침만으로는 반영되지 않는다 (실제로 겪은 문제).
            self.send_header("Cache-Control", "no-store, must-revalidate")
            self._cors()
            self.end_headers()
            self.wfile.write(content)

    def do_POST(self):
        if self.path == "/api/save-schedule":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            ok, detail = save_schedule_entries(body.get("entries") or [])
            payload = json.dumps(
                {"ok": ok, "detail": detail}, ensure_ascii=False
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(payload))
            self._cors()
            self.end_headers()
            self.wfile.write(payload)

        elif self.path == "/api/search":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            dates  = body.get("dates") or []
            if not dates:
                result = {"ok": False, "error": "날짜 없음"}
            else:
                try:
                    result = search_dates_availability(dates)
                except Exception as e:
                    result = {"ok": False, "error": str(e)}
            payload = json.dumps(result, ensure_ascii=False).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(payload))
            self._cors()
            self.end_headers()
            self.wfile.write(payload)

        elif self.path == "/api/save-login-advance":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            ok, detail = update_config_login_advance(body.get("minutes"))
            payload = json.dumps({"ok": ok, "detail": detail}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(payload))
            self._cors()
            self.end_headers()
            self.wfile.write(payload)

        elif self.path == "/api/save-slots-per-account":
            length = int(self.headers.get("Content-Length", 0))
            body   = json.loads(self.rfile.read(length))
            ok, detail = update_config_slots_per_account(body.get("count"))
            payload = json.dumps({"ok": ok, "detail": detail}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", len(payload))
            self._cors()
            self.end_headers()
            self.wfile.write(payload)


def start_api_server():
    """사용 가능한 포트를 찾아 백그라운드 HTTP API 서버를 시작한다."""
    for port in range(8765, 8800):
        try:
            # 검색처럼 오래 걸리는 요청이 저장·페이지 로드를 막지 않도록 스레드 처리
            server = ThreadingHTTPServer(("127.0.0.1", port), _APIHandler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server, port
        except OSError:
            continue
    raise RuntimeError("사용 가능한 포트 없음 (8765-8799)")


# ─── 데이터 로드 ──────────────────────────────────────────────────────────────

def load_data():
    """계정(accounts.txt) + 예약 계획(reservation.json)을 합쳐 로드한다.

    계정은 전부 싣고, 예약은 계획에 있는 계정에만 붙인다.
    checked 는 사이드바 체크 상태의 초기값 — 계획에 있던 계정만 체크된다.
    """
    config.reload()

    try:
        plan = schedule.load()
        by_id = {e["id"]: e["raw_slots"] for e in plan["entries"]}
    except schedule.ScheduleError:
        by_id = {}   # 계획이 아직 없으면 전부 빈 상태로 시작한다

    accounts = []
    for a in config.load_accounts():
        raw = by_id.get(a["user_id"], [])
        reservations = sorted(
            ({"date": d, "hour": int(h), "court": int(c)}
             for d, h, c in (x.split(":") for x in raw)),
            key=lambda r: (r["date"], r["hour"], r["court"]),
        )
        accounts.append({
            "num":          a["num"],
            "name":         a.get("name", ""),
            "user_id":      a["user_id"],
            "user_pw":      a.get("user_pw", ""),
            "color":        ACCOUNT_COLORS[(a["num"] - 1) % len(ACCOUNT_COLORS)],
            "reservations": reservations,
            "checked":      a["user_id"] in by_id,
        })
    return accounts


def load_settings():
    """config.toml에서 실행 설정값을 로드한다 (헤더 표시용)."""
    config.reload()
    return {
        "login_advance_minutes": config.LOGIN_ADVANCE_MINUTES,
        "slots_per_account":     config.SLOTS_PER_ACCOUNT,
    }


def load_holidays(years):
    """대한민국 공휴일(대체공휴일·음력 공휴일 포함)을 "YYYY-MM-DD" 목록으로 반환한다.

    holidays 패키지 미설치 시 빈 목록 — 카운터의 공휴일 수요만 빠진다.
    """
    try:
        import holidays as _holidays
    except ImportError:
        print("[viewer] holidays 패키지 없음 — 공휴일 수요 미반영 (pip3 install holidays)")
        return []
    return sorted(d.isoformat() for d in _holidays.KR(years=list(years)))


def get_initial_month(accounts):
    """예약 데이터 중 가장 빠른 연월을 반환한다."""
    dates = [r["date"] for a in accounts for r in a["reservations"]]
    if dates:
        d = sorted(dates)[0]
        return int(d[:4]), int(d[5:7])
    from datetime import datetime
    n = datetime.now()
    return n.year, n.month


# ─── 빈자리 검색 ──────────────────────────────────────────────────────────────

VIEWER_HOURS = [6, 8, 10, 12, 14, 16, 18, 20]  # config.AVAILABLE_HOURS와 동일


def search_dates_availability(dates):
    """선택된 날짜들의 코트별 빈자리를 병렬 조회한다 (읽기 전용, 예약 안 함).

    첫 번째 계정으로 로그인 1회 → 날짜×코트 페이지를 Semaphore(4)로 병렬 조회.
    어느 한 코트라도 휴장일 패턴이면 해당 날짜 전체를 휴장으로 처리한다.

    Returns: {"ok", "results", "closed_dates", "searched_dates", "elapsed"}
             실패 시 {"ok": False, "error": str}
    """
    import asyncio
    import time

    from reservation_async import TennisReservationAsync, is_likely_closure

    accounts = load_data()
    if not accounts:
        return {"ok": False, "error": "계정 없음"}
    cred = accounts[0]
    t0 = time.time()

    async def _run():
        async with TennisReservationAsync() as bot:
            if not await bot.login(cred["user_id"], cred["user_pw"]):
                return {"ok": False, "error": "로그인 실패"}

            sem = asyncio.Semaphore(4)

            async def fetch(date_str, court):
                y, m, d = (int(x) for x in date_str.split("-"))
                async with sem:
                    html = await bot.get_reservation_page(court, y, m, d)
                    await asyncio.sleep(0.05)  # 서버 부하 완화
                return date_str, court, html

            pages = await asyncio.gather(
                *(fetch(ds, c) for ds in dates for c in ALL_COURTS)
            )

            closed = set()
            slots_by_key = {}
            for date_str, court, html in pages:
                if not html:
                    continue
                slots = bot.get_available_slots(html)
                if is_likely_closure(slots):
                    closed.add(date_str)
                slots_by_key[(date_str, court)] = slots

            results = [
                {"date": ds, "court": c, "hour": s["start_hour"]}
                for (ds, c), slots in sorted(slots_by_key.items())
                if ds not in closed
                for s in slots
                if s["start_hour"] in VIEWER_HOURS
            ]
            results.sort(key=lambda r: (r["date"], r["hour"], r["court"]))
            return {
                "ok": True,
                "results": results,
                "closed_dates": sorted(closed),
                "searched_dates": sorted(dates),
                "elapsed": round(time.time() - t0, 1),
            }

    return asyncio.run(_run())


# ─── HTML 생성 ────────────────────────────────────────────────────────────────

_CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f1f5f9;color:#1e293b;font-size:14px}

/* 전체 레이아웃 */
.app{display:flex;flex-direction:column;height:100vh;overflow:hidden}
.app-header{display:flex;align-items:center;justify-content:space-between;padding:10px 18px;background:#0f172a;color:#fff;flex-shrink:0}
.app-header h1{font-size:16px;font-weight:700;display:flex;align-items:center;gap:8px}
.hdr-info{font-size:12px;font-weight:600;color:#cbd5e1;background:rgba(255,255,255,.08);border:1px solid rgba(255,255,255,.18);border-radius:6px;padding:4px 10px;display:inline-flex;align-items:center;gap:5px}
.hdr-num{width:46px;background:rgba(255,255,255,.14);border:1px solid rgba(255,255,255,.25);border-radius:4px;color:#fff;font-size:12px;font-weight:700;text-align:center;padding:2px 4px;outline:none}
.hdr-num:focus{border-color:rgba(255,255,255,.6);background:rgba(255,255,255,.2)}
.hdr-info.short{background:rgba(239,68,68,.3);border-color:rgba(239,68,68,.65);color:#fecaca}

/* 헤더 모드 토글 (배치/검색) */
.mode-seg{display:flex;border:1px solid rgba(255,255,255,.3);border-radius:7px;overflow:hidden;margin:0 4px}
.mode-seg .seg{border:none!important;border-radius:0!important;background:transparent;padding:5px 11px;font-size:12px;cursor:pointer;color:#cbd5e1;transition:background .15s;white-space:nowrap}
.mode-seg .seg:hover{background:rgba(255,255,255,.12)}
#modeDispatch.on{background:#6366f1!important;color:#fff;font-weight:700}
#modeSearch.on{background:#16a34a!important;color:#fff;font-weight:700}
.header-btns{display:flex;gap:6px;align-items:center}
.header-btns button{padding:5px 12px;border:1px solid rgba(255,255,255,.25);border-radius:6px;background:transparent;color:#fff;cursor:pointer;font-size:12px;transition:background .15s}
.header-btns button:hover{background:rgba(255,255,255,.12)}
.header-btns button:disabled{opacity:.25;cursor:not-allowed}
.header-btns button:disabled:hover{background:transparent}
#saveBtn{background:rgba(34,197,94,.28);border-color:rgba(34,197,94,.6);font-weight:600}
#saveBtn.dirty{background:#f59e0b;border-color:#f59e0b;color:#1e293b;font-weight:800;animation:pulse 1.6s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.62}}
.layout{display:flex;flex:1;overflow:hidden}

.sidebar{width:220px;min-width:220px;background:#fff;border-right:1px solid #e2e8f0;overflow-y:auto;padding:10px 8px;display:flex;flex-direction:column;gap:4px}
.sidebar-label{font-size:10px;font-weight:700;color:#94a3b8;letter-spacing:.08em;text-transform:uppercase;padding:4px 4px 2px}
.acct-card{padding:7px 8px;border-radius:8px;border:1.5px solid transparent;transition:all .15s;background:#fafafa}
.acct-card.on{border-color:var(--c);background:color-mix(in srgb,var(--c) 7%,#fff)}
.acct-card.off{opacity:.38}
.acct-r1{display:flex;align-items:center;gap:5px;margin-bottom:3px}
.acct-cb{width:15px;height:15px;cursor:pointer;flex-shrink:0}
.acct-dot{width:10px;height:10px;border-radius:50%;flex-shrink:0;background:var(--c)}
.acct-num{font-size:10px;font-weight:700;color:#64748b;min-width:14px}
.acct-id{font-size:12px;font-weight:700;color:#1e293b;flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.acct-name{font-size:11px;font-weight:600;color:#64748b;flex-shrink:0;max-width:62px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.acct-r2{display:flex;align-items:center;gap:3px;padding-left:20px}
.pw-box{flex:1;border:none;background:#f1f5f9;border-radius:4px;padding:2px 5px;font-size:10px;color:#475569;font-family:monospace;outline:none;cursor:default}
.pw-eye{background:none;border:none;cursor:pointer;font-size:11px;color:#94a3b8;padding:0;line-height:1}
.pw-eye:hover{color:#475569}
.acct-r3{padding-left:20px;margin-top:2px;font-size:10px;color:#94a3b8;display:flex;align-items:center;gap:5px}
.cnt-delta{font-weight:800;font-size:10px;padding:0 4px;border-radius:4px;line-height:15px}
.cnt-delta.up{color:#065f46;background:#6ee7b7}
.cnt-delta.down{color:#7f1d1d;background:#fca5a5}
.cnt-delta.same{color:#475569;background:#e2e8f0}
.cnt-delta.move{color:#1e3a8a;background:#93c5fd}
@keyframes flashCard{0%{background:#fde68a}100%{background:transparent}}
.acct-card.flash{animation:flashCard 1.1s ease-out}

/* 달력 영역 */
.cal-area{flex:1;overflow:auto;padding:14px}
.month-nav{display:flex;align-items:center;gap:12px;margin-bottom:12px}
.month-nav > button{width:30px;height:30px;border:1px solid #e2e8f0;border-radius:7px;background:#fff;cursor:pointer;font-size:14px;transition:background .15s}
.month-nav > button:hover{background:#f8fafc}
.month-title{font-size:17px;font-weight:700;min-width:110px;text-align:center}

/* 일괄 날짜 선택 */
.quick-days{display:flex;gap:4px;margin-left:6px}
.quick-days button{padding:5px 10px;font-size:11px;border:1px solid #e2e8f0;border-radius:6px;background:#fff;cursor:pointer;color:#475569;transition:background .15s;white-space:nowrap}
.quick-days button:hover{background:#f8fafc}

/* 달력 그리드 */
.cal-grid{display:grid;grid-template-columns:repeat(7,1fr);gap:3px}
.dow-hd{text-align:center;font-size:11px;font-weight:600;color:#64748b;padding:5px 0}
.dow-hd.sat{color:#2563eb}.dow-hd.sun{color:#dc2626}

.day-cell{background:#fff;border:1px solid #e2e8f0;border-radius:8px;min-height:unset;overflow:hidden;transition:box-shadow .15s}
.day-cell:hover{box-shadow:0 2px 8px rgba(0,0,0,.08)}
.day-cell.blank{background:#f8fafc;border-color:#f1f5f9}
.day-cell.is-today{border:2px solid #3b82f6}
.day-cell.is-sat{background:#eff6ff}.day-cell.is-sun{background:#fef2f2}

.day-num{font-size:12px;font-weight:700;padding:4px 7px 2px;display:flex;justify-content:space-between;align-items:center}
.day-num .dow-tag{font-size:9px;font-weight:500;color:#94a3b8}
.day-num-l{display:flex;align-items:center;gap:3px}
.day-num-r{display:flex;align-items:center;gap:4px}
.cnt-pill{font-size:8px;font-weight:800;color:#fff;background:#6366f1;border-radius:6px;padding:0 4px;line-height:12px}
.day-cb{width:11px;height:11px;accent-color:#6366f1;cursor:pointer;margin:0}
.mode-search .day-cb{accent-color:#16a34a}
.closed-badge{font-size:8px;font-weight:700;color:#dc2626;background:#fee2e2;border-radius:4px;padding:1px 3px;line-height:1}
.day-num.sat-n{color:#2563eb}.day-num.sun-n{color:#dc2626}

/* 미니 그리드 (코트×시간) */
.mini{padding:0 4px 5px;display:grid;gap:1px}
.ct-hd{font-size:8px;font-weight:700;color:#94a3b8;text-align:center;padding-bottom:1px;line-height:1.2}
.t-label{font-size:8px;color:#94a3b8;font-weight:500;text-align:right;padding-right:2px;line-height:1;display:flex;align-items:center;justify-content:flex-end}

/* 슬롯 */
.slot{height:15px;border-radius:3px;display:flex;align-items:center;justify-content:center;font-size:8px;font-weight:800;cursor:pointer;position:relative;transition:opacity .2s,transform .1s;user-select:none}
.slot:hover{transform:scale(1.15);z-index:20}
.slot.empty{background:#f1f5f9;border:1px dashed #cbd5e1;color:#d1d5db}
.slot.booked{color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.35)}
.slot.dup{background:#fef3c7!important;border:1.5px solid #f59e0b!important;color:#92400e;flex-direction:column;font-size:7px;gap:0;line-height:1.1}
.slot.dimmed{opacity:.08!important;pointer-events:none}
.slot.avail{background:#dcfce7;border:1.5px solid #22c55e;color:#16a34a}
.slot.taken{background:#f8fafc;border:1px dashed #e2e8f0;color:#cbd5e1}
/* 배정 슬롯 위 검색 결과 — 초록 원 안의 번호=빈자리 / 빨간 테두리+×=이미 마감(그대로 두면 예약 실패)
   × 는 빈 슬롯의 마감 표시와 같은 문자다 — '× = 마감' 으로 기호를 통일한다. */
.slot.booked.chk-ok{background-image:radial-gradient(circle at 50% 50%,transparent 0 5.6px,#22c55e 5.6px 6.5px,rgba(255,255,255,.95) 6.5px 7.2px,transparent 7.2px)!important;font-size:7px}
/* 중복 슬롯은 내용이 2줄이라 원을 씌울 수 없다 — 좌하단 초록 점으로 대신한다 */
.slot.dup.chk-ok::before{content:'';position:absolute;left:1px;bottom:1px;width:4px;height:4px;border-radius:50%;background:#22c55e;box-shadow:0 0 0 1px #fff}
.slot.chk-no{border:1.5px solid #dc2626!important}
/* × 를 flex 아이템으로 둬 번호와 나란히 놓는다 — 두 자리 번호와 겹치지 않는다.
   중복 슬롯은 내용이 이미 2줄이고 자체 ⚠ 가 있어 테두리만 빨갛게 한다. */
.slot.booked.chk-no{gap:1px;font-size:7px}
.slot.booked.chk-no::before{content:'×';font-size:9px;font-weight:900;color:#dc2626;line-height:1;text-shadow:0 0 2px #fff,0 0 2px #fff,0 0 2px #fff}
.mini.searched-only{opacity:.62}   /* 검색만 한 날 — 결과는 읽히되 배정 있는 날보다 뒤로 */
.mini.no-res{opacity:.28}
.mini.no-res .slot{cursor:default}
.mini.no-res .slot:hover{transform:none}

/* 툴팁 */
#tip{position:fixed;background:rgba(15,23,42,.93);color:#fff;padding:7px 11px;border-radius:8px;font-size:12px;line-height:1.65;pointer-events:none;z-index:9999;display:none;white-space:nowrap;box-shadow:0 4px 20px rgba(0,0,0,.3);max-width:280px}
#tip.show{display:block}
.tip-head{font-weight:700;margin-bottom:1px}
.tip-dup{display:inline-block;background:#f59e0b;color:#1e293b;border-radius:4px;padding:0 5px;font-size:10px;font-weight:700;margin-bottom:3px}

/* 범례 */
.legend{display:flex;gap:14px;margin-top:10px;align-items:center;font-size:11px;color:#64748b;flex-wrap:wrap}
.leg-item{display:flex;align-items:center;gap:4px}
.leg-box{width:14px;height:14px;border-radius:3px;flex-shrink:0}
.leg-empty{background:#f1f5f9;border:1px dashed #cbd5e1}
.leg-booked{background:#3b82f6}
.leg-dup{background:#fef3c7;border:1.5px solid #f59e0b}
.leg-avail{background:#dcfce7;border:1.5px solid #22c55e}
.leg-taken{background:#f8fafc;border:1px dashed #e2e8f0}
.leg-chkok{background:#3b82f6;background-image:radial-gradient(circle at 50% 50%,transparent 0 5.6px,#22c55e 5.6px 6.5px,rgba(255,255,255,.95) 6.5px 7.2px,transparent 7.2px)}
.leg-chkno{background:#3b82f6;border:1.5px solid #dc2626;position:relative}
.leg-chkno::before{content:'×';position:absolute;left:0;top:50%;transform:translateY(-50%);font-size:9px;font-weight:900;color:#dc2626;line-height:1;text-shadow:0 0 2px #fff,0 0 2px #fff,0 0 2px #fff}
.leg-ckd{background:#3b82f6;box-shadow:0 0 0 2px #0f172a}
/* ── 포커스 반전 ── */
.acct-card.fc{background:var(--c)!important;border-color:var(--c)!important}
.acct-card.fc .acct-id,.acct-card.fc .acct-num,.acct-card.fc .acct-name,.acct-card.fc .acct-r3{color:#fff!important}
.acct-card.fc .pw-box{background:rgba(255,255,255,.2);color:#fff}
.slot.hi{outline:2px solid rgba(255,255,255,.9);z-index:5;filter:brightness(1.12)}
.slot.dfm{opacity:.07!important;pointer-events:none}
/* ── 슬롯 체크 ── */
.slot.ckd{box-shadow:0 0 0 2px #0f172a!important;z-index:6}
.slot.ckd::after{content:'✓';position:absolute;top:-6px;right:-4px;font-size:9px;color:#0f172a;font-weight:900;background:#fff;border-radius:50%;line-height:1;padding:0 1px;z-index:7}
/* ── 실시간 저장 토스트 ── */
#toast{position:fixed;top:16px;left:50%;transform:translateX(-50%) translateY(-50px);background:#1e293b;color:#fff;padding:8px 20px;border-radius:20px;font-size:13px;font-weight:600;opacity:0;transition:all .25s;z-index:9999;pointer-events:none}
#toast.show{transform:translateX(-50%) translateY(0);opacity:1}
#toast.err{background:#ef4444}
"""

_JS = r"""
/* ── 상태 ── */
let selected = new Set(ACCOUNTS.filter(a => a.checked).map(a => a.num));
let dirty = false;        // 저장 안 된 변경이 있는가
let cntDelta = {};        // 재배치 직후 계정별 건수 변화 {num: 증감}
let CY, CM;
const ALL_HOURS = [6, 8, 10, 12, 14, 16, 18, 20]; // config.py AVAILABLE_HOURS와 동일
let focusedAcct = null;   // 포커스(반전)된 계정 번호
let checkedSlots = new Set(); // 체크된 슬롯 키 "날짜:시간:코트"
let dispatchDays = new Set();      // 배치 모드 선택 날짜 "YYYY-MM-DD" — 재배치(config.toml)에만 사용
let searchDays = new Set();        // 검색 모드 선택 날짜 "YYYY-MM-DD" — 빈자리 검색에만 사용
let selMode = 'dispatch';          // 날짜 체크박스가 편집하는 대상: 'dispatch' | 'search'
let initializedMonths = new Set(); // 기본 선택(토·일·공휴일)을 마친 월 "YYYY-MM"
let availSlots = new Set();        // 검색 결과 빈자리 "YYYY-MM-DD:시간:코트"
let closedDates = new Set();       // 휴장일 추정 날짜 "YYYY-MM-DD"
let searchedDates = new Set();     // 검색을 수행한 날짜 "YYYY-MM-DD"
let searching = false;             // 검색 진행 중 플래그

/* ── 초기화 ── */
(function init() {
  const dates = ACCOUNTS.flatMap(a => a.reservations.map(r => r.date)).sort();
  if (dates.length) {
    const p = dates[0].split('-');
    CY = +p[0]; CM = +p[1];
  } else {
    const n = new Date(); CY = n.getFullYear(); CM = n.getMonth() + 1;
  }
  buildSidebar();
  updateModeButtons();
  buildCalendar();
  markDirty(false);
})();

/* ── 사이드바 ── */
function buildSidebar() {
  const sb = document.getElementById('sb');
  sb.innerHTML = '<div class="sidebar-label">계정 목록</div>' +
    ACCOUNTS.map(a => {
      const on = selected.has(a.num);
      const fc = a.num === focusedAcct ? ' fc' : '';
      const fl = a.num in cntDelta ? ' flash' : '';
      return `
<div class="acct-card ${on?'on':'off'}${fc}${fl}" style="--c:${a.color}" id="ac${a.num}"
     onclick="if(!event.target.closest('input,button'))focusAcct(${a.num})">
  <div class="acct-r1">
    <input type="checkbox" class="acct-cb" ${on?'checked':''} onchange="toggleAcct(${a.num})" style="accent-color:${a.color}">
    <span class="acct-dot"></span>
    <span class="acct-num">${a.num}</span>
    <span class="acct-id" title="${a.user_id}">${a.user_id}</span>
    ${a.name ? `<span class="acct-name" title="${a.name}">${a.name}</span>` : ''}
  </div>
  <div class="acct-r2">
    <input type="password" id="pw${a.num}" value="${a.user_pw}" class="pw-box" readonly>
    <button class="pw-eye" onclick="togglePw(${a.num})" title="비밀번호 보기">👁</button>
  </div>
  <div class="acct-r3"><span${keptHint(a)}>📅 ${planCount(a)}건</span>${deltaBadge(a.num)}</div>
</div>`;
    }).join('');
}

/* 사이드바에 보이는 건수 = 저장했을 때 reservation.json 에 들어갈 건수.
   체크를 해제하면 그 계정은 계획에서 빠지므로 0건이다.
   달력(slotMap)·재배치 풀도 같은 기준을 쓴다. */
function planCount(a) {
  return selected.has(a.num) ? a.reservations.length : 0;
}

/* 해제해도 배정 자체는 메모리에 남아 있다 — 다시 체크하면 돌아온다 */
function keptHint(a) {
  if (selected.has(a.num) || !a.reservations.length) return '';
  return ` title="체크하면 배정 ${a.reservations.length}건이 돌아옵니다"`;
}

/* 재배치 결과 배지 — 저장하면 지워진다.
   건수가 같아도 슬롯 내용이 바뀌는 경우가 흔하므로 둘을 구분해 보여준다. */
function deltaBadge(num) {
  const c = cntDelta[num];
  if (!c) return '';
  if (c.d > 0)  return `<span class="cnt-delta up"   title="재배치로 ${c.d}건 늘어남">▲${c.d}</span>`;
  if (c.d < 0)  return `<span class="cnt-delta down" title="재배치로 ${-c.d}건 줄어듦">▼${-c.d}</span>`;
  if (c.moved)  return `<span class="cnt-delta move" title="건수는 같지만 배정된 슬롯이 바뀜">↻</span>`;
  return `<span class="cnt-delta same" title="재배치했지만 배정이 그대로">=</span>`;
}

function toggleAcct(num) {
  selected.has(num) ? selected.delete(num) : selected.add(num);
  const card = document.getElementById('ac'+num);
  if (card) { card.classList.toggle('on', selected.has(num)); card.classList.toggle('off', !selected.has(num)); }
  buildCalendar();  // 선택 상태 변경 시 슬롯 맵 재계산 → 중복 판정 갱신
  markDirty();      // 체크 해제 = 계획에서 제외 → 저장 대상 변경
}

function selectAll(v) {
  ACCOUNTS.forEach(a => {
    v ? selected.add(a.num) : selected.delete(a.num);
    const card = document.getElementById('ac'+a.num);
    if (card) { card.classList.toggle('on',v); card.classList.toggle('off',!v); }
    const cb = card && card.querySelector('.acct-cb');
    if (cb) cb.checked = v;
  });
  buildCalendar();  // 선택 상태 변경 시 슬롯 맵 재계산 → 중복 판정 갱신
  markDirty();
}

function refreshDim(el) {
  const accts = JSON.parse(el.dataset.a);
  el.classList.toggle('dimmed', accts.length > 0 && !accts.some(n => selected.has(n)));
}

function togglePw(num) {
  const el = document.getElementById('pw'+num);
  if (el) el.type = el.type === 'password' ? 'text' : 'password';
}

/* ── 월 이동 ── */
function changeMonth(d) {
  CM += d;
  if (CM > 12) { CM = 1; CY++; }
  if (CM < 1)  { CM = 12; CY--; }
  buildCalendar();
}

/* ── 대상 날짜 선택 (배치/검색 모드별 독립) ── */
function modeDays() {
  return selMode === 'search' ? searchDays : dispatchDays;
}

function ensureMonthDefaults() {
  // 처음 표시하는 월은 두 모드 모두 토·일·공휴일을 기본 선택.
  // 이미 초기화한 월은 건너뛰어 사용자가 해제한 날짜가 되살아나지 않게 한다.
  const mKey = `${CY}-${String(CM).padStart(2,'0')}`;
  if (initializedMonths.has(mKey)) return;
  initializedMonths.add(mKey);
  const lastDay = new Date(CY, CM, 0).getDate();
  for (let d = 1; d <= lastDay; d++) {
    const dow = new Date(CY, CM - 1, d).getDay(); // 0=일, 6=토
    const ds = `${mKey}-${String(d).padStart(2,'0')}`;
    if (dow === 0 || dow === 6 || HOLIDAYS.has(ds)) {
      dispatchDays.add(ds);
      searchDays.add(ds);
    }
  }
}

function toggleDay(dateStr, on) {
  on ? modeDays().add(dateStr) : modeDays().delete(dateStr);
  updateCapacity();
}

function setMode(m) {
  selMode = m;
  document.getElementById('modeDispatch').classList.toggle('on', m === 'dispatch');
  document.getElementById('modeSearch').classList.toggle('on', m === 'search');
  updateModeButtons();
  buildCalendar();
}

function updateModeButtons() {
  // 활성 모드에서 사용 가능한 버튼만 enable
  // 배치 모드: 계정 전체 선택/해제, 재배치 | 검색 모드: 검색
  const dispatch = selMode === 'dispatch';
  document.getElementById('btnSelAll').disabled   = !dispatch;
  document.getElementById('btnDeselAll').disabled = !dispatch;
  document.getElementById('redistBtn').disabled   = !dispatch;
  document.getElementById('searchBtn').disabled   = dispatch || searching;
}

function quickDays(kind) {
  // 활성 모드의 현재 월 선택을 일괄 재설정: weekend | weekday | all | none
  ensureMonthDefaults();
  const days = modeDays();
  const pfx = `${CY}-${String(CM).padStart(2,'0')}`;
  const lastDay = new Date(CY, CM, 0).getDate();
  for (let d = 1; d <= lastDay; d++) {
    const ds = `${pfx}-${String(d).padStart(2,'0')}`;
    const dow = new Date(CY, CM - 1, d).getDay();
    const wk = dow === 0 || dow === 6;
    const on = kind === 'all' || (kind === 'weekend' && wk) || (kind === 'weekday' && !wk);
    on ? days.add(ds) : days.delete(ds);
  }
  buildCalendar();
}

/* ── 슬롯 맵 ── */
function slotMap() {
  const m = {};
  const pfx = `${CY}-${String(CM).padStart(2,'0')}`;
  ACCOUNTS.forEach(a => {
    // 포커스 계정은 selected 여부와 무관하게 항상 처리
    // (체크박스 해제 시 checkedSlots 편집 내용이 사라지는 현상 방지)
    if (a.num !== focusedAcct && !selected.has(a.num)) return;

    // 포커스 계정: 사용자가 편집 중인 checkedSlots 기준
    // (체크 추가 → 달력에 색상+번호 표시 / 체크 해제 → 달력에서 제거)
    // 다른 계정: 기존 reservations 기준
    const resList = (a.num === focusedAcct)
      ? [...checkedSlots]
          .filter(k => k.split(':')[0].startsWith(pfx))
          .map(k => { const [d,h,c] = k.split(':'); return {date:d, hour:+h, court:+c}; })
      : a.reservations;

    resList.forEach(r => {
      if (!r.date.startsWith(pfx)) return;
      const day = +r.date.split('-')[2];
      ((m[day] ??= {})[r.hour] ??= {})[r.court] ??= [];
      if (!m[day][r.hour][r.court].includes(a.num))
        m[day][r.hour][r.court].push(a.num);
    });
  });
  return m;
}

/* ── 달력 렌더링 ── */
function buildCalendar() {
  ensureMonthDefaults();
  document.getElementById('tip').classList.remove('show');
  document.getElementById('mtitle').textContent = `${CY}년 ${CM}월`;
  const sm = slotMap();
  const today = new Date();
  const todayD = (today.getFullYear()===CY && today.getMonth()+1===CM) ? today.getDate() : -1;

  const firstDow = (new Date(CY, CM-1, 1).getDay() + 6) % 7; // 월=0
  const lastDay  = new Date(CY, CM, 0).getDate();

  const DOW = ['월','화','수','목','금','토','일'];
  let h = `<div class="cal-grid${selMode === 'search' ? ' mode-search' : ''}">`;
  DOW.forEach((d,i) => h += `<div class="dow-hd ${i===5?'sat':i===6?'sun':''}">${d}</div>`);

  // 앞 빈 셀
  for (let i = 0; i < firstDow; i++) h += '<div class="day-cell blank"></div>';

  for (let day = 1; day <= lastDay; day++) {
    const dow = (firstDow + day - 1) % 7;
    const sat = dow===5, sun = dow===6;
    const cells = sm[day];
    const colCss = `18px repeat(4,1fr)`;
    const pad = String(day).padStart(2,'0');
    const dateStr = `${CY}-${String(CM).padStart(2,'0')}-${pad}`;

    const hasRes = !!cells;
    h += `<div class="day-cell${sat?' is-sat':sun?' is-sun':''}${day===todayD?' is-today':''}">`;
    const cbTitle = selMode === 'search' ? '검색 대상 포함' : '배치 대상 포함';
    const closedBadge = closedDates.has(dateStr) ? '<span class="closed-badge">휴장</span>' : '';
    // 그 날 배정된 건수 (중복 슬롯은 계정 수만큼 센다 — 실제 예약 시도 횟수)
    const nRes = cells ? Object.values(cells).reduce(
      (s, byCt) => s + Object.values(byCt).reduce((t, a) => t + a.length, 0), 0) : 0;
    const cntPill = nRes ? `<span class="cnt-pill" title="배정 ${nRes}건">${nRes}</span>` : '';
    h += `<div class="day-num${sat?' sat-n':sun?' sun-n':''}"><span class="day-num-l"><input type="checkbox" class="day-cb" ${modeDays().has(dateStr)?'checked':''} onchange="toggleDay('${dateStr}', this.checked)" title="${cbTitle}">${day}${closedBadge}</span><span class="day-num-r">${cntPill}<span class="dow-tag">${DOW[dow]}</span></span></div>`;

    // 예약 유무와 관계없이 모든 날짜에 미니 그리드 표시. 밝기 3단계로
    // "배정 있는 날 / 검색만 한 날 / 아무것도 없는 날" 을 한눈에 가른다.
    const miniCls = hasRes ? '' : (searchedDates.has(dateStr) ? ' searched-only' : ' no-res');
    h += `<div class="mini${miniCls}" style="grid-template-columns:${colCss}">`;
    h += '<div></div>'; // 시간 레이블 자리
    [1,2,3,4].forEach(c => h += `<div class="ct-hd">C${c}</div>`);
    ALL_HOURS.forEach(hr => {
      h += `<div class="t-label">${String(hr).padStart(2,'0')}</div>`;
      [1,2,3,4].forEach(ct => {
        const accts = cells?.[hr]?.[ct] || [];  // cells 없어도 안전
        h += makeSlot(accts, dateStr, hr, ct);
      });
    });
    h += '</div>';
    h += '</div>';
  }

  // 뒷 빈 셀
  const used = firstDow + lastDay;
  const rem = used % 7;
  if (rem) for (let i = 0; i < 7-rem; i++) h += '<div class="day-cell blank"></div>';

  h += '</div>';
  document.getElementById('cal').innerHTML = h;
  bindTips();
  // 체크된 슬롯 클래스 복원 (달력 재렌더링 후)
  checkedSlots.forEach(key => {
    const [d, h2, c] = key.split(':');
    document.querySelectorAll(`.slot[data-d="${d}"][data-h="${h2}"][data-c="${c}"]`)
      .forEach(el => el.classList.add('ckd'));
  });
  refreshFocus();
  updateCapacity();
}

/* ── 포커스(반전) ── */
function focusAcct(num) {
  flushFocus();   // 다른 계정으로 옮기기 전에 편집 내용을 메모리에 반영
  if (focusedAcct === num) {
    // 예약 변경 모드 OFF — ACCOUNTS(메모리) 기준으로 표시
    focusedAcct = null;
    checkedSlots = new Set();
    ACCOUNTS.forEach(a => {
      const card = document.getElementById('ac'+a.num);
      if (card) card.classList.remove('fc');
    });
    buildCalendar();
    return;
  }

  // 예약 변경 모드 ON — ACCOUNTS(메모리) 기준으로 예약 로드
  focusedAcct = num;
  const acct = ACCOUNTS.find(a => a.num === num);
  checkedSlots = new Set(
    (acct?.reservations || []).map(r => `${r.date}:${r.hour}:${r.court}`)
  );
  ACCOUNTS.forEach(a => {
    const card = document.getElementById('ac'+a.num);
    if (card) card.classList.toggle('fc', focusedAcct === a.num);
  });
  buildCalendar();
}

function refreshFocus() {
  document.querySelectorAll('.slot[data-a]').forEach(el => {
    const accts = JSON.parse(el.dataset.a);
    el.classList.remove('hi', 'dfm');
    if (focusedAcct === null) return;
    if (accts.includes(focusedAcct)) el.classList.add('hi');
    else if (accts.length > 0)       el.classList.add('dfm');
  });
}

/* ── 슬롯 체크 + 실시간 저장 ── */
function clickSlot(el, dateStr, hr, ct) {
  if (!focusedAcct) return;
  const key = `${dateStr}:${hr}:${ct}`;
  checkedSlots.has(key) ? checkedSlots.delete(key) : checkedSlots.add(key);
  flushFocus();          // 편집 내용을 메모리(ACCOUNTS)에 즉시 반영
  buildCalendar();
  buildSidebar();
  markDirty();           // 파일 저장은 [저장] 버튼에서만 한다
}

/* ── 편집 버퍼 → 메모리 ──
   checkedSlots 는 "포커스 계정의 편집 중 상태"다. 파일에 바로 쓰지 않으므로
   ACCOUNTS 에 되돌려 놓아야 다른 계정으로 옮겼다 와도 내용이 남는다. */
function flushFocus() {
  if (!focusedAcct) return;
  const a = ACCOUNTS.find(x => x.num === focusedAcct);
  if (!a) return;
  a.reservations = [...checkedSlots].map(k => {
    const [date, hour, court] = k.split(':');
    return { date, hour: +hour, court: +court };
  }).sort((x, y) => x.date.localeCompare(y.date) || x.hour - y.hour || x.court - y.court);
}

function markDirty(on = true) {
  dirty = on;
  const btn = document.getElementById('saveBtn');
  if (!btn) return;
  const n = ACCOUNTS.filter(a => selected.has(a.num) && a.reservations.length).length;
  btn.classList.toggle('dirty', dirty);
  btn.textContent = dirty ? `💾 저장 (미저장)` : `💾 저장됨 · ${n}명`;
}

async function saveSchedule() {
  flushFocus();
  const entries = ACCOUNTS
    .filter(a => selected.has(a.num) && a.reservations.length)
    .map(a => ({
      id: a.user_id,
      name: a.name,
      slots: a.reservations.map(r => `${r.date}:${r.hour}:${r.court}`),
    }));
  if (!entries.length && !confirm('배정된 계정이 없습니다. 빈 계획으로 저장할까요?')) return;

  try {
    const resp = await fetch('/api/save-schedule', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entries }),
    });
    const { ok, detail } = await resp.json();
    if (ok) {
      markDirty(false);
      cntDelta = {};        // 저장했으면 변화량 배지를 지운다
      buildSidebar();
      showToast(`✓ ${detail.accounts}명 / ${detail.slots}건 저장됨`);
      (detail.warnings || []).forEach(w => showToast('⚠ ' + w, true));
    } else {
      showToast('✗ 저장 실패: ' + detail, true);
    }
  } catch (e) {
    showToast('✗ 연결 오류', true);
  }
}

window.addEventListener('beforeunload', e => {
  if (dirty) { e.preventDefault(); e.returnValue = ''; }
});

/* ── 로그인 시작 시점(분) 저장 ── */
async function saveLoginAdvance(el) {
  let v = parseInt(el.value, 10);
  if (isNaN(v) || v < 1)   v = 1;
  if (v > 120)             v = 120;
  el.value = v;  // 정규화된 값으로 표시 복원
  try {
    const resp = await fetch('/api/save-login-advance', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ minutes: v }),
    });
    const { ok, detail } = await resp.json();
    showToast(ok ? `✓ 오픈 ${detail}분 전 로그인으로 저장됨` : `✗ 저장 실패: ${detail}`, !ok);
  } catch (e) {
    showToast('✗ 연결 오류', true);
  }
}

/* ── 계정당 배정 슬롯 수 저장 ── */
async function saveSlotsPerAccount(el) {
  let v = parseInt(el.value, 10);
  if (isNaN(v) || v < 1)   v = 1;
  if (v > 10)              v = 10;
  el.value = v;  // 정규화된 값으로 표시 복원
  updateCapacity();
  try {
    const resp = await fetch('/api/save-slots-per-account', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ count: v }),
    });
    const { ok, detail } = await resp.json();
    showToast(ok ? `✓ 계정당 ${detail}개 배정으로 저장됨` : `✗ 저장 실패: ${detail}`, !ok);
  } catch (e) {
    showToast('✗ 연결 오류', true);
  }
}

function showToast(msg, isError = false) {
  const el = document.getElementById('toast');
  if (!el) return;
  el.textContent = msg;
  el.className = 'show' + (isError ? ' err' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = ''; }, 2000);
}

function makeSlot(accts, dateStr, hr, ct) {
  const ad = JSON.stringify(accts).replace(/'/g, '&#39;');
  const timeStr = `${String(hr).padStart(2,'0')}:00`;
  const oc = `onclick="clickSlot(this,'${dateStr}',${hr},${ct})"`;

  // 검색 결과(= 서버의 실제 상태)는 배정 유무와 무관하게 판정한다.
  // 배정된 슬롯도 이미 마감일 수 있으므로 계정색을 덮지 않고 위에 덧입힌다.
  const key = `${dateStr}:${hr}:${ct}`;
  const chk = availSlots.has(key) ? 'chk-ok'
            : (searchedDates.has(dateStr) && !closedDates.has(dateStr)) ? 'chk-no' : '';
  const chkTip = chk === 'chk-ok' ? '\n검색: 빈자리 ✓'
               : chk === 'chk-no' ? '\n⚠ 검색: 마감 — 예약 실패함' : '';

  if (!accts.length) {
    // 검색 결과 오버레이: 빈자리 ○(초록) / 검색했지만 빈자리 아님 ×(마감)
    if (chk === 'chk-ok') {
      const tip = encodeURIComponent(`빈자리 (검색)\n${dateStr} ${timeStr}\n코트 ${ct}`);
      return `<div class="slot empty avail" data-a="[]" data-d="${dateStr}" data-h="${hr}" data-c="${ct}" data-tip="${tip}" ${oc}>○</div>`;
    }
    if (chk === 'chk-no') {
      return `<div class="slot empty taken" data-a="[]" data-d="${dateStr}" data-h="${hr}" data-c="${ct}" ${oc}>×</div>`;
    }
    return `<div class="slot empty" data-a="[]" data-d="${dateStr}" data-h="${hr}" data-c="${ct}" ${oc}>□</div>`;
  }
  if (accts.length === 1) {
    const a = ACCOUNTS.find(x => x.num === accts[0]);
    const tip = encodeURIComponent(`${a.user_id}${a.name ? ' (' + a.name + ')' : ''}\n${dateStr} ${timeStr}\n코트 ${ct}${chkTip}`);
    return `<div class="slot booked ${chk}" style="background:${a.color}" data-a='${ad}' data-d="${dateStr}" data-h="${hr}" data-c="${ct}" data-tip="${tip}" ${oc}>${a.num}</div>`;
  }
  // 중복
  const lines = accts.map(n => { const a = ACCOUNTS.find(x=>x.num===n); return `${a.num}: ${a.user_id}${a.name ? ' (' + a.name + ')' : ''}`; });
  const tip = encodeURIComponent(`⚠ 중복 ${accts.length}건\n${lines.join('\n')}\n${dateStr} ${timeStr} 코트${ct}${chkTip}`);
  const [n1, n2] = accts;
  return `<div class="slot dup ${chk}" data-a='${ad}' data-d="${dateStr}" data-h="${hr}" data-c="${ct}" data-tip="${tip}" ${oc}><span>${n1}</span><span>⚠${n2}</span></div>`;
}

/* ── 유틸: Fisher-Yates 셔플 ── */
function shuffle(arr) {
  for (let i = arr.length - 1; i > 0; i--) {
    const j = Math.floor(Math.random() * (i + 1));
    [arr[i], arr[j]] = [arr[j], arr[i]];
  }
  return arr;
}

/* ── 슬롯 풀 & 배정 (재배치·상단 가능/필요 카운터 공용) ── */
function buildPool(targetDays, pfx) {
  // 우선순위별 섹션으로 구분
  //   8시: 토(dow=6) 3코트(1→2→3) / 그 외(일·평일) 4코트(1→2→3→4)
  //   6시: 3코트(1→2→3) / 10시: 1코트(1번)
  // 미선택 계정은 저장 시 계획에서 빠지므로(0건) 슬롯을 점유하지 않는다.
  // 예전에는 이들의 예약을 풀에서 제외했는데, 달력(slotMap)에는 이미 안 보이는
  // 슬롯을 풀에서만 빼는 셈이라 모순이었고 쓸 수 있는 슬롯이 낭비됐다.
  const sec8 = [], sec6 = [], sec10 = [];
  const push = (arr, ds, hour, court) => arr.push({ date: ds, hour, court });
  targetDays.forEach(day => {
    const ds    = `${pfx}-${String(day).padStart(2,'0')}`;
    const isSat = new Date(CY, CM - 1, day).getDay() === 6;
    (isSat ? [1,2,3] : [1,2,3,4]).forEach(c => push(sec8, ds, 8, c));
    [1,2,3].forEach(c => push(sec6, ds, 6, c));
    push(sec10, ds, 10, 1);
  });
  return [sec8, sec6, sec10];
}

function assignPool(pool, accounts, perAcct) {
  // 하드 제약: 동일 날짜 금지 — 서버가 계정당 1일 1건만 허용하므로
  // 같은 계정에 같은 날짜가 배정되면 정각에 한 건은 반드시 실패한다
  //
  // 라운드로빈: 한 바퀴에 계정당 1개씩, perAcct 바퀴를 돈다.
  // 계정 순서대로 perAcct 를 다 채우면(first-fit) 풀이 모자랄 때 앞 계정이
  // 풀을 비워 뒤 계정이 통째로 0건이 됐다 — 22계정×3개(풀 30)에서 앞 10명이
  // 3건씩 가져가고 12명이 0건. 순서가 고정이라 매달 같은 사람이 굶었다.
  // 바퀴로 돌면 부족분이 뒤로 몰리지 않고 계정 간 1건 차이 안에서 갈린다.
  const used = new Array(pool.length).fill(false);
  const state = accounts.map(a => ({ account_num: a.num, slots: [], dates: new Set() }));

  for (let round = 0; round < perAcct; round++) {
    for (const st of state) {
      for (let i = 0; i < pool.length; i++) {
        if (used[i]) continue;
        const s = pool[i];
        if (st.dates.has(s.date)) continue;
        st.slots.push(s);
        st.dates.add(s.date);
        used[i] = true;
        break;
      }
    }
  }

  return state.map(st => ({ account_num: st.account_num, slots: st.slots }));
}

/* ── 필요 수: 체크된 배치 날짜 수요 ── */
function checkedDemand() {
  // 토 7(6시3+8시3+10시1) / 그 외(일·평일·공휴일) 8(6시3+8시4+10시1)
  // buildPool의 날짜당 슬롯 구성과 동일한 규칙
  const pfx = `${CY}-${String(CM).padStart(2,'0')}`;
  let need = 0;
  dispatchDays.forEach(ds => {
    if (!ds.startsWith(pfx)) return;
    const day = +ds.split('-')[2];
    need += new Date(CY, CM - 1, day).getDay() === 6 ? 7 : 8;
  });
  return need;
}

/* ── 상단 가능/필요 카운터 ── */
function updateCapacity() {
  // 가능 = 선택 계정 수 × 계정당 배정 수 (용량) / 필요 = 체크된 배치 날짜 수요
  const el = document.getElementById('capText');
  if (!el) return;
  const numAccts = ACCOUNTS.filter(a => selected.has(a.num)).length;
  // 입력 중(oninput)에도 호출되므로 범위를 벗어난 임시 값은 1~10으로 클램프
  const perAcct = Math.min(10, Math.max(1,
    parseInt(document.getElementById('slotsPer').value, 10) || 4));
  const possible = numAccts * perAcct;
  const need = checkedDemand();
  el.textContent = `${possible}/${need}`;
  document.getElementById('capBox').classList.toggle('short', possible < need);
}

/* ── 재배치 ── */
async function redistribute() {
  // 1. 달력에서 체크된 재배치 대상 날짜 수집 (기본: 토·일)
  ensureMonthDefaults();
  const pfx = `${CY}-${String(CM).padStart(2,'0')}`;
  const targetDays = [...dispatchDays]
    .filter(ds => ds.startsWith(pfx))
    .map(ds => +ds.split('-')[2])
    .sort((a, b) => a - b);
  if (!targetDays.length) { showToast('선택된 날짜 없음', true); return; }
  const accts = ACCOUNTS.filter(a => selected.has(a.num));
  if (!accts.length) { showToast('선택된 계정 없음', true); return; }

  // 2. 슬롯 풀 생성 — 섹션 내부 셔플(날짜 간 순서 랜덤) → 우선순위 순으로 연결
  const [sec8, sec6, sec10] = buildPool(targetDays, pfx);
  shuffle(sec8); shuffle(sec6); shuffle(sec10);
  const pool = [...sec8, ...sec6, ...sec10];

  // 3. 계정당 N개 배정 (헤더 입력값, config.toml [schedule] slots_per_account)
  const perAcct = parseInt(document.getElementById('slotsPer').value, 10) || 4;
  const assignments = assignPool(pool, accts, perAcct);

  // 4. 결과 요약 & 확인 — 선택일 수요 vs 계정 용량(예약인 × 인당 개수) 비교
  const totalSlots = assignments.reduce((s, a) => s + a.slots.length, 0);
  const capacity   = accts.length * perAcct;
  const demand     = checkedDemand();
  let msg = `${CY}년 ${CM}월 배치 선택일 ${targetDays.length}일\n풀 ${pool.length}개 슬롯 → 선택 계정 ${accts.length}개에 ${totalSlots}개 배정\n체크 해제된 계정은 이번 계획에서 빠집니다(0건). 계속?`;
  if (capacity < demand) msg = `⚠ 용량 부족: 선택일 수요 ${demand}개 > 예약인 ${accts.length}명 × 인당 ${perAcct}개 = ${capacity}개\n` + msg;
  if (!confirm(msg)) return;

  // 5. 메모리에 반영 — 파일 저장은 [저장] 버튼에서 한 번에 한다.
  //    왼쪽 사이드바 건수가 조용히 바뀌면 알아채기 어려우므로 변화량을 함께 남긴다.
  const keyOf = a => a.reservations.map(r => `${r.date}:${r.hour}:${r.court}`).sort().join('|');
  const before = Object.fromEntries(ACCOUNTS.map(a => [a.num, {n: a.reservations.length, k: keyOf(a)}]));
  assignments.forEach(item => {
    const a = ACCOUNTS.find(x => x.num === item.account_num);
    if (a) a.reservations = item.slots.slice()
             .sort((x, y) => x.date.localeCompare(y.date) || x.hour - y.hour || x.court - y.court);
  });
  cntDelta = {};
  ACCOUNTS.forEach(a => {
    if (!selected.has(a.num)) return;
    cntDelta[a.num] = { d: a.reservations.length - before[a.num].n,
                        moved: keyOf(a) !== before[a.num].k };
  });
  const vals    = Object.values(cntDelta);
  const changed = vals.filter(c => c.d !== 0).length;
  const moved   = vals.filter(c => c.d === 0 && c.moved).length;

  focusedAcct = null;
  checkedSlots = new Set();
  buildSidebar();
  buildCalendar();
  markDirty();
  showToast(`✓ ${accts.length}명 ${totalSlots}개 재배치 · 건수변동 ${changed}명 · 슬롯교체 ${moved}명 — [저장] 을 눌러야 반영됩니다`);
}

/* ── 빈자리 검색 ── */
async function runSearch() {
  if (searching) return;
  ensureMonthDefaults();
  const pfx = `${CY}-${String(CM).padStart(2,'0')}`;
  const dates = [...searchDays].filter(ds => ds.startsWith(pfx)).sort();
  if (!dates.length) { showToast('선택된 날짜 없음', true); return; }

  searching = true;
  const btn = document.getElementById('searchBtn');
  const orig = btn.textContent;
  btn.textContent = `⏳ ${dates.length}일 검색 중…`;
  btn.disabled = true;
  try {
    const resp = await fetch('/api/search', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ dates }),
    });
    const r = await resp.json();
    if (r.ok) {
      // 같은 날짜의 이전 결과를 지우고 최신 결과로 교체 (날짜 단위 병합)
      const fresh = new Set(r.searched_dates);
      availSlots = new Set([...availSlots].filter(k => !fresh.has(k.slice(0, 10))));
      r.results.forEach(s => availSlots.add(`${s.date}:${s.hour}:${s.court}`));
      fresh.forEach(d => { searchedDates.add(d); closedDates.delete(d); });
      (r.closed_dates || []).forEach(d => closedDates.add(d));
      buildCalendar();
      const closedMsg = r.closed_dates?.length ? ` (휴장 ${r.closed_dates.length}일)` : '';
      showToast(`✓ ${r.searched_dates.length}일 검색 — 빈자리 ${r.results.length}건${closedMsg}, ${r.elapsed}초`);
    } else {
      showToast('✗ 검색 실패: ' + (r.error || ''), true);
    }
  } catch (e) {
    showToast('✗ 연결 오류', true);
  } finally {
    searching = false;
    btn.textContent = orig;
    updateModeButtons();  // 검색 중 모드를 바꿨어도 올바른 enable 상태로 복원
  }
}

/* ── 툴팁 ── */
function bindTips() {
  const tip = document.getElementById('tip');
  document.querySelectorAll('.slot[data-tip]').forEach(el => {
    el.addEventListener('mouseenter', e => {
      const lines = decodeURIComponent(el.dataset.tip).split('\n');
      tip.innerHTML = lines.map((l, i) => {
        if (i === 0) return `<div class="tip-head">${l}</div>`;
        if (l.startsWith('⚠')) return `<div><span class="tip-dup">${l}</span></div>`;
        return `<div>${l}</div>`;
      }).join('');
      tip.classList.add('show');
      move(e);
    });
    el.addEventListener('mousemove', move);
    el.addEventListener('mouseleave', () => tip.classList.remove('show'));
  });
  function move(e) {
    tip.style.left = Math.min(e.clientX+14, window.innerWidth-200) + 'px';
    tip.style.top  = Math.min(e.clientY-10, window.innerHeight-120) + 'px';
  }
}
"""


def build_html(accounts, init_year, init_month, api_port=8765, settings=None):
    data_json = json.dumps(accounts, ensure_ascii=False)
    # 월 이동은 클라이언트에서만 일어나므로 초기 연도 ±1~2년 치 공휴일을 미리 심는다
    holidays_json = json.dumps(load_holidays(range(init_year - 1, init_year + 3)))
    login_adv = (settings or {}).get("login_advance_minutes", 10)
    slots_per = (settings or {}).get("slots_per_account", 4)
    return f"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>테니스장 예약 현황 — {init_year}년 {init_month}월</title>
<style>{_CSS}</style>
</head>
<body>
<div class="app">
  <header class="app-header">
    <h1>🎾 고양시 테니스장 예약 현황</h1>
    <div class="header-btns">
      <span class="hdr-info" id="capBox" title="가능 = 선택 계정 × 계정당 배정 수 / 필요 = 체크된 배치 날짜 수요 — 토 7(6시3+8시3+10시1), 그 외(일·평일·공휴일) 8(6시3+8시4+10시1)">📊 가능/필요 <b id="capText">-</b></span>
      <span class="hdr-info">⏱ 오픈 <input id="loginAdv" type="number" min="1" max="120" value="{login_adv}" class="hdr-num" onchange="saveLoginAdvance(this)">분 전 로그인 시작</span>
      <span class="hdr-info">👤 계정당 <input id="slotsPer" type="number" min="1" max="10" value="{slots_per}" class="hdr-num" oninput="updateCapacity()" onchange="saveSlotsPerAccount(this)">개 배정</span>
      <div class="mode-seg">
        <button id="modeDispatch" class="seg on" onclick="setMode('dispatch')">🔀 배치 모드</button>
        <button id="modeSearch" class="seg" onclick="setMode('search')">🔍 검색 모드</button>
      </div>
      <button id="btnSelAll" onclick="selectAll(true)">계정 전체 선택</button>
      <button id="btnDeselAll" onclick="selectAll(false)">계정 전체 해제</button>
      <button id="searchBtn" onclick="runSearch()" style="background:rgba(22,163,74,.35);border-color:rgba(22,163,74,.7)">🔍 검색</button>
      <button id="redistBtn" onclick="redistribute()" style="background:rgba(99,102,241,.35);border-color:rgba(99,102,241,.7)">🔀 재배치</button>
      <button id="saveBtn" onclick="saveSchedule()" title="reservation.json 에 저장">💾 저장</button>
    </div>
  </header>
  <div class="layout">
    <aside class="sidebar" id="sb"></aside>
    <main class="cal-area">
      <div class="month-nav">
        <button onclick="changeMonth(-1)">◀</button>
        <span class="month-title" id="mtitle"></span>
        <button onclick="changeMonth(1)">▶</button>
        <div class="quick-days">
          <button onclick="quickDays('weekend')">주말</button>
          <button onclick="quickDays('weekday')">평일</button>
          <button onclick="quickDays('all')">전체</button>
          <button onclick="quickDays('none')">해제</button>
        </div>
      </div>
      <div id="cal"></div>
      <div class="legend">
        <span class="leg-item"><span class="leg-box leg-empty"></span>빈 슬롯</span>
        <span class="leg-item"><span class="leg-box leg-booked"></span>단일 예약</span>
        <span class="leg-item"><span class="leg-box leg-dup"></span>⚠ 중복</span>
        <span class="leg-item"><span class="leg-box leg-avail"></span>빈자리(검색)</span>
        <span class="leg-item"><span class="leg-box leg-taken"></span>마감(검색)</span>
        <span class="leg-item"><span class="leg-box leg-chkok"></span>배정 + 빈자리</span>
        <span class="leg-item"><span class="leg-box leg-chkno"></span>배정 + 마감</span>
        <span class="leg-item"><span class="leg-box leg-ckd"></span>선택(편집 중)</span>
        <span class="leg-item" style="color:#94a3b8">ID 클릭 → 반전 &nbsp;|&nbsp; 슬롯 클릭 → 체크 → 💾 저장</span>
      </div>
    </main>
  </div>
</div>
<div id="toast"></div>
<div id="tip"></div>
<script>
const ACCOUNTS = {data_json};
const HOLIDAYS = new Set({holidays_json});
{_JS}
</script>
</body>
</html>"""


# ─── 진입점 ───────────────────────────────────────────────────────────────────

def main():
    backup_config()
    accounts = load_data()

    if not accounts:
        print("[ERROR] accounts.txt 에 계정이 없습니다.")
        print("  → 한 줄에 하나씩 적으세요:  이름,아이디,비밀번호")
        sys.exit(1)

    if len(sys.argv) == 3:
        try:
            init_year, init_month = int(sys.argv[1]), int(sys.argv[2])
        except ValueError:
            print("사용법: python3 viewer.py [year] [month]")
            sys.exit(1)
    else:
        init_year, init_month = get_initial_month(accounts)

    global _HTML_CONTENT, _BUILD_PARAMS
    settings = load_settings()
    _, api_port = start_api_server()
    # do_GET이 매 요청마다 최신 config.toml로 재빌드할 때 재사용하는 파라미터
    _BUILD_PARAMS = {
        "init_year": init_year,
        "init_month": init_month,
        "api_port": api_port,
    }
    html = build_html(accounts, init_year, init_month, api_port, settings)

    # HTTP 서버에서 same-origin으로 서빙 → fetch CORS 차단 없음
    # (재빌드 실패 시 fallback으로도 사용)
    _HTML_CONTENT = html

    # fallback: file로도 저장 (디버깅용)
    # "/tmp" 하드코딩은 Windows에서 현재 드라이브 루트로 해석돼 FileNotFoundError 가 난다.
    # 서버는 이미 떠 있으므로 이 파일 저장 실패로 뷰어가 죽지는 않게 한다.
    fallback_path = Path(tempfile.gettempdir()) / "tennis_viewer.html"
    try:
        fallback_path.write_text(html, encoding="utf-8")
    except OSError as e:
        print(f"[viewer] fallback HTML 저장 건너뜀: {e}")

    total_res = sum(len(a["reservations"]) for a in accounts)
    print(f"[viewer] 계정 {len(accounts)}개 / 예약 총 {total_res}건")
    print(f"[viewer] 초기 표시: {init_year}년 {init_month}월")
    print(f"[viewer] 주소: http://127.0.0.1:{api_port}")
    print(f"[viewer] 브라우저 실행 중... (종료: Ctrl+C)")
    webbrowser.open(f"http://127.0.0.1:{api_port}/")

    try:
        threading.Event().wait()   # 브라우저가 열린 채로 서버 유지
    except KeyboardInterrupt:
        print("\n[viewer] 종료합니다.")


if __name__ == "__main__":
    main()
