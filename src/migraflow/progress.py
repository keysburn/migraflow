"""Ephemeral display events; never a source of transfer or checkpoint state."""
import time
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from threading import RLock

from rich.console import Group
from rich.live import Live
from rich.panel import Panel
from rich.progress import BarColumn, Progress, TaskProgressColumn, TextColumn
from rich.table import Table
from rich.text import Text


_observer = ContextVar("migraflow_progress", default=None)


@contextmanager
def observing(observer):
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


def emit(kind, **values):
    observer = _observer.get()
    if observer is not None:
        try:
            observer(kind, **values)
        except Exception:
            # A broken display must not turn a successful copy into a failed job.
            _observer.set(None)


def observed():
    return _observer.get() is not None


def _duration(seconds):
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    return f"{seconds // 3600:02d}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


def _bytes(value):
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:,.1f} {unit}"
        value /= 1024


def _safe(value, encoding=None):
    # repr escapes control characters and surrogate filenames; Text disables markup.
    text = ascii(str(value))[1:-1] if any(not c.isprintable() for c in str(value)) else str(value)
    return text.encode(encoding, errors="backslashreplace").decode(encoding) if encoding else text


class Dashboard:
    def __init__(self, console, enabled):
        self.console = console
        self.enabled = enabled
        self.lock = RLock()
        self.started = time.monotonic()
        self.data = {"status": "PREPARING", "phase": "사전 점검 및 계획 확인", "job": "-",
                     "planned_files": None, "planned_bytes": 0, "verified": 0,
                     "source": "-", "destination": "-", "algorithm": "-", "checkpoint": "-",
                     "files": 0, "total_files": None, "bytes": 0, "total_bytes": 0,
                     "phase_started": self.started, "current": "-", "retries": 0,
                     "reused": 0, "errors": 0, "successes": 0, "failures": 0,
                     "hash_bytes": 0, "hash_total": None, "hash_started": self.started,
                     "hash_active": False, "note": "Ctrl+C: 체크포인트를 남기고 중지"}
        self.data["rsync"] = None
        self.live = None
        self.samples = deque(maxlen=64)
        self.samples.append((self.started, 0))

    def __enter__(self):
        if self.enabled:
            self.live = Live(self, console=self.console, refresh_per_second=4, transient=True)
            try:
                self.live.start(refresh=True)
            except (OSError, UnicodeError):
                self.enabled = False
        return self.update if self.enabled else None

    def __exit__(self, exc_type, exc, traceback):
        if self.live is not None:
            try:
                self.live.stop()
            except (OSError, UnicodeError):
                pass

    def update(self, kind, **values):
        with self.lock:
            state = self.data
            if kind == "init":
                state.update(values)
            elif kind == "phase":
                state.update(values, files=0, bytes=0, successes=0, failures=0,
                             phase_started=time.monotonic(), hash_active=False, current="-", rsync=None,
                             hash_total=None, hash_bytes=0)
                self.samples.clear()
                self.samples.append((time.monotonic(), 0))
            elif kind == "processed":
                state["files"] += values.get("files", 0)
                state["bytes"] += values.get("bytes", 0)
                state["successes"] += values.get("successes", 0)
                state["failures"] += values.get("failures", 0)
                stamp = time.monotonic()
                if stamp - self.samples[-1][0] >= 0.5:
                    self.samples.append((stamp, state["files"]))
                while len(self.samples) > 1 and self.samples[1][0] < stamp - 10:
                    self.samples.popleft()
            elif kind == "watch":
                result = values["result"]
                previous = state["files"]
                state.update(job=result["id"], status=result["status"], phase="DB 상태 감시", files=result["verified_files"],
                             verified=result["verified_files"], planned_files=result["files"], total_files=result["files"],
                             planned_bytes=result["bytes"], total_bytes=result["bytes"], retries=result["retry_count"],
                             reused=result["reused_files"], algorithm=result["hash_algorithm"], checkpoint=result["updated_at"],
                             successes=result["verified_files"], failures=result.get("file_statuses", {}).get("VERIFY_FAILED", 0),
                             errors=max(result.get("file_statuses", {}).get("VERIFY_FAILED", 0), int(bool(result.get("error")))),
                             source=result["config"]["source"]["path"], destination=result["config"]["destination"]["path"],
                             database_bytes=values["database_bytes"], state_free_bytes=values["state_free_bytes"],
                             note="Ctrl+C: 감시만 종료 · 실행 중인 작업은 계속됩니다")
                if not state.get("watching") or state["files"] < previous:
                    state["watch_start_files"] = state["files"]
                    state["phase_started"] = time.monotonic()
                    self.samples.clear()
                    self.samples.append((time.monotonic(), state["files"]))
                    state["watching"] = True
                elif time.monotonic() - self.samples[-1][0] >= 0.5:
                    self.samples.append((time.monotonic(), state["files"]))
            elif kind == "hash_start":
                state.update(current=_safe(values["path"]), hash_total=values["size"],
                             hash_bytes=0, hash_started=time.monotonic(), hash_active=True)
            elif kind == "hash_chunk":
                state["hash_bytes"] += values["bytes"]
            elif kind == "hash_end":
                state["hash_active"] = False
            elif kind == "error":
                state["errors"] += 1
                state["note"] = _safe(values["error"])
                state["retries"] += int(values.get("retry", False))
                state["hash_active"] = False
            elif kind == "rsync":
                state["rsync"] = values
            elif kind == "reuse":
                state["reused"] += values["files"]
            elif kind == "checkpoint":
                state["verified"] = values.get("verified", state["verified"] + values.get("delta", 0))
                state["checkpoint"] = values.get("time", state["checkpoint"])
            elif kind == "status":
                state["status"] = values["status"]
                if state["status"] in ("RETRY_WAIT", "FAILED", "INTERRUPTED", "VERIFIED", "VERIFY_FAILED"):
                    state["hash_active"] = False
                if values.get("error"):
                    state["note"] = _safe(values["error"])
                    state["errors"] = max(1, state["errors"])
                elif state["status"] == "RETRY_WAIT":
                    state["note"] = "일시 오류: 제한된 대기 후 재시도"
                else:
                    state["note"] = "Ctrl+C: 체크포인트를 남기고 중지"

    def __rich_console__(self, console, options):
        with self.lock:
            state = self.data.copy()
            samples = tuple(self.samples)
        now = time.monotonic()
        elapsed = now - state["phase_started"]
        measured_files = state["files"] - state.get("watch_start_files", 0)
        rate = measured_files / elapsed if elapsed > 0 and measured_files > 0 else None
        remaining = max(0, (state["total_files"] or 0) - state["files"])
        eta = remaining / rate if rate and state["status"] not in ("RETRY_WAIT", "FAILED", "INTERRUPTED") else None
        sample = next((item for item in samples if item[0] >= now - 10), samples[-1])
        recent = max(0, state["files"] - sample[1]) / max(now - sample[0], 0.001) if now - sample[0] <= 10 else 0
        progress = Progress(TextColumn("{task.description}"), BarColumn(), TaskProgressColumn(),
                            console=console, auto_refresh=False)
        progress.add_task("검증 체크포인트", total=state["planned_files"], completed=state["verified"])
        progress.add_task("현재 단계", total=state["total_files"], completed=state["files"])
        if state["rsync"] is not None:
            progress.add_task("rsync 배치 · 참고", total=100, completed=state["rsync"]["percent"])
        if state["hash_total"] is not None:
            progress.add_task("파일 해시 읽기", total=state["hash_total"], completed=state["hash_bytes"])
        details = Table.grid(padding=(0, 2), expand=True)
        details.add_column(style="dim", no_wrap=True)
        details.add_column(overflow="fold")
        for label, value in (
            ("작업", state["job"]), ("상태 / 단계", f"{state['status']} / {state['phase']}"),
            ("경로", f"{_safe(state['source'])} → {_safe(state['destination'])}"),
            ("전체 계획 / 검증 방식", f"{state['planned_files'] if state['planned_files'] is not None else '미확정'} files · {_bytes(state['planned_bytes'])} · {state['algorithm']}"),
            ("경과 / 단계 ETA", f"{_duration(now - self.started)} / {_duration(eta)}"),
            ("검증 파일" if state.get("watching") else "단계 파일 / 내용량",
             f"{state['files']:,} / {state['total_files'] if state['total_files'] is not None else '미확정'}" +
             (" · 바이트 진행은 실행 화면에서 확인" if state.get("watching") else f" · {_bytes(state['bytes'])} / {_bytes(state['total_bytes'])}")),
            ("파일 처리 속도", f"{rate:.1f} files/s (단계 평균)" if rate else "측정 대기 · 전송은 배치 완료 후 반영"),
            ("최근 파일 처리 속도", f"{recent:.1f} files/s (최대 10초 창)"),
            ("해시 읽기 속도", f"{_bytes(state['hash_bytes'] / max(now - state['hash_started'], 0.001))}/s" if state["hash_active"] else "-"),
            ("현재 읽는 파일", state["current"]),
            ("이번 단계 검증", f"성공 {state['successes']:,} · 실패 {state['failures']:,}"),
            ("재사용 / 재시도 / 오류", f"{state['reused']:,} / {state['retries']:,} / {state['errors']:,}"),
            ("마지막 상태 커밋", state["checkpoint"]),
        ):
            details.add_row(Text(label), Text(_safe(value, console.encoding)))
        if state["rsync"] is not None:
            value = state["rsync"]
            details.add_row(Text("rsync 참고 수치"), Text(
                f"{_bytes(value['bytes'])} · {value['speed']} · 표시 시간 {value['time']}"))
        if "database_bytes" in state:
            details.add_row(Text("DB+WAL / 상태 디스크 여유"), Text(f"{_bytes(state['database_bytes'])} / {_bytes(state['state_free_bytes'])}"))
        yield Panel(Group(details, Text(""), progress, Text(_safe(state["note"], console.encoding), style="dim")),
                    title=Text("MigraFlow · LIVE", style="bold cyan"), border_style="cyan",
                    subtitle=Text("진행 표시는 완료 증거가 아닙니다 · 최종 결과를 확인하세요", style="dim"))
