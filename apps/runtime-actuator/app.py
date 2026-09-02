import asyncio
import os
import signal
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException


TARGET = os.environ["RUNTIME_ACTUATOR_TARGET"]
ALLOW_STOP = os.getenv("RUNTIME_ACTUATOR_ALLOW_STOP", "false").lower() == "true"
STATE_DIR = Path(os.getenv("RUNTIME_ACTUATOR_STATE_DIR", "/run/opspilot"))
QUARANTINE_MARKER = STATE_DIR / "quarantined"
SOCKET_PATH = STATE_DIR / "actuator.sock"
PROCESS_MATCH = os.environ["RUNTIME_ACTUATOR_PROCESS_MATCH"]
TICKS_PER_SECOND = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
NANOSECONDS_PER_TICK = 1_000_000_000 // TICKS_PER_SECOND
previous_cpu_total = 0
previous_system_total = 0


def target_pid() -> int:
    matches = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit() or entry.name == "1":
            continue
        try:
            command = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode()
        except (OSError, UnicodeDecodeError):
            continue
        if command.startswith(PROCESS_MATCH):
            matches.append(int(entry.name))
    if len(matches) != 1:
        raise RuntimeError(f"expected one {TARGET} workload process, found {len(matches)}")
    return matches[0]


def target_state() -> str:
    if QUARANTINE_MARKER.exists():
        return "stopped"
    try:
        fields = Path(f"/proc/{target_pid()}/status").read_text(encoding="utf-8").splitlines()
        state = next(line for line in fields if line.startswith("State:"))
    except (OSError, StopIteration):
        return "exited"
    return "stopped" if "T (stopped)" in state else "running"


def target_identity() -> tuple[int, str]:
    pid = target_pid()
    return pid, Path(f"/proc/{pid}/stat").read_text(encoding="utf-8").split()[21]


def clear_quarantine() -> None:
    QUARANTINE_MARKER.unlink(missing_ok=True)


def cpu_stats() -> dict:
    global previous_cpu_total, previous_system_total
    process = Path(f"/proc/{target_pid()}/stat").read_text(encoding="utf-8").split()
    cpu_total = (int(process[13]) + int(process[14])) * NANOSECONDS_PER_TICK
    host_cpu = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()[1:]
    system_total = sum(int(value) for value in host_cpu) * NANOSECONDS_PER_TICK
    result = {
        "target": TARGET,
        "cpu_total_usage": cpu_total,
        "previous_cpu_total_usage": previous_cpu_total or cpu_total,
        "system_cpu_usage": system_total,
        "previous_system_cpu_usage": previous_system_total or system_total,
        "online_cpus": os.cpu_count() or 1,
    }
    previous_cpu_total = cpu_total
    previous_system_total = system_total
    return result


async def restart_target() -> None:
    before = target_identity()
    clear_quarantine()
    os.kill(before[0], signal.SIGCONT)
    os.kill(before[0], signal.SIGTERM)
    for _ in range(100):
        await asyncio.sleep(0.1)
        try:
            if target_identity() != before and target_state() == "running":
                return
        except (OSError, RuntimeError):
            continue
    raise RuntimeError(f"target did not restart: {TARGET}")


@asynccontextmanager
async def lifespan(_app: FastAPI):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    async def restore_stopped_state() -> None:
        while QUARANTINE_MARKER.exists():
            try:
                os.kill(target_pid(), signal.SIGSTOP)
                return
            except (OSError, RuntimeError):
                await asyncio.sleep(0.1)

    task = asyncio.create_task(restore_stopped_state()) if QUARANTINE_MARKER.exists() else None
    try:
        yield
    finally:
        if task is not None:
            task.cancel()


app = FastAPI(title="OpsPilot Runtime Actuator", version="0.1.0", lifespan=lifespan)


@app.get("/health")
async def health():
    return {"status": "ok", "service": "opspilot-runtime-actuator", "target": TARGET}


@app.get("/v1/status")
async def status():
    return {"target": TARGET, "status": target_state()}


@app.get("/v1/stats")
async def stats():
    try:
        return cpu_stats()
    except OSError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/restart")
async def restart():
    try:
        await restart_target()
        return {"status": "completed", "result": f"restarted {TARGET}"}
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@app.post("/v1/stop")
async def stop():
    if not ALLOW_STOP:
        raise HTTPException(status_code=403, detail=f"stop is not enabled for {TARGET}")
    try:
        QUARANTINE_MARKER.write_text(TARGET, encoding="utf-8")
        os.kill(target_pid(), signal.SIGSTOP)
        return {"status": "completed", "result": f"stopped {TARGET}"}
    except (OSError, RuntimeError) as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc
