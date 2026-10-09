"""Read pre-registration evidence at its pinned commit after report moves."""

import subprocess
from pathlib import PurePosixPath


def committed_report(commit: str, report: str) -> str:
    """Find exactly one report by basename in the specified Git tree and read it."""
    paths = subprocess.run(
        ['git', 'ls-tree', '-r', '--name-only', commit], check=True,
        capture_output=True, text=True, encoding='utf-8',
    ).stdout.splitlines()
    matches = [path for path in paths if PurePosixPath(path).name == report]
    if len(matches) != 1:
        raise RuntimeError(f'Expected exactly one {report} in commit {commit}')
    return subprocess.run(
        ['git', 'show', f'{commit}:{matches[0]}'], check=True,
        capture_output=True, text=True, encoding='utf-8',
    ).stdout
