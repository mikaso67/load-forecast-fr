"""Recover orphan runs of the local production deployments through the Prefect API."""

import argparse
import asyncio
import json
import os
from datetime import UTC, datetime

from prefect.client.orchestration import PrefectClient, get_client
from prefect.client.schemas.filters import DeploymentFilter, FlowRunFilter
from prefect.states import Cancelled, StateType

ACTIVE = (StateType.PENDING, StateType.RUNNING, StateType.CANCELLING)


async def cancel_orphans(client: PrefectClient, *, older_than: int,
                         now: datetime | None = None, restart: bool = False) -> dict:
    """Cancel stale Pending states; include running work only after runner shutdown."""
    from load_forecast.flows import SCHEDULES

    if older_than < 0:
        raise ValueError('Age must be nonnegative')
    if restart and older_than != 0:
        raise ValueError('Restart recovery requires age zero and a stopped runner')
    states = ACTIVE if restart else (StateType.PENDING,)
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError('Current time must be timezone aware')
    deployment_filter = DeploymentFilter(name={'any_': list(SCHEDULES)})
    deployments, offset = [], 0
    while True:
        page = await client.read_deployments(deployment_filter=deployment_filter,
                                             limit=200, offset=offset)
        deployments.extend(page)
        if len(page) < 200:
            break
        offset += len(page)
    ids = []
    for deployment in deployments:
        entry = (deployment.entrypoint or '').replace('\\', '/')
        if (deployment.name in SCHEDULES and entry in (
                f'src/load_forecast/flows.py:{deployment.name}',
                f'load_forecast.flows:{deployment.name}')):
            ids.append(deployment.id)
    if not ids:
        return {'cancelled': 0, 'deployments': 0}
    run_filter = FlowRunFilter(deployment_id={'any_': ids},
                               state={'type': {'any_': list(states)}})
    runs, offset = [], 0
    # Collect before mutating: cancelling while paging would shift the filtered result set.
    while True:
        page = await client.read_flow_runs(flow_run_filter=run_filter, limit=200, offset=offset)
        runs.extend(page)
        if len(page) < 200:
            break
        offset += len(page)
    cancelled = 0
    for candidate in runs:
        run = await client.read_flow_run(candidate.id)
        state = run.state
        if (run.deployment_id not in ids or state is None or state.type not in states
                or state.name == 'AwaitingConcurrencySlot'
                or state.id != candidate.state.id
                or (now - state.timestamp).total_seconds() < older_than):
            continue
        result = await client.set_flow_run_state(run.id, Cancelled(message=(
            f'Operational orphan recovery: {state.name} state older than {older_than}s.')),
            force=True)
        if result.state is None or result.state.type != StateType.CANCELLED:
            raise RuntimeError(f'Cancellation not confirmed for run {run.id}')
        cancelled += 1
    return {'cancelled': cancelled, 'deployments': len(ids)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('cancel-orphans',))
    parser.add_argument('--older-than', type=int, required=True)
    parser.add_argument('--restart', action='store_true',
                        help='Include Running/Cancelling only after stopping the runner (age 0)')
    args = parser.parse_args()
    if args.older_than < 0:
        parser.error('--older-than must be nonnegative')
    if args.restart and args.older_than != 0:
        parser.error('--restart requires --older-than 0 and a stopped runner')
    if not os.environ.get('PREFECT_API_URL'):
        raise SystemExit('PREFECT_API_URL is required; no implicit server is started')

    async def execute():
        async with get_client() as client:
            return await cancel_orphans(client, older_than=args.older_than, restart=args.restart)

    print(json.dumps(asyncio.run(execute())))


if __name__ == '__main__':
    main()
