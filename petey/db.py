import os
import psycopg2
from psycopg2.pool import ThreadedConnectionPool
from psycopg2.extras import DictCursor, DictRow
import contextlib
from dotenv import load_dotenv

from threading import BoundedSemaphore

_pool = None
_pool_semaphore = None

def init_pool():
    global _pool, _pool_semaphore
    if _pool is not None:
        return
    env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
    load_dotenv(env_path)
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        # Fall back to a local/default Postgres URL to prevent startup crashes if not set during compile checks
        db_url = "postgresql://postgres:postgres@localhost:5432/petey"
    
    # Initialize the threaded connection pool and matching semaphore
    try:
        _pool = ThreadedConnectionPool(1, 20, db_url)
        _pool_semaphore = BoundedSemaphore(20)
    except Exception as e:
        print(f"[DB] Error creating connection pool: {e}")
        # We will try again on the next query or print a warning
        raise e

class Row:
    pass

class PostgresCursorAdapter:
    def __init__(self, real_cursor):
        self.cursor = real_cursor
        self.lastrowid = None

    def execute(self, sql, params=None):
        # 1. Translate parameter placeholder '?' to '%s'
        sql = sql.replace("?", "%s")
        
        # 2. Translate SQLite-specific 'INSERT OR IGNORE' to 'ON CONFLICT DO NOTHING'
        if "INSERT OR IGNORE" in sql:
            sql = sql.replace("INSERT OR IGNORE", "INSERT")
            if "ON CONFLICT" not in sql:
                sql += " ON CONFLICT DO NOTHING"

        # 3. Transparently fetch the inserted ID to populate cursor.lastrowid
        is_insert = sql.strip().upper().startswith("INSERT")
        should_return_id = is_insert and "RETURNING" not in sql.upper() and "ON CONFLICT DO NOTHING" not in sql.upper()

        if should_return_id:
            sql += " RETURNING id"

        self.cursor.execute(sql, params or ())

        if should_return_id:
            try:
                row = self.cursor.fetchone()
                if row:
                    if isinstance(row, dict) or isinstance(row, DictRow):
                        self.lastrowid = row['id']
                    else:
                        self.lastrowid = row[0]
            except Exception:
                pass
        return self

    def fetchone(self):
        return self.cursor.fetchone()

    def fetchall(self):
        return self.cursor.fetchall()

    def __iter__(self):
        return iter(self.cursor)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()

    def __getattr__(self, name):
        return getattr(self.cursor, name)

class PostgresConnectionAdapter:
    def __init__(self):
        self._closed = False
        self._semaphore_acquired = False
        self.conn = None
        if _pool_semaphore is not None:
            acquired = _pool_semaphore.acquire(timeout=5.0)
            if not acquired:
                raise Exception("Database connection pool exhausted (semaphore timeout).")
            self._semaphore_acquired = True
        try:
            self.conn = _pool.getconn()
        except Exception as e:
            if self._semaphore_acquired:
                _pool_semaphore.release()
                self._semaphore_acquired = False
            raise e
        self.cursor_obj = None
        self._row_factory = None

    @property
    def row_factory(self):
        return self._row_factory

    @row_factory.setter
    def row_factory(self, val):
        self._row_factory = val

    def cursor(self):
        if not self.conn:
            raise Exception("Connection is closed.")
        real_cursor = self.conn.cursor(cursor_factory=DictCursor)
        self.cursor_obj = PostgresCursorAdapter(real_cursor)
        return self.cursor_obj

    def execute(self, sql, params=None):
        cur = self.cursor()
        cur.execute(sql, params)
        return cur

    def commit(self):
        if self.conn:
            self.conn.commit()

    def rollback(self):
        if self.conn:
            self.conn.rollback()

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.cursor_obj:
            try:
                self.cursor_obj.close()
            except Exception:
                pass
        if self.conn:
            try:
                _pool.putconn(self.conn)
            except Exception:
                pass
            self.conn = None
        if getattr(self, "_semaphore_acquired", False):
            try:
                _pool_semaphore.release()
            except Exception:
                pass
            self._semaphore_acquired = False

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if exc_type is not None:
            self.rollback()
        else:
            self.commit()
        self.close()

def connect(path=None):
    init_pool()
    return PostgresConnectionAdapter()
