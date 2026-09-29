"""Cold, same-image backups for an isolated portable Factory installation."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import subprocess
import tarfile
import uuid

import bootstrap

VOLUMES = (
    "hatchet-postgres-data", "hatchet-config-data", "bridge-data", "ingress-data",
    "lifecycle-data", "linear-token-data", "linear-published-token",
)
PROJECT_VOLUMES = (*VOLUMES, "operator-socket", "maintenance")
CONFIG_FILES = {".env", bootstrap.MARKER, "env/hatchet-client.env",
                *("env/" + name for name in bootstrap.ENV_FILES),
                *("keys/hatchet/" + name for name in ("master.key", "private_ec256.key", "public_ec256.key", "public_handle_unencrypted_ec256.key"))}
DEPLOYMENT_FILES = ("docker-compose.yml", "compose.portable.yml")
IDENTITY_LABEL = "io.driftward.factory.installation"


def _docker(args: list[str], env: dict | None = None) -> str:
    result = subprocess.run(["docker", *args], env=env, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f"Docker {args[0]} failed; private output withheld")
    return result.stdout.strip()


def _path(path: Path) -> Path:
    path = bootstrap.checked_path(path)
    if "," in str(path):
        raise ValueError("snapshot/installation paths must not contain commas")
    return path


def _hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _private_files(directory: Path) -> set[str]:
    files = set()
    for path in directory.rglob("*"):
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode) or not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
            raise ValueError("state must contain only regular files and directories")
        if info.st_mode & 0o077:
            raise ValueError("snapshot configuration must be private")
        if path.is_file():
            files.add(path.relative_to(directory).as_posix())
    return files


def _archive_check(path: Path) -> None:
    with tarfile.open(path) as archive:
        for entry in archive:
            name = PurePosixPath(entry.name)
            if name.is_absolute() or ".." in name.parts or not (entry.isfile() or entry.isdir()):
                raise ValueError("unsafe volume archive; links and special files are not supported")


def _available_volumes(env: dict | None = None) -> set[str]:
    return set(_docker(["volume", "ls", "--format", "{{.Name}}"], env).splitlines())


def _no_writers(volumes: list[str], env: dict | None = None) -> None:
    for volume in volumes:
        if _docker(["ps", "--filter", f"volume={volume}", "--format", "{{.ID}}"], env):
            raise ValueError("a running container still mounts a snapshot volume; refusing to continue")


def _tar(image: str, volume: str, directory: Path, filename: str, *, restore: bool, env: dict | None = None) -> None:
    # Container root must read/restore database ownership, but archive files
    # must belong to the host operator. Stream over stdio, never a writable bind.
    command = ["docker", "run", "--rm", "--network", "none", "--read-only", "--security-opt", "no-new-privileges:true",
               "--mount", f"type=volume,src={volume},dst=/state" + ("" if restore else ",readonly"),
               *(["--interactive"] if restore else []), "--entrypoint", "tar", image,
               "-xf" if restore else "-cf", "-", "-C", "/state", *([] if restore else ["."])]
    flags = os.O_RDONLY if restore else os.O_WRONLY | os.O_CREAT | os.O_EXCL
    fd = os.open(directory / filename, flags | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "rb" if restore else "wb") as archive:
        result = subprocess.run(command, env=env, stdin=archive if restore else subprocess.DEVNULL,
                                stdout=subprocess.DEVNULL if restore else archive, stderr=subprocess.PIPE)
    if result.returncode:
        raise RuntimeError("Docker volume archive operation failed; private output withheld")


def backup(installation, snapshot: Path, *, apply: bool = False) -> None:
    source = _path(installation.directory)
    snapshot = _path(snapshot)
    metadata, values = bootstrap.validate_directory(source)
    installation.assert_owned_resources()
    if snapshot.exists() or source == snapshot or source in snapshot.parents or not snapshot.parent.is_dir():
        raise ValueError("snapshot must be a new directory outside the installation")
    files = {".env", bootstrap.MARKER}
    for relative in ("env", "keys"):
        files.update(relative + "/" + name for name in _private_files(source / relative))
    if files != CONFIG_FILES:
        raise ValueError("backup requires complete keys/client credentials and only known configuration files")
    config = json.loads(installation.compose("config", "--format", "json", private=True))
    images = {item["image"] for item in config["services"].values()}
    image_ids = {image: _docker(["image", "inspect", "--format", "{{.Id}}", image], installation.env) for image in images}
    existing = _available_volumes(installation.env)
    volumes = [suffix for suffix in VOLUMES if f"{installation.project}-{suffix}" in existing]
    if not set(VOLUMES[:5]).issubset(volumes):
        raise ValueError("installation has not created all required durable volumes")
    for suffix in volumes:
        info = json.loads(_docker(["volume", "inspect", f"{installation.project}-{suffix}"], installation.env))[0]
        labels = info.get("Labels") or {}
        if labels.get("com.docker.compose.project") != installation.project or labels.get("com.docker.compose.volume") != "factory-" + suffix or labels.get(IDENTITY_LABEL) != metadata["installation_id"]:
            raise ValueError("volume does not belong to the validated installation")
    print(f"Cold backup: stop {installation.project}; preserve {len(volumes)} volumes; destination {snapshot}")
    if not apply:
        return
    installation.compose("stop")
    _no_writers([f"{installation.project}-{suffix}" for suffix in volumes], installation.env)
    snapshot.mkdir(mode=0o700)
    for name in ("config", "config/env", "config/keys", "config/keys/hatchet", "volumes"):
        (snapshot / name).mkdir(mode=0o700)
    for relative in sorted(files):
        bootstrap._create_private(snapshot / "config" / relative, bootstrap._read_private(source / relative))
    for suffix in volumes:
        _tar(image_ids[values["FACTORY_IMAGE"]], f"{installation.project}-{suffix}", snapshot / "volumes", f"{suffix}.tar", restore=False, env=installation.env)
        archive = snapshot / "volumes" / f"{suffix}.tar"
        archive.chmod(0o600)
        _archive_check(archive)
    hashes = {name: _hash(snapshot / name) for name in sorted(_private_files(snapshot))}
    bootstrap._create_private(snapshot / "manifest.json", json.dumps({
        "format_version": 1, "project": installation.project, "installation_id": metadata["installation_id"],
        "factory_image": values["FACTORY_IMAGE"], "images": image_ids, "volumes": volumes, "sha256": hashes,
        "deployment_sha256": {name: _hash(Path(__file__).parent / name) for name in DEPLOYMENT_FILES},
    }, sort_keys=True, indent=2) + "\n")
    print("Backup complete; installation remains stopped. Use up to resume it explicitly.")


def restore(snapshot: Path, directory: Path, project: str, *, apply: bool = False, port_offset: int = 0) -> None:
    snapshot, directory = _path(snapshot), _path(directory)
    if not snapshot.is_dir() or snapshot.stat().st_mode & 0o077:
        raise ValueError("snapshot must be a private directory")
    manifest = json.loads(bootstrap._read_private(snapshot / "manifest.json"))
    if manifest.get("format_version") != 1:
        raise ValueError("unsupported snapshot format")
    if manifest.get("deployment_sha256") != {name: _hash(Path(__file__).parent / name) for name in DEPLOYMENT_FILES}:
        raise ValueError("restore requires the same deployment bundle; cross-version restore is unsupported")
    if directory.exists() or snapshot == directory or snapshot in directory.parents:
        raise ValueError("restore requires a new directory outside the snapshot")
    if project == manifest["project"]:
        raise ValueError("restore requires a new project name")
    # Reuse bootstrap's project, image, path and port validation without writing.
    updated = bootstrap.init_directory(directory, manifest["factory_image"], project, port_offset=port_offset)
    volumes = manifest["volumes"]
    if not isinstance(volumes, list) or len(volumes) != len(set(volumes)) or not set(VOLUMES[:5]).issubset(volumes) or not set(volumes).issubset(VOLUMES):
        raise ValueError("invalid snapshot volume list")
    expected = {"config/" + name for name in CONFIG_FILES} | {f"volumes/{suffix}.tar" for suffix in volumes}
    if set(manifest["sha256"]) != expected or _private_files(snapshot) != expected | {"manifest.json"}:
        raise ValueError("snapshot contains missing or unexpected files")
    for name, digest in manifest["sha256"].items():
        if _hash(snapshot / name) != digest:
            raise ValueError("snapshot checksum mismatch")
    for suffix in volumes:
        _archive_check(snapshot / "volumes" / f"{suffix}.tar")
    values = bootstrap.read_env(snapshot / "config" / ".env")
    marker = json.loads(bootstrap._read_private(snapshot / "config" / bootstrap.MARKER))
    if values["FACTORY_IMAGE"] != manifest["factory_image"] or marker["installation_id"] != manifest["installation_id"] or values["FACTORY_PROJECT_NAME"] != manifest["project"]:
        raise ValueError("snapshot installation identity mismatch")
    if _docker(["ps", "--all", "--filter", f"label=com.docker.compose.project={project}", "--format", "{{.ID}}"]):
        raise ValueError("restore project already has containers")
    if project in _docker(["network", "ls", "--format", "{{.Name}}"]).splitlines():
        raise ValueError("restore project already has a network")
    existing = _available_volumes()
    if any(f"{project}-{suffix}" in existing for suffix in PROJECT_VOLUMES):
        raise ValueError("restore project already has volumes")
    if manifest["factory_image"] not in manifest["images"]:
        raise ValueError("snapshot does not identify its Factory image")
    print(f"Restore {len(volumes)} cold volumes into new project {project} at {directory}; leave stopped")
    if not apply:
        return
    for image, image_id in manifest["images"].items():
        if "@sha256:" not in image:
            # Local development images must already exist; never pull a mutable tag.
            actual = _docker(["image", "inspect", "--format", "{{.Id}}", image])
        else:
            _docker(["pull", image])
            actual = _docker(["image", "inspect", "--format", "{{.Id}}", image])
        if actual != image_id:
            raise ValueError("restore requires the exact saved images; cross-version restore is unsupported")
    directory.mkdir(mode=0o700)
    for relative in ("env", "keys", "keys/hatchet", "run", "run/operator"):
        (directory / relative).mkdir(mode=0o700)
    for name in sorted(CONFIG_FILES - {".env", bootstrap.MARKER}):
        bootstrap._create_private(directory / name, bootstrap._read_private(snapshot / "config" / name))
    for key in ("FACTORY_PROJECT_NAME", "COMPOSE_PROJECT_NAME", "FACTORY_INSTALL_DIR", "FACTORY_ENV_DIR", "FACTORY_OPERATOR_RUN_DIR", "HATCHET_SERVER_URL", *bootstrap.PORTS):
        values[key] = updated[key]
    bootstrap._create_private(directory / ".env", bootstrap.env_text(values))
    restored_id = str(uuid.uuid4())
    for suffix in volumes:
        name = f"{project}-{suffix}"
        # Check again immediately before creation. Never target an existing volume.
        if name in _available_volumes():
            raise ValueError("restore volume appeared concurrently; refusing to overwrite it")
        _docker(["volume", "create", "--label", f"com.docker.compose.project={project}",
                 "--label", f"com.docker.compose.volume=factory-{suffix}", "--label", f"{IDENTITY_LABEL}={restored_id}", name])
        info = json.loads(_docker(["volume", "inspect", name]))[0]
        if (info.get("Labels") or {}).get(IDENTITY_LABEL) != restored_id:
            raise ValueError("restore volume was created by another installation; refusing to overwrite it")
        _no_writers([name])
        _tar(manifest["images"][manifest["factory_image"]], name, snapshot / "volumes", f"{suffix}.tar", restore=True)
    marker.update(project=project, installation_id=restored_id)
    bootstrap._create_private(directory / bootstrap.MARKER, json.dumps(marker, sort_keys=True) + "\n")
    bootstrap.validate_directory(directory)
    print("Restore complete and stopped. Start only this new installation; retain the snapshot and original volumes.")
