"""Protect the shadow runner's isolation and original CI command sequence."""

import importlib.util
import json
import os
import signal
import time
from pathlib import Path
import subprocess
import sys

import pytest


spec = importlib.util.spec_from_file_location(
    "buildkite_shadow", Path(__file__).resolve().parents[1] / "scripts/buildkite/check.py"
)
shadow = importlib.util.module_from_spec(spec)
spec.loader.exec_module(shadow)


def test_child_environment_isolates_homes_and_drops_credentials(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/real/hermes")
    monkeypatch.setenv("PYPI_TOKEN", "must-not-be-inherited")
    monkeypatch.setenv("PYTHONPATH", "/real/python")
    monkeypatch.setenv("PIP_INDEX_URL", "https://credentialed.example.invalid")
    env = shadow.isolated_environment(tmp_path)
    for key in ("HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TMPDIR"):
        assert Path(env[key]).is_relative_to(tmp_path)
        assert Path(env[key]).is_dir()
    for key in ("PYPI_TOKEN", "PYTHONPATH", "PIP_INDEX_URL"):
        assert key not in env


def test_preserves_stages_and_disables_real_hermes_activation(tmp_path, monkeypatch):
    monkeypatch.setattr(shadow, "ROOT", tmp_path)
    calls = []

    def fake_run(command, env, **kwargs):
        calls.append((command, dict(env)))
        if command[:3] == ["python", "-m", "build"]:
            output = Path(command[4])
            output.mkdir()
            (output / "plugin.whl").write_bytes(b"test wheel")

    monkeypatch.setattr(shadow, "run", fake_run)
    shadow.run_lane()
    commands = [command for command, _ in calls]
    assert commands[1:6] == [
        ["python", "-m", "pip", "install", "--upgrade", "pip"],
        ["python", "-m", "pip", "install", "-e", ".[dev]"],
        ["python", "-m", "pytest"],
        ["python", "-m", "py_compile", "__init__.py", "cli.py",
         "src/hermes_muninndb_plugin/__init__.py", "src/hermes_muninndb_plugin/cli.py"],
        ["python", "-m", "build", "--outdir", str(Path(calls[0][1]["HOME"]).parent / "dist")],
    ]
    installer = commands[-2]
    assert installer[0].endswith("wheel-venv/bin/hermes-muninndb-install")
    assert installer[1:] == ["--no-activate", "--json"]
    assert calls[-2][1]["VIRTUAL_ENV"].endswith("wheel-venv")
    assert calls[-2][1]["PATH"].split(":")[0].endswith("wheel-venv/bin")
    homes = {env["HOME"] for _, env in calls}
    assert len(homes) == 1
    assert not Path(homes.pop()).exists()  # Temporary job state is cleaned up.


def test_command_failure_preserves_status(monkeypatch):
    def fail():
        raise subprocess.CalledProcessError(23, ["python", "-m", "pytest"])

    monkeypatch.setattr(shadow, "run_lane", fail)
    monkeypatch.setattr(shadow, "runtime_matches", lambda lane: True)
    assert shadow.main(["3.11"]) == 23


@pytest.mark.parametrize("built_wheels", [("current.whl",), (), ("first.whl", "second.whl")],
                         ids=["stale-checkout-wheel", "zero-wheels", "multiple-wheels"])
def test_private_build_uses_exactly_one_current_wheel(tmp_path, monkeypatch, built_wheels):
    project = tmp_path / "project"
    checkout_dist = project / "dist"
    checkout_dist.mkdir(parents=True)
    stale = checkout_dist / "000-stale.whl"
    stale.write_bytes(b"earlier build")
    monkeypatch.setattr(shadow, "ROOT", project)
    monkeypatch.setattr(shadow, "runtime_matches", lambda lane: True)
    installed = []
    homes = []

    def fake_run(command, env, **kwargs):
        homes.append(Path(env["HOME"]))
        if command[:3] == ["python", "-m", "build"]:
            output = Path(command[command.index("--outdir") + 1]) if "--outdir" in command else checkout_dist
            output.mkdir(exist_ok=True)
            for name in built_wheels:
                (output / name).write_bytes(b"current build")
        if command[0].endswith("wheel-venv/bin/pip"):
            wheel = Path(command[-1])
            installed.append((wheel, wheel.read_bytes()))

    monkeypatch.setattr(shadow, "run", fake_run)
    assert shadow.main(["3.11"]) == (0 if len(built_wheels) == 1 else 1)
    if len(built_wheels) == 1:
        assert len(installed) == 1
        wheel, contents = installed[0]
        assert wheel == homes[0].parent / "dist" / built_wheels[0]
        assert contents == b"current build"
        assert not wheel.exists()
    else:
        assert installed == []
    assert stale.read_bytes() == b"earlier build"
    assert list(checkout_dist.iterdir()) == [stale]
    assert homes and all(not home.parent.exists() for home in homes)


def test_wrong_runtime_is_rejected_before_any_commands(monkeypatch):
    def unexpected():
        pytest.fail("Wrong-runtime validation must not run commands")

    monkeypatch.setattr(shadow, "run_lane", unexpected)
    other_lane = "3.12" if sys.version_info.minor == 11 else "3.11"
    assert shadow.main([other_lane]) == 1


def wait_until(predicate, timeout=8):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.02)
    pytest.fail("Timed out waiting for subprocess fixture")


def process_running(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    # A terminated orphan awaiting init is not executing; the controller reaps
    # the container's process tree after the wrapper has reaped its direct child.
    stat = Path(f"/proc/{pid}/stat")
    return not (stat.exists() and stat.read_text().split()[2] == "Z")


def subprocess_fixture(tmp_path, mode):
    """Use actual wrapper control flow with local fake pip/pytest executables."""
    project = tmp_path / "project"
    project.mkdir()
    temporary = tmp_path / "temporary"
    temporary.mkdir()
    ready = tmp_path / "ready.json"
    heartbeat = tmp_path / "heartbeat"
    commands = tmp_path / "commands"
    fake = tmp_path / "python-fixture"
    child_code = (
        "import pathlib,signal,time; "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN); "
        "signal.signal(signal.SIGINT,signal.SIG_IGN); "
        f"p=pathlib.Path({str(heartbeat)!r}); "
        'exec("while True:\\n p.write_text(str(time.time_ns()))\\n time.sleep(.02)")'
    )
    fake.write_text(f"""#!{sys.executable}
import json,os,pathlib,signal,subprocess,sys,time
args=sys.argv[1:]
with pathlib.Path({str(commands)!r}).open('a') as log:log.write(' '.join(args)+'\\n')
if args[:2]==['-m','venv']:
    bindir=pathlib.Path(args[2])/'bin';bindir.mkdir(parents=True)
    (bindir/'python').symlink_to({str(fake)!r})
elif args==['-m','pytest']:
    signal.signal(signal.SIGTERM,signal.SIG_IGN)
    signal.signal(signal.SIGINT,signal.SIG_IGN)
    child=subprocess.Popen([{sys.executable!r},'-I','-c',{child_code!r}])
    ready=pathlib.Path({str(ready)!r});pending=ready.with_suffix('.tmp')
    pending.write_text(json.dumps({{'pid':os.getpid(),'pgid':os.getpgrp(),'child':child.pid,'env':dict(os.environ)}}));pending.replace(ready)
    if {mode!r}=='failure':sys.exit(23)
    while True:time.sleep(.05)
""")
    fake.chmod(0o755)
    driver = tmp_path / "driver.py"
    driver.write_text(f"""import importlib.util,sys
from pathlib import Path
spec=importlib.util.spec_from_file_location('wrapper',{str(Path(shadow.__file__).resolve())!r})
wrapper=importlib.util.module_from_spec(spec);spec.loader.exec_module(wrapper)
wrapper.ROOT=Path({str(project)!r})
wrapper.sys.executable={str(fake)!r}
wrapper.runtime_matches=lambda lane:True
raise SystemExit(wrapper.main(['3.11']))
""")
    env = {"PATH": os.defpath, "HOME": str(tmp_path / "outside-home"),
           "TMPDIR": str(temporary), "PYPI_TOKEN": "fixture-secret",
           "PIP_INDEX_URL": "https://fixture.invalid", "PYTHONPATH": "/fixture/python",
           "PYTEST_ADDOPTS": "--fixture-invalid", "PYTEST_CURRENT_TEST": "parent fixture",
           "HERMES_HOME": str(tmp_path / "outside-hermes")}
    return driver, env, ready, heartbeat, commands, temporary


@pytest.mark.parametrize("signum", [signal.SIGTERM, signal.SIGINT, None],
                         ids=["term", "int", "command-failure"])
def test_subprocess_cancellation_and_failure_stop_descendants_and_clean_state(tmp_path, signum):
    driver, env, ready, heartbeat, commands, temporary = subprocess_fixture(
        tmp_path, "failure" if signum is None else "wait"
    )
    process = subprocess.Popen([sys.executable, "-I", "-B", str(driver)],
                               env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True)
    record = None
    try:
        wait_until(lambda: ready.exists() and heartbeat.exists())
        record = json.loads(ready.read_text())
        if signum is not None:
            process.send_signal(signum)
            # Repeated cancellation must not interrupt bounded cleanup.
            for _ in range(4):
                time.sleep(.02)
                if process.poll() is None:
                    process.send_signal(signum)
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == (23 if signum is None else 128 + signum), (stdout, stderr)
        wait_until(lambda: not process_running(record['pid']) and not process_running(record['child']))
        observed = heartbeat.read_text()
        time.sleep(.08)
        assert heartbeat.read_text() == observed
        assert list(temporary.iterdir()) == []
        for key in ("HOME", "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "TMPDIR"):
            assert not Path(record['env'][key]).exists()
            assert Path(record['env'][key]).is_relative_to(temporary)
        for key in ("PYPI_TOKEN", "PIP_INDEX_URL", "PYTHONPATH", "PYTEST_ADDOPTS", "PYTEST_CURRENT_TEST"):
            assert key not in record['env']
        assert "-m build" not in commands.read_text()
    finally:
        # Red runs intentionally leave fixture processes alive. Clean only the
        # recorded fixture groups/PIDs so the test cannot leak into later runs.
        if record is None and ready.exists():
            record = json.loads(ready.read_text())
        groups = {process.pid}
        if record:
            groups.add(record['pgid'])
        for group in groups:
            try:
                os.killpg(group, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=5)
