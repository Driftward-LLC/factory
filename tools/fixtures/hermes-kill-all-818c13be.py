# Verbatim ProcessRegistry.kill_all from NousResearch/hermes-agent
# 818c13be1dc4fd28987e1e881a9408224afd4535 tools/process_registry.py.
# Whole upstream file SHA256: 4dacd3f6753d7b81e4ab338f7612624ec1f11c074cc969db49024a967a3b625e
# Extracted for disposable selection tests; never import Hermes recovery code.
def kill_all(
    self, task_id: Optional[str] = None, *, exclude_ids: frozenset = frozenset(),
    source: str = "kill_all", consume_output: bool = False) -> int:
    """Kill all running processes, optionally only those ``task_id`` spawned (its ``owner_task_id``).
    Returns how many it stopped. A session whose systemd scope did not stop is not counted
    (its descendants may live on), and a later sweep retries it even after it finished.

    Sessions with ``persist_on_release=True`` are skipped when ``source`` is an
    agent-lifecycle sweep (the default ``kill_all`` release path, a gateway turn
    timeout, or ``agent_close``): an explicitly persisted background job must survive
    session end / compression / error recovery (#41225). An explicit operator stop
    (``process.kill`` via process_manage, CLI /stop) passes its own source and still
    reaches them, so the user can always stop a persisted process on purpose."""
    lifecycle = source in self._LIFECYCLE_KILL_SOURCES
    with self._lock:
        targets = [
            s for s in (*self._running.values(), *self._finished.values())
            if (task_id is None or s.owner_task_id == task_id)
            and s.id not in exclude_ids and (not s.exited or s._scope_stop_pending)
            and not (lifecycle and s.persist_on_release)
        ]
    results = [self.kill_process(s.id, source=source, consume_output=consume_output) for s in targets]
    return sum(
        r.get("status") in {"killed", "already_exited"} and "scope_stop_failed" not in r
        for r in results)
