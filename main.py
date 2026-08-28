#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
진입점 — 다중 계정 런처 (tmux / iTerm2 네이티브 split / Linux 터미널)

accounts.txt 에 계정을 적고 viewer.py 에서 예약을 배정해 저장하면,
reservation.json 에 실린 계정을 [launcher] group_size(기본 4)개씩 묶어
그룹마다 새 터미널 창을 열고, 각 pane에서 `reserve.py --account N` 을 실행한다.
계정 1개만 돌리려면 reserve.py 를 직접 실행한다.

플랫폼별 동작:
  macOS  (기본)   : iTerm2 네이티브 split pane / Terminal.app 탭
  macOS  + --tmux : tmux 세션 → iTerm2/Terminal.app 창
  Linux  (기본)   : 계정마다 개별 터미널 창 (분할 없음)
  Linux  + --tmux : tmux 세션 → 감지된 터미널 에뮬레이터 창
  --background    : 터미널 창 없이 subprocess + 로그 파일

사용법:
    python3 main.py                  # tmux 세션 + 새 터미널 창 (기본)
    python3 main.py --tmux           # tmux 세션으로 실행
    python3 main.py --background     # 백그라운드 subprocess + 로그 파일
    python3 main.py --only 1-10      # 계정 부분 선택 (다중 PC 분산)
    python3 main.py --account 3      # 계정 3만 이 터미널에서 (reserve.py 위임)
    python3 main.py --search 9       # 9월 빈자리 검색 1회 (reserve.py 위임)
    python3 main.py --test           # 테스트 모드 (즉시 실행, 대관신청 전 중단)
    python3 main.py --test 300       # 오픈을 300초 후로 강제 (전 계정 공통)
    python3 main.py --logincheck     # 로그인 테스트만
    python3 main.py --dry-run        # 실행 내용 출력만 (창 미생성)
"""

import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import config
import schedule
from utils import add_worker_args, parse_open_time

# ─── 상수 ─────────────────────────────────────────────────────────────────────

TMUX_SESSION_PREFIX = "tennis"
SCRIPT_DIR = Path(__file__).parent.resolve()
RESERVE_PY = SCRIPT_DIR / "reserve.py"   # 각 pane에서 실행할 단일 계정 진입점
LOGS_DIR = SCRIPT_DIR / "logs"   # 백그라운드 실행 로그 (reservation_async 타이밍 로그와 동일 폴더)
TMP_DIR = Path("/tmp")

IS_MACOS = sys.platform == "darwin"

# Linux 터미널 에뮬레이터 감지 우선순위.
# 각 항목: (실행파일명, 명령 빌더 키)
LINUX_TERMINALS = [
    "gnome-terminal",
    "xterm",
    "konsole",
    "xfce4-terminal",
    "alacritty",
    "kitty",
    "terminator",
    "tilix",
    "lxterminal",
    "mate-terminal",
    "rxvt",
]

# iTerm2 새 창은 non-login shell로 실행되어 기본 PATH 가
# /usr/bin:/bin:/usr/sbin:/sbin 뿐이므로 Homebrew tmux 를 찾지 못한다.
# 현재 환경에서 경로를 확정해 스크립트에 하드코딩한다.
TMUX_BIN = shutil.which("tmux") or "tmux"


# ─── 계정 파싱 ────────────────────────────────────────────────────────────────

def parse_account_range(spec):
    """계정 선택 문자열을 번호 집합으로 변환한다.

    "1-10" → {1..10}, "1,3,5" → {1,3,5}, "1-5,8" → {1,2,3,4,5,8}
    """
    nums = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            lo, hi = part.split("-", 1)
            lo, hi = int(lo), int(hi)
            if lo > hi:
                raise ValueError(f"범위 역순: {part}")
            nums.update(range(lo, hi + 1))
        else:
            nums.add(int(part))
    if not nums:
        raise ValueError(f"빈 선택: {spec!r}")
    return nums


def load_targets(logincheck=False):
    """실행 대상 목록을 반환한다.

    - 평소: reservation.json 의 entries (배정된 계정만). no 는 그 파일의 일련번호.
    - --logincheck: 예약이 필요 없으므로 accounts.txt 전 계정.

    slots 는 그 계정이 잡을 예약 건수다. 브라우저 모드에서 이 수만큼 Chrome 이
    뜨므로 인스턴스 수를 미리 계산하는 데 쓴다 (--logincheck 는 계정당 1개).

    Returns: ([{"no", "name", "user_id", "slots"}, ...], generated_at, warnings)
    """
    if logincheck:
        return ([{"no": a["num"], "name": a["name"], "user_id": a["user_id"], "slots": 1}
                 for a in config.load_accounts()], "", [])
    plan = schedule.load()
    return ([{"no": e["no"], "name": e["name"], "user_id": e["id"],
              "slots": len(e["slots"])}
             for e in plan["entries"]], plan["generated_at"], plan["warnings"])


def chunk_accounts(accounts, size):
    """계정 목록을 size개씩 그룹으로 나눈다."""
    return [accounts[i:i + size] for i in range(0, len(accounts), size)]


# ─── 터미널 앱 감지 ──────────────────────────────────────────────────────────

def detect_terminal_app():
    """사용 가능한 터미널 앱을 감지한다.

    macOS:
      실행 중인 iTerm2 → 설치된 iTerm2 → 'terminal' (Terminal.app)
      Returns: 'iterm2' | 'terminal'

    Linux:
      LINUX_TERMINALS 목록을 순서대로 탐색해 첫 번째 발견된 것을 반환.
      Returns: 실행파일명 문자열 (예: 'gnome-terminal') | None
    """
    if IS_MACOS:
        try:
            result = subprocess.run(
                ["osascript", "-e",
                 'tell application "System Events" to return '
                 '(name of every process) contains "iTerm2"'],
                capture_output=True, text=True, timeout=3,
            )
            if result.stdout.strip() == "true":
                return "iterm2"
        except Exception:
            pass
        if Path("/Applications/iTerm.app").exists():
            return "iterm2"
        return "terminal"
    else:
        for term in LINUX_TERMINALS:
            if shutil.which(term):
                return term
        return None


def build_linux_open_cmd(terminal, script_path):
    """Linux 터미널 에뮬레이터별 새 창 + 스크립트 실행 명령을 반환한다.

    각 에뮬레이터마다 플래그가 다르므로 이 함수에서 중앙 관리한다.
    """
    path = str(script_path)

    # gnome-terminal 3.x 이후 '-e' 는 deprecated → '--' 로 분리
    if terminal == "gnome-terminal":
        return ["gnome-terminal", "--", "bash", path]

    # xterm / rxvt: -e 뒤에 명령
    if terminal in ("xterm", "rxvt"):
        return [terminal, "-e", "bash", path]

    # konsole: -e 뒤에 명령
    if terminal == "konsole":
        return ["konsole", "-e", "bash", path]

    # xfce4-terminal: -e "명령" (문자열 통째로)
    if terminal == "xfce4-terminal":
        return ["xfce4-terminal", "-e", f"bash {path}"]

    # alacritty: -e 뒤에 명령 (3.0+ 에서 -e → -- 로 변경됐지만 호환 유지)
    if terminal == "alacritty":
        return ["alacritty", "-e", "bash", path]

    # kitty: 직접 명령 전달
    if terminal == "kitty":
        return ["kitty", "bash", path]

    # terminator / tilix / lxterminal / mate-terminal: -e "명령 문자열"
    return [terminal, "-e", f"bash {path}"]


# ─── 계정 스크립트 생성 (기본 / tmux 공용) ──────────────────────────────────

def create_acct_scripts(group, extra_flags, dry_run=False):
    """계정별 /tmp/tennis_<id>.sh 를 생성하고 경로 목록을 반환한다.

    python 절대 경로를 하드코딩하므로 제한된 PATH 환경에서도 동작한다.

    dry_run 이면 **파일을 쓰지 않는다.** 실행하지 않을 스크립트가 /tmp 에 남으면
    나중에 손으로 실행했을 때 진짜 예약이 돌아간다.
    """
    py = sys.executable
    flags_str = " ".join(extra_flags)
    acct_paths = []
    for acct in group:
        path = TMP_DIR / f"tennis_{acct['user_id']}.sh"
        body = (
            "#!/bin/bash\n"
            f"{py} {RESERVE_PY} --account {acct['user_id']} {flags_str}\n"
            "echo ''\n"
            f"echo '[{acct['user_id']}] 완료."
            " 엔터를 누르면 닫힙니다.'\n"
            "read\n"
        )
        if not dry_run:
            path.write_text(body)
            path.chmod(0o755)
        acct_paths.append(path)
    return acct_paths


# ─── tmux 모드: 그룹 스크립트 생성 ──────────────────────────────────────────

def create_group_scripts(group, session_name, extra_flags, group_idx, dry_run=False):
    """tmux 세션 생성 + pane 분할 + attach 를 담은 그룹 스크립트를 /tmp에 생성.

    Returns: (Path, str) — 경로와 내용. dry_run 이면 파일을 쓰지 않는다.
    """
    acct_paths = create_acct_scripts(group, extra_flags, dry_run=dry_run)
    n = len(group)
    user_ids = ", ".join(a["user_id"] for a in group)

    lines = [
        "#!/bin/bash",
        f'SESSION="{session_name}"',
        f'TMUX="{TMUX_BIN}"',
        "",
        "# 기존 세션이 있으면 제거",
        '"$TMUX" kill-session -t "$SESSION" 2>/dev/null || true',
        "",
        f"# 그룹 {group_idx}: {user_ids}",
        f'"$TMUX" new-session -d -s "$SESSION" "{acct_paths[0]}"',
    ]

    if n >= 2:
        lines.append(
            f'"$TMUX" split-window -t "$SESSION:0.0" -h "{acct_paths[1]}"'
        )
    if n >= 3:
        lines.append(
            f'"$TMUX" split-window -t "$SESSION:0.0" -v "{acct_paths[2]}"'
        )
    if n >= 4:
        lines.append(
            f'"$TMUX" split-window -t "$SESSION:0.1" -v "{acct_paths[3]}"'
        )

    if n > 1:
        lines.append('"$TMUX" select-layout -t "$SESSION:0" tiled')

    lines += [
        '"$TMUX" select-pane -t "$SESSION:0.0"',
        "",
        "# bash 프로세스를 tmux attach 로 교체 → 터미널 창이 tmux 세션을 직접 표시",
        'exec "$TMUX" attach-session -t "$SESSION"',
    ]

    group_path = TMP_DIR / f"tennis_group_{group_idx}.sh"
    content = "\n".join(lines) + "\n"
    if not dry_run:
        group_path.write_text(content)
        group_path.chmod(0o755)
    return group_path, content


# ─── macOS 기본 모드: AppleScript 생성 ───────────────────────────────────────

def build_iterm2_split_applescript(acct_paths):
    """iTerm2 네이티브 split pane AppleScript를 생성한다 (macOS 전용).

    레이아웃:
      1개: [s1          ]
      2개: [s1    | s2  ]
      3개: [s1    | s2  ] / [s3          ]
      4개: [s1    | s2  ] / [s3    | s4  ]

    split 방향:
      vertically   = 수직 divider → s1 오른쪽에 s2 생성
      horizontally = 수평 divider → s1/s2 아래에 s3/s4 생성
    """
    n = len(acct_paths)
    p = [str(path) for path in acct_paths]

    lines = [
        'tell application "iTerm2"',
        '    activate',
        '    set w to (create window with default profile)',
        '    set s1 to current session of w',
        f'    tell s1 to write text "{p[0]}"',
    ]

    if n >= 2:
        lines += [
            '    -- s2: s1 오른쪽 (수직 divider)',
            '    tell s1',
            '        set s2 to (split vertically with default profile)',
            '    end tell',
            f'    tell s2 to write text "{p[1]}"',
        ]

    if n >= 3:
        lines += [
            '    -- s3: s1 아래 (수평 divider)',
            '    tell s1',
            '        set s3 to (split horizontally with default profile)',
            '    end tell',
            f'    tell s3 to write text "{p[2]}"',
        ]

    if n == 4:
        lines += [
            '    -- s4: s2 아래 (수평 divider)',
            '    tell s2',
            '        set s4 to (split horizontally with default profile)',
            '    end tell',
            f'    tell s4 to write text "{p[3]}"',
        ]

    lines += [
        '    tell s1 to select',
        'end tell',
    ]
    return "\n".join(lines)


def build_terminal_tabs_applescript(acct_paths):
    """Terminal.app에서 같은 창에 탭을 추가하는 AppleScript를 생성한다 (macOS 전용)."""
    lines = [
        'tell application "Terminal"',
        '    activate',
        f'    do script "bash {acct_paths[0]}"',
    ]
    for path in acct_paths[1:]:
        lines += [
            '    delay 0.3',
            f'    do script "bash {path}" in front window',
        ]
    lines.append('end tell')
    return "\n".join(lines)


def run_osascript(applescript, dry_run=False, label="osascript"):
    """macOS: AppleScript 실행 또는 dry-run 출력."""
    if dry_run:
        print(f"  [{label}]")
        for line in applescript.splitlines():
            print(f"       {line}")
        print()
        return
    subprocess.run(["osascript", "-e", applescript], check=True)


# ─── 터미널 창 열기 (플랫폼 공용) ────────────────────────────────────────────

def tmux_available():
    try:
        subprocess.run([TMUX_BIN, "-V"], capture_output=True, check=True)
        return True
    except (FileNotFoundError, subprocess.CalledProcessError):
        return False


def open_new_terminal(script_path, terminal_app, dry_run=False):
    """새 터미널 창에서 스크립트를 실행한다 (tmux 그룹 스크립트용).

    macOS : AppleScript (iTerm2 / Terminal.app)
    Linux : 감지된 터미널 에뮬레이터로 subprocess.Popen
    """
    if IS_MACOS:
        path_str = str(script_path)
        if terminal_app == "iterm2":
            applescript = (
                'tell application "iTerm2"\n'
                f'    create window with default profile command "bash {path_str}"\n'
                'end tell'
            )
        else:
            applescript = (
                'tell application "Terminal"\n'
                f'    do script "bash {path_str}"\n'
                '    activate\n'
                'end tell'
            )
        run_osascript(applescript, dry_run, label="osascript (tmux)")

    else:
        # Linux
        if not terminal_app:
            print(f"  [WARN] 터미널 에뮬레이터 없음 — 백그라운드로 실행: {script_path.name}")
            if not dry_run:
                LOGS_DIR.mkdir(exist_ok=True)
                log = LOGS_DIR / f"{script_path.stem}.log"
                with open(log, "w") as lf:
                    subprocess.Popen(["bash", str(script_path)],
                                     stdout=lf, stderr=subprocess.STDOUT)
            return

        cmd = build_linux_open_cmd(terminal_app, script_path)
        if dry_run:
            print(f"  [Linux {terminal_app}] {' '.join(cmd)}")
        else:
            subprocess.Popen(cmd)


# ─── 실행 모드 1: tmux ───────────────────────────────────────────────────────

def run_multi_terminal(accounts, extra_flags, group_size, dry_run=False):
    """tmux 세션 + 새 터미널 창으로 계정을 분할 실행한다.

    macOS / Linux 모두 동작한다.
    """
    groups = chunk_accounts(accounts, group_size)
    terminal_app = detect_terminal_app()
    total = len(groups)

    print(f"[launch] {len(accounts)}개 계정 → {total}개 터미널 창 "
          f"(창당 최대 {group_size}개 pane)")
    print(f"[launch] 모드: tmux  |  터미널 앱: {terminal_app or '없음 (백그라운드 fallback)'}")
    print()

    for i, group in enumerate(groups, 1):
        session = f"{TMUX_SESSION_PREFIX}_{i}"
        user_ids = [a["user_id"] for a in group]
        print(f"  ── 그룹 {i}/{total}  세션={session}  "
              f"계정={', '.join(user_ids)}")

        if not dry_run:
            subprocess.run(
                [TMUX_BIN, "kill-session", "-t", session], capture_output=True
            )

        group_script, script_body = create_group_scripts(
            group, session, extra_flags, i, dry_run=dry_run
        )

        if dry_run:
            separator = "     " + "─" * 50
            print(f"     그룹 스크립트: {group_script}  (dry-run — 파일 생성 안 함)")
            print()
            print(separator)
            for line in script_body.splitlines():
                print(f"     {line}")
            print(separator)
            open_new_terminal(group_script, terminal_app, dry_run=True)
            print()
            continue

        open_new_terminal(group_script, terminal_app, dry_run=False)
        if i < total:
            time.sleep(0.5)

    if not dry_run:
        print()
        print(f"[launch] {total}개 터미널 창 생성 완료.")
        sessions = [f"{TMUX_SESSION_PREFIX}_{i}" for i in range(1, total + 1)]
        print(f"         세션 목록: {', '.join(sessions)}")
        print(f"         재접속:    {TMUX_BIN} attach-session -t {TMUX_SESSION_PREFIX}_1")


# ─── 실행 모드 2: 터미널 네이티브 분할 (기본) ────────────────────────────────

def run_without_tmux(accounts, extra_flags, group_size, dry_run=False):
    """기본 모드: 터미널 앱의 네이티브 분할 / 개별 창으로 실행한다.

    macOS (iTerm2)   : AppleScript split pane → 2×2 분할 창
    macOS (Terminal) : AppleScript 탭 → 같은 창에 탭
    Linux            : 계정마다 개별 터미널 창 (분할 미지원)
    둘 다 없음       : run_background_fallback() 으로 자동 전환
    """
    terminal_app = detect_terminal_app()

    if IS_MACOS:
        _run_no_tmux_macos(accounts, extra_flags, group_size, terminal_app, dry_run)
    else:
        _run_no_tmux_linux(accounts, extra_flags, terminal_app, dry_run)


def _run_no_tmux_macos(accounts, extra_flags, group_size, terminal_app, dry_run):
    """macOS: iTerm2 split pane 또는 Terminal.app 탭."""
    groups = chunk_accounts(accounts, group_size)
    total = len(groups)

    if terminal_app == "iterm2":
        mode_label = "iTerm2 네이티브 split pane"
    else:
        mode_label = "Terminal.app 탭"

    print(f"[launch] {len(accounts)}개 계정 → {total}개 터미널 창 "
          f"(창당 최대 {group_size}개 pane)")
    print(f"[launch] 모드: 네이티브 분할 ({mode_label})")
    print()

    for i, group in enumerate(groups, 1):
        user_ids = [a["user_id"] for a in group]
        print(f"  ── 그룹 {i}/{total}  계정={', '.join(user_ids)}")

        acct_paths = create_acct_scripts(group, extra_flags, dry_run=dry_run)

        if terminal_app == "iterm2":
            applescript = build_iterm2_split_applescript(acct_paths)
            label = f"iTerm2 split (그룹 {i})"
        else:
            applescript = build_terminal_tabs_applescript(acct_paths)
            label = f"Terminal.app 탭 (그룹 {i})"

        if dry_run:
            separator = "     " + "─" * 50
            print()
            print(separator)
            for line in applescript.splitlines():
                print(f"     {line}")
            print(separator)
            print()
            continue

        run_osascript(applescript, dry_run=False, label=label)
        if i < total:
            time.sleep(0.5)

    if not dry_run:
        print()
        print(f"[launch] {total}개 터미널 창 생성 완료 ({mode_label})")


def _run_no_tmux_linux(accounts, extra_flags, terminal_app, dry_run):
    """Linux: 계정마다 개별 터미널 창을 연다.

    Linux 터미널 에뮬레이터는 AppleScript 같은 원격 분할 API가 없으므로
    계정 한 개당 터미널 창 한 개를 생성한다. 모든 창이 동시에 실행된다.
    """
    if not terminal_app:
        print("[INFO] 터미널 에뮬레이터를 찾을 수 없음 → 백그라운드 subprocess로 전환")
        run_background_fallback(accounts, extra_flags)
        return

    total = len(accounts)
    print(f"[launch] {total}개 계정 → {total}개 터미널 창 (창당 1개)")
    print(f"[launch] 모드: 네이티브 (Linux {terminal_app}, 계정당 개별 창)")
    print()

    for i, acct in enumerate(accounts, 1):
        # 계정 스크립트는 그룹이 아닌 단일 계정으로 생성
        acct_paths = create_acct_scripts([acct], extra_flags, dry_run=dry_run)
        cmd = build_linux_open_cmd(terminal_app, acct_paths[0])

        print(f"  [{i}/{total}] {acct['user_id']}: "
              f"{' '.join(cmd)}")

        if not dry_run:
            subprocess.Popen(cmd)
            if i < total:
                time.sleep(0.2)

    if not dry_run:
        print()
        print(f"[launch] {total}개 터미널 창 생성 완료.")


# ─── 실행 모드 3: background ─────────────────────────────────────────────────

def run_background_fallback(accounts, extra_flags, detach=False):
    """--background: 터미널 창 없이 백그라운드 subprocess + 로그 파일로 실행.

    기본은 전 계정이 끝날 때까지 기다리며 결과를 요약한다 — 정각에 무엇이
    성공했는지 그 자리에서 보기 위해서다.

    detach=True 면 시작만 하고 셸을 돌려준다. start_new_session 으로 프로세스
    그룹을 분리하므로 터미널을 닫거나 SSH 가 끊겨도 워커는 계속 돈다.
    """
    print(f"[launch] {len(accounts)}개 계정을 백그라운드로 실행합니다.")
    LOGS_DIR.mkdir(exist_ok=True)
    procs = []
    for a in accounts:
        cmd = [sys.executable, str(RESERVE_PY), "--account", a["user_id"]] + extra_flags
        log_path = LOGS_DIR / f"{a['user_id']}.log"
        with open(log_path, "w") as log_f:
            proc = subprocess.Popen(cmd, stdout=log_f, stderr=subprocess.STDOUT,
                                    start_new_session=detach)
        procs.append((a, proc, log_path))
        print(f"  [{a['no']:2d}] {a['user_id']}: PID {proc.pid} → logs/{log_path.name}")

    print()
    if detach:
        print("[launch] 시작 완료 — 셸을 돌려줍니다. 터미널을 닫아도 계속 돕니다.")
        print("         결과 보기: tail -f logs/*.log")
        print("         전체 종료: pkill -f 'reserve.py --account'")
        return
    print("[launch] 모든 프로세스 시작 완료. Ctrl+C 로 전체 종료.")
    print()

    try:
        for a, proc, _ in procs:
            ret = proc.wait()
            status = "성공" if ret == 0 else f"실패(코드 {ret})"
            print(f"  [{a['no']:2d}] {a['user_id']}: {status}")
    except KeyboardInterrupt:
        print("\n[launch] 중단 — 모든 프로세스를 종료합니다.")
        for _, proc, _ in procs:
            proc.terminate()


# ─── 진입점 ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="다중 계정 런처 (macOS / Linux 공용)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        # 접두 매칭을 끈다. 켜두면 reserve.py 용 `--account 5` 가 `--only=5` 로
        # 조용히 해석되어 워커 대신 런처가 한 겹 더 열린다.
        allow_abbrev=False,
        epilog="""
실행 모드:
  (없음)        터미널 네이티브 분할  [기본]
                  macOS/iTerm2  : 2×2 split pane
                  macOS/Terminal: 탭
                  Linux         : 계정당 개별 창
  --tmux        tmux 세션 + 새 터미널 창
  --background  백그라운드 subprocess + 로그 파일

설정 파일:
  accounts.txt       이름,아이디,비밀번호   (비밀번호는 여기에만)
  config.toml        실행 파라미터
  reservation.json   이번 회차 예약 — viewer.py 의 [저장] 이 만든다

실행 대상은 reservation.json 의 entries 다 (--logincheck 는 accounts.txt 전 계정).
        """,
    )
    parser.add_argument("--test", nargs="?", const=True, default=None,
                        metavar="초|HH:MM",
                        help="테스트 모드 (대관신청 전 중단). 값을 주면 그 시각을 "
                             "오픈으로 강제해 전 계정이 동일 시각에 발사한다. "
                             "값이 없으면 오픈을 기다리지 않고 즉시 실행")
    parser.add_argument("--logincheck", action="store_true",
                        help="로그인 테스트만 실행")

    launcher = parser.add_argument_group("런처 전용")
    launcher.add_argument("--only", metavar="범위",
                          help="실행할 계정 번호 선택 (다중 PC 분산용). "
                               "예: 1-10 / 1,3,5 / 1-5,8  (미지정 시 전체)")
    launcher.add_argument("--tmux", action="store_true",
                          help="tmux 세션으로 실행 (기본은 터미널 네이티브 분할)")
    launcher.add_argument("--background", action="store_true",
                          help="백그라운드 subprocess + 로그 파일로 실행 "
                               "(전 계정이 끝날 때까지 기다리며 결과를 요약한다)")
    launcher.add_argument("--detach", action="store_true",
                          help="--background 로 시작하고 기다리지 않고 셸을 돌려준다 "
                               "(SSH 접속을 끊어도 계속 돈다)")
    launcher.add_argument("--dry-run", action="store_true",
                          help="생성될 명령/스크립트 내용 출력 (창 미생성)")

    worker = parser.add_argument_group(
        "단일 계정",
        "--account/--search/--search2 를 주면 창을 열지 않고 reserve.py 로 넘어간다. "
        "--browser 는 넘기지 않고 각 창의 reserve.py 에 전달한다 "
        "(전 계정에 주면 브라우저가 계정 수만큼 뜨므로 --only 와 함께 쓴다).",
    )
    add_worker_args(worker)

    args = parser.parse_args()

    # 단일 계정 실행과 빈자리 검색은 창을 열 일이 없다. reserve.py 로 프로세스를
    # 넘긴다. import 가 아니라 execv 인 이유: reserve.py 는 aiohttp 등으로 300ms
    # 넘게 걸리는데, 런처 기본 경로가 그 비용을 늘 물면 안 된다. 프로세스가 통째로
    # 교체되므로 exit code·Ctrl+C·출력이 그대로 이어진다.
    if args.account is not None or args.search or args.search2:
        conflicts = [name for name, given in (
            ("--only", args.only), ("--tmux", args.tmux), ("--detach", args.detach),
            ("--background", args.background), ("--dry-run", args.dry_run),
        ) if given]
        if conflicts:
            print(f"[ERROR] {', '.join(conflicts)} 은(는) 창을 여는 런처 전용 옵션이라 "
                  f"--account/--search/--search2 와 함께 쓸 수 없습니다.")
            sys.exit(1)
        os.execv(sys.executable,
                 [sys.executable, str(RESERVE_PY), *sys.argv[1:]])

    try:
        accounts, generated_at, warnings = load_targets(logincheck=args.logincheck)
    except (schedule.ScheduleError, config.ConfigError) as e:
        print(f"[ERROR] {e}")
        sys.exit(1)

    # --only: reservation.json 의 일련번호(no)로 부분 선택 — 다중 PC(IP) 분산용
    if args.only:
        try:
            wanted = parse_account_range(args.only)
        except ValueError as e:
            print(f"[ERROR] --only: {e}")
            sys.exit(1)
        available = {a["no"] for a in accounts}
        missing = sorted(wanted - available)
        if missing:
            print(f"[ERROR] --only: 없는 번호: {missing} (1~{max(available)} 범위)")
            sys.exit(1)
        accounts = [a for a in accounts if a["no"] in wanted]

    if not accounts:
        print("[ERROR] 실행할 계정이 없습니다.")
        print()
        print("  python3 viewer.py 에서 계정을 체크해 예약을 배정하고 [저장] 을 누르세요.")
        sys.exit(1)

    print("=" * 60)
    print("고양시 테니스장 다중 계정 런처")
    print("=" * 60)
    print(f"  플랫폼: {'macOS' if IS_MACOS else sys.platform}")
    if args.logincheck:
        print(f"  대상: {config.ACCOUNTS_FILE.name} 전 계정 {len(accounts)}개")
    else:
        print(f"  계획: {schedule.SCHEDULE_FILE.name} (저장 시각 {generated_at or '알 수 없음'})")
        print(f"  대상: {len(accounts)}개 계정")
    for a in accounts:
        label = f" {a['name']}" if a["name"] else ""
        print(f"  [{a['no']:2d}] {a['user_id']}{label}")
    for w in warnings:
        print(f"  [경고] {w}")
    print()

    extra_flags = []
    if args.test is not None:
        if args.test is True:
            extra_flags.append("--test")
            print("  모드: 테스트 (즉시 실행, 대관신청 전 중단)")
        else:
            # 상대 초는 여기서 한 번만 절대 시각으로 변환해
            # 모든 계정 프로세스가 동일한 오픈 시각을 공유하게 한다.
            # 변환 규칙은 reserve.py와 반드시 같아야 하므로 utils의 함수를 공유한다.
            try:
                open_at = parse_open_time(args.test).strftime("%H:%M")
            except ValueError as e:
                print(f"[ERROR] --test: {e}")
                sys.exit(1)
            extra_flags += ["--test", open_at]
            print(f"  모드: 테스트 — 오픈 {open_at} 강제 (전 계정 공통, 신청 직전 중단)")
    elif args.logincheck:
        extra_flags.append("--logincheck")
        print("  모드: 로그인 테스트")
    else:
        print("  모드: 실제 예약")

    if args.browser:
        # 브라우저 모드는 "예약 1건 = Chrome 1개" 다. 런처가 계정 프로세스를
        # 동시에 띄우므로 전체 인스턴스 수 = 예약 건수 총합이 되고, 그 사이
        # 어떤 상한도 걸리지 않는다 (MAX_CONCURRENT 는 프로세스 안에서만 적용).
        # 실측 인스턴스당 약 900MB — 대상을 좁히지 않으면 메모리가 먼저 터지고,
        # 그 결과가 "로그인 실패" 로 나타난다.
        chrome = sum(a["slots"] for a in accounts)
        if not args.only:
            print(f"[ERROR] --browser 는 대상을 좁혀서 쓰세요.")
            print(f"  지금 그대로면 Chrome {chrome}개가 동시에 뜹니다 "
                  f"(인스턴스당 약 900MB → 약 {chrome * 0.9:.0f}GB).")
            print()
            print("  계정 하나만:      python3 main.py --account <아이디> --browser --test")
            print("  일부만:           python3 main.py --only 1-2 --browser --test")
            print()
            print("  브라우저 모드는 디버깅용입니다. 정각 실전은 HTTP 모드(기본)를 씁니다.")
            sys.exit(1)
        extra_flags.append("--browser")
        print(f"  브라우저 모드: Chrome {chrome}개 (약 {chrome * 0.9:.1f}GB 예상)")
    print()

    # 실행 모드 결정 (우선순위: --background > --tmux > 터미널 네이티브 분할[기본])
    if args.background or args.detach:
        if args.dry_run:
            print("[dry-run] 실행될 명령:")
            for a in accounts:
                flags = " ".join(extra_flags)
                print(f"  python3 {RESERVE_PY} --account {a['user_id']} {flags}")
        else:
            run_background_fallback(accounts, extra_flags, detach=args.detach)

    elif args.tmux and tmux_available():
        run_multi_terminal(accounts, extra_flags,
                           group_size=config.GROUP_SIZE, dry_run=args.dry_run)

    else:
        # --tmux를 줬는데 tmux가 없으면 멈추지 않고 기본 모드로 내려간다.
        # 정각을 앞두고 "실행 안 됨"보다 "다른 방식으로라도 실행됨"이 낫다.
        if args.tmux:
            print("[경고] tmux를 찾을 수 없어 터미널 네이티브 분할로 실행합니다.")
            print()
        run_without_tmux(accounts, extra_flags,
                         group_size=config.GROUP_SIZE, dry_run=args.dry_run)


if __name__ == "__main__":
    main()
