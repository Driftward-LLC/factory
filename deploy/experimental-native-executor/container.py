"""Fixed executor-role bootstrap; never consult shared Hermes homes or host auth."""
import os
from pathlib import Path
import shutil
import stat
import sys

HOME = Path("/tmp/factory-native-profile")
SEED = Path("/run/factory-native-seed")
ENV_KEYS = {"OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GH_TOKEN"}


def prepare():
    # Seeds contain newly provisioned API/App credentials only. OAuth sessions,
    # refreshable shared auth, plugins, hooks and historical state are excluded.
    allowed = {"config.yaml", ".env", ".gitconfig", "skills"}
    if set(p.name for p in SEED.iterdir()) - allowed:
        raise ValueError("unsupported seed input")
    total = 0
    files = []
    for base, directories, names in os.walk(SEED, followlinks=False):
        for name in directories + names:
            source = Path(base, name)
            info = source.lstat()
            relative = source.relative_to(SEED)
            if (not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))
                    or info.st_mode & 0o022 or info.st_uid not in (0, 10000)
                    or stat.S_ISREG(info.st_mode) and info.st_nlink != 1
                    or len(relative.parts) > 12):
                raise ValueError("unsafe seed input")
            if stat.S_ISREG(info.st_mode):
                total += info.st_size
                if total > 4 * 1024 * 1024 or len(files) >= 256:
                    raise ValueError("seed too large")
                files.append((source, relative))
    HOME.mkdir(mode=0o700)  # Never reuse an earlier profile, including empty ones.
    for source, relative in files:
        destination = HOME / relative
        destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        shutil.copyfile(source, destination)
        destination.chmod(0o600)
    (HOME / "workspace").mkdir(mode=0o700)
    (HOME / "ready").touch(mode=0o600)


def environment():
    from dotenv import dotenv_values
    values = dotenv_values(HOME / ".env", interpolate=False)
    if set(values) - ENV_KEYS or any(not value or "\n" in value or "\r" in value for value in values.values()):
        raise ValueError("unsupported executor environment")
    os.environ.update(values)
    os.environ.update(HOME=str(HOME), HERMES_HOME=str(HOME),
                      XDG_CONFIG_HOME=str(HOME / ".config"), XDG_CACHE_HOME=str(HOME / ".cache"),
                      TERMINAL_CWD=str(HOME / "workspace"))
    os.chdir(HOME / "workspace")


try:
    if sys.argv[1] != "--existing":
        prepare()
    else:
        if not (HOME / "ready").is_file():
            raise ValueError("profile unavailable")
    environment()
    command = sys.argv[2:]
    os.execvpe(command[0], command, os.environ)
except Exception:
    # Diagnostics could contain credential values or native output.
    sys.exit(1)
