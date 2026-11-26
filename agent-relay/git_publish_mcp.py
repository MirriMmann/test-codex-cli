"""A scoped Git publication tool for Codex, using the MCP stdio transport.

Only the configured checkout and its HTTPS GitHub origin can be published.
The model never receives a general shell or unrestricted Git tool here.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit


class PublishError(RuntimeError):
    """A diagnostic that is safe to return to the model."""


class PublishUncertain(PublishError):
    """A push was attempted but the remote result could not be confirmed."""


def git_failure(output: bytes) -> str:
    detail = output.decode('utf-8', errors='replace').lower()
    if any(word in detail for word in ('authentication failed', 'could not read username',
                                       'terminal prompts disabled', 'invalid username', 'bad credentials')):
        return 'GitHub authentication failed. Sign in to Git Credential Manager for this repository.'
    if any(word in detail for word in ('permission denied', 'access is denied', 'permission to',
                                       'unable to create', 'read-only file system', '403')):
        return 'Git access was denied. Check repository write access and local Git directory permissions.'
    if any(word in detail for word in ('non-fast-forward', 'fetch first', 'rejected', 'stale info')):
        return 'Push was rejected. Fetch and reconcile the remote branch before publishing again; force push is disabled.'
    if any(word in detail for word in ('could not resolve host', 'failed to connect', 'connection timed out',
                                       'network is unreachable', 'connection reset', 'ssl certificate')):
        return 'GitHub could not be reached. Check the network, proxy and TLS configuration.'
    if any(word in detail for word in ('please tell me who you are', 'unable to auto-detect email')):
        return 'Git commit identity is missing. Set user.name and user.email for this checkout.'
    if 'repository not found' in detail:
        return 'The GitHub repository is unavailable. Check the origin URL and account access.'
    return 'Git publication failed. Check the local Git configuration; no automatic retry was performed.'


class GitPublisher:
    def __init__(self, workdir: str | Path):
        self.root = Path(workdir).expanduser().resolve()
        if not self.root.is_dir():
            raise PublishError('The configured repository directory does not exist.')
        top = self.git('rev-parse', '--show-toplevel').decode('utf-8', errors='replace').strip()
        if Path(top).resolve() != self.root:
            raise PublishError('The configured directory must be the root of its Git checkout.')

    def git(self, *args: str, timeout: int = 45) -> bytes:
        env = os.environ.copy()
        for name in ('GIT_DIR', 'GIT_WORK_TREE', 'GIT_INDEX_FILE', 'GIT_COMMON_DIR',
                     'GIT_OBJECT_DIRECTORY', 'GIT_ALTERNATE_OBJECT_DIRECTORIES',
                     'GIT_NAMESPACE', 'GIT_CONFIG', 'GIT_CONFIG_COUNT', 'GIT_CONFIG_PARAMETERS'):
            env.pop(name, None)
        env.update(GIT_TERMINAL_PROMPT='0', GCM_INTERACTIVE='Never')
        options = {'creationflags': subprocess.CREATE_NO_WINDOW} if os.name == 'nt' else {}
        try:
            result = subprocess.run(
                ['git', '-C', str(self.root), '--literal-pathspecs', '-c', 'core.fsmonitor=false',
                 *(['-c', 'core.hooksPath=' + self._hooks_path] if getattr(self, '_hooks_path', '') else []), *args],
                capture_output=True, timeout=timeout, env=env, **options)
        except subprocess.TimeoutExpired:
            raise PublishError('Git publication timed out. Inspect the current branch before trying again.') from None
        except OSError:
            raise PublishError('Git could not be started. Check the Git installation.') from None
        if result.returncode:
            raise PublishError(git_failure(result.stderr + b'\n' + result.stdout))
        return result.stdout

    def destination(self) -> tuple[str, str]:
        # --all detects multiple push destinations; get-url expands insteadOf rules.
        remotes = self.git('remote', 'get-url', '--push', '--all', 'origin').decode('utf-8', errors='replace').splitlines()
        if len(remotes) != 1:
            raise PublishError('Publishing requires exactly one origin push URL.')
        remote = remotes[0]
        parsed = urlsplit(remote)
        if (parsed.scheme != 'https' or parsed.netloc != 'github.com' or parsed.query or parsed.fragment
                or not re.fullmatch(r'/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', parsed.path)
                or any(part in ('.', '..') for part in parsed.path.split('/')[1:])):
            raise PublishError('Publishing is restricted to an HTTPS origin on github.com without embedded credentials.')
        try:
            branch = self.git('symbolic-ref', '--quiet', '--short', 'HEAD').decode('utf-8').strip()
        except PublishError:
            raise PublishError('Publishing requires a checked-out branch; detached HEAD is not supported.') from None
        if not branch:
            raise PublishError('Publishing requires a checked-out branch.')
        return branch, remote

    def safe_path(self, value: str) -> str:
        if not isinstance(value, str) or not value or '\x00' in value:
            raise PublishError('Each publication path must be a nonempty relative path.')
        portable = value.replace('\\', '/')
        parts = portable.split('/')
        if (Path(portable).is_absolute() or PureWindowsPath(value).drive or ':' in portable
                or '..' in parts or portable.startswith('/')):
            raise PublishError('Publication paths must stay inside the configured repository.')
        for part in parts:
            name = part.casefold()
            if (name in ('.git', '.ssh', '.aws', '.codex', 'auth_data.json', 'credentials.json',
                         'id_rsa', 'id_dsa', 'id_ecdsa', 'id_ed25519')
                    or (name.startswith('.env') and name != '.env.example')
                    or name.endswith(('.sqlite3', '.sqlite3-journal', '.sqlite3-wal', '.sqlite3-shm'))
                    or name.endswith(('.pem', '.key', '.p12', '.pfx', '.keystore', '.jks'))):
                raise PublishError('Credential files and Git metadata cannot be published by this tool.')
        candidate = self.root / portable
        try:
            candidate.resolve().relative_to(self.root)
        except (ValueError, OSError):
            raise PublishError('Publication paths must stay inside the configured repository.') from None
        current = candidate
        while current != self.root:
            if current.is_symlink():
                raise PublishError('Symlink paths cannot be published by this tool.')
            current = current.parent
        return candidate.relative_to(self.root).as_posix()

    def names(self, *args: str) -> set[str]:
        return {os.fsdecode(value) for value in self.git(*args).split(b'\x00') if value}

    def head(self) -> str:
        # --verify HEAD fails for an unborn checkout; symbolic-ref still exists.
        try:
            return self.git('rev-parse', '--verify', 'HEAD').decode('ascii').strip()
        except PublishError:
            self.git('symbolic-ref', '--quiet', 'HEAD')
            return ''

    def remote_head(self, remote: str, branch: str) -> str:
        output = self.git('ls-remote', '--refs', remote, 'refs/heads/' + branch, timeout=45)
        lines = output.decode('ascii').splitlines()
        if not lines:
            return ''
        if len(lines) != 1:
            raise PublishError('Remote branch verification returned an ambiguous result.')
        fields = lines[0].split()
        if len(fields) != 2 or fields[1] != 'refs/heads/' + branch or not re.fullmatch(r'[0-9a-f]{40,64}', fields[0]):
            raise PublishError('Remote branch verification returned an invalid result.')
        return fields[0]

    def publish_reviewed(self, plan: dict, message: str) -> dict:
        """Publish an exact host-sealed manifest, never a model-selected directory."""
        with tempfile.TemporaryDirectory(prefix='agentrelay-empty-hooks-') as hooks:
            self._hooks_path = hooks
            try:
                return self._publish_reviewed(plan, message)
            finally:
                self._hooks_path = ''

    def publish_current(self, destination: dict, message: str) -> dict:
        """Explicit human publication of current Git state; no coding-report gate."""
        import threading
        from publication import digest, source_digest
        from verification import repository_snapshot
        branch, remote = self.destination()
        head = self.head()
        if (branch, remote, head) != (destination.get('branch'), destination.get('remote'), destination.get('head')):
            raise PublishError('Repository HEAD, branch or origin changed while publication was queued. Send a new publication request.')
        paths = (self.names('diff', '--name-only', '--no-renames', '-z')
                 | self.names('diff', '--cached', '--name-only', '--no-renames', '-z')
                 | self.names('ls-files', '--others', '--exclude-standard', '-z'))
        paths = sorted(self.safe_path(path) for path in paths)
        snapshot = repository_snapshot(str(self.root), threading.Event())
        if snapshot is None or any(path not in snapshot['files'] for path in paths):
            raise PublishError('The current files cannot be safely snapshotted for publication.')
        if not head and not paths:
            raise PublishError('The repository has no commit or files to publish.')
        remote_head = self.remote_head(remote, branch)
        if remote_head and remote_head != head:
            if not head:
                raise PublishError('The remote branch already has commits. Fetch and reconcile it before publication.')
            try:
                self.git('merge-base', '--is-ancestor', remote_head, head)
            except PublishError:
                raise PublishError('The remote branch is ahead or diverged. Fetch and reconcile it; force push is disabled.') from None
        self._check_current_content(paths, remote_head, head)
        plan = {'eligible': True, 'mode': 'current', 'root': str(self.root),
                'branch': branch, 'remote': remote, 'head': head, 'remote_head': remote_head,
                'paths': paths, 'snapshot': digest(snapshot), 'sources': source_digest(snapshot)}
        return self.publish_reviewed(plan, message)

    def _check_current_content(self, paths: list[str], remote_head: str, head: str) -> None:
        from privacy import PrivacyGuard, PrivacyViolation
        guard = PrivacyGuard.configured('')
        def check(data):
            try:
                guard.require_clean(data.decode('utf-8', errors='replace'))
            except PrivacyViolation:
                raise PublishError('Possible credentials found in publication content. Remove secrets before publishing.') from None
        for path in paths:
            source = self.root / self.safe_path(path)
            if source.is_file():
                check(source.read_bytes())
        # A clean tree can still have unpublished local commits. Inspect every
        # new blob in that history, including secrets removed in a later commit.
        if not head or head == remote_head:
            return
        commits = self.git('rev-list', head, *(['^' + remote_head] if remote_head else [])).decode('ascii').splitlines()
        if len(commits) > 2000:
            raise PublishError('Too many unpublished commits for one bounded publication request.')
        seen = set()
        for commit in commits:
            changed = self.names('diff-tree', '--root', '-m', '--no-commit-id', '--name-only', '--no-renames', '-r', '-z', commit)
            for path in changed:
                self.safe_path(path)
                entry = self.git('ls-tree', '-z', commit, '--', path)
                if not entry:
                    continue  # deletion
                fields = entry.split(b'\t', 1)[0].split()
                if len(fields) != 3 or fields[0] not in (b'100644', b'100755') or fields[1] != b'blob':
                    raise PublishError('Unsupported file in unpublished commits.')
                blob = fields[2].decode('ascii')
                if blob not in seen:
                    check(self.git('cat-file', 'blob', blob))
                    seen.add(blob)

    def _publish_reviewed(self, plan: dict, message: str) -> dict:
        import threading
        from publication import digest, source_digest
        from verification import repository_snapshot
        if (not isinstance(plan, dict) or plan.get('eligible') is not True
                or Path(plan.get('root', '')).resolve() != self.root):
            raise PublishError('The reviewed publication manifest does not match this repository.')
        branch, remote = self.destination()
        if (branch, remote, self.head()) != (plan.get('branch'), plan.get('remote'), plan.get('head')):
            raise PublishError('Repository HEAD, branch or origin changed after verification. Run a new task/check.')
        try:
            rewrites = self.git('config', '--get-regexp', r'^url\..*\.(insteadof|pushinsteadof)$')
        except PublishError:
            rewrites = b''  # Git returns 1 when no matching keys exist.
        if rewrites:
            raise PublishError('URL rewriting is not supported for reviewed publication. Configure a direct HTTPS GitHub origin.')
        self.git('check-ref-format', '--branch', branch)
        paths = plan.get('paths')
        current = plan.get('mode') == 'current'
        if not isinstance(paths, list) or (not paths and not current) or len(paths) > 1000:
            raise PublishError('The reviewed manifest must contain exact changed file paths.')
        paths = sorted({self.safe_path(path) for path in paths})
        if any((self.root / path).is_dir() for path in paths):
            raise PublishError('Directory publication is not supported for reviewed tasks.')
        snapshot = repository_snapshot(str(self.root), threading.Event())
        if snapshot is None or digest(snapshot) != plan.get('snapshot'):
            raise PublishError('Repository files or index changed after verification. Run a new task/check.')
        if self.remote_head(remote, branch) != (plan.get('remote_head', '') if current else plan['head']):
            raise PublishError('The remote branch differs from the task baseline. Reconcile it and verify again; no force push.')
        batches, batch, size = [], [], 0
        for path in paths:
            if batch and size + len(path) > 12000:
                batches.append(batch)
                batch, size = [], 0
            batch.append(path)
            size += len(path) + 3
        if batch:
            batches.append(batch)
        for batch in batches:
            attributes = self.git('check-attr', '-z', 'filter', '--', *batch).split(b'\0')
            if any(value not in (b'unspecified', b'unset') for value in attributes[2::3]):
                raise PublishError('Custom Git filters require manual review; publication was blocked before staging.')
        staged = self.names('diff', '--cached', '--name-only', '--no-renames', '-z')
        if staged - set(paths):
            raise PublishError('The index contains unrelated changes; publication was blocked.')
        # Fixed-size batches; explicit paths only, including tracked deletions.
        for batch in batches:
            self.git('add', '--', *batch)
        if self.names('diff', '--cached', '--name-only', '--no-renames', '-z') != set(paths):
            raise PublishError('Staged files differ from the reviewed manifest.')
        if not current:
            self.git('diff', '--cached', '--check')
        tree = self.git('write-tree').decode('ascii').strip()
        for path in paths:
            entry = self.git('ls-tree', '-z', tree, '--', path)
            source = self.root / path
            if not source.exists():
                if entry:
                    raise PublishError('A reviewed deletion is still present in the staged tree.')
                continue
            fields = entry.split(b'\t', 1)[0].split()
            expected_blob = self.git('hash-object', '--path=' + path, '--', path).strip()
            if (len(fields) != 3 or fields[0] not in (b'100644', b'100755')
                    or fields[1] != b'blob' or fields[2] != expected_blob):
                raise PublishError('The staged content differs from the reviewed source files.')
        staged_snapshot = repository_snapshot(str(self.root), threading.Event())
        if staged_snapshot is None or source_digest(staged_snapshot) != plan['sources']:
            raise PublishError('Source files changed during staging; publication was blocked.')
        if paths:
            self.git('commit', '-m', message, timeout=90)
        commit = self.head()
        parents = self.git('rev-list', '--parents', '-n', '1', commit).decode('ascii').split()[1:]
        expected_parents = [plan['head']] if plan['head'] else []
        committed_tree = self.git('rev-parse', commit + '^{tree}').decode('ascii').strip()
        after = repository_snapshot(str(self.root), threading.Event())
        if (not commit or (paths and parents != expected_parents) or (not paths and commit != plan['head']) or committed_tree != tree
                or after is None or source_digest(after) != plan['sources']
                or self.destination() != (branch, remote)):
            raise PublishError('The commit, files, branch or origin changed during publication. A local commit may remain; push was blocked.')
        try:
            # URL is pinned, never a mutable remote alias; no tags/mirror/force.
            already = current and plan.get('remote_head') == commit
            if not already:
                self.git('-c', 'push.followTags=false', 'push', '--no-follow-tags', remote,
                         f'{commit}:refs/heads/{branch}', timeout=120)
            if self.remote_head(remote, branch) != commit:
                raise PublishError('The remote branch does not point to the created commit.')
            if self.destination() != (branch, remote) or self.head() != commit:
                raise PublishError('The local destination changed before publication could be confirmed.')
        except PublishError as exc:
            raise PublishUncertain(str(exc) + ' Publication is not confirmed; do not retry automatically.') from None
        result = {'status': 'published', 'verified': True, 'commit': commit, 'branch': branch, 'remote': remote}
        if current:
            result.update(already_published=already, created_commit=bool(paths), technical_checks_only=True)
        return result

    def publish(self, paths: list[str], message: str) -> dict[str, str]:
        if not isinstance(paths, list) or len(paths) > 1000:
            raise PublishError('paths must be a list of at most 1000 repository-relative paths.')
        if not isinstance(message, str) or not message.strip() or '\x00' in message or len(message) > 10000:
            raise PublishError('A nonempty commit message of at most 10000 characters is required.')
        requested = {self.safe_path(path) for path in paths}
        branch, remote = self.destination()
        staged = self.names('diff', '--cached', '--name-only', '--no-renames', '-z')
        changed = staged | self.names('diff', '--name-only', '--no-renames', '-z')
        changed |= self.names('ls-files', '--others', '--exclude-standard', '-z')

        def selected(path: str) -> bool:
            return any(request == '.' or path == request or path.startswith(request + '/') for request in requested)

        if any(not selected(path) for path in staged):
            raise PublishError('The index contains changes outside the requested paths. Review or unstage them before publishing.')
        # Expand directory requests to changed files first, so git add never sweeps in credentials.
        files = sorted(self.safe_path(path) for path in changed if selected(path))
        if files:
            # Batches stay below Windows command-line length limits.
            batch: list[str] = []
            size = 0
            for path in files:
                if batch and size + len(path) > 12000:
                    self.git('add', '--', *batch)
                    batch, size = [], 0
                batch.append(path)
                size += len(path) + 3
            if batch:
                self.git('add', '--', *batch)
        after_stage = self.names('diff', '--cached', '--name-only', '--no-renames', '-z')
        if after_stage - set(files):
            raise PublishError('The Git index changed during publication. Review it before trying again.')
        if after_stage:
            self.git('commit', '-m', message, timeout=90)
        commit = self.git('rev-parse', '--verify', 'HEAD').decode('ascii').strip()
        # Recheck the destination immediately before pushing, including after commit hooks.
        if self.destination() != (branch, remote):
            raise PublishError('The publication branch or origin changed. Review the checkout before trying again.')
        self.git('push', '--set-upstream', 'origin', f'{commit}:refs/heads/{branch}', timeout=120)
        return {'commit': commit, 'branch': branch, 'remote': remote}


TOOLS = [{
    'name': 'publish_changes',
    'description': ('Commit the specified repository-relative files and push the current branch to its HTTPS GitHub origin. '
                    'Use only when the user asks to publish. Directory paths include changed files recursively; credentials are rejected. '
                    'An empty paths list pushes the current commit without staging files. No force push or automatic retry.'),
    'inputSchema': {
        'type': 'object', 'additionalProperties': False,
        'properties': {
            'paths': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 1000},
            'message': {'type': 'string', 'minLength': 1, 'maxLength': 10000},
        },
        'required': ['paths', 'message'],
    },
}]


def handle_request(request: object, publisher: GitPublisher) -> dict | None:
    if not isinstance(request, dict):
        return {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32600, 'message': 'Invalid request'}}
    ident = request.get('id')
    method = request.get('method')
    if request.get('jsonrpc') != '2.0' or not isinstance(method, str):
        return {'jsonrpc': '2.0', 'id': ident, 'error': {'code': -32600, 'message': 'Invalid request'}}
    if 'id' not in request:
        return None
    response = {'jsonrpc': '2.0', 'id': ident}
    params = request.get('params', {})
    if not isinstance(params, dict):
        return dict(response, error={'code': -32602, 'message': 'Invalid params'})
    if method == 'initialize':
        return dict(response, result={
            'protocolVersion': params.get('protocolVersion', '2024-11-05'),
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'agent-relay-git', 'version': '1.0.0'},
        })
    if method == 'ping':
        return dict(response, result={})
    if method == 'tools/list':
        return dict(response, result={'tools': TOOLS})
    if method == 'tools/call':
        arguments = params.get('arguments', {})
        try:
            if params.get('name') != 'publish_changes':
                raise PublishError('Unknown publication tool.')
            if not isinstance(arguments, dict) or set(arguments) != {'paths', 'message'}:
                raise PublishError('publish_changes requires only paths and message.')
            value = publisher.publish(arguments['paths'], arguments['message'])
            return dict(response, result={'content': [{'type': 'text', 'text': json.dumps(value)}]})
        except PublishError as exc:
            return dict(response, result={'isError': True, 'content': [{'type': 'text', 'text': str(exc)}]})
        except Exception:
            return dict(response, result={'isError': True, 'content': [{'type': 'text', 'text': 'Publication failed unexpectedly. Inspect the checkout before retrying.'}]})
    return dict(response, error={'code': -32601, 'message': 'Method not found'})


def serve(publisher: GitPublisher, incoming=None, outgoing=None) -> None:
    incoming = sys.stdin if incoming is None else incoming
    outgoing = sys.stdout if outgoing is None else outgoing
    for line in incoming:
        try:
            request = json.loads(line)
        except (ValueError, TypeError):
            response = {'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Parse error'}}
        else:
            response = handle_request(request, publisher)
        if response is not None:
            outgoing.write(json.dumps(response) + '\n')
            outgoing.flush()


def main() -> int:
    sys.stdin.reconfigure(encoding='utf-8')
    sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--workdir', required=True)
    args = parser.parse_args()
    try:
        publisher = GitPublisher(args.workdir)
    except PublishError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    serve(publisher)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
