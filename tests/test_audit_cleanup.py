"""The publication audit detects credential shapes without disclosing their values."""

import runpy
from pathlib import Path

import pytest

AUDIT = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'scripts/audit_cleanup.py'))


@pytest.mark.parametrize(('content', 'rule'), [
    (b'heading\nPOSTGRES_PASSWORD="synthetic-fixture"\n', 'credential-assignment'),
    (b'heading\npostgresql://fixture:synthetic-fixture@localhost/db', 'credential-url'),
    (b'heading\n' + b'gh' + b'p_' + b'X' * 36, 'provider-token'),
    (b'heading\n-----BEGIN ' + b'PRIVATE KEY-----', 'private-key'),
    (b"heading\nALTER ROLE fixture PASSWORD 'synthetic-fixture'", 'sql-password'),
    (b'heading\nBearer ' + b'X' * 20, 'authorization-token'),
])
def test_hits_are_redacted(content: bytes, rule: str) -> None:
    findings = AUDIT['hits'](content)
    assert {'rule': rule, 'line': 2} in findings
    assert all(set(finding) == {'rule', 'line'} for finding in findings)
    assert 'synthetic-fixture' not in repr(findings)


def test_empty_template_is_not_a_literal_credential() -> None:
    assert AUDIT['hits'](b'POSTGRES_PASSWORD=\n') == []


def test_empty_template_does_not_capture_the_next_line() -> None:
    assert AUDIT['hits'](b'POSTGRES_PASSWORD=\nNEXT_SETTING=value\n') == []
