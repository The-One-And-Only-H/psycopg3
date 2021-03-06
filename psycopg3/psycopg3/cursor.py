"""
psycopg3 cursor objects
"""

# Copyright (C) 2020 The Psycopg Team

import sys
from types import TracebackType
from typing import Any, AsyncIterator, Callable, Generic, Iterator, List
from typing import Optional, Sequence, Type, TYPE_CHECKING
from contextlib import contextmanager

from . import errors as e
from . import pq
from .pq import ConnStatus, ExecStatus, Format
from .copy import Copy, AsyncCopy
from .proto import ConnectionType, Query, Params, DumpersMap, LoadersMap, PQGen
from ._column import Column
from ._queries import PostgresQuery

if sys.version_info >= (3, 7):
    from contextlib import asynccontextmanager
else:
    from .utils.context import asynccontextmanager

if TYPE_CHECKING:
    from .proto import Transformer
    from .pq.proto import PGconn, PGresult
    from .connection import Connection, AsyncConnection  # noqa: F401

execute: Callable[["PGconn"], PQGen[List["PGresult"]]]

if pq.__impl__ == "c":
    from psycopg3_c import _psycopg3

    execute = _psycopg3.execute

else:
    from . import generators

    execute = generators.execute


class BaseCursor(Generic[ConnectionType]):
    ExecStatus = pq.ExecStatus

    _transformer: "Transformer"
    _rowcount: int

    def __init__(
        self,
        connection: ConnectionType,
        format: Format = Format.TEXT,
    ):
        self._conn = connection
        self.format = format
        self.dumpers: DumpersMap = {}
        self.loaders: LoadersMap = {}
        self._reset()
        self.arraysize = 1
        self._closed = False

    def _reset(self) -> None:
        self._results: List["PGresult"] = []
        self.pgresult = None
        self._pos = 0
        self._iresult = 0
        self._rowcount = -1
        self._query: Optional[bytes] = None
        self._params: Optional[List[Optional[bytes]]] = None

    @property
    def connection(self) -> ConnectionType:
        """The connection this cursor is using."""
        return self._conn

    @property
    def closed(self) -> bool:
        """`True` if the cursor is closed."""
        return self._closed

    @property
    def status(self) -> Optional[pq.ExecStatus]:
        # TODO: do we want this?
        res = self.pgresult
        return res.status if res else None

    @property
    def query(self) -> Optional[bytes]:
        """The last query sent to the server, if available."""
        return self._query

    @property
    def params(self) -> Optional[List[Optional[bytes]]]:
        """The last set of parameters sent to the server, if available."""
        return self._params

    @property
    def pgresult(self) -> Optional["PGresult"]:
        """The `~psycopg3.pq.PGresult` exposed by the cursor."""
        return self._pgresult

    @pgresult.setter
    def pgresult(self, result: Optional["PGresult"]) -> None:
        self._pgresult = result
        if result and self._transformer:
            self._transformer.pgresult = result

    @property
    def description(self) -> Optional[List[Column]]:
        """
        A list of `Column` objects describing the current resultset.

        `!None` if the current resultset didn't return tuples.
        """
        res = self.pgresult
        if not res or res.status != ExecStatus.TUPLES_OK:
            return None
        return [Column(self, i) for i in range(res.nfields)]

    @property
    def rowcount(self) -> int:
        """Number of records affected by the precedent operation."""
        return self._rowcount

    def setinputsizes(self, sizes: Sequence[Any]) -> None:
        # no-op
        pass

    def setoutputsize(self, size: Any, column: Optional[int] = None) -> None:
        # no-op
        pass

    def nextset(self) -> Optional[bool]:
        """
        Move to the next result set if `execute()` returned more than one.

        Return `!True` if a new result is available, which will be the one
        methods `!fetch*()` will operate on.
        """
        self._iresult += 1
        if self._iresult < len(self._results):
            self.pgresult = self._results[self._iresult]
            self._pos = 0
            nrows = self.pgresult.command_tuples
            self._rowcount = nrows if nrows is not None else -1
            return True
        else:
            return None

    def _start_query(self) -> None:
        from . import adapt

        if self.closed:
            raise e.InterfaceError("the cursor is closed")

        if self._conn.closed:
            raise e.InterfaceError("the connection is closed")

        if self._conn.pgconn.status != ConnStatus.OK:
            raise e.InterfaceError(
                f"cannot execute operations: the connection is"
                f" in status {self._conn.pgconn.status}"
            )

        self._reset()
        self._transformer = adapt.Transformer(self)

    def _execute_send(
        self, query: Query, params: Optional[Params], no_pqexec: bool = False
    ) -> None:
        """
        Implement part of execute() before waiting common to sync and async
        """
        pgq = PostgresQuery(self._transformer)
        pgq.convert(query, params)

        if pgq.params or no_pqexec or self.format == Format.BINARY:
            self._query = pgq.query
            self._params = pgq.params
            self._conn.pgconn.send_query_params(
                pgq.query,
                pgq.params,
                param_formats=pgq.formats,
                param_types=pgq.types,
                result_format=self.format,
            )
        else:
            # if we don't have to, let's use exec_ as it can run more than
            # one query in one go
            self._query = pgq.query
            self._params = None
            self._conn.pgconn.send_query(pgq.query)

    _status_ok = {
        ExecStatus.TUPLES_OK,
        ExecStatus.COMMAND_OK,
        ExecStatus.EMPTY_QUERY,
    }
    _status_copy = {
        ExecStatus.COPY_IN,
        ExecStatus.COPY_OUT,
        ExecStatus.COPY_BOTH,
    }

    def _execute_results(self, results: Sequence["PGresult"]) -> None:
        """
        Implement part of execute() after waiting common to sync and async
        """
        if not results:
            raise e.InternalError("got no result from the query")

        statuses = {res.status for res in results}
        badstats = statuses - self._status_ok
        if not badstats:
            self._results = list(results)
            self.pgresult = results[0]
            nrows = self.pgresult.command_tuples
            if nrows is not None:
                if self._rowcount < 0:
                    self._rowcount = nrows
                else:
                    self._rowcount += nrows

            return

        if results[-1].status == ExecStatus.FATAL_ERROR:
            raise e.error_from_result(
                results[-1], encoding=self._conn.client_encoding
            )
        elif badstats & self._status_copy:
            raise e.ProgrammingError(
                "COPY cannot be used with execute(); use copy() insead"
            )
        else:
            raise e.InternalError(
                f"got unexpected status from query:"
                f" {', '.join(sorted(s.name for s in sorted(badstats)))}"
            )

    def _send_prepare(
        self, name: bytes, query: Query, params: Optional[Params]
    ) -> PostgresQuery:
        """
        Implement part of execute() before waiting common to sync and async
        """
        pgq = PostgresQuery(self._transformer)
        pgq.convert(query, params)

        self._query = pgq.query
        self._conn.pgconn.send_prepare(name, pgq.query, param_types=pgq.types)

        return pgq

    def _send_query_prepared(self, name: bytes, pgq: PostgresQuery) -> None:
        self._params = pgq.params
        self._conn.pgconn.send_query_prepared(
            name,
            pgq.params,
            param_formats=pgq.formats,
            result_format=self.format,
        )

    def _check_result(self) -> None:
        res = self.pgresult
        if not res:
            raise e.ProgrammingError("no result available")
        elif res.status != ExecStatus.TUPLES_OK:
            raise e.ProgrammingError(
                "the last operation didn't produce a result"
            )

    def _check_copy_results(self, results: Sequence["PGresult"]) -> None:
        """
        Check that the value returned in a copy() operation is a legit COPY.
        """
        if len(results) != 1:
            raise e.InternalError(
                f"expected 1 result from copy, got {len(results)} instead"
            )

        result = results[0]
        status = result.status
        if status in (ExecStatus.COPY_IN, ExecStatus.COPY_OUT):
            return
        elif status == ExecStatus.FATAL_ERROR:
            raise e.error_from_result(
                result, encoding=self._conn.client_encoding
            )
        else:
            raise e.ProgrammingError(
                "copy() should be used only with COPY ... TO STDOUT or COPY ..."
                f" FROM STDIN statements, got {ExecStatus(status).name}"
            )


class Cursor(BaseCursor["Connection"]):
    __module__ = "psycopg3"

    def __enter__(self) -> "Cursor":
        return self

    def __exit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        self.close()

    def close(self) -> None:
        """
        Close the current cursor and free associated resources.
        """
        self._closed = True
        self._reset()

    def execute(
        self, query: Query, params: Optional[Params] = None
    ) -> "Cursor":
        """
        Execute a query or command to the database.
        """
        with self._conn.lock:
            self._start_query()
            self._conn._start_query()
            self._execute_send(query, params)
            gen = execute(self._conn.pgconn)
            results = self._conn.wait(gen)
            self._execute_results(results)
        return self

    def executemany(self, query: Query, params_seq: Sequence[Params]) -> None:
        """
        Execute the same command with a sequence of input data.
        """
        with self._conn.lock:
            self._start_query()
            self._conn._start_query()
            first = True
            for params in params_seq:
                if first:
                    pgq = self._send_prepare(b"", query, params)
                    gen = execute(self._conn.pgconn)
                    (result,) = self._conn.wait(gen)
                    if result.status == ExecStatus.FATAL_ERROR:
                        raise e.error_from_result(
                            result, encoding=self._conn.client_encoding
                        )
                else:
                    pgq.dump(params)

                self._send_query_prepared(b"", pgq)
                gen = execute(self._conn.pgconn)
                (result,) = self._conn.wait(gen)
                self._execute_results((result,))

    def fetchone(self) -> Optional[Sequence[Any]]:
        """
        Return the next record from the current recordset.

        Return `!None` the recordset is finished.
        """
        self._check_result()
        rv = self._transformer.load_row(self._pos)
        if rv is not None:
            self._pos += 1
        return rv

    def fetchmany(self, size: int = 0) -> List[Sequence[Any]]:
        """
        Return the next *size* records from the current recordset.

        *size* default to `!self.arraysize` if not specified.
        """
        self._check_result()
        if not size:
            size = self.arraysize

        pos = self._pos
        load = self._transformer.load_row
        rv: List[Sequence[Any]] = []

        for _ in range(size):
            row = load(pos)
            if row is None:
                break
            pos += 1
            rv.append(row)

        self._pos = pos
        return rv

    def fetchall(self) -> List[Sequence[Any]]:
        """
        Return all the remaining records from the current recordset.
        """
        return list(self)

    def __iter__(self) -> Iterator[Sequence[Any]]:
        self._check_result()

        load = self._transformer.load_row

        while 1:
            row = load(self._pos)
            if row is None:
                break
            self._pos += 1
            yield row

    @contextmanager
    def copy(self, statement: Query) -> Iterator[Copy]:
        """
        Initiate a :sql:`COPY` operation and return an object to manage it.
        """
        with self._start_copy(statement) as copy:
            yield copy

    def _start_copy(self, statement: Query) -> Copy:
        with self._conn.lock:
            self._start_query()
            self._conn._start_query()
            # Make sure to avoid PQexec to avoid receiving a mix of COPY and
            # other operations.
            self._execute_send(statement, None, no_pqexec=True)
            gen = execute(self._conn.pgconn)
            results = self._conn.wait(gen)
            self._check_copy_results(results)
            self.pgresult = results[0]  # will set it on the transformer too

        return Copy(self)


class AsyncCursor(BaseCursor["AsyncConnection"]):
    __module__ = "psycopg3"

    async def __aenter__(self) -> "AsyncCursor":
        return self

    async def __aexit__(
        self,
        exc_type: Optional[Type[BaseException]],
        exc_val: Optional[BaseException],
        exc_tb: Optional[TracebackType],
    ) -> None:
        await self.close()

    async def close(self) -> None:
        self._closed = True
        self._reset()

    async def execute(
        self, query: Query, params: Optional[Params] = None
    ) -> "AsyncCursor":
        async with self._conn.lock:
            self._start_query()
            await self._conn._start_query()
            self._execute_send(query, params)
            gen = execute(self._conn.pgconn)
            results = await self._conn.wait(gen)
            self._execute_results(results)
        return self

    async def executemany(
        self, query: Query, params_seq: Sequence[Params]
    ) -> None:
        async with self._conn.lock:
            self._start_query()
            await self._conn._start_query()
            first = True
            for params in params_seq:
                if first:
                    pgq = self._send_prepare(b"", query, params)
                    gen = execute(self._conn.pgconn)
                    (result,) = await self._conn.wait(gen)
                    if result.status == ExecStatus.FATAL_ERROR:
                        raise e.error_from_result(
                            result, encoding=self._conn.client_encoding
                        )
                else:
                    pgq.dump(params)

                self._send_query_prepared(b"", pgq)
                gen = execute(self._conn.pgconn)
                (result,) = await self._conn.wait(gen)
                self._execute_results((result,))

    async def fetchone(self) -> Optional[Sequence[Any]]:
        self._check_result()
        rv = self._transformer.load_row(self._pos)
        if rv is not None:
            self._pos += 1
        return rv

    async def fetchmany(self, size: int = 0) -> List[Sequence[Any]]:
        self._check_result()
        if not size:
            size = self.arraysize

        pos = self._pos
        load = self._transformer.load_row
        rv: List[Sequence[Any]] = []

        for _ in range(size):
            row = load(pos)
            if row is None:
                break
            pos += 1
            rv.append(row)

        self._pos = pos
        return rv

    async def fetchall(self) -> List[Sequence[Any]]:
        res = []
        async for rec in self:
            res.append(rec)

        return res

    async def __aiter__(self) -> AsyncIterator[Sequence[Any]]:
        self._check_result()

        load = self._transformer.load_row

        while 1:
            row = load(self._pos)
            if row is None:
                break
            self._pos += 1
            yield row

    @asynccontextmanager
    async def copy(self, statement: Query) -> AsyncIterator[AsyncCopy]:
        copy = await self._start_copy(statement)
        async with copy:
            yield copy

    async def _start_copy(self, statement: Query) -> AsyncCopy:
        async with self._conn.lock:
            self._start_query()
            await self._conn._start_query()
            # Make sure to avoid PQexec to avoid receiving a mix of COPY and
            # other operations.
            self._execute_send(statement, None, no_pqexec=True)
            gen = execute(self._conn.pgconn)
            results = await self._conn.wait(gen)
            self._check_copy_results(results)
            self.pgresult = results[0]  # will set it on the transformer too

        return AsyncCopy(self)


class NamedCursorMixin:
    pass


class NamedCursor(NamedCursorMixin, Cursor):
    pass


class AsyncNamedCursor(NamedCursorMixin, AsyncCursor):
    pass
