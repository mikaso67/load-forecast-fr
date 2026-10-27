"""Exercise cancellation through the real Prefect client on an isolated HTTP transport."""

import asyncio
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
from prefect.client.orchestration import PrefectClient, get_client
from prefect.client.schemas.objects import FlowRun
from prefect.client.schemas.responses import DeploymentResponse
from prefect.settings import PREFECT_SERVER_ANALYTICS_ENABLED, temporary_settings
from prefect.states import (
    AwaitingConcurrencySlot,
    Cancelling,
    Completed,
    Pending,
    Running,
    Scheduled,
)
from prefect.testing.utilities import prefect_test_harness

from load_forecast import ops

NOW = datetime(2026, 10, 21, 14, tzinfo=UTC)


def test_real_prefect_command_releases_orphan_deployment_slot():
    with (temporary_settings({PREFECT_SERVER_ANALYTICS_ENABLED: False}),
          prefect_test_harness(server_startup_timeout=60), get_client(sync_client=True) as client):
        flow_id = client.create_flow_from_name('collect_consumption')
        deployment_id = client.create_deployment(flow_id, name='collect_consumption',
            entrypoint='src/load_forecast/flows.py:collect_consumption', concurrency_limit=1)
        orphan = client.create_flow_run_from_deployment(deployment_id, state=Scheduled())
        assert client.set_flow_run_state(orphan.id, Pending()).state.type == ops.StateType.PENDING
        scheduled = client.create_flow_run_from_deployment(deployment_id, state=Scheduled())
        waiting = client.create_flow_run_from_deployment(deployment_id, state=Scheduled())
        assert client.set_flow_run_state(waiting.id, Pending()).state.name == 'AwaitingConcurrencySlot'
        assert client.read_deployment(deployment_id).global_concurrency_limit.active_slots == 1
        environment = os.environ | {'PREFECT_API_URL': str(client.api_url),
            'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        assert environment['PREFECT_API_URL'].startswith('http://127.0.0.1:')
        result = subprocess.run([sys.executable, '-m', 'load_forecast.ops', 'cancel-orphans',
            '--older-than', '0'], env=environment, capture_output=True, text=True, timeout=60)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['cancelled'] == 1
        assert client.read_flow_run(orphan.id).state.type == ops.StateType.CANCELLED
        assert client.read_flow_run(scheduled.id).state.name == 'Scheduled'
        assert client.read_flow_run(waiting.id).state.name == 'AwaitingConcurrencySlot'
        assert client.read_deployment(deployment_id).global_concurrency_limit.active_slots == 0
        assert client.set_flow_run_state(waiting.id, Pending()).state.type == ops.StateType.PENDING


@pytest.mark.parametrize('age,restart', [(0, False), (0, True), (900, False)])
def test_cancel_only_project_stale_active_states(age, restart):
    deployment = DeploymentResponse(id=uuid4(), name='collect_consumption', flow_id=uuid4(),
        entrypoint='src\\load_forecast\\flows.py:collect_consumption')
    foreign = DeploymentResponse(id=uuid4(), name='collect_consumption', flow_id=uuid4(),
        entrypoint='other_project/flows.py:collect_consumption')
    states = [Pending(name='Submitting'), Running(), Cancelling(), Scheduled(),
              AwaitingConcurrencySlot(), Pending(), Running(), Pending(), Completed()]
    runs = [FlowRun(id=uuid4(), flow_id=deployment.flow_id, deployment_id=deployment.id,
                    state=state) for state in states]
    for run in runs:
        run.state.timestamp = NOW - timedelta(seconds=901)
    runs[1].state.timestamp = NOW - timedelta(seconds=2000)
    runs[2].state.timestamp = NOW - timedelta(seconds=2000)
    runs[5].deployment_id = foreign.id
    runs[6].state.timestamp = NOW - timedelta(seconds=30)
    changed = runs[7].model_copy(deep=True)
    changed.state = Scheduled()
    cancelled = []

    def handler(request):
        body = json.loads(request.content) if request.content else {}
        if request.url.path == '/csrf-token':
            return httpx.Response(404, json={'detail': 'CSRF protection is disabled.'})
        if request.url.path == '/deployments/filter':
            assert body['deployments']['name']['any_']
            data = [d.model_dump(mode='json') for d in (deployment, foreign)]
        elif request.url.path == '/flow_runs/filter':
            assert body['flow_runs']['state']['type']['any_'] == (
                ['PENDING', 'RUNNING', 'CANCELLING'] if restart else ['PENDING'])
            assert body['flow_runs']['deployment_id']['any_'] == [str(deployment.id)]
            data = [r.model_dump(mode='json') for r in runs]
        elif request.url.path.endswith('/set_state'):
            cancelled.append(request.url.path.split('/')[2])
            assert body['force'] is True
            assert body['state']['type'] == 'CANCELLED'
            assert 'orphan' in body['state']['message']
            data = {'status': 'ACCEPT', 'state': body['state'], 'details': {}}
        else:
            run_id = request.url.path.split('/')[-1]
            run = next(r for r in runs if str(r.id) == run_id)
            data = (changed if run.id == changed.id else run).model_dump(mode='json')
        return httpx.Response(200, json=data)

    async def exercise():
        async with PrefectClient('http://ops.test', httpx_settings={
                'transport': httpx.MockTransport(handler)}) as client:
            if restart:
                return await ops.cancel_orphans(client, older_than=age, now=NOW, restart=True)
            return await ops.cancel_orphans(client, older_than=age, now=NOW)

    result = asyncio.run(exercise())
    expected = runs[:3] + [runs[6]] if restart else [runs[0]]
    assert set(cancelled) == {str(r.id) for r in expected}
    assert result['cancelled'] == len(expected)


def test_command_refuses_implicit_server_and_negative_age(monkeypatch):
    monkeypatch.delenv('PREFECT_API_URL', raising=False)
    monkeypatch.setattr('sys.argv', ['ops', 'cancel-orphans', '--older-than', '0'])
    with pytest.raises(SystemExit, match='PREFECT_API_URL'):
        ops.main()
    monkeypatch.setenv('PREFECT_API_URL', 'http://127.0.0.1:4200/api')
    monkeypatch.setattr('sys.argv', ['ops', 'cancel-orphans', '--older-than', '-1'])
    with pytest.raises(SystemExit):
        ops.main()
    monkeypatch.setattr('sys.argv', ['ops', 'cancel-orphans', '--older-than', '900', '--restart'])
    with pytest.raises(SystemExit):
        ops.main()


def test_cleanup_pages_before_mutating_and_accepts_exact_age_boundary():
    deployment = DeploymentResponse(name='reconcile_deliveries', flow_id=uuid4(),
        entrypoint='load_forecast.flows:reconcile_deliveries')
    runs = [FlowRun(flow_id=deployment.flow_id, deployment_id=deployment.id,
                    state=Pending()) for _ in range(201)]
    for run in runs:
        run.state.timestamp = NOW - timedelta(seconds=900)
    by_id = {r.id: r for r in runs}
    client = SimpleNamespace(read_deployments=AsyncMock(return_value=[deployment]),
        read_flow_runs=AsyncMock(side_effect=[runs[:200], runs[200:]]),
        read_flow_run=AsyncMock(side_effect=lambda key: by_id[key]))

    async def cancel(*args, **kwargs):
        assert client.read_flow_runs.await_count == 2
        return SimpleNamespace(state=SimpleNamespace(type=ops.StateType.CANCELLED))
    client.set_flow_run_state = AsyncMock(side_effect=cancel)
    assert asyncio.run(ops.cancel_orphans(client, older_than=900, now=NOW))['cancelled'] == 201
    assert [c.kwargs['offset'] for c in client.read_flow_runs.call_args_list] == [0, 200]


def test_watchdog_cleanup_is_after_health_before_runner():
    script = (Path(__file__).resolve().parents[1] / 'scripts/start_production.ps1').read_text()
    healthy = script.index('if ($Runner -and (Test-PrefectHealth -Attempts 15))')
    restart = script.index('if (-not (Test-PrefectHealth -Attempts 60))')
    runner = script.index("@('-m', 'load_forecast.flows', 'serve')")
    assert healthy < script.index('Invoke-OrphanRecovery -OlderThan 900') < restart
    assert restart < script.index('Invoke-OrphanRecovery -OlderThan 0 -Restart') < runner


@pytest.mark.parametrize('healthy,age', [(True, 900), (False, 0)])
@pytest.mark.parametrize('failure', ['none', 'exit', 'throw', 'stderr', 'stderr_ok'])
def test_watchdog_branches_execute_cleanup_without_live_processes(tmp_path, healthy, age, failure):
    root = Path(__file__).resolve().parents[1]
    script = (root / 'scripts/start_production.ps1').read_text()
    python_line = next(line for line in script.splitlines() if line.startswith('$PythonPath ='))
    script = script.replace(python_line, "$PythonPath = 'Invoke-TestPython'")
    workspace = tmp_path / 'workspace'
    (workspace / 'scripts').mkdir(parents=True)
    (workspace / 'scripts/load_env.ps1').write_text('')
    (workspace / 'start.ps1').write_text(script)
    trace = tmp_path / 'trace.txt'
    harness = r'''
$env:LFF_RUNTIME_DIR = Join-Path $args[0] 'runtime'
$env:POSTGRES_USER = 'fixture'
$env:POSTGRES_PASSWORD = 'fixture'
$global:Trace = $args[1]
$global:Healthy = [bool]::Parse($args[2])
$global:FailCleanup = $args[3]
function git { 'fixture' }
function docker { $global:LASTEXITCODE = 0 }
function Test-NetConnection { $true }
function Start-Sleep { }
function Invoke-RestMethod { Add-Content $global:Trace 'health'; return $true }
function Get-CimInstance {
    if ($global:Healthy) {
        [pscustomobject]@{CommandLine='python -m load_forecast.flows serve'; ProcessId=99999}
    }
}
function Get-NetTCPConnection { }
function Stop-Process { throw 'Unexpected process stop' }
function Start-Process {
    param($FilePath, $ArgumentList, $WindowStyle, $WorkingDirectory,
          $RedirectStandardOutput, $RedirectStandardError)
    if ($ErrorActionPreference -ne 'Stop') { throw 'Error preference leaked into startup' }
    Add-Content $global:Trace ('start:' + ($ArgumentList -join ' '))
}
function Invoke-TestPython {
    Add-Content $global:Trace ('cleanup:' + ($args -join ' '))
    if ($global:FailCleanup -eq 'throw') { throw 'fixture cleanup error' }
    if ($global:FailCleanup -in @('stderr', 'stderr_ok')) {
        $NativeExit = if ($global:FailCleanup -eq 'stderr_ok') { 0 } else { 1 }
        & powershell.exe -NoProfile -NonInteractive -Command "[Console]::Error.WriteLine('fixture cleanup error'); exit $NativeExit"
        return
    }
    if ($global:FailCleanup -eq 'exit') { Write-Output 'fixture cleanup error' }
    $global:LASTEXITCODE = [int]($global:FailCleanup -eq 'exit')
}
& (Join-Path $args[0] 'start.ps1') -WorkspacePath $args[0]
'''
    harness_file = tmp_path / 'harness.ps1'
    harness_file.write_text(harness)
    result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-File',
        str(harness_file), str(workspace), str(trace), str(healthy), str(failure)],
        capture_output=True, text=True, timeout=30)
    events = trace.read_text().splitlines()
    cleanup = f'cleanup:-m load_forecast.ops cancel-orphans --older-than {age}'
    if not healthy:
        cleanup += ' --restart'
    assert cleanup in events
    assert events.index('health') < events.index(cleanup)
    if not healthy:
        assert events.index(cleanup) < events.index('start:-m load_forecast.flows serve')
    else:
        assert 'start:-m load_forecast.flows serve' not in events
    assert result.returncode == 0, result.stderr
    if failure not in ('none', 'stderr_ok'):
        log = (workspace / 'runtime/logs/orphan_recovery.log').read_text(encoding='utf-8-sig')
        assert 'fixture cleanup error' in log
        incidents = list((workspace / 'runtime/incidents').glob('*.json'))
        assert len(incidents) == 1
        incident = json.loads(incidents[0].read_text(encoding='utf-8-sig'))
        assert incident['event'] == 'orphan_recovery_failed'
        assert incident['older_than'] == age
        assert incident['restart'] == (not healthy)
    else:
        assert not list((workspace / 'runtime/incidents').glob('*'))
        if failure == 'stderr_ok':
            log = (workspace / 'runtime/logs/orphan_recovery.log').read_text(encoding='utf-8-sig')
            assert 'fixture cleanup error' in log
