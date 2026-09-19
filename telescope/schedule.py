"""launchd scheduling.

Generates, installs and inspects the two agents that keep Telescope current:

* ``com.telescope.sync``   — pulls new items several times a day
* ``com.telescope.digest`` — writes the daily digest

Why two agents rather than one: pulling data is cheap, frequent and safe to
retry, while writing the digest happens once. Separating them means a feed that
is briefly down at digest time has already been captured by an earlier sync,
instead of leaving that day thin.

Both agents run the project's own interpreter (``sys.executable -m
telescope.cli``), so the plists do not depend on PATH, a shell profile, or
``uv`` being on the path. Everything is an absolute path — launchd does not
expand ``~``.

Times are system local time, which is what ``StartCalendarInterval`` means. On a
machine set to Asia/Singapore, ``Hour 8`` is 08:00 SGT.
"""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SYNC_LABEL = "com.telescope.sync"
DIGEST_LABEL = "com.telescope.digest"

# Sync a quarter-hour before each digest-relevant boundary so the 08:00 digest
# always reads fresh data.
DEFAULT_SYNC_HOURS: tuple[int, ...] = (0, 3, 6, 9, 12, 15, 18, 21)
DEFAULT_SYNC_MINUTE = 45
DEFAULT_DIGEST_HOUR = 8
DEFAULT_DIGEST_MINUTE = 0

REQUIRED_PLIST_KEYS = (
    "Label",
    "ProgramArguments",
    "WorkingDirectory",
    "StandardOutPath",
    "StandardErrorPath",
)


@dataclass
class AgentSpec:
    """Everything needed to write and reason about one agent."""

    label: str
    program_args: list[str]
    working_dir: Path
    stdout_path: Path
    stderr_path: Path
    calendar: list[dict[str, int]]

    @property
    def plist_name(self) -> str:
        return f"{self.label}.plist"


@dataclass
class AgentStatus:
    label: str
    plist_path: Path
    installed: bool
    loaded: bool
    state: str | None = None
    last_exit_code: int | None = None
    runs: int | None = None
    stdout_path: Path | None = None
    stderr_path: Path | None = None
    detail: str | None = None


def program_arguments(*args: str) -> list[str]:
    """Run the module through this interpreter — no PATH lookup, no wrapper."""
    return [sys.executable, "-m", "telescope.cli", *args]


def launch_agents_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "LaunchAgents"


def log_dir(home: Path | None = None) -> Path:
    return (home or Path.home()) / "Library" / "Logs" / "Telescope"


def default_specs(
    project_root: Path,
    *,
    home: Path | None = None,
    sync_hours: tuple[int, ...] | list[int] | None = None,
    sync_minute: int = DEFAULT_SYNC_MINUTE,
    digest_hour: int = DEFAULT_DIGEST_HOUR,
    digest_minute: int = DEFAULT_DIGEST_MINUTE,
) -> list[AgentSpec]:
    """The two agents, with sane defaults."""
    logs = log_dir(home)
    project_root = Path(project_root)

    hours = tuple(sync_hours) if sync_hours else DEFAULT_SYNC_HOURS
    sync_calendar = [{"Hour": int(hour), "Minute": int(sync_minute)} for hour in hours]

    return [
        AgentSpec(
            label=SYNC_LABEL,
            program_args=program_arguments("sync"),
            working_dir=project_root,
            stdout_path=logs / "sync.log",
            stderr_path=logs / "sync.err.log",
            calendar=sync_calendar,
        ),
        AgentSpec(
            label=DIGEST_LABEL,
            program_args=program_arguments("digest"),
            working_dir=project_root,
            stdout_path=logs / "digest.log",
            stderr_path=logs / "digest.err.log",
            calendar=[{"Hour": int(digest_hour), "Minute": int(digest_minute)}],
        ),
    ]


def build_plist(spec: AgentSpec) -> bytes:
    """Serialise an agent to plist XML.

    Built with plistlib rather than string templates so paths containing spaces
    — and yours does — cannot produce malformed XML.
    """
    payload: dict[str, object] = {
        "Label": spec.label,
        "ProgramArguments": list(spec.program_args),
        "WorkingDirectory": str(spec.working_dir),
        "StandardOutPath": str(spec.stdout_path),
        "StandardErrorPath": str(spec.stderr_path),
        "ProcessType": "Background",
        # A missed run fires on wake, which is the whole reason launchd is used
        # here instead of cron. RunAtLoad stays off so a reinstall does not
        # trigger an immediate fetch.
        "RunAtLoad": False,
        "StartCalendarInterval": spec.calendar,
    }
    return plistlib.dumps(payload)


def describe(spec: AgentSpec) -> str:
    """Human-readable schedule, for `telescope schedule show`."""
    times = ", ".join(
        f"{entry['Hour']:02d}:{entry['Minute']:02d}" for entry in spec.calendar
    )
    return f"{spec.label}: {' '.join(spec.program_args[2:])} at {times} (local time)"


def _domain() -> str:
    """The GUI domain for this user; agents run as the logged-in user."""
    return f"gui/{os.getuid()}"


def _run(runner, args: list[str]) -> subprocess.CompletedProcess:
    return runner(args, capture_output=True, text=True)


def load_agent(
    spec: AgentSpec, plist_path: Path, *, runner=subprocess.run
) -> tuple[bool, str]:
    """Load (or reload) an agent. Returns ``(ok, how_or_error)``.

    ``bootout`` first makes this idempotent: reinstalling after an edit actually
    picks up the change instead of silently keeping the old definition.
    """
    _run(runner, ["launchctl", "bootout", f"{_domain()}/{spec.label}"])

    modern = _run(runner, ["launchctl", "bootstrap", _domain(), str(plist_path)])
    if modern.returncode == 0:
        return True, "bootstrap"

    # macOS 26 still honours the legacy verbs, so fall back rather than fail.
    legacy = _run(runner, ["launchctl", "load", "-w", str(plist_path)])
    if legacy.returncode == 0:
        return True, "load (legacy)"

    detail = (modern.stderr or legacy.stderr or "launchctl failed").strip()
    return False, detail


def unload_agent(
    spec: AgentSpec, plist_path: Path, *, runner=subprocess.run
) -> None:
    _run(runner, ["launchctl", "bootout", f"{_domain()}/{spec.label}"])
    _run(runner, ["launchctl", "unload", "-w", str(plist_path)])


def install(
    specs: list[AgentSpec],
    *,
    agents_dir: Path | None = None,
    runner=subprocess.run,
    load: bool = True,
) -> list[tuple[Path, bool, str]]:
    """Write the plists and load them. Returns ``(path, ok, detail)`` per agent."""
    directory = Path(agents_dir) if agents_dir else launch_agents_dir()
    directory.mkdir(parents=True, exist_ok=True)

    results: list[tuple[Path, bool, str]] = []
    for spec in specs:
        spec.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        path = directory / spec.plist_name
        path.write_bytes(build_plist(spec))
        if load:
            ok, detail = load_agent(spec, path, runner=runner)
        else:
            ok, detail = True, "written, not loaded (--no-load)"
        results.append((path, ok, detail))
    return results


def uninstall(
    specs: list[AgentSpec], *, agents_dir: Path | None = None, runner=subprocess.run
) -> list[Path]:
    directory = Path(agents_dir) if agents_dir else launch_agents_dir()
    removed: list[Path] = []
    for spec in specs:
        path = directory / spec.plist_name
        unload_agent(spec, path, runner=runner)
        if path.exists():
            path.unlink()
            removed.append(path)
    return removed


_STATE_RE = re.compile(r"^\s*state = (.+)$", re.MULTILINE)
_EXIT_RE = re.compile(r"^\s*last exit code = (-?\d+)$", re.MULTILINE)
_RUNS_RE = re.compile(r"^\s*runs = (\d+)$", re.MULTILINE)


def agent_status(
    spec: AgentSpec, *, agents_dir: Path | None = None, runner=subprocess.run
) -> AgentStatus:
    directory = Path(agents_dir) if agents_dir else launch_agents_dir()
    path = directory / spec.plist_name

    result = _run(runner, ["launchctl", "print", f"{_domain()}/{spec.label}"])
    loaded = result.returncode == 0

    state = last_exit = runs = None
    detail = None
    if loaded:
        text = result.stdout or ""
        if match := _STATE_RE.search(text):
            state = match.group(1).strip()
        if match := _EXIT_RE.search(text):
            last_exit = int(match.group(1))
        if match := _RUNS_RE.search(text):
            runs = int(match.group(1))
    else:
        detail = (result.stderr or "").strip() or "not loaded"

    return AgentStatus(
        label=spec.label,
        plist_path=path,
        installed=path.exists(),
        loaded=loaded,
        state=state,
        last_exit_code=last_exit,
        runs=runs,
        stdout_path=spec.stdout_path,
        stderr_path=spec.stderr_path,
        detail=detail,
    )


def tail(path: Path, *, lines: int = 40) -> str:
    """Last ``lines`` of a log file, or a note that it does not exist yet."""
    if not path.exists():
        return f"(no log yet: {path})"
    content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(content) <= lines:
        return "\n".join(content)
    return "\n".join([f"… {len(content) - lines} earlier lines …", *content[-lines:]])
