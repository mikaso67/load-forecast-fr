"""Report moves preserve pinned evidence and the single-test lock."""

import importlib
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from load_forecast import experiment_reports

ROOT = Path(__file__).resolve().parents[1]


def test_committed_report_uses_path_in_pinned_tree(monkeypatch):
    run = Mock(side_effect=[
        subprocess.CompletedProcess([], 0, 'archive/EXP-007.md\ndocs/other.md\n'),
        subprocess.CompletedProcess([], 0, 'frozen choice'),
    ])
    monkeypatch.setattr(experiment_reports.subprocess, 'run', run)
    assert experiment_reports.committed_report('pinned', 'EXP-007.md') == 'frozen choice'
    assert run.call_args_list[0].args[0][-1] == 'pinned'
    assert run.call_args_list[1].args[0] == ['git', 'show', 'pinned:archive/EXP-007.md']


@pytest.mark.parametrize('paths', ['', 'a/EXP-007.md\nb/EXP-007.md\n'])
def test_missing_or_ambiguous_report_refuses(paths, monkeypatch):
    run = Mock(return_value=subprocess.CompletedProcess([], 0, paths))
    monkeypatch.setattr(experiment_reports.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='exactly one'):
        experiment_reports.committed_report('pinned', 'EXP-007.md')
    assert run.call_count == 1


def test_final_test_completed_protocol_refuses_before_any_data_or_http(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    runner = importlib.import_module('run_test2025')
    protocol = (ROOT / 'docs/experiments/TEST-2025.md').read_text(encoding='utf-8')
    monkeypatch.setattr(runner, 'git', lambda *args: '' if args[0] == 'status' else protocol)
    read = Mock(side_effect=AssertionError('Must not read test data'))
    http = Mock(side_effect=AssertionError('Must not contact tracking'))
    monkeypatch.setattr(runner, 'load_consumption', read)
    monkeypatch.setattr(runner, 'mlflow_runs_of_test', http)
    with pytest.raises(RuntimeError, match='not a results-free protocol'):
        runner.final_run()
    read.assert_not_called()
    http.assert_not_called()


def test_rehearsal_dispatch_bypasses_final_test_guard(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts'))
    runner = importlib.import_module('run_test2025')
    rehearsal = Mock()
    final = Mock(side_effect=AssertionError('Must not start final run'))
    monkeypatch.setattr(runner, 'rehearsal', rehearsal)
    monkeypatch.setattr(runner, 'final_run', final)
    monkeypatch.setattr(sys, 'argv', ['run_test2025.py', '--rehearsal'])
    runner.main()
    rehearsal.assert_called_once_with()
    final.assert_not_called()
