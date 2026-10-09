from datetime import date
from unittest.mock import MagicMock, patch

import numpy as np
import pandas as pd
import pytest
import requests
from matplotlib import image as mpimg

from load_forecast import backtest_plots
from load_forecast import evaluation as ev
from load_forecast.backtest import log_backtest, score_backtest
from load_forecast.backtest_plots import BLUE, GREY, plot_data, save_backtest_plots
from load_forecast.rte_reference import RTE_STAR


def synthetic_frame(first='2024-03-20', last='2024-04-05'):
    index = pd.date_range(pd.Timestamp(first, tz='Europe/Paris').tz_convert('UTC'),
                          pd.Timestamp(last, tz='Europe/Paris').tz_convert('UTC'),
                          freq='30min', inclusive='left')
    frame = pd.DataFrame({ev.ACTUAL: 40000.0, ev.MODEL: 40020.0,
                          ev.RTE: 40010.0}, index=index)
    local = ev.local_dates(index)
    frame.loc[local == date(2024, 3, 31), ev.MODEL] = 40100
    return frame


def test_plot_values_common_slots_local_calendar_and_missing_reference():
    frame = synthetic_frame()
    first, last = date(2024, 3, 20), date(2024, 4, 4)
    local = ev.local_dates(frame.index)
    frame.loc[local == date(2024, 3, 23), ev.RTE] = np.nan
    reference = pd.Series(40005.0, index=frame.index)
    reference.loc[local == date(2024, 3, 24)] = np.nan
    data = plot_data(frame, first, last, reference=reference)
    assert data['series'].loc[local == date(2024, 3, 23)].isna().all().all()
    assert data['monthly'].index.tolist() == ['2024-03', '2024-04']
    kept = ev.common_slots(frame)
    march = kept.loc[kept.index.tz_convert('Europe/Paris').month == 3]
    assert data['monthly'].loc['2024-03', ev.MODEL] == pytest.approx(
        (march[ev.MODEL] - march[ev.ACTUAL]).abs().mean())
    assert len(data['difference']) == 16
    assert data['difference'].loc['2024-03-23':'2024-03-24'].isna().all()
    assert data['difference'].loc['2024-03-25'] == 15
    assert data['bootstrap'].n_missing == 2
    assert data['bootstrap'].mean == pytest.approx(data['difference'].mean())
    assert data['types'].loc['dst', 'n_slots'] == 46
    assert data['types'].loc['dst', ev.MODEL] == 100
    # A winter UTC date can belong to the next Paris month/hour.
    frame.loc[pd.Timestamp('2024-03-31 22:00', tz='UTC'), ev.MODEL] = 40200
    changed = plot_data(frame, first, last)
    assert changed['monthly'].loc['2024-04', ev.MODEL] > data['monthly'].loc['2024-04', ev.MODEL]
    assert changed['hourly'].loc[0, ev.MODEL] > data['hourly'].loc[0, ev.MODEL]


def test_both_dst_days_are_slot_weighted_and_cold_is_explicit():
    frame = synthetic_frame('2024-03-20', '2024-11-05')
    local = ev.local_dates(frame.index)
    frame.loc[local == date(2024, 10, 27), ev.MODEL] = 40010
    cold = {date(2024, 3, 31): True, date(2024, 4, 3): True}
    data = plot_data(frame, date(2024, 3, 20), date(2024, 11, 4), cold=cold)
    assert data['types'].loc['dst', 'n_slots'] == 96
    assert data['types'].loc['dst', ev.MODEL] == pytest.approx((46 * 100 + 50 * 10) / 96)
    assert data['types'].loc['cold', 'n_slots'] == 48
    assert data['types'].loc['cold_any', 'n_slots'] == 94
    assert data['hourly'].loc[2, ev.MODEL] == pytest.approx(
        (frame.loc[frame.index.tz_convert('Europe/Paris').hour == 2, ev.MODEL] - 40000).abs().mean())


def test_missing_rows_make_line_gaps_and_invalid_inputs_fail():
    frame = synthetic_frame()
    missing = frame.index[100]
    data = plot_data(frame.drop(index=missing), date(2024, 3, 20), date(2024, 4, 4))
    assert data['series'].loc[missing].isna().all()
    with pytest.raises(ValueError, match='calendar'):
        plot_data(frame, date(2024, 3, 21), date(2024, 4, 4))
    frame.iloc[0, 0] = np.inf
    with pytest.raises(ValueError, match='finite'):
        plot_data(frame, date(2024, 3, 20), date(2024, 4, 4))


def test_five_readable_pngs_and_plot_style(tmp_path):
    frame = synthetic_frame()
    paths = save_backtest_plots(frame, date(2024, 3, 20), date(2024, 4, 4), tmp_path)
    assert {p.name for p in paths} == {'actual_vs_forecast.png', 'mae_by_monthly.png',
                                     'mae_by_hourly.png', 'mae_by_types.png',
                                     'daily_difference.png'}
    for path in paths:
        pixels = mpimg.imread(path)
        assert pixels.shape == (675, 1650, 4)
        np.testing.assert_allclose(pixels[0, 0], [1, 1, 1, 1])
        colors = np.rint(pixels[:, :, :3] * 255).astype(int)
        for hex_color in (BLUE, GREY):
            rgb = [int(hex_color[i:i + 2], 16) for i in (1, 3, 5)]
            assert np.any(np.all(colors == rgb, axis=2))


@pytest.mark.parametrize('tracking_uri', [None, 'http://tracking.example:7000/prefix/'])
@pytest.mark.parametrize('fail_upload', [False, True])
def test_logger_uploads_created_plots_before_finishing(fail_upload, tracking_uri, monkeypatch):
    if tracking_uri is None:
        monkeypatch.delenv('MLFLOW_TRACKING_URI', raising=False)
    else:
        monkeypatch.setenv('MLFLOW_TRACKING_URI', tracking_uri)
    server = (tracking_uri or 'http://127.0.0.1:5000').rstrip('/')
    frame = synthetic_frame()
    frame['exp001'] = 40030.0
    first, last = date(2024, 3, 20), date(2024, 4, 4)
    result = score_backtest(frame, frame[ev.ACTUAL], frame[ev.RTE])
    response = MagicMock()
    response.json.return_value = {'run': {'info': {
        'run_id': 'test-run', 'artifact_uri': 'mlflow-artifacts:/0/test-run/artifacts'}}}
    uploaded = {}

    def put(url, *, data, headers, timeout):
        assert url.startswith(f'{server}/api/2.0/mlflow-artifacts/artifacts/'
                              '0/test-run/artifacts/plots/')
        assert headers == {'Content-Type': 'image/png'} and timeout == 30
        uploaded[url.rsplit('/', 1)[-1]] = data.read()
        if fail_upload:
            raise requests.HTTPError('Upload failed')
        return response

    with patch('load_forecast.backtest.requests.Session') as mock:
        session = mock.return_value.__enter__.return_value
        session.post.return_value = response
        session.put.side_effect = put
        kwargs = {'exp': 'synthetic', 'frame': frame,
                  'reference_column': 'exp001', 'reference_name': 'EXP-001'}
        if fail_upload:
            with pytest.raises(requests.HTTPError):
                log_backtest(result, first, last, **kwargs)
            assert session.post.call_count == 2
            assert session.post.call_args.kwargs['json']['status'] == 'FAILED'
        else:
            assert log_backtest(result, first, last, **kwargs) == 'test-run'
            assert len(uploaded) == 5
            assert all(content.startswith(b'\x89PNG\r\n\x1a\n') for content in uploaded.values())
            assert session.post.call_args_list[-1].kwargs['json']['status'] == 'FINISHED'
        assert all(call.args[0].startswith(f'{server}/api/2.0/mlflow/runs/')
                   for call in session.post.call_args_list)


def test_custom_reference_grouped_errors_use_identical_slots_and_dst_weights():
    frame = synthetic_frame('2024-03-20', '2024-11-05')
    local = ev.local_dates(frame.index)
    frame['exp001'] = 40030.0
    frame.loc[local == date(2024, 10, 27), 'exp001'] = 40070.0
    frame.loc[local == date(2024, 3, 23), 'exp001'] = np.nan
    frame.loc[local == date(2024, 3, 24), ev.RTE] = np.nan
    cold = {date(2024, 4, 3): True}
    data = plot_data(frame, date(2024, 3, 20), date(2024, 11, 4),
                     reference_column='exp001', cold=cold)
    paired = frame.dropna()
    errors = paired[[ev.MODEL, 'exp001']].sub(paired[ev.ACTUAL], axis=0).abs()
    paris = errors.index.tz_convert('Europe/Paris')
    pd.testing.assert_frame_equal(data['monthly'], errors.groupby(paris.strftime('%Y-%m')).mean())
    pd.testing.assert_frame_equal(data['hourly'], errors.groupby(paris.hour).mean().reindex(range(24)))
    expected = ev.mae_by_day_type(paired.assign(**{ev.RTE: paired.exp001}), cold)
    pd.testing.assert_frame_equal(data['types'], expected.rename(columns={ev.RTE: 'exp001'}))
    assert data['types'].loc['dst', 'exp001'] == pytest.approx((46 * 30 + 50 * 70) / 96)
    assert data['difference'].loc['2024-03-23':'2024-03-24'].isna().all()
    assert data['difference'].loc['2024-03-25'] == -10
    assert data['types'].loc['cold', 'exp001'] == 30


@pytest.mark.parametrize('column, name', [('exp001', 'EXP-001'), (RTE_STAR, 'RTE* a2')])
def test_custom_reference_labels_are_rendered(tmp_path, column, name):
    frame = synthetic_frame()
    frame[column] = 40030.0
    data = plot_data(frame, date(2024, 3, 20), date(2024, 4, 4), reference_column=column)
    assert (data['monthly'][column] == 30).all()
    assert data['difference'].loc['2024-03-25'] == -10
    with patch('load_forecast.backtest_plots._figure', wraps=backtest_plots._figure) as figure:
        paths = save_backtest_plots(frame, date(2024, 3, 20), date(2024, 4, 4),
                                    tmp_path, reference_column=column, reference_name=name)
    assert len(paths) == 5
    assert all(name in call.args[0] for call in figure.call_args_list[1:])
    assert all(mpimg.imread(path).shape == (675, 1650, 4) for path in paths)


@pytest.mark.parametrize('column', [ev.ACTUAL, ev.MODEL])
def test_reference_column_must_be_a_separate_forecast(column):
    with pytest.raises(ValueError, match='separate forecast'):
        plot_data(synthetic_frame(), date(2024, 3, 20), date(2024, 4, 4), reference_column=column)
