"""Read-only inventory and redacted credential-pattern scan of local Git history."""

import ast
import hashlib
import json
import re
import subprocess
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / 'data/audit/inventory_056'
RULES = {
    'private-key': rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----',
    'provider-token': rb'\b(?:AKIA[0-9A-Z]{16}|gh[pousr]_[A-Za-z0-9]{36,255}|'
                      rb'github_pat_[A-Za-z0-9_]{40,255}|sk-[A-Za-z0-9_-]{20,255}|'
                      rb'xox[baprs]-[A-Za-z0-9-]{10,255})\b',
    'credential-url': rb'[a-z][a-z0-9+.-]*://[^\s/:]+:[^\s/@]+@',
    'sql-password': rb'(?i)\bPASSWORD\s+(?:\'[^\'\r\n]+\'|:[a-z_]+)',
    'authorization-token': rb'(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9+/=_-]{16,}',
    'credential-assignment': rb'(?i)\b(?:[A-Z_]*(?:password|passwd|pwd|secret|token|'
                             rb'api_key|apikey|access_key)[A-Z_]*)[\"\']?\s*[:=][ \t]*'
                             rb'(?:[\"\'][^\"\'\r\n]+[\"\']|[^\s,;#]+)',
}


def git(*arguments: str) -> bytes:
    """Suppress subprocess diagnostics, which might contain matched content."""
    result = subprocess.run(['git', *arguments], cwd=ROOT, capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError('Git audit command failed; no command output disclosed')
    return result.stdout


def hits(content: bytes) -> list[dict]:
    """Return rule and line only; never persist content or matched values."""
    return [{'rule': name, 'line': content.count(b'\n', 0, match.start()) + 1}
            for name, pattern in RULES.items() for match in re.finditer(pattern, content)]


def main() -> None:
    OUTPUT.mkdir(parents=True, exist_ok=True)
    tracked = sorted(git('ls-files', '-z').decode().rstrip('\0').split('\0'))
    ignored = git('ls-files', '--others', '--ignored', '--exclude-standard', '-z')
    ignored_paths = sorted(filter(None, ignored.decode().split('\0')))
    other = git('ls-files', '--others', '--exclude-standard', '-z')
    other_paths = sorted(filter(None, other.decode().split('\0')))
    inventory = []
    imports = {}
    current_hits = []
    todos = []
    hashes = {}
    for path in tracked + other_paths:
        content = (ROOT / path).read_bytes()
        inventory.append({'path': path, 'bytes': len(content), 'tracked': path in tracked})
        hashes.setdefault(hashlib.sha256(content).hexdigest(), []).append(path)
        current_hits.extend({'path': path, **hit} for hit in hits(content))
        if path.endswith('.py'):
            tree = ast.parse(content)
            imports[path] = sorted({node.module for node in ast.walk(tree)
                                    if isinstance(node, ast.ImportFrom) and node.module}
                                   | {alias.name for node in ast.walk(tree)
                                      if isinstance(node, ast.Import) for alias in node.names})
        if b'\0' not in content:
            todos.extend({'path': path, 'line': number}
                         for number, line in enumerate(content.splitlines(), 1)
                         if re.search(rb'\b(?:TODO|FIXME|HACK|XXX)\b', line))

    # Batch-read every reachable object, including deleted files and all local refs.
    objects = git('rev-list', '--objects', '--all').decode().splitlines()
    object_paths = {line.split(' ', 1)[0]: line.split(' ', 1)[1]
                    for line in objects if ' ' in line}
    object_ids = [line.split(' ', 1)[0] for line in objects]
    tree_refs = []
    for row in git('for-each-ref', '--format=%(refname) %(objecttype)').decode().splitlines():
        ref, kind = row.split()
        if kind == 'tree':
            tree_refs.append((ref, git('ls-tree', '-r', '-z', ref).split(b'\0')))
    result = subprocess.run(['git', 'cat-file', '--batch'], cwd=ROOT,
                            input=('\n'.join(object_ids) + '\n').encode(),
                            capture_output=True, check=False)
    if result.returncode:
        raise RuntimeError('Git object scan failed; diagnostics suppressed')
    position = 0
    blob_count = 0
    commit_count = 0
    history = []
    blob_sizes = []
    message_hits = []
    for _ in object_ids:
        newline = result.stdout.index(b'\n', position)
        oid, kind, size = result.stdout[position:newline].decode().split()
        size = int(size)
        content = result.stdout[newline + 1:newline + 1 + size]
        position = newline + 2 + size
        if kind == 'commit':
            commit_count += 1
            message_hits.extend({'commit': oid, **hit}
                                for hit in hits(content.split(b'\n\n', 1)[-1]))
        if kind != 'blob':
            continue
        blob_count += 1
        blob_sizes.append({'blob': oid, 'bytes': size, 'path': object_paths.get(oid)})
        found = hits(content)
        if not found:
            continue
        commits = git('log', '--all', '--full-history', '--root', '-m', '--format=%H',
                      f'--find-object={oid}').decode().splitlines()
        if not commits:
            checkpoints = [{'ref': ref, 'path': entry.split(b'\t', 1)[1].decode()}
                           for ref, entries in tree_refs for entry in entries
                           if b'\t' in entry
                           and entry.split(b'\t', 1)[0].endswith(oid.encode())]
            if not checkpoints:
                raise RuntimeError(f'Cannot map flagged blob {oid} to a commit or tree ref')
            history.append({'blob': oid, 'commit': None,
                            'paths': sorted({item['path'] for item in checkpoints}),
                            'tree_refs': sorted({item['ref'] for item in checkpoints}),
                            'hits': found})
            continue
        # Recover all paths at an actual introducing/removing commit, not just rev-list's label.
        commit = commits[-1]
        entries = git('ls-tree', '-r', '-z', commit).split(b'\0')
        paths = [entry.split(b'\t', 1)[1].decode() for entry in entries
                 if b'\t' in entry and entry.split(b'\t', 1)[0].endswith(oid.encode())]
        if not paths:
            paths = [object_paths.get(oid, '(path unavailable)')]
        history.append({'blob': oid, 'commit': commit, 'paths': paths, 'hits': found})

    summary = {
        'head': git('rev-parse', 'HEAD').decode().strip(),
        'refs': git('for-each-ref', '--format=%(refname)').decode().splitlines(),
        'inventory': inventory, 'ignored_count': len(ignored_paths),
        'ignored_roots': dict(Counter(path.split('/')[0] for path in ignored_paths)),
        'imports': imports, 'duplicates': [paths for paths in hashes.values() if len(paths) > 1],
        'todo_locations': todos, 'current_credential_patterns': current_hits,
        'reachable_blobs_scanned': blob_count, 'commits_scanned': commit_count,
        'largest_history_blobs': sorted(blob_sizes, key=lambda row: row['bytes'],
                                        reverse=True)[:10],
        'sensitive_history_paths': sorted({path for path in object_paths.values()
                                          if Path(path).name == '.env'
                                          or Path(path).suffix in ('.pem', '.key', '.p12')}),
        'history_credential_patterns': history, 'commit_message_patterns': message_hits,
    }
    (OUTPUT / 'report.json').write_text(json.dumps(summary, indent=2) + '\n', encoding='utf-8')
    # Local environment and generated data are inventoried by name, never read for secrets.
    (OUTPUT / 'ignored_files.txt').write_text('\n'.join(ignored_paths) + '\n', encoding='utf-8')
    print(f'Inventory: {len(tracked)} tracked, {len(other_paths)} untracked, '
          f'{len(ignored_paths)} ignored files')
    print(f'History: {commit_count} commits, {blob_count} unique reachable blobs scanned')
    print(f'Credential-pattern candidates: {len(current_hits)} current locations, '
          f'{len(history)} historical blobs, {len(message_hits)} commit-message locations')
    print('Redacted report: data/audit/inventory_056/report.json')


if __name__ == '__main__':
    main()
