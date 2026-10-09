"""Freeze boundaries, partial staging, immutable identities and DST replay."""

from contextlib import nullcontext
from datetime import date, datetime, timedelta
from unittest.mock import MagicMock, Mock

import pandas as pd
import pytest

from load_forecast import weather as w
from load_forecast import weather_production as p
from load_forecast.cutoff import forecast_cutoff
from load_forecast.weather_features import temperature_day

DAY = date(2024, 7, 16)


def stored_points(day=DAY, fallback=False):
    run = w.candidate_runs(day)[int(fallback)]
    rows = []
    for step in range(run.first_step, run.first_step + 27):
        for point in w.POINTS:
            rows.append({'run_init_time': run.init_time, 'end_step': step,
                         'variable': 'TMP', 'point': point, 'message_hash': f'original-{step}',
                         'value': 280.0 + step, 'units': 'K',
                         'valid_time': run.init_time + timedelta(hours=step),
                         'file_last_modified': run.init_time + timedelta(hours=4),
                         'index_last_modified': run.init_time + timedelta(hours=4),
                         'original_metadata': {}})
    return rows


@pytest.mark.parametrize('day,slots', [(date(2024, 3, 31), 46), (date(2024, 10, 27), 50)])
def test_freeze_and_dst_replay(day, slots):
    rows = stored_points(day)
    with pytest.raises(ValueError, match='cutoff'):
        p.pin_selection(day, rows, now=forecast_cutoff(day) - timedelta(microseconds=1))
    selection = p.pin_selection(day, rows, now=forecast_cutoff(day))
    assert selection['cutoff'] == forecast_cutoff(day)
    assert selection['status'] == 'primary'
    frame = temperature_day(day, pd.Series(selection), p.replay_points(selection, rows))
    assert len(frame) == slots and str(frame.index.tz) == 'UTC'
    assert frame.temperature_c.notna().all()


def test_late_revision_and_second_manifest_do_not_change_pinned_features():
    rows = stored_points()
    original = p.pin_selection(DAY, rows, now=forecast_cutoff(DAY))
    before = temperature_day(DAY, pd.Series(original), p.replay_points(original, rows))
    late = rows[12] | {'message_hash': 'late', 'value': 999.0,
                       'file_last_modified': forecast_cutoff(DAY) + timedelta(seconds=1)}
    revised = rows + [late]
    second = p.pin_selection(DAY, revised, now=forecast_cutoff(DAY) + timedelta(hours=1))
    assert second == original
    after = temperature_day(DAY, pd.Series(original), p.replay_points(original, revised))
    pd.testing.assert_frame_equal(before, after)
    # The caller explicitly asks for the original hash, even with a second day selection.
    eligible = rows[12] | {'message_hash': 'eligible-revision', 'value': 291.,
                           'file_last_modified': forecast_cutoff(DAY) - timedelta(seconds=1)}
    other = p.pin_selection(DAY, revised + [eligible], now=forecast_cutoff(DAY))
    assert other['selection_hash'] != original['selection_hash']
    manifests = {(DAY, item['selection_hash']): item | {'original_metadata': item['manifest']}
                 for item in (original, other)}
    db = MagicMock()
    cursor = db.cursor.return_value.__enter__.return_value
    cursor.fetchone.side_effect = lambda: manifests[cursor.execute.call_args.args[1]]
    cursor.fetchall.return_value = revised + [eligible]
    pd.testing.assert_frame_equal(p.read_pinned_temperature(db, DAY, original['selection_hash']),
                                  before)
    assert cursor.execute.call_args_list[0].args[1] == (DAY, original['selection_hash'])


def test_latest_eligible_revision_selected_but_equal_time_conflict_falls_back():
    rows = stored_points()
    newer = rows[12] | {'message_hash': 'newer', 'value': 290.,
                        'file_last_modified': forecast_cutoff(DAY) - timedelta(seconds=1)}
    selection = p.pin_selection(DAY, rows + [newer], now=forecast_cutoff(DAY))
    assert p._identity(newer) in selection['manifest']['points']
    assert p._identity(rows[12]) not in selection['manifest']['points']
    tie = newer | {'message_hash': 'tie'}
    selection = p.pin_selection(DAY, rows + [newer, tie] + stored_points(fallback=True),
                                now=forecast_cutoff(DAY))
    assert selection['status'] == 'fallback'


@pytest.mark.parametrize('change', [
    {'file_last_modified': forecast_cutoff(DAY)},
    {'index_last_modified': forecast_cutoff(DAY)},
])
def test_file_and_index_must_both_precede_freeze(change):
    rows = stored_points()
    rows[0] = rows[0] | change
    selection = p.pin_selection(DAY, rows, now=forecast_cutoff(DAY))
    assert selection['status'] == 'weather missing'
    assert not selection['manifest']['points']
    frame = temperature_day(DAY, pd.Series(selection), p.replay_points(selection, rows))
    assert frame.temperature_c.isna().all()


def test_replay_refuses_corruption_or_missing_identity():
    rows = stored_points()
    selection = p.pin_selection(DAY, rows, now=forecast_cutoff(DAY))
    with pytest.raises(ValueError, match='missing'):
        p.replay_points(selection, rows[1:])
    with pytest.raises(ValueError, match='ambiguous'):
        p.replay_points(selection, rows + [rows[0]])
    with pytest.raises(ValueError, match='hash'):
        p.replay_points(selection | {'selection_hash': 'corrupt'}, rows)


def test_staging_before_freeze_downloads_available_pairs_and_never_selects(monkeypatch):
    primary, fallback = w.candidate_runs(DAY)
    info = w.ObjectInfo(primary.init_time, 100, 'etag')
    # A file without its index is not downloadable yet. Both runs can be partially staged.
    available = {primary.keys[0]: info, primary.keys[1]: info, primary.keys[2]: info}
    listing = Mock(side_effect=[available, {fallback.keys[0]: info, fallback.keys[1]: info}])
    monkeypatch.setattr(p, 'list_objects', listing)
    point = stored_points()[0] | {'latitude': 48., 'longitude': 2., 'start_step': 21,
                                'source_url': 'source', 'index_line': 'line',
                                'byte_start': 0, 'byte_end': 99}
    extract = Mock(return_value=[point])
    monkeypatch.setattr(p, 'extract_run', extract)
    clock = Mock(wraps=datetime)
    clock.now.return_value = forecast_cutoff(DAY) - timedelta(hours=3)
    monkeypatch.setattr(p, 'datetime', clock)
    db = MagicMock()
    db.transaction.side_effect = lambda: nullcontext()
    cursor = db.cursor.return_value.__enter__.return_value
    cursor.rowcount = 1
    assert p.stage_candidates(db, Mock(), DAY) == {'steps': 2, 'points_inserted': 2}
    assert [call.kwargs['steps'] for call in extract.call_args_list] == [(21,), (27,)]
    assert all('INSERT INTO raw.gfs_points' in call.args[0]
               for call in cursor.executemany.call_args_list)
    db.execute.assert_not_called()
    cursor.execute.assert_not_called()
    metadata = cursor.executemany.call_args_list[0].args[1][0][-2].obj
    assert len(metadata['objects']) == 2
    assert cursor.executemany.call_args_list[0].args[1][0][-1] < forecast_cutoff(DAY)


def test_select_staged_appends_pinned_manifest_without_http(monkeypatch):
    monkeypatch.setattr(p, 'list_objects', Mock(side_effect=AssertionError('HTTP in selection')))
    db = MagicMock()
    cursor = db.cursor.return_value.__enter__.return_value
    cursor.fetchall.return_value = stored_points()
    with pytest.raises(ValueError, match='cutoff'):
        p.select_staged(db, DAY, now=forecast_cutoff(DAY) - timedelta(seconds=1))
    db.cursor.assert_not_called()
    selection = p.select_staged(db, DAY, now=forecast_cutoff(DAY))
    insert = cursor.execute.call_args_list[-1]
    assert 'INSERT INTO raw.gfs_days' in insert.args[0]
    assert insert.args[1][1] == selection['selection_hash']
    assert len(insert.args[1][-2].obj['points']) == 324


@pytest.mark.parametrize('variables', [(), ('TMP', 'TMP'), ('unknown',)])
def test_invalid_fields_refused(variables):
    with pytest.raises(ValueError, match='variables'):
        p.pin_selection(DAY, [], now=forecast_cutoff(DAY), variables=variables)


def test_naive_clock_refused():
    with pytest.raises(ValueError, match='aware'):
        p.pin_selection(DAY, [], now=datetime(2024, 7, 15))


def test_extract_one_step_and_reject_invalid_steps(monkeypatch):
    run = w.candidate_runs(DAY)[0]
    info = w.ObjectInfo(run.init_time, 100, 'etag')
    reply = Mock(status_code=200, text='index')
    monkeypatch.setattr(w, 'request', lambda *args, **kwargs: nullcontext(reply))
    monkeypatch.setattr(w, 'check_object', Mock())
    monkeypatch.setattr(w, 'index_ranges', Mock(return_value={'TMP': (0, 99, 'line')}))
    fetch = Mock(return_value=b'field')
    monkeypatch.setattr(w, 'fetch_message', fetch)
    monkeypatch.setattr(w, 'decode_points', lambda *args: [{'variable': 'TMP'}])
    rows = w.extract_run(Mock(), run, dict.fromkeys(run.keys, info), ('TMP',), steps=(22,))
    assert len(rows) == 1 and fetch.call_count == 1
    for steps in [(20,), (22, 22), (48,)]:
        with pytest.raises(ValueError, match='steps'):
            w.extract_run(Mock(), run, {}, ('TMP',), steps=steps)
