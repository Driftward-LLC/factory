/** Fixed runtime-side reader: no checkout, repository config, hooks, or worker credentials. */
export const PINNED_PR_DIFF_QUERY = String.raw`
import json, os, re, resource, selectors, signal, subprocess, sys, tempfile, time
from contextlib import contextmanager

repo, base, head, output_limit, budget_ms = sys.argv[1:]
assert re.fullmatch(r'[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+', repo)
assert all(part not in ('.', '..') for part in repo.split('/'))
assert all(re.fullmatch(r'[a-f0-9]{40}', sha) for sha in (base, head))
output_limit, budget_ms = int(output_limit), int(budget_ms)
assert 0 < output_limit <= 16 * 1024 * 1024 and 0 < budget_ms <= 120000
deadline = time.monotonic() + budget_ms / 1000
disk_limit = 256 * 1024 * 1024
resource.setrlimit(resource.RLIMIT_FSIZE, (disk_limit, disk_limit))
resource.setrlimit(resource.RLIMIT_AS, (512 * 1024 * 1024, 512 * 1024 * 1024))
resource.setrlimit(resource.RLIMIT_CPU, (60, 60))

def expired(*_):
    raise TimeoutError()

signal.signal(signal.SIGTERM, expired)
signal.signal(signal.SIGALRM, expired)
signal.setitimer(signal.ITIMER_REAL, budget_ms / 1000)
environment = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
environment.update(GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_GLOBAL='/dev/null',
                   GIT_TERMINAL_PROMPT='0', GIT_ASKPASS='/bin/false', LC_ALL='C')

@contextmanager
def source_directory():
    temporary = tempfile.TemporaryDirectory(prefix='factory-pr-diff-')
    try:
        yield temporary.name
    finally:
        # The reader's watchdog must not interrupt removal halfway through.
        signal.setitimer(signal.ITIMER_REAL, 0)
        temporary.cleanup()

def source_size(directory):
    size = 0
    for root, _, files in os.walk(directory):
        for filename in files:
            try:
                size += os.lstat(os.path.join(root, filename)).st_size
            except FileNotFoundError:
                # Git can rename or remove lock files between the listing and lstat.
                pass
    return size

try:
    with source_directory() as directory:
        git = ['git', '-c', 'core.hooksPath=/dev/null', '-c', 'core.fsmonitor=false',
               '-c', 'gc.auto=0', '-c', 'maintenance.auto=false', '-c', 'fetch.unpackLimit=1',
               '-c', 'protocol.file.allow=never', '-c', 'protocol.ext.allow=never',
               '-c', 'credential.helper=', '-c', 'credential.helper=!gh auth git-credential',
               '--git-dir=' + directory + '/source.git']

        def run(arguments, limit=output_limit):
            process = subprocess.Popen(git + arguments, env=environment, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
            try:
                with selectors.DefaultSelector() as selected:
                    selected.register(process.stdout, selectors.EVENT_READ, 0)
                    selected.register(process.stderr, selectors.EVENT_READ, 1)
                    buffers = [bytearray(), bytearray()]
                    while selected.get_map():
                        if time.monotonic() >= deadline:
                            raise TimeoutError()
                        if source_size(directory) > disk_limit:
                            raise RuntimeError()
                        for key, _ in selected.select(min(.05, max(0, deadline - time.monotonic()))):
                            chunk = os.read(key.fileobj.fileno(), 65536)
                            if not chunk:
                                selected.unregister(key.fileobj)
                                continue
                            buffers[key.data].extend(chunk)
                            if len(buffers[key.data]) > (limit if key.data == 0 else 65536):
                                raise OverflowError()
                    if process.wait(timeout=max(.001, deadline - time.monotonic())) != 0:
                        raise RuntimeError()
                    return bytes(buffers[0]).decode('utf-8', errors='strict')
            finally:
                # Docker exec cancellation does not reliably signal its remote children.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
                process.stdout.close()
                process.stderr.close()

        run(['init', '--bare', '--template='], 65536)
        run(['remote', 'add', 'origin', 'https://github.com/' + repo + '.git'], 65536)
        run(['config', 'remote.origin.promisor', 'true'], 65536)
        run(['config', 'remote.origin.partialclonefilter', 'blob:none'], 65536)
        run(['fetch', '--quiet', '--filter=blob:none', '--no-tags', '--no-recurse-submodules',
             'origin', base, head], 65536)
        for sha in (base, head):
            assert run(['rev-parse', '--verify', sha + '^{commit}'], 65536).strip() == sha
        # Full ancestry is fetched; a shallow boundary must never stand in for the PR merge base.
        assert run(['rev-parse', '--is-shallow-repository'], 65536).strip() == 'false'
        comparison = base + '...' + head
        options = ['--no-ext-diff', '--no-textconv', '--no-color', '--find-renames', '--ignore-submodules=none']
        names = run(['diff'] + options + ['--name-only', '-z', comparison, '--'])
        assert names.endswith('\0')
        files = [{'path': name} for name in names[:-1].split('\0')]
        diff = run(['diff'] + options + ['--full-index', '--src-prefix=a/', '--dst-prefix=b/', comparison, '--'])
        result = json.dumps({'files': files, 'diffText': diff}, ensure_ascii=True)
        if len(result.encode()) > output_limit:
            raise OverflowError()
        # Clean up source objects before handing evidence back across the gateway.
    sys.stdout.write(result)
except OverflowError:
    sys.exit(73)
except (TimeoutError, subprocess.TimeoutExpired):
    sys.exit(74)
except Exception:
    # Neither repository content nor credential-helper diagnostics belong in error evidence.
    sys.exit(75)
`;

export function isGitHubDiffSizeLimit(error: unknown): boolean {
  if (!error || typeof error !== 'object') return false;
  const failure = error as { code?: unknown; killed?: unknown; stderr?: unknown };
  return failure.code === 1 && failure.killed !== true && typeof failure.stderr === 'string'
    && /HTTP 406\b/.test(failure.stderr)
    && /\btoo_large\b|diff exceeded the maximum|diff is too large/i.test(failure.stderr);
}
