"""Conservative retention of trusted local production artifacts."""

import hashlib
import json
import logging
import pickle
import re
import shutil
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np

from load_forecast.gam_model import SlotGAM
from load_forecast.linear_model import SlotOLS
from load_forecast.pipeline_artifacts import read_artifact, write_artifact
from load_forecast.production import WeeklyModels

LOGGER = logging.getLogger(__name__)


def _refit(root: Path, variant: str, refit: date, identity: str) -> tuple[Path, WeeklyModels]:
    if variant not in ('A', 'B') or not re.fullmatch(r'[A-Za-z0-9_-]+', identity):
        raise ValueError('Invalid refit identity or variant')
    candidates = [root / 'refits' / variant / f'{refit}.pkl',
                  root / 'refit_attempts' / f'{identity}.pkl']
    for path in candidates:
        if path.exists():
            prepared = read_artifact(path)
            models = prepared.models
            if (prepared.variant == variant and models.refit == refit
                    and models.identity == identity):
                return path, models
    raise FileNotFoundError('No kept refit matches the selected state model identity')


def model_signature(models: WeeklyModels) -> str:
    """Hash prediction parameters, excluding mutable statsmodels and booster caches."""
    def slot_parameters(model):
        if isinstance(model, SlotOLS):
            return ['ols', model.columns_, model.coef_.tolist()]
        if isinstance(model, SlotGAM):
            smoother = model.result_.model.smoother
            return ['gam', model.smooth_cols, model.linear_cols_, model.missing_as_nan,
                    model.lower_.to_dict(), model.upper_.to_dict(),
                    model.slope_lower_, model.slope_upper_,
                    np.asarray(model.result_.params).tolist(),
                    [[s.degree, s.knots.tolist(), s.include_intercept,
                      None if s.ctransf is None else s.ctransf.tolist()] for s in smoother.smoothers]]
        raise TypeError('Unsupported model type in retention comparison')
    gbm = models.gbm
    parameters = [str(models.refit), models.identity,
                  {name: {slot: slot_parameters(model) for slot, model in slots.items()}
                   for name, slots in models.slots.items()},
                  None if gbm is None else [gbm.columns_, gbm.booster_.model_to_string()]]
    return hashlib.sha256(json.dumps(parameters, sort_keys=True, allow_nan=False).encode()).hexdigest()


def kept_models(root: Path, variant: str, refit: date, identity: str, digest: str,
                reference: Path | None = None) -> WeeklyModels:
    """Verify a pinned replacement archive or the original model bytes before loading."""
    path, models = _refit(root, variant, refit, identity)
    if reference is not None and reference.exists():
        proof = read_artifact(reference)
        if (proof['original_sha256'] != digest or proof['refit_path'] != str(path)
                or proof['refit_sha256'] != hashlib.sha256(path.read_bytes()).hexdigest()
                or proof['signature'] != model_signature(models)):
            raise ValueError('Kept refit differs from its pinned retention reference')
    elif hashlib.sha256(pickle.dumps(models, protocol=pickle.HIGHEST_PROTOCOL)).hexdigest() != digest:
        raise ValueError('Kept refit needs a verified retention reference')
    return models


def prune_artifacts(root: Path, selected: list[dict], now: datetime, *,
                    protected: set[str] | None = None, dry_run: bool = True) -> dict:
    """Plan and optionally apply retention, preserving every selected state and kept refit."""
    root = root.resolve()
    if now.tzinfo is None:
        raise ValueError('Retention requires an aware timestamp')
    # Reject links before planning any mutation, including Windows junctions.
    for parent in ('runs', 'inputs', 'refits', 'refit_attempts'):
        folder = root / parent
        paths = [folder, *folder.rglob('*')] if folder.exists() else []
        if any(p.is_symlink() or p.is_junction() or not p.resolve().is_relative_to(root)
               for p in paths):
            raise ValueError('Retention refuses linked or escaping artifact paths')
    by_run = {row['run_id']: row for row in selected}
    latest = {variant: max((r for r in selected if r['variant'] == variant),
                          key=lambda r: r['day'], default=None) for variant in ('A', 'B')}
    keep_full = {row['run_id'] for row in latest.values() if row} | (protected or set())
    plan: list[tuple[Path, str]] = []
    proofs = {}
    today = now.astimezone(ZoneInfo('Europe/Paris')).date()
    for folder in sorted((root / 'runs').glob('*')):
        if not folder.is_dir() or folder.name in keep_full:
            continue
        row = by_run.get(folder.name)
        if row is None:
            newest = max([folder.stat().st_mtime, *(p.stat().st_mtime for p in folder.rglob('*'))])
            if newest < (now - timedelta(days=28)).timestamp():
                plan.append((folder, 'unselected run older than 28 days'))
        elif row['day'] < today - timedelta(days=14):
            state_path = folder / f"state_{row['day']}.pkl"
            payload = read_artifact(state_path, row['sha256'])
            expected = folder / f"models_{payload['refit']}_{payload['models_identity']}.pkl"
            if expected.exists():
                raw = expected.read_bytes()
                if hashlib.sha256(raw).hexdigest() != payload['models_sha256']:
                    raise ValueError('Run model hash differs from selected state')
                source, models = _refit(root, row['variant'], payload['refit'], payload['models_identity'])
                signature = model_signature(models)
                if signature != model_signature(pickle.loads(raw)):
                    raise ValueError('Kept refit prediction parameters differ from selected models')
                proofs[expected] = (folder / f"refit_{payload['models_identity']}.pkl",
                                    {'original_sha256': payload['models_sha256'],
                                     'refit_path': str(source), 'signature': signature,
                                     'refit_sha256': hashlib.sha256(source.read_bytes()).hexdigest()})
                plan.append((expected, 'duplicate models verified in kept refit'))
    monday = today - timedelta(days=today.weekday())
    for path in sorted((root / 'inputs').glob('*.pkl')):
        day = date.fromisoformat(path.stem)
        if day < today - timedelta(days=14) and day not in (monday, monday - timedelta(days=7)):
            plan.append((path, 'frozen inputs older than 14 days'))
    for path, reason in plan:
        LOGGER.info('%s %s: %s', 'Would delete' if dry_run else 'Deleting', path, reason)
        if not dry_run:
            if path in proofs:
                reference, proof = proofs[path]
                write_artifact(reference, proof)
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
    return {'dry_run': dry_run, 'paths': [str(p) for p, _ in plan], 'count': len(plan)}
