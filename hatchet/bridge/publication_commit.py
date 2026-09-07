#!/usr/bin/env python3
"""Descriptor-safe bridge publication commit and rollback helper.

The command has fixed production paths.  ``Publication`` is deliberately a
private test seam; neither the installer nor the command accepts a target-root
override.  Snapshot and commit run in this same process so there is no shell
mutation gap between its final descriptor reproof and atomic publication.
"""
from __future__ import annotations
import argparse, base64, hashlib, json, os, stat, sys, time, uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import test_capability

HERE=Path(__file__).resolve().parent
INVENTORY=json.loads((HERE/"publication-targets.json").read_text())
ETC=Path("/etc/driftward"); SYSTEMD=Path("/etc/systemd/system"); RELEASE=Path("/docker/driftward/hatchet/releases/bridge"); STATE=Path("/var/lib/driftward-hatchet-bridge")

class PublicationError(RuntimeError): pass

@dataclass(frozen=True)
class Publication:
    etc: Path=ETC; systemd: Path=SYSTEMD; release: Path=RELEASE
    targets: tuple[str,...]=tuple(INVENTORY["targets"]+[f"/etc/systemd/system/{u}" for u in INVENTORY["units"]])
    directories: tuple[str,...]=tuple(INVENTORY["directories"])
    transients: tuple[str,...]=tuple(INVENTORY["transients"])
    uid: int=0; gid: int=0
    state: Path=STATE

def command_publication() -> Publication:
    cap=test_capability.load()
    if cap is None: return Publication()
    return Publication(
        etc=cap.etc, systemd=cap.systemd, release=cap.release,
        targets=tuple(str(cap.root/value.lstrip("/")) for value in Publication.targets),
        directories=tuple(str(cap.root/value.lstrip("/")) for value in Publication.directories),
        transients=tuple(str(cap.root/value.lstrip("/")) for value in Publication.transients),
        uid=cap.uid, gid=cap.gid, state=cap.state,
    )

class PublicationTransaction:
    """Retains every parent directory descriptor until terminal journal fsync."""
    def __init__(self,pub:Publication): self.pub=pub; self.parents:dict[Path,tuple[int,os.stat_result]]={}
    def open_parent(self,path:Path)->int:
        parent=path.parent
        if parent not in self.parents:
            fd=_open_dir(parent); self.parents[parent]=(fd,os.fstat(fd))
        fd,baseline=self.parents[parent]
        now=os.fstat(fd)
        if (now.st_dev,now.st_ino)!=(baseline.st_dev,baseline.st_ino): raise PublicationError(f"publication parent replaced: {parent}")
        return fd
    def verify_entry(self,path:Path,expected:dict[str,Any]|None=None)->dict[str,Any]:
        fd=self.open_parent(path); info=os.stat(path.name,dir_fd=fd,follow_symlinks=False) if expected is not None else None
        if expected is not None and (info.st_dev,info.st_ino)!=(expected["dev"],expected["ino"]): raise PublicationError(f"publication target replaced: {path}")
        return {"dev":info.st_dev,"ino":info.st_ino} if info else {}
    def close(self)->None:
        for fd,_ in self.parents.values(): os.close(fd)

def _hashfd(fd:int)->str:
    os.lseek(fd,0,os.SEEK_SET); h=hashlib.sha256()
    while data:=os.read(fd,1<<20): h.update(data)
    return h.hexdigest()

def _write_all(fd:int, data:bytes)->None:
    """Durably write a record even when write(2) is short or interrupted."""
    view=memoryview(data)
    while view:
        try: wrote=os.write(fd,view)
        except InterruptedError: continue
        if wrote<=0: raise PublicationError("short publication journal write")
        view=view[wrote:]

def _open_dir(path:Path)->int:
    try: return os.open(path,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    except OSError as e: raise PublicationError(f"unsafe publication directory: {path}") from e

def _at(dirfd:int,name:str,flags:int)->int:
    try: return os.open(name,flags|os.O_NOFOLLOW|os.O_CLOEXEC,dir_fd=dirfd)
    except OSError as e: raise PublicationError(f"unsafe publication target: {name}") from e

def _entry(parent:Path,name:str, uid:int,gid:int)->dict[str,Any]:
    try: fd=_open_dir(parent)
    except PublicationError:
        return {"state":"absent"}
    try:
        try: info=os.stat(name,dir_fd=fd,follow_symlinks=False)
        except FileNotFoundError: return {"state":"absent"}
        if stat.S_ISLNK(info.st_mode):
            target=os.readlink(name,dir_fd=fd)
            return {"state":"symlink","mode":stat.S_IMODE(info.st_mode),"uid":info.st_uid,"gid":info.st_gid,"target":target}
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=uid or info.st_gid!=gid or info.st_nlink!=1: raise PublicationError(f"unsafe existing target: {parent/name}")
        data=_at(fd,name,os.O_RDONLY)
        try:
            digest=_hashfd(data)
            os.lseek(data,0,os.SEEK_SET)
            content=base64.b64encode(os.read(data,info.st_size)).decode()
        finally: os.close(data)
        return {"state":"file","mode":stat.S_IMODE(info.st_mode),"uid":info.st_uid,"gid":info.st_gid,"dev":info.st_dev,"ino":info.st_ino,"mtime_ns":info.st_mtime_ns,"sha256":digest,"content":content}
    finally: os.close(fd)

def _dir_entry(path:Path, uid:int,gid:int)->dict[str,Any]:
    try: info=os.lstat(path)
    except FileNotFoundError: return {"state":"absent"}
    if not stat.S_ISDIR(info.st_mode) or info.st_uid!=uid or info.st_gid!=gid: raise PublicationError(f"unsafe publication directory: {path}")
    fd=_open_dir(path)
    try: empty=not any(name not in (".","..") for name in os.listdir(fd))
    finally: os.close(fd)
    return {"state":"dir","mode":stat.S_IMODE(info.st_mode),"uid":info.st_uid,"gid":info.st_gid,"empty":empty}

def snapshot(pub:Publication)->dict[str,Any]:
    values=[]
    for target in pub.targets:
        p=Path(target); values.append({"path":target,"entry":_entry(p.parent,p.name,pub.uid,pub.gid)})
    for path in pub.directories:
        values.append({"path":path,"entry":_dir_entry(Path(path),pub.uid,pub.gid)})
    for path in pub.transients:
        p=Path(path); values.append({"path":path,"entry":_entry(p.parent,p.name,pub.uid,pub.gid)})
    # Inodes prove the pre-publication snapshot has not been swapped before
    # commit, but rollback cannot recreate an inode after atomically restoring
    # a file.  The terminal rollback digest is therefore deliberately a
    # content/metadata/directory-shape proof, while `_verify_snapshot` still
    # compares the full entries (including dev/ino) before publication.
    def rollback_view(value:Any)->Any:
        if isinstance(value,dict): return {key:rollback_view(item) for key,item in value.items() if key not in {"dev","ino"}}
        if isinstance(value,list): return [rollback_view(item) for item in value]
        return value
    encoded=json.dumps(rollback_view(values),sort_keys=True,separators=(",",":")).encode()
    return {"version":3,"entries":values,"digest":hashlib.sha256(encoded).hexdigest()}

class Journal:
    """No-clobber, fsynced publication transaction evidence."""
    def __init__(self, pub:Publication, transaction:str):
        self.pub=pub; self.transaction=transaction; self.path=pub.state/f"publication-{transaction}.journal"
    def append(self, record:dict[str,Any])->None:
        self.pub.state.mkdir(mode=0o700,parents=True,exist_ok=True)
        fd=os.open(self.path,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,0o600) if not self.path.exists() else os.open(self.path,os.O_WRONLY|os.O_APPEND|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            _write_all(fd,(json.dumps(record,sort_keys=True,separators=(",",":"))+"\n").encode()); os.fsync(fd)
        finally: os.close(fd)
        parent=_open_dir(self.pub.state); os.fsync(parent); os.close(parent)
    def records(self)->list[dict[str,Any]]:
        fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC)
        try:
            raw=os.read(fd,1<<24).decode()
            if not raw.endswith("\n"): raise PublicationError("torn publication journal")
            try: return [json.loads(line) for line in raw.splitlines()]
            except json.JSONDecodeError as exc: raise PublicationError("torn publication journal") from exc
        finally: os.close(fd)

def _journal(pub:Publication, snap:dict[str,Any], version:str, transition:str="normal", transaction:str|None=None)->Journal:
    j=Journal(pub,transaction or uuid.uuid4().hex)
    j.append({"status":"prepared","transaction":j.transaction,"commit":version,"snapshot":snap,"desired":{"release":version,"units":INVENTORY["units"]},"transition":transition,"operations":["directories","policy","routing","units","release","current"]})
    test_capability.audit("prepared-journal", transaction=j.transaction)
    test_capability.kill_if("prepared-journal")
    return j

def _verify_snapshot(pub:Publication, expected:dict[str,Any])->None:
    actual=snapshot(pub)
    if actual["digest"]!=expected.get("digest") or actual["entries"]!=expected.get("entries"): raise PublicationError("publication pre-state changed")

def _mkdir(path:Path,mode:int,uid:int,gid:int,created:list[str])->None:
    if path.exists():
        before=_dir_entry(path,uid,gid)
        if before["state"]!="dir": raise PublicationError(f"unsafe existing directory: {path}")
        return
    if not path.parent.exists(): _mkdir(path.parent,0o755,uid,gid,created)
    parent_fd=_open_dir(path.parent)
    try:
        os.mkdir(path.name,mode,dir_fd=parent_fd); os.chown(path.name,uid,gid,dir_fd=parent_fd,follow_symlinks=False); os.fsync(parent_fd); created.append(str(path))
    finally: os.close(parent_fd)

def _write(parent:Path,name:str,data:bytes,mode:int,uid:int,gid:int,created:list[str],tx:PublicationTransaction|None=None,mtime_ns:int|None=None)->None:
    fd=tx.open_parent(parent/name) if tx else _open_dir(parent); owned=tx is None; temp=f".{name}.publish-{os.getpid()}"
    try:
        try: os.unlink(temp,dir_fd=fd)
        except FileNotFoundError: pass
        out=os.open(temp,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC,mode,dir_fd=fd)
        try: os.write(out,data); os.fsync(out)
        finally: os.close(out)
        os.chown(temp,uid,gid,dir_fd=fd,follow_symlinks=False); os.chmod(temp,mode,dir_fd=fd,follow_symlinks=False)
        os.replace(temp,name,src_dir_fd=fd,dst_dir_fd=fd)
        if mtime_ns is not None: os.utime(name,ns=(mtime_ns,mtime_ns),dir_fd=fd,follow_symlinks=False)
        os.fsync(fd); created.append(str(parent/name))
        verify=_entry(parent,name,uid,gid)
        expected={"state":"file","mode":mode,"uid":uid,"gid":gid,"sha256":hashlib.sha256(data).hexdigest(),"content":base64.b64encode(data).decode()}
        if mtime_ns is not None: expected["mtime_ns"]=mtime_ns
        if any(verify.get(k)!=v for k,v in expected.items()): raise PublicationError(f"publication verification failed: {name}")
    finally:
        try: os.unlink(temp,dir_fd=fd)
        except FileNotFoundError: pass
        if owned: os.close(fd)

def _symlink(parent:Path,name:str,target:str,uid:int,gid:int,created:list[str],tx:PublicationTransaction|None=None)->None:
    fd=tx.open_parent(parent/name) if tx else _open_dir(parent); owned=tx is None; temp=f".{name}.publish-{os.getpid()}"
    try:
        try: os.unlink(temp,dir_fd=fd)
        except FileNotFoundError: pass
        os.symlink(target,temp,dir_fd=fd); os.replace(temp,name,src_dir_fd=fd,dst_dir_fd=fd); os.fsync(fd); created.append(str(parent/name))
        got=_entry(parent,name,uid,gid)
        if got["state"]!="symlink" or got["target"]!=target: raise PublicationError("release symlink verification failed")
    finally:
        try: os.unlink(temp,dir_fd=fd)
        except FileNotFoundError: pass
        if owned: os.close(fd)

def commit(pub:Publication, stage:Path, version:str, expected:dict[str,Any], transition:str="normal", fail_after:int|None=None, transaction:str|None=None)->dict[str,Any]:
    _verify_snapshot(pub,expected)
    # Reproof and every mutation below are intentionally in this function.
    journal=_journal(pub,expected,version,transition,transaction); created:list[str]=[]; step=0; tx=PublicationTransaction(pub)
    def done(name:str)->None:
        nonlocal step
        step+=1; journal.append({"status":"applied","step":step,"operation":name,"created":list(created)})
        test_capability.audit("publication-applied", operation=name, step=step)
        test_capability.kill_if(f"publication-applied-{name}")
        if fail_after==step: raise PublicationError(f"injected interruption after {name}")
    try:
        _mkdir(pub.etc,0o750,pub.uid,pub.gid,created); _mkdir(pub.release.parent,0o755,pub.uid,pub.gid,created); _mkdir(pub.release,0o755,pub.uid,pub.gid,created); done("directories")
        _write(pub.etc,"hatchet-routing-registry.json",(stage/"etc/driftward/hatchet-routing-registry.json").read_bytes(),0o640,pub.uid,pub.gid,created,tx); done("routing")
        for unit in INVENTORY["units"]: _write(pub.systemd,unit,(stage/"units"/unit).read_bytes(),0o644,pub.uid,pub.gid,created,tx)
        done("units")
        release=pub.release/version
        if release.exists(): raise PublicationError("release target unexpectedly exists")
        os.rename(stage/"release",release); fd=_open_dir(pub.release); os.fsync(fd); os.close(fd); created.append(str(release)); done("release")
        _symlink(pub.release,"current",str(release),pub.uid,pub.gid,created,tx); done("current")
        journal.append({"status":"committed","created":created})
        test_capability.audit("committed-journal")
        test_capability.kill_if("committed-journal")
        return {"created":created,"orphan_release":str(release),"journal":str(journal.path)}
    except BaseException: raise
    finally: tx.close()

def rollback(pub:Publication, journal_path:Path, *, recovery_evidence:bool=False)->None:
    """Restore exact pre-state from the prepared journal; leave its evidence."""
    journal=Journal(pub,journal_path.name.removeprefix("publication-").removesuffix(".journal"))
    records=journal.records()
    if not records or records[0].get("status")!="prepared": raise PublicationError("invalid publication journal")
    snap=records[0]["snapshot"]
    snap_paths={item["path"] for item in snap["entries"]}
    failures:list[str]=[]
    # Release-version directories are dynamic and therefore appear only in the
    # operation log. Remove contents first, then their containing directory.
    for record in reversed(records):
        for value in reversed(record.get("created",[])):
            path=Path(value)
            if str(path) in snap_paths or not str(path).startswith(str(pub.release)+"/"): continue
            try:
                info=os.lstat(path)
                if stat.S_ISLNK(info.st_mode) or stat.S_ISREG(info.st_mode): path.unlink()
                elif stat.S_ISDIR(info.st_mode):
                    for root, dirs, files in os.walk(path,topdown=False,followlinks=False):
                        for name in files: (Path(root)/name).unlink()
                        for name in dirs: os.rmdir(Path(root)/name)
                    os.rmdir(path)
            # Each applied journal record carries the cumulative created set.
            # A crash after several records therefore repeats paths while
            # walking backwards; an already-removed entry is the expected
            # idempotent rollback result, not a recovery failure.
            except FileNotFoundError: pass
            except OSError as exc: failures.append(f"remove {path}: {exc}")
    # Restore concrete leaves before handling any directory.  This preserves
    # pre-existing parents and ensures absent parents can be removed last.
    directory_paths=set(pub.directories)
    entries=[item for item in snap["entries"] if item["path"] not in directory_paths]
    directories=[item for item in snap["entries"] if item["path"] in directory_paths]
    for item in entries:
        path=Path(item["path"]); entry=item["entry"]
        try:
            if entry["state"]=="file":
                if not isinstance(entry.get("mtime_ns"),int): raise PublicationError(f"snapshot lacks mtime proof: {path}")
                path.parent.mkdir(parents=True,exist_ok=True); _write(path.parent,path.name,base64.b64decode(entry["content"]),entry["mode"],entry["uid"],entry["gid"],[],mtime_ns=entry["mtime_ns"])
            elif entry["state"]=="symlink": _symlink(path.parent,path.name,entry["target"],entry["uid"],entry["gid"],[])
            else:
                try:
                    if path.is_dir() and not path.is_symlink(): os.rmdir(path)
                    else: path.unlink()
                except FileNotFoundError: pass
        except OSError as exc: failures.append(f"restore {path}: {exc}")
    # Directory removal/restoration is deliberately last and deepest first.
    for item in sorted(directories,key=lambda x:len(x["path"]),reverse=True):
        path=Path(item["path"]); entry=item["entry"]
        try:
            if entry["state"]=="absent":
                if path.exists(): os.rmdir(path)
            else:
                path.mkdir(parents=True,exist_ok=True); os.chmod(path,entry["mode"])
        except OSError as exc: failures.append(f"restore directory {path}: {exc}")
    if failures: raise PublicationError("rollback failures: "+"; ".join(failures))
    if snapshot(pub)["digest"]!=snap["digest"]: raise PublicationError("rollback pre-state digest mismatch")
    terminal={"status":"rolled_back"}
    if recovery_evidence:
        # The safe-down wrapper deliberately rolls even a publication-committed
        # journal: publication is not the recovery commit boundary.  Health
        # and marker removal have not happened, so restoring this snapshot is
        # the only state in which a retry can be proved from scratch.
        terminal["recovery"]="safe_down_retry_fresh_install"
        terminal["publication_boundary"]=records[-1].get("status")
    journal.append(terminal)

def main()->int:
    p=argparse.ArgumentParser(); p.add_argument("mode",choices=("snapshot","commit","rollback","check-incomplete")); p.add_argument("--snapshot"); p.add_argument("--stage"); p.add_argument("--release-version"); p.add_argument("--journal"); p.add_argument("--transaction"); p.add_argument("--recovery-evidence",action="store_true")
    a=p.parse_args(); pub=command_publication()
    if a.mode=="snapshot":
        if not a.snapshot: p.error("snapshot needs --snapshot")
        Path(a.snapshot).write_text(json.dumps(snapshot(pub),sort_keys=True)); return 0
    if a.mode=="check-incomplete":
        for path in pub.state.glob("publication-*.journal"):
            rows=Journal(pub,path.name.removeprefix("publication-").removesuffix(".journal")).records()
            if not rows or rows[-1].get("status") not in ("committed","rolled_back"): raise PublicationError("incomplete publication journal")
        return 0
    if a.mode=="rollback":
        if not a.journal: p.error("rollback needs --journal")
        rollback(pub,Path(a.journal),recovery_evidence=a.recovery_evidence); return 0
    if not a.stage or not a.release_version: p.error("commit needs --stage and --release-version")
    expected=json.loads(Path(a.snapshot).read_text()); commit(pub,Path(a.stage),a.release_version,expected,transaction=a.transaction); return 0
if __name__=="__main__":
    try: raise SystemExit(main())
    except PublicationError as e: print(f"publication failed: {e}",file=sys.stderr); raise SystemExit(1)
