"""Published audit entry points use a read-only snapshot and explicit UTC bounds."""

import json
import runpy
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


class AuditConnection:
    def __init__(self) -> None:
        self.calls = []

    def transaction(self):
        return nullcontext()

    def execute(self, query, params=None):
        self.calls.append((query, params))
        if 'original_metadata' in query:
            rows = [('2024-01-01', 'weather missing', {'failures': {'primary': ['file']}})]
        else:
            rows = [('primary', 1)] if 'GROUP BY status' in query else [(1,)]
        return SimpleNamespace(description=[SimpleNamespace(name='value')],
                               fetchall=lambda: rows, fetchone=lambda: rows[0],
                               __iter__=lambda: iter(rows))


@pytest.mark.parametrize('script', ['audit_eco2mix.py', 'audit_weather.py'])
def test_audit_snapshot_and_bounds(script, monkeypatch, tmp_path, capsys):
    module = runpy.run_path(str(ROOT / 'scripts' / script))
    connection = AuditConnection()
    # The cursor iterator is a protocol method, so supply it on the type.
    original = connection.execute

    class Cursor:
        def __init__(self, cursor):
            self.cursor = cursor
            self.description = cursor.description

        def __iter__(self):
            return iter(self.cursor.fetchall())

        def fetchall(self):
            return self.cursor.fetchall()

        def fetchone(self):
            return self.cursor.fetchone()

    monkeypatch.setattr(connection, 'execute', lambda *args: Cursor(original(*args)))
    globals_ = module['main'].__globals__
    monkeypatch.setitem(globals_, 'connect_database', lambda: nullcontext(connection))
    monkeypatch.setitem(globals_, 'OUTPUT', tmp_path / 'results.json')
    monkeypatch.chdir(tmp_path)
    module['main']()
    assert connection.calls[0][0].endswith('REPEATABLE READ, READ ONLY')
    assert all(query.lstrip().startswith(('SELECT', 'WITH', '--'))
               for query, _ in connection.calls[1:])
    if script == 'audit_eco2mix.py':
        params = [params for _, params in connection.calls if params is not None]
        assert params == [{'start': datetime(2021, 3, 22, 23, tzinfo=UTC),
                           'end': datetime(2026, 10, 3, 22, tzinfo=UTC)}]
        assert set(json.loads((tmp_path / 'results.json').read_text())) == {
            'row_counts', 'q1', 'q2', 'q3', 'q4', 'q5', 'q6', 'dst_sample', 'source_keys'}
    else:
        assert 'Points: 1' in capsys.readouterr().out
