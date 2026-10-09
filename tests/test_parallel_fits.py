from datetime import date

import numpy as np
import pandas as pd
import pytest

from load_forecast.backtest import fit_slot_models, forecast_day
from load_forecast.gam_model import SlotGAM


@pytest.mark.parametrize('day,count', [(date(2024, 3, 31), 46), (date(2024, 10, 27), 50)])
def test_parallel_gam_fits_and_dst_forecasts_are_bit_identical(monkeypatch, day, count):
    index = pd.date_range('2024-01-01', '2024-10-28', freq='30min', tz='UTC')
    rng = np.random.default_rng(49)
    temperature = pd.Series(rng.uniform(-5, 30, len(index)), index=index)
    history = 40000 + 30 * (temperature - 15) ** 2 + rng.normal(0, 100, len(index))

    def builder(slots, _history, _day):
        return pd.DataFrame({'temperature_c': temperature.reindex(slots),
                             'weekend': (slots.tz_convert('Europe/Paris').dayofweek >= 5) * 1.0},
                            index=slots)

    def factory():
        return SlotGAM(['temperature_c'], alpha_grid=(100.0,))

    kwargs = dict(builder=builder, published_at=None, training_start=date(2024, 1, 2))
    monkeypatch.delenv('LFF_N_JOBS', raising=False)
    serial = fit_slot_models(history, day, factory, cache={}, **kwargs)
    monkeypatch.setenv('LFF_N_JOBS', '6')
    parallel = fit_slot_models(history, day, factory, cache={}, **kwargs)
    assert list(serial) == list(parallel) == list(range(48))
    assert len({id(model) for model in parallel.values()}) == 48
    for slot in serial:
        np.testing.assert_array_equal(serial[slot].result_.params, parallel[slot].result_.params)
        assert serial[slot].alpha_ == parallel[slot].alpha_
        assert serial[slot].slope_lower_ == parallel[slot].slope_lower_
        assert serial[slot].slope_upper_ == parallel[slot].slope_upper_
    first = forecast_day(serial, history, day, day, builder)
    second = forecast_day(parallel, history, day, day, builder)
    pd.testing.assert_frame_equal(first, second, check_exact=True)
    assert len(first) == count and first['model'].notna().all()


@pytest.mark.parametrize('workers', ['0', '-1', 'six', '1.5', ''])
def test_invalid_worker_count_fails_clearly(monkeypatch, workers):
    monkeypatch.setenv('LFF_N_JOBS', workers)
    with pytest.raises(ValueError, match='LFF_N_JOBS must be a positive integer'):
        fit_slot_models(pd.Series(dtype=float), date(2024, 3, 31), lambda: None,
                        builder=lambda *args: None, published_at=None,
                        training_start=date(2024, 1, 1), cache={})
