#!/usr/bin/env python3
"""Open the fixed maintenance capability without following attacker paths."""
from __future__ import annotations
import argparse,fcntl,os,stat,sys
from pathlib import Path
import test_capability

ROOT=Path("/run/driftward-hatchet-bridge"); NAME="maintenance.lock"
class LockError(RuntimeError): pass
def _settings() -> tuple[Path, int, int]:
    cap = test_capability.load()
    if cap is None: return ROOT, 0, 0
    return cap.runtime, cap.uid, cap.gid
def acquire(root_path:Path, uid:int, gid:int)->None:
    try:
        root_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        run=os.open(root_path.parent,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            try: os.mkdir(root_path.name,0o750,dir_fd=run)
            except FileExistsError: pass
        finally: os.close(run)
        root=os.open(root_path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    except OSError as e: raise LockError("unsafe maintenance lock directory") from e
    try:
        try: fd=os.open(NAME,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_CLOEXEC,0o600,dir_fd=root)
        except OSError as e: raise LockError("unsafe maintenance lock") from e
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_gid!=gid or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1: raise LockError("invalid maintenance lock metadata")
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        os.set_inheritable(fd,True); os.dup2(fd,3,inheritable=True)
        if fd!=3: os.close(fd)
    finally:
        # ``root`` can initially occupy FD 3.  After dup2 the number names
        # the inherited lock capability, not the directory descriptor.
        if root != 3: os.close(root)
def acquire_existing(root_path:Path, uid:int, gid:int)->None:
    try:
        root=os.open(root_path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try: fd=os.open(NAME,os.O_RDWR|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=root)
        finally: os.close(root)
    except OSError as e: raise LockError("existing maintenance lock is unavailable") from e
    try:
        info=os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_gid!=gid or stat.S_IMODE(info.st_mode)!=0o600 or info.st_nlink!=1: raise LockError("invalid existing maintenance lock metadata")
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        os.set_inheritable(fd,True); os.dup2(fd,3,inheritable=True)
        if fd!=3: os.close(fd)
    except BaseException:
        if fd!=3: os.close(fd)
        raise
def valid(root_path:Path)->bool:
    try:
        root=os.open(root_path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            expected=os.stat(NAME,dir_fd=root,follow_symlinks=False); actual=os.fstat(3)
            return stat.S_ISREG(actual.st_mode) and (expected.st_dev,expected.st_ino)==(actual.st_dev,actual.st_ino)
        finally: os.close(root)
    except OSError: return False
def main()->int:
    p=argparse.ArgumentParser(); p.add_argument("mode",choices=("check","exec","exec-existing")); p.add_argument("command",nargs=argparse.REMAINDER); a=p.parse_args()
    root_path, uid, gid = _settings()
    if a.mode=="check": return 0 if valid(root_path) else 1
    if not a.command: p.error("exec needs command")
    (acquire_existing if a.mode=="exec-existing" else acquire)(root_path, uid, gid); test_capability.audit("maintenance-lock-exec"); os.execvp(a.command[0],a.command)
if __name__=="__main__":
    try: raise SystemExit(main())
    except (LockError,OSError) as e: print(f"maintenance lock failed: {e}",file=sys.stderr); raise SystemExit(1)
