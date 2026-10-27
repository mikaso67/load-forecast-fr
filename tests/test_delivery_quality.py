from datetime import date, timedelta

import pytest

from load_forecast.delivery_quality import degraded_input_reasons
from load_forecast.evaluation import last_usable_slot_start


@pytest.mark.parametrize('day', [date(2026, 10, 21), date(2026, 10, 22)])
def test_incident_payload_provenance(day):
    assert degraded_input_reasons(day, '2026-10-19T11:30:00Z', True) == [
        'expert_backup_chain', 'stale_consumption']


@pytest.mark.parametrize('day', [date(2026, 1, 12), date(2026, 7, 12),
                               date(2026, 3, 30), date(2026, 10, 26)])
def test_fresh_boundary_and_each_degradation_independently(day):
    boundary = last_usable_slot_start(day)
    assert degraded_input_reasons(day, boundary.isoformat(), False) == []
    assert degraded_input_reasons(day, boundary.isoformat(), True) == ['expert_backup_chain']
    assert degraded_input_reasons(day, (boundary - timedelta(minutes=30)).isoformat(), False) == [
        'stale_consumption']
    assert degraded_input_reasons(day, None, False) == ['missing_consumption']
    with pytest.raises(ValueError, match='timezone aware'):
        degraded_input_reasons(day, '2026-01-01T00:00:00', False)
