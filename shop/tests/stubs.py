"""Stand-ins for fastapi, sqlalchemy and stripe, so the shop's own code (app/main.py) can be
imported and exercised without installing those packages.

What is real: app/main.py, app/luarmor.py, the templates (rendered by Starlette's real
Jinja2Templates) and every SQL statement (run by SQLite). What is faked: the web framework's
routing (route functions are called directly), the SQLAlchemy engine (a thin wrapper over
sqlite3 that has the same begin()/execute()/first()/rowcount shape) and the Stripe client.

The table is built from the Column definitions in main.py's LicenseKey model, so a column the
SQL uses but the model lacks fails here the same way it would on a fresh database.
"""
import contextlib
import importlib
import os
import sqlite3
import sys
import types
from datetime import datetime, timezone
from pathlib import Path

from starlette import responses as _responses
from starlette.requests import Request
from starlette.staticfiles import StaticFiles
from starlette.templating import Jinja2Templates

SHOP_DIR = Path(__file__).resolve().parent.parent


class IntegrityError(Exception):
    pass


# ── sqlalchemy ─────────────────────────────────────────────────────────────
class Integer:
    def __init__(self, *a, **kw): pass


class BigInteger(Integer):
    pass


class String(Integer):
    pass


class Text(String):
    pass


class DateTime(Integer):
    pass


class Column:
    def __init__(self, type_, *a, primary_key=False, unique=False, nullable=True, **kw):
        self.type, self.primary_key, self.unique, self.nullable = type_, primary_key, unique, nullable
        self.name = None

    def __set_name__(self, owner, name):
        self.name = name

    def __eq__(self, other):
        return ("eq", self.name, other)

    __hash__ = object.__hash__


class TextClause:
    def __init__(self, sql):
        self.text = sql


def _ddl_type(col):
    kind = col.type if isinstance(col.type, type) else type(col.type)
    if issubclass(kind, DateTime):
        return "TIMESTAMP"
    if issubclass(kind, String):
        return "TEXT"
    return "INTEGER"


class _Metadata:
    def __init__(self):
        self.models = []

    def create_all(self, bind=None):
        for model in self.models:
            cols = []
            for name, col in model.__columns__:
                piece = f'"{name}" {_ddl_type(col)}'
                if col.primary_key:
                    piece += " PRIMARY KEY AUTOINCREMENT"
                if col.unique:
                    piece += " UNIQUE"
                if not col.nullable and not col.primary_key:
                    piece += " NOT NULL"
                cols.append(piece)
            with bind.begin() as conn:
                conn.execute(f"CREATE TABLE IF NOT EXISTS {model.__tablename__} ({', '.join(cols)})")


def declarative_base():
    meta = _Metadata()

    class Base:
        metadata = meta

        def __init_subclass__(cls, **kw):
            super().__init_subclass__(**kw)
            cls.__columns__ = [(n, v) for n, v in vars(cls).items() if isinstance(v, Column)]
            meta.models.append(cls)

    return Base


class _Result:
    def __init__(self, cursor):
        self._cur = cursor
        self.rowcount = cursor.rowcount

    def first(self):
        return self._cur.fetchone()

    def fetchall(self):
        return self._cur.fetchall()


class _Conn:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, stmt, params=None):
        sql = stmt.text if isinstance(stmt, TextClause) else str(stmt)
        try:
            return _Result(self._raw.execute(sql, params or {}))
        except sqlite3.IntegrityError as e:
            raise IntegrityError(str(e)) from e


class FakeEngine:
    def __init__(self):
        self.raw = sqlite3.connect(":memory:", detect_types=sqlite3.PARSE_DECLTYPES,
                                   check_same_thread=False)
        self.raw.row_factory = sqlite3.Row

    @contextlib.contextmanager
    def begin(self):
        try:
            yield _Conn(self.raw)
        except BaseException:
            self.raw.rollback()
            raise
        else:
            self.raw.commit()


class _Query:
    def __init__(self, engine, model):
        self.engine, self.model, self.clauses = engine, model, []

    def filter(self, clause):
        self.clauses.append(clause)
        return self

    def first(self):
        where = " AND ".join(f'"{name}" = ?' for _, name, _ in self.clauses)
        values = [value for _, _, value in self.clauses]
        row = self.engine.raw.execute(
            f"SELECT * FROM {self.model.__tablename__} WHERE {where} LIMIT 1", values).fetchone()
        if row is None:
            return None
        obj = self.model.__new__(self.model)
        for key in row.keys():
            setattr(obj, key, row[key])
        return obj


class _Session:
    def __init__(self, engine):
        self.engine = engine

    def query(self, model):
        return _Query(self.engine, model)

    def close(self):
        pass


def _adapt_datetime(d):
    if d.tzinfo is not None:
        d = d.astimezone(timezone.utc).replace(tzinfo=None)
    return d.isoformat(" ")


# ── fastapi ────────────────────────────────────────────────────────────────
class FastAPI:
    def __init__(self, *a, **kw):
        pass

    def _decorator(self, *a, **kw):
        return lambda fn: fn

    get = head = post = put = delete = middleware = exception_handler = _decorator

    def mount(self, *a, **kw):
        pass


async def _http_exception_handler(request, exc):
    return _responses.PlainTextResponse(str(exc.detail), status_code=exc.status_code)


# ── stripe ─────────────────────────────────────────────────────────────────
def _unexpected(name):
    def call(*a, **kw):
        raise AssertionError(f"unexpected call to stripe.{name}")
    return staticmethod(call)


def _make_stripe():
    mod = types.ModuleType("stripe")

    class StripeError(Exception):
        pass

    class InvalidRequestError(StripeError):
        pass

    class SignatureVerificationError(StripeError):
        pass

    class Session:
        create = _unexpected("checkout.Session.create")
        retrieve = _unexpected("checkout.Session.retrieve")
        list = _unexpected("checkout.Session.list")

    class Webhook:
        construct_event = staticmethod(lambda payload, sig, secret: None)

    mod.StripeError, mod.InvalidRequestError = StripeError, InvalidRequestError
    mod.SignatureVerificationError = SignatureVerificationError
    mod.checkout = types.SimpleNamespace(Session=Session)
    mod.Webhook = Webhook
    return mod


def _module(name, **attrs):
    mod = types.ModuleType(name)
    mod.__dict__.update(attrs)
    return mod


_main = None


def install():
    """Put the stand-ins in sys.modules and import app.main. Returns (main, stripe_stub)."""
    global _main
    if _main is not None:
        return _main, sys.modules["stripe"]
    os.environ.setdefault("DATABASE_URL", "sqlite://")
    for var in ("STRIPE_SECRET_KEY", "STRIPE_WEBHOOK_SECRET", "LUARMOR_API_KEY",
                "LUARMOR_PROJECT_ID", "LUARMOR_API_URL", "LUARMOR_USE_API",
                "LUARMOR_STOCK_DAY", "LUARMOR_STOCK_WEEK", "LUARMOR_STOCK_MONTH",
                "SCRIPT_ADMIN_TOKEN"):
        os.environ.pop(var, None)
    sqlite3.register_adapter(datetime, _adapt_datetime)
    sqlite3.register_converter("TIMESTAMP", lambda b: datetime.fromisoformat(b.decode()))

    engine = FakeEngine()
    sys.modules["fastapi"] = _module("fastapi", FastAPI=FastAPI, Request=Request)
    sys.modules["fastapi.exception_handlers"] = _module(
        "fastapi.exception_handlers", http_exception_handler=_http_exception_handler)
    sys.modules["fastapi.responses"] = _module(
        "fastapi.responses", FileResponse=_responses.FileResponse, JSONResponse=_responses.JSONResponse,
        PlainTextResponse=_responses.PlainTextResponse, RedirectResponse=_responses.RedirectResponse,
        Response=_responses.Response)
    sys.modules["fastapi.staticfiles"] = _module("fastapi.staticfiles", StaticFiles=StaticFiles)
    sys.modules["fastapi.templating"] = _module("fastapi.templating", Jinja2Templates=Jinja2Templates)
    sys.modules["sqlalchemy"] = _module(
        "sqlalchemy", BigInteger=BigInteger, Column=Column, DateTime=DateTime, Integer=Integer,
        String=String, Text=Text, create_engine=lambda *a, **kw: engine, text=TextClause)
    sys.modules["sqlalchemy.exc"] = _module("sqlalchemy.exc", IntegrityError=IntegrityError)
    sys.modules["sqlalchemy.orm"] = _module(
        "sqlalchemy.orm", declarative_base=declarative_base,
        sessionmaker=lambda bind=None, **kw: (lambda: _Session(bind)))
    sys.modules["stripe"] = _make_stripe()

    sys.path.insert(0, str(SHOP_DIR))
    _main = importlib.import_module("app.main")
    _main.init_db()
    return _main, sys.modules["stripe"]


def make_request(path="/", method="GET", query=b"", body=b"", headers=None, client=("203.0.113.9", 5000)):
    """A Starlette Request built by hand, so route functions can be called directly."""
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            return {"type": "http.disconnect"}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    raw_headers = [(b"host", b"shop.test")] + [(k.lower().encode(), v.encode())
                                                for k, v in (headers or {}).items()]
    scope = {"type": "http", "method": method, "path": path, "raw_path": path.encode(),
             "query_string": query, "headers": raw_headers, "server": ("shop.test", 443),
             "scheme": "https", "client": client, "root_path": ""}
    return Request(scope, receive)
