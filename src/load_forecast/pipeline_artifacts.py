"""Immutable trusted local artifacts for production jobs, states and late refits."""

import hashlib
import os
import pickle
from pathlib import Path
from uuid import uuid4

from load_forecast.serving import _no_replace


def write_artifact(path: Path, value: object) -> str:
    """Publish a pickle once; a retry must supply exactly the same payload."""
    payload = pickle.dumps(value, protocol=pickle.HIGHEST_PROTOCOL)
    digest = hashlib.sha256(payload).hexdigest()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f'{path.name}.{uuid4().hex}.tmp')
    with temporary.open('xb') as stream:
        stream.write(payload)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        _no_replace(temporary, path)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise ValueError('Conflicting immutable pipeline artifact') from None
    finally:
        temporary.unlink(missing_ok=True)
    return digest


def read_artifact(path: Path, digest: str | None = None) -> object:
    """Read only a pipeline-owned trusted path, optionally checking its recorded SHA256."""
    payload = path.read_bytes()
    if digest is not None and hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError('Pipeline artifact hash mismatch')
    return pickle.loads(payload)
