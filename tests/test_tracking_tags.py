"""Run authorship and optional ticket configuration."""

from datetime import date

import pytest

from load_forecast.tracking import backtest_tags


@pytest.mark.parametrize('author', [None, '', 'portfolio-owner'])
def test_backtest_tags_read_environment_at_call_time(author, monkeypatch):
    monkeypatch.delenv('LFF_AUTHOR', raising=False)
    if author is not None:
        monkeypatch.setenv('LFF_AUTHOR', author)
    first, last = date.fromisoformat('2024-01-01'), date.fromisoformat('2024-12-31')
    tags = backtest_tags(first, last, 'EXP-001')
    assert tags == {'author': author or 'developer', 'exp': 'EXP-001',
                    'backtest_start': str(first), 'backtest_end': str(last)}
    monkeypatch.setenv('LFF_AUTHOR', 'updated-owner')
    assert backtest_tags(first, last, 'EXP-001', 'example') == (
        tags | {'author': 'updated-owner', 'ticket': 'example'})
