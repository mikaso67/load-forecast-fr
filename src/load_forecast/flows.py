"""Native Windows Prefect flows and Paris-time deployments (D17)."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import date, datetime, timedelta

from prefect import flow, serve
from prefect.schedules import Cron

from load_forecast.cutoff import PARIS
from load_forecast.pipeline import make_runtime

_RUNTIME = ContextVar('pipeline_runtime', default=None)


@contextmanager
def use_runtime(runtime):
    """Supply an isolated runtime for an offline rehearsal without starting a Prefect server."""
    token = _RUNTIME.set(runtime)
    try:
        yield
    finally:
        _RUNTIME.reset(token)


def current_runtime():
    return _RUNTIME.get() or make_runtime()


def delivery_day(day: date | None, offset: int = 1) -> date:
    """Resolve schedule parameters from the current Paris date, never the host time zone."""
    return day or (datetime.now(PARIS).date() + timedelta(days=offset))


@flow(name='collect_consumption', retries=2, retry_delay_seconds=60)
def collect_consumption(day: date | None = None) -> dict:
    return current_runtime().collect(delivery_day(day, 0))


@flow(name='stage_weather', retries=2, retry_delay_seconds=60)
def stage_weather(day: date | None = None) -> dict:
    return current_runtime().stage(delivery_day(day))


@flow(name='prepare_fallback', retries=1, retry_delay_seconds=60)
def prepare_fallback(day: date | None = None, offset: int = 2) -> dict:
    runtime, target = current_runtime(), delivery_day(day, offset)
    try:
        return runtime.prepare_fallback(target)
    except Exception:
        runtime.alert('fallback_preparation_failed', target)
        raise


@flow(name='daily_forecast', retries=0)
def daily_forecast(day: date | None = None) -> list[dict]:
    runtime, target = current_runtime(), delivery_day(day)
    runtime.reconcile()
    # Independent state histories run concurrently so the shadow cannot hold up publication.
    with ThreadPoolExecutor(max_workers=2) as workers:
        futures = [workers.submit(runtime.forecast, target, variant) for variant in ('B', 'A')]
        results, errors = [], []
        for future in futures:
            try:
                results.append(future.result())
            except Exception as error:
                errors.append(error)
    if errors:
        raise errors[0]
    runtime.reconcile()
    try:
        choice = runtime.authority.read_choice(target)
    except FileNotFoundError:
        choice = None
    if choice is not None and choice['kind'] in ('primary', 'fallback'):
        runtime.publish_site(target)
    return results


@flow(name='reconcile_deliveries', retries=2, retry_delay_seconds=30)
def reconcile_deliveries() -> dict:
    return current_runtime().reconcile()


@flow(name='score_and_monitor', retries=2, retry_delay_seconds=60)
def score_and_monitor(day: date | None = None) -> dict:
    runtime = current_runtime()
    runtime.reconcile()
    target = delivery_day(day, -1)
    result = runtime.score(target)
    runtime.publish_site(target)
    return result


@flow(name='consolidated_refresh', retries=2, retry_delay_seconds=60)
def consolidated_refresh(day: date | None = None, dry_run: bool = False) -> dict:
    runtime, target = current_runtime(), delivery_day(day, 0)
    result = runtime.refresh(target)
    runtime.reconcile()
    return {'collector': result, 'scoring': runtime.score(target),
            'prune': runtime.prune(target, dry_run=dry_run)}


SCHEDULES = {
    'collect_consumption': [('*/30 * * * *', {}), ('55 11 * * *', {})],
    'stage_weather': [('30 5 * * *', {}), ('0,30 6-10 * * *', {}), ('0,30 11 * * *', {})],
    'prepare_fallback': [('0 13 * * *', {'offset': 2}), ('0 6 * * *', {'offset': 1})],
    'daily_forecast': [('0 12 * * *', {})],
    'reconcile_deliveries': [('*/10 * * * *', {})],
    'score_and_monitor': [('0 8 * * *', {})],
    'consolidated_refresh': [('0 2 * * 1', {})],
}


def deployments() -> list:
    """Build deployment descriptions without registering or starting any process."""
    return [globals()[name].to_deployment(name=name, concurrency_limit=1,
                schedules=[Cron(cron, timezone='Europe/Paris', parameters=parameters)
                           for cron, parameters in schedules])
            for name, schedules in SCHEDULES.items()]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode', choices=('serve', 'dry-run'))
    parser.add_argument('--day', type=date.fromisoformat)
    args = parser.parse_args()
    if args.mode == 'serve':
        serve(*deployments(), pause_on_shutdown=True)
    else:
        if args.day is None:
            parser.error('dry-run requires --day')
        from load_forecast.pipeline_dry_run import dry_run
        dry_run(args.day)


if __name__ == '__main__':
    main()
