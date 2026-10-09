"""Clean readers share one setting and keep SQL values separate from identifiers."""

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest

from load_forecast.clean_data import load_clean_load
from load_forecast.database import clean_query, clean_table


def test_default_and_runtime_override(monkeypatch):
    monkeypatch.delenv('LFF_CLEAN_TABLE', raising=False)
    assert clean_table() == '"analytics"."load_30min"'
    monkeypatch.setenv('LFF_CLEAN_TABLE', 'sandbox.load_30min')
    assert clean_query('SELECT * FROM {table} WHERE value_time < %s').as_string() == (
        'SELECT * FROM "sandbox"."load_30min" WHERE value_time < %s'
    )


@pytest.mark.parametrize('table', ['', 'load_30min', '.load_30min', 'schema.', 'a.b.c'])
def test_invalid_relation(table, monkeypatch):
    monkeypatch.setenv('LFF_CLEAN_TABLE', table)
    with pytest.raises(ValueError, match='schema.table'):
        clean_table()


def test_identifier_escaping(monkeypatch):
    monkeypatch.setenv('LFF_CLEAN_TABLE', 'schema.table"; SELECT 1')
    assert clean_table() == '"schema"."table""; SELECT 1"'


def test_dataframe_reader_uses_setting_and_preserves_bounds(monkeypatch):
    monkeypatch.setenv('LFF_CLEAN_TABLE', 'sandbox.load_30min')
    connection = MagicMock()
    cursor = connection.cursor.return_value.__enter__.return_value
    cursor.description = [('value_time',), ('consommation',)]
    cursor.fetchall.return_value = [(datetime(2024, 1, 1, tzinfo=UTC), 45000)]
    end = datetime(2024, 1, 2, tzinfo=UTC)
    result = load_clean_load(connection, end_time=end)
    query, params = cursor.execute.call_args.args
    assert 'FROM "sandbox"."load_30min"' in query.as_string()
    assert params == [end]
    assert str(result.value_time.dt.tz) == 'UTC'
    assert result.consommation.iloc[0] == 45000
