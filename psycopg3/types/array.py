"""
Adapters for arrays
"""

# Copyright (C) 2020 The Psycopg Team

import re
import struct
from typing import Any, Generator, List, Optional, Tuple

from .. import errors as e
from ..adapt import Format, Adapter, TypeCaster, Transformer
from ..adapt import AdaptContext
from .oids import builtins

TEXT_OID = builtins["text"].oid
TEXT_ARRAY_OID = builtins["text"].array_oid


class BaseListAdapter(Adapter):
    def __init__(self, src: type, context: AdaptContext = None):
        super().__init__(src, context)
        self._tx = Transformer(context)

    def _array_oid(self, base_oid: int) -> int:
        """
        Return the oid of the array from the oid of the base item.

        Fall back on text[].
        TODO: we shouldn't consider builtins only, but other adaptation
        contexts too
        """
        oid = 0
        if base_oid:
            info = builtins.get(base_oid)
            if info is not None:
                oid = info.array_oid

        return oid or TEXT_ARRAY_OID


@Adapter.text(list)
class TextListAdapter(BaseListAdapter):
    # from https://www.postgresql.org/docs/current/arrays.html#ARRAYS-IO
    #
    # The array output routine will put double quotes around element values if
    # they are empty strings, contain curly braces, delimiter characters,
    # double quotes, backslashes, or white space, or match the word NULL.
    # TODO: recognise only , as delimiter. Should be configured
    _re_needs_quote = re.compile(
        br"""(?xi)
          ^$              # the empty string
        | ["{},\\\s]      # or a char to escape
        | ^null$          # or the word NULL
        """
    )

    # Double quotes and backslashes embedded in element values will be
    # backslash-escaped.
    _re_escape = re.compile(br'(["\\])')

    def adapt(self, obj: List[Any]) -> Tuple[bytes, int]:
        tokens: List[bytes] = []

        oid = 0

        def adapt_list(obj: List[Any]) -> None:
            nonlocal oid

            if not obj:
                tokens.append(b"{}")
                return

            tokens.append(b"{")
            for item in obj:
                if isinstance(item, list):
                    adapt_list(item)
                elif item is None:
                    tokens.append(b"NULL")
                else:
                    ad = self._tx.adapt(item)
                    if isinstance(ad, tuple):
                        if oid == 0:
                            oid = ad[1]
                            got_type = type(item)
                        elif oid != ad[1]:
                            raise e.DataError(
                                f"array contains different types,"
                                f" at least {got_type} and {type(item)}"
                            )
                        ad = ad[0]

                    if ad is not None:
                        if self._re_needs_quote.search(ad) is not None:
                            ad = (
                                b'"' + self._re_escape.sub(br"\\\1", ad) + b'"'
                            )
                        tokens.append(ad)
                    else:
                        tokens.append(b"NULL")

                tokens.append(b",")

            tokens[-1] = b"}"

        adapt_list(obj)

        return b"".join(tokens), self._array_oid(oid)


@Adapter.binary(list)
class BinaryListAdapter(BaseListAdapter):
    def adapt(self, obj: List[Any]) -> Tuple[bytes, int]:
        if not obj:
            return _struct_head.pack(0, 0, TEXT_OID), TEXT_ARRAY_OID

        data: List[bytes] = [b"", b""]  # placeholders to avoid a resize
        dims: List[int] = []
        hasnull = 0
        oid = 0

        def calc_dims(L: List[Any]) -> None:
            if isinstance(L, self.src):
                if not L:
                    raise e.DataError("lists cannot contain empty lists")
                dims.append(len(L))
                calc_dims(L[0])

        calc_dims(obj)

        def adapt_list(L: List[Any], dim: int) -> None:
            nonlocal oid, hasnull
            if len(L) != dims[dim]:
                raise e.DataError("nested lists have inconsistent lengths")

            if dim == len(dims) - 1:
                for item in L:
                    ad = self._tx.adapt(item, Format.BINARY)
                    if isinstance(ad, tuple):
                        if oid == 0:
                            oid = ad[1]
                            got_type = type(item)
                        elif oid != ad[1]:
                            raise e.DataError(
                                f"array contains different types,"
                                f" at least {got_type} and {type(item)}"
                            )
                        ad = ad[0]
                    if ad is None:
                        hasnull = 1
                        data.append(b"\xff\xff\xff\xff")
                    else:
                        data.append(_struct_len.pack(len(ad)))
                        data.append(ad)
            else:
                for item in L:
                    if not isinstance(item, self.src):
                        raise e.DataError(
                            "nested lists have inconsistent depths"
                        )
                    adapt_list(item, dim + 1)  # type: ignore

        adapt_list(obj, 0)

        if oid == 0:
            oid = TEXT_OID

        data[0] = _struct_head.pack(len(dims), hasnull, oid or TEXT_OID)
        data[1] = b"".join(_struct_dim.pack(dim, 1) for dim in dims)
        return b"".join(data), self._array_oid(oid)


class ArrayCasterBase(TypeCaster):
    base_oid: int

    def __init__(self, oid: int, context: AdaptContext = None):
        super().__init__(oid, context)
        self._tx = Transformer(context)


class ArrayCasterText(ArrayCasterBase):

    # Tokenize an array representation into item and brackets
    # TODO: currently recognise only , as delimiter. Should be configured
    _re_parse = re.compile(
        br"""(?xi)
        (     [{}]                        # open or closed bracket
            | " (?: [^"\\] | \\. )* "     # or a quoted string
            | [^"{},\\]+                  # or an unquoted non-empty string
        ) ,?
        """
    )

    def cast(self, data: bytes) -> List[Any]:
        rv = None
        stack: List[Any] = []
        cast = self._tx.get_cast_function(self.base_oid, Format.TEXT)

        for m in self._re_parse.finditer(data):
            t = m.group(1)
            if t == b"{":
                a: List[Any] = []
                if rv is None:
                    rv = a
                if stack:
                    stack[-1].append(a)
                stack.append(a)

            elif t == b"}":
                if not stack:
                    raise e.DataError("malformed array, unexpected '}'")
                rv = stack.pop()

            else:
                if not stack:
                    wat = (
                        t[:10].decode("utf8", "replace") + "..."
                        if len(t) > 10
                        else ""
                    )
                    raise e.DataError(f"malformed array, unexpected '{wat}'")
                if t == b"NULL":
                    v = None
                else:
                    if t.startswith(b'"'):
                        t = self._re_unescape.sub(br"\1", t[1:-1])
                    v = cast(t)

                stack[-1].append(v)

        assert rv is not None
        return rv

    _re_unescape = re.compile(br"\\(.)")


_struct_head = struct.Struct("!III")
_struct_dim = struct.Struct("!II")
_struct_len = struct.Struct("!i")


class ArrayCasterBinary(ArrayCasterBase):
    def cast(self, data: bytes) -> List[Any]:
        ndims, hasnull, oid = _struct_head.unpack_from(data[:12])
        if not ndims:
            return []

        fcast = self._tx.get_cast_function(oid, Format.BINARY)

        p = 12 + 8 * ndims
        dims = [
            _struct_dim.unpack_from(data, i)[0] for i in list(range(12, p, 8))
        ]

        def consume(p: int) -> Generator[Any, None, None]:
            while 1:
                size = _struct_len.unpack_from(data, p)[0]
                p += 4
                if size != -1:
                    yield fcast(data[p : p + size])
                    p += size
                else:
                    yield None

        items = consume(p)

        def agg(dims: List[int]) -> List[Any]:
            if not dims:
                return next(items)
            else:
                dim, dims = dims[0], dims[1:]
                return [agg(dims) for _ in range(dim)]

        return agg(dims)


def register(
    array_oid: int,
    base_oid: int,
    context: AdaptContext = None,
    name: Optional[str] = None,
) -> None:
    if not name:
        name = f"oid{base_oid}"

    for format, base in (
        (Format.TEXT, ArrayCasterText),
        (Format.BINARY, ArrayCasterBinary),
    ):
        tcname = f"{name.title()}Array{format.name.title()}Caster"
        t = type(tcname, (base,), {"base_oid": base_oid})
        TypeCaster.register(array_oid, t, context=context, format=format)


def register_all_arrays() -> None:
    """
    Associate the array oid of all the types in TypeCaster.globals.

    This function is designed to be called once at import time, after having
    registered all the base casters.
    """
    for t in builtins:
        if t.array_oid and (
            (t.oid, Format.TEXT) in TypeCaster.globals
            or (t.oid, Format.BINARY) in TypeCaster.globals
        ):
            register(t.array_oid, t.oid, name=t.name)