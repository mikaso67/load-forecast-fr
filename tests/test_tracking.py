"""Guard the shared MLflow configuration against divergent server addresses."""

import ast
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from load_forecast.tracking import DEFAULT_TRACKING_URI, mlflow_tracking_uri


@pytest.mark.parametrize('configured', [None, '', 'http://tracking.example:7000/prefix///'])
def test_tracking_uri_reads_current_environment(configured, monkeypatch):
    monkeypatch.delenv('MLFLOW_TRACKING_URI', raising=False)
    assert mlflow_tracking_uri() == DEFAULT_TRACKING_URI
    if configured is not None:
        monkeypatch.setenv('MLFLOW_TRACKING_URI', configured)
    assert mlflow_tracking_uri() == (configured or DEFAULT_TRACKING_URI).rstrip('/')
    monkeypatch.setenv('MLFLOW_TRACKING_URI', 'http://another.example:7001')
    assert mlflow_tracking_uri() == 'http://another.example:7001'


def test_no_fixed_mlflow_address_outside_shared_default():
    root = Path(__file__).resolve().parents[1]
    defaults = []
    violations = []
    for folder in ('src', 'scripts'):
        for path in (root / folder).rglob('*.py'):
            tree = ast.parse(path.read_text(encoding='utf-8'))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                    continue
                if not node.value.startswith(('http://', 'https://')):
                    continue
                url = urlsplit(node.value)
                if url.hostname not in ('127.0.0.1', 'localhost', '::1') or url.port != 5000:
                    continue
                location = (path.relative_to(root).as_posix(), node.lineno)
                if location[0] == 'src/load_forecast/tracking.py' and node.value == DEFAULT_TRACKING_URI:
                    defaults.append(location)
                else:
                    violations.append(location)
    assert not violations, f'Fixed MLflow addresses outside the shared default: {violations}'
    assert len(defaults) == 1
