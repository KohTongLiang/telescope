"""launchd scheduling: plist generation, install/uninstall, status parsing.

Nothing here touches the real system: the agents directory is a temp path and
``launchctl`` is a fake runner that records the calls it was given.
"""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path

import pytest

from telescope.schedule import (
    DIGEST_LABEL,
    SYNC_LABEL,
    agent_status,
    build_plist,
    default_specs,
    describe,
    install,
    tail,
    uninstall,
)

PROJECT = Path("/Users/example/Projects/telescope")

STATUS_OUTPUT = """
gui/501/com.telescope.sync = {
\tactive count = 0
\tpath = /Users/example/Library/LaunchAgents/com.telescope.sync.plist
\tstate = waiting
\truns = 7
\tlast exit code = 0
}
"""


def make_runner(returncodes: dict[str, int] | None = None, stdout: str = ""):
    """Fake subprocess.run that records calls and returns canned codes."""
    calls: list[list[str]] = []
    codes = returncodes or {}

    def runner(args, **kwargs):
        calls.append(list(args))
        verb = args[1] if len(args) > 1 else ""
        code = codes.get(verb, 0)
        return subprocess.CompletedProcess(
            args,
            code,
            stdout if verb == "print" else "",
            "" if code == 0 else f"{verb} failed",
        )

    runner.calls = calls  # type: ignore[attr-defined]
    return runner


@pytest.fixture
def specs(tmp_path):
    return default_specs(PROJECT, home=tmp_path)


# -- plist generation --------------------------------------------------------


def test_plist_contains_the_required_keys(specs):
    payload = plistlib.loads(build_plist(specs[0]))
    assert payload["Label"] == SYNC_LABEL
    assert payload["WorkingDirectory"] == str(PROJECT)
    assert payload["RunAtLoad"] is False
    assert payload["ProcessType"] == "Background"
    assert payload["StartCalendarInterval"]


def test_plist_has_no_tilde_paths(specs):
    # launchd does not expand ~, so a tilde would silently point nowhere.
    text = build_plist(specs[0]).decode("utf-8")
    assert "~" not in text
    assert str(PROJECT) in text


def test_plist_paths_are_absolute(specs):
    for spec in specs:
        payload = plistlib.loads(build_plist(spec))
        assert Path(payload["WorkingDirectory"]).is_absolute()
        assert Path(payload["StandardOutPath"]).is_absolute()
        assert Path(payload["StandardErrorPath"]).is_absolute()
        assert Path(payload["ProgramArguments"][0]).is_absolute()


def test_program_arguments_run_the_module_not_a_path_lookup(specs):
    sync = plistlib.loads(build_plist(specs[0]))["ProgramArguments"]
    digest = plistlib.loads(build_plist(specs[1]))["ProgramArguments"]
    assert sync[1:] == ["-m", "telescope.cli", "sync"]
    assert digest[1:] == ["-m", "telescope.cli", "digest"]


def test_sync_schedule_is_repeating_and_before_the_digest(specs):
    sync = plistlib.loads(build_plist(specs[0]))["StartCalendarInterval"]
    assert len(sync) == 8
    assert {entry["Minute"] for entry in sync} == {45}
    # The last sync runs at 06:45, ahead of the 08:00 digest.
    assert [entry["Hour"] for entry in sync] == [0, 3, 6, 9, 12, 15, 18, 21]


def test_digest_schedule_is_a_single_daily_time(specs):
    digest = plistlib.loads(build_plist(specs[1]))["StartCalendarInterval"]
    assert digest == [{"Hour": 8, "Minute": 0}]


def test_custom_schedule_is_honoured(tmp_path):
    specs = default_specs(PROJECT, home=tmp_path, sync_hours=[6, 18], digest_hour=7, digest_minute=30)
    sync = plistlib.loads(build_plist(specs[0]))["StartCalendarInterval"]
    digest = plistlib.loads(build_plist(specs[1]))["StartCalendarInterval"]
    assert [entry["Hour"] for entry in sync] == [6, 18]
    assert digest == [{"Hour": 7, "Minute": 30}]


def test_describe_is_readable(specs):
    text = describe(specs[1])
    assert DIGEST_LABEL in text
    assert "08:00" in text
    assert "local time" in text


# -- install / uninstall -----------------------------------------------------


def test_install_writes_plists_and_bootstraps(tmp_path, specs):
    agents = tmp_path / "LaunchAgents"
    runner = make_runner()

    results = install(specs, agents_dir=agents, runner=runner)

    assert [path.name for path, _, _ in results] == [
        f"{SYNC_LABEL}.plist",
        f"{DIGEST_LABEL}.plist",
    ]
    for path, ok, _ in results:
        assert ok
        assert path.exists()
        assert plistlib.loads(path.read_bytes())["Label"] in (SYNC_LABEL, DIGEST_LABEL)

    verbs = [call[1] for call in runner.calls]
    assert verbs.count("bootstrap") == 2
    # bootout first makes reinstall idempotent.
    assert verbs.count("bootout") == 2


def test_install_writes_log_directories(tmp_path, specs):
    agents = tmp_path / "LaunchAgents"
    install(specs, agents_dir=agents, runner=make_runner())
    for spec in specs:
        assert spec.stdout_path.parent.is_dir()


def test_install_falls_back_to_legacy_load(tmp_path, specs):
    runner = make_runner({"bootstrap": 1, "load": 0})
    results = install(specs, agents_dir=tmp_path / "agents", runner=runner)
    assert all(ok for _, ok, _ in results)
    assert ("load (legacy)") in results[0][2]
    assert any(call[1] == "load" for call in runner.calls)


def test_install_reports_failure_without_raising(tmp_path, specs):
    runner = make_runner({"bootstrap": 1, "load": 1})
    results = install(specs, agents_dir=tmp_path / "agents", runner=runner)
    assert not any(ok for _, ok, _ in results)
    assert "bootstrap failed" in results[0][2]


def test_install_no_load_only_writes(tmp_path, specs):
    runner = make_runner()
    results = install(specs, agents_dir=tmp_path / "agents", runner=runner, load=False)
    assert all(ok for _, ok, _ in results)
    assert runner.calls == []
    assert (tmp_path / "agents" / f"{SYNC_LABEL}.plist").exists()


def test_uninstall_removes_plists_and_boots_out(tmp_path, specs):
    agents = tmp_path / "agents"
    install(specs, agents_dir=agents, runner=make_runner())

    runner = make_runner()
    removed = uninstall(specs, agents_dir=agents, runner=runner)

    assert len(removed) == 2
    assert not (agents / f"{SYNC_LABEL}.plist").exists()
    assert all(call[1] in ("bootout", "unload") for call in runner.calls)


def test_uninstall_is_safe_when_nothing_installed(tmp_path, specs):
    assert uninstall(specs, agents_dir=tmp_path / "none", runner=make_runner()) == []


# -- status ------------------------------------------------------------------


def test_status_reports_loaded_state(tmp_path, specs):
    runner = make_runner({"print": 0}, stdout=STATUS_OUTPUT)
    status = agent_status(specs[0], agents_dir=tmp_path, runner=runner)
    assert status.loaded
    assert status.state == "waiting"
    assert status.last_exit_code == 0
    assert status.runs == 7


def test_status_reports_not_loaded(tmp_path, specs):
    runner = make_runner({"print": 1})
    status = agent_status(specs[0], agents_dir=tmp_path, runner=runner)
    assert not status.loaded
    assert not status.installed
    assert status.detail


def test_status_flags_a_failing_last_run(tmp_path, specs):
    output = STATUS_OUTPUT.replace("last exit code = 0", "last exit code = 1")
    runner = make_runner({"print": 0}, stdout=output)
    status = agent_status(specs[0], agents_dir=tmp_path, runner=runner)
    assert status.last_exit_code == 1


# -- tail --------------------------------------------------------------------


def test_tail_reports_a_missing_log(tmp_path):
    assert "no log yet" in tail(tmp_path / "absent.log")


def test_tail_returns_the_last_lines(tmp_path):
    log = tmp_path / "sync.log"
    log.write_text("\n".join(f"line {index}" for index in range(100)), encoding="utf-8")
    text = tail(log, lines=5)
    assert text.endswith("line 99")
    assert "earlier lines" in text
    assert "line 95" in text
    assert "line 94" not in text


def test_tail_returns_everything_when_short(tmp_path):
    log = tmp_path / "short.log"
    log.write_text("only line", encoding="utf-8")
    assert tail(log, lines=40) == "only line"
