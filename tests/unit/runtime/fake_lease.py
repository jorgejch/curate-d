"""An in-memory stand-in for advisory locks in one PostgreSQL database."""

import threading

import psycopg

from curate_d.runtime.lease import ExecutorLease


class FakeLockServer:
    """Advisory locks shared by every session it opens, as within one database."""

    def __init__(self, events: list[str] | None = None) -> None:
        self.holders: dict[int, FakeSession] = {}
        self.events = events if events is not None else []
        self.sessions: list[FakeSession] = []
        self.refuse_connections = 0
        """Connection attempts to fail before accepting one."""
        self.refuse_locks = 0
        """Lock attempts to refuse, as if another session held the key."""
        self.fail_probes = False
        """Make every `holds` check fail, as a dropped connection would."""
        self._mutex = threading.Lock()

    def connect(self) -> "FakeSession":
        """Open a session, or fail while `refuse_connections` is positive."""
        if self.refuse_connections > 0:
            self.refuse_connections -= 1
            raise psycopg.OperationalError("connection refused")
        session = FakeSession(self)
        self.sessions.append(session)
        return session

    def lease(self, executor_id: str, *, probe_interval: float = 0.01) -> ExecutorLease:
        """Return a real lease whose sessions this server provides."""
        return ExecutorLease(executor_id, connect=self.connect, probe_interval=probe_interval)

    def factory(self, _settings: object, executor_id: str) -> ExecutorLease:
        """Match the worker's lease factory signature."""
        return self.lease(executor_id)


class FakeSession:
    """One session; closing it releases its locks, like a PostgreSQL backend exiting."""

    def __init__(self, server: FakeLockServer) -> None:
        self.server = server
        self.closed = False

    def _check(self) -> None:
        if self.closed:
            raise psycopg.OperationalError("the connection is closed")

    def try_lock(self, key: int) -> bool:
        self._check()
        with self.server._mutex:
            if self.server.refuse_locks > 0:
                self.server.refuse_locks -= 1
                return False
            holder = self.server.holders.setdefault(key, self)
        if holder is self:
            self.server.events.append("lease-acquired")
        return holder is self

    def try_lock_shared(self, key: int) -> bool:
        raise NotImplementedError

    def unlock_shared(self, key: int) -> None:
        raise NotImplementedError

    def holds(self, key: int) -> bool:
        self._check()
        if self.server.fail_probes:
            raise psycopg.OperationalError("server closed the connection unexpectedly")
        return self.server.holders.get(key) is self

    def unlock(self, key: int) -> None:
        self._check()
        with self.server._mutex:
            if self.server.holders.get(key) is self:
                del self.server.holders[key]
                self.server.events.append("lease-released")

    def holder(self, key: int) -> str | None:
        self._check()
        return "pid 42 (fake)" if key in self.server.holders else None

    def close(self) -> None:
        with self.server._mutex:
            for key in [key for key, holder in self.server.holders.items() if holder is self]:
                del self.server.holders[key]
        self.closed = True
