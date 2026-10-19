#!/usr/bin/env python3
"""Unit tests for app/database.py -- the thin sqlite helper layer.

WHY: every persistent store in this app (SMS, call history, network stats,
remember-me tokens) funnels through these five helpers. A silent regression
here loses user data across all features at once -- the old
delete-the-DB-on-lock bug in get_rows destroyed SMS history exactly this way.
These tests pin down the create/connect/read contracts, including the ugly
corners (what happens when sqlite cannot open the file), against throwaway
temp databases. /etc is never touched.

Run via `python3 tests/run_tests.py` or `python3 -m unittest tests.test_database`.
"""

import logging
import os
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO_ROOT, "app"))

# tests/run_tests.py wraps the suite in a patch.dict mocking `pcat_config`,
# which makes the real pcat_util.pcat_logger crash at import (RotatingFileHandler
# receives a MagicMock filename). Import the real pcat_util PACKAGE, then graft
# a lightweight pcat_logger stub on top -- database.py only calls the logger.
import pcat_util  # noqa: E402  -- real package, empty __init__.py
if "pcat_util.pcat_logger" not in sys.modules:
    _stub = types.ModuleType("pcat_util.pcat_logger")
    _stub.logger = logging.getLogger("test_database")
    sys.modules["pcat_util.pcat_logger"] = _stub
    pcat_util.pcat_logger = _stub

import database  # noqa: E402

# Two statements + embedded newlines/indentation exercise init_db's
# split-and-normalize logic the same way the real stores' schemas do.
SCHEMA = """
CREATE TABLE t(
  id INTEGER PRIMARY KEY,
  name VARCHAR(30),
  val INTEGER
);
CREATE INDEX IF NOT EXISTS idx_t_name ON t(name);
"""


class DatabaseTestBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory(prefix="db-test-")
        self.tmp = self.tmpdir.name
        self.db = os.path.join(self.tmp, "test.db")

    def tearDown(self):
        self.tmpdir.cleanup()

    def table_names(self, path=None):
        con = sqlite3.connect(path or self.db)
        names = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")]
        con.close()
        return names

    def rows(self, sql, path=None):
        con = sqlite3.connect(path or self.db)
        out = list(con.execute(sql))
        con.close()
        return out


class InitDbTests(DatabaseTestBase):

    def test_creates_new_db_with_all_statements(self):
        """A missing schema statement means the app's first boot leaves the
        store unusable, so every ';'-separated statement must execute."""
        self.assertTrue(database.init_db(self.db, SCHEMA))
        self.assertIn("t", self.table_names())
        con = sqlite3.connect(self.db)
        indexes = [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")]
        con.close()
        self.assertIn("idx_t_name", indexes)

    def test_existing_db_returns_true_without_reapplying_schema(self):
        """On an existing file init_db is a no-op: it must not error, but it
        also does NOT run create_sql -- callers needing migrations on old DBs
        must apply them via connect_db (as NetworkStats.__init__ does for its
        index)."""
        database.init_db(self.db, SCHEMA)
        con = sqlite3.connect(self.db)
        con.execute("insert into t(name,val) values('keep',1)")
        con.commit()
        con.close()
        # Second schema adds a table; it must NOT appear (documents the no-op).
        self.assertTrue(database.init_db(self.db, SCHEMA + ";CREATE TABLE t2(id INTEGER);"))
        self.assertNotIn("t2", self.table_names())
        # Existing data untouched.
        self.assertEqual(self.rows("select name from t"), [("keep",)])

    def test_unopenable_path_returns_false(self):
        """If sqlite can't create the file (e.g. missing directory on a
        read-only overlay), the caller gets False instead of a crash loop."""
        bad = os.path.join(self.tmp, "no-such-dir", "x.db")
        self.assertFalse(database.init_db(bad, SCHEMA))
        self.assertFalse(os.path.exists(bad))

    def test_unopenable_existing_file_is_recreated_with_schema(self):
        """Corrupt-DB recovery must produce a USABLE store: init_db deletes
        the wedged file, reconnects, and -- the part that used to be broken --
        falls through to create_sql. Before the fix the stale `file_exists`
        flag hit the early "exists, no need to create" return, leaving a
        permanently table-less DB that no later boot repaired."""
        with open(self.db, "w") as f:
            f.write("wedged")
        real_connect = sqlite3.connect
        calls = {"n": 0}

        def flaky_connect(path, timeout=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise sqlite3.OperationalError("unable to open database file")
            return real_connect(path, timeout=timeout)

        with patch.object(database.sqlite3, "connect", side_effect=flaky_connect):
            self.assertTrue(database.init_db(self.db, SCHEMA))
        # The wedged file was replaced AND the schema applied: usable store.
        self.assertTrue(os.path.exists(self.db))
        self.assertEqual(self.table_names(), ["t"])


class ConnectDbTests(DatabaseTestBase):

    def test_no_commands_is_a_connectivity_probe(self):
        """connect_db([]) is used as a reachability check; it must succeed and
        leave the DB untouched."""
        database.init_db(self.db, SCHEMA)
        self.assertTrue(database.connect_db(self.db))
        self.assertIn("t", self.table_names())

    def test_commands_are_executed_and_committed(self):
        """Each command commits individually -- a later failure must not roll
        back earlier writes (stats inserts rely on this)."""
        database.init_db(self.db, SCHEMA)
        self.assertTrue(database.connect_db(self.db, [
            "insert into t(name,val) values('a',1)",
            "insert into t(name,val) values('b',2)",
        ]))
        self.assertEqual(self.rows("select count(*) from t"), [(2,)])

    def test_unopenable_path_returns_false(self):
        bad = os.path.join(self.tmp, "no-such-dir", "x.db")
        self.assertFalse(database.connect_db(bad, ["select 1"]))

    def test_unopenable_existing_file_returns_false_without_deleting(self):
        """Unlike init_db, connect_db must never remove an existing DB on a
        transient open failure -- that would destroy user data."""
        database.init_db(self.db, SCHEMA)
        with patch.object(database.sqlite3, "connect",
                          side_effect=sqlite3.OperationalError("locked")):
            self.assertFalse(database.connect_db(self.db, ["select 1"]))
        self.assertTrue(os.path.exists(self.db))
        self.assertIn("t", self.table_names())

    def test_bad_sql_propagates_to_caller(self):
        """Documents current behavior: write-path SQL errors are NOT caught
        here, so a malformed command crashes the calling thread. Callers must
        pass known-good SQL (or wrap in try/except like flush_pending does)."""
        database.init_db(self.db, SCHEMA)
        with self.assertRaises(sqlite3.OperationalError):
            database.connect_db(self.db, ["not valid sql"])


class ConnectDbParamTests(DatabaseTestBase):

    def test_tuple_and_plain_string_commands(self):
        """Accepts both (sql, params) tuples and bare SQL strings, so mixed
        migration/insert batches work in one call."""
        database.init_db(self.db, SCHEMA)
        self.assertTrue(database.connect_db_param(self.db, [
            ("insert into t(name,val) values(?,?)", ["a", 1]),
            "insert into t(name,val) values('plain',2)",
        ]))
        self.assertEqual(sorted(r[0] for r in self.rows("select name from t")),
                         ["a", "plain"])

    def test_no_commands_returns_true(self):
        database.init_db(self.db, SCHEMA)
        self.assertTrue(database.connect_db_param(self.db))

    def test_params_neutralize_sql_injection(self):
        """The whole point of this helper: hostile content (e.g. an incoming
        SMS body) must be stored literally, never executed."""
        database.init_db(self.db, SCHEMA)
        evil = "x'); DROP TABLE t;--"
        self.assertTrue(database.connect_db_param(self.db, [
            ("insert into t(name,val) values(?,?)", [evil, 1]),
        ]))
        self.assertIn("t", self.table_names(), "table must survive injection attempt")
        self.assertEqual(self.rows("select name from t"), [(evil,)])

    def test_unopenable_path_returns_false(self):
        bad = os.path.join(self.tmp, "no-such-dir", "x.db")
        self.assertFalse(database.connect_db_param(bad, ["select 1"]))

    def test_unopenable_existing_file_returns_false(self):
        database.init_db(self.db, SCHEMA)
        with patch.object(database.sqlite3, "connect",
                          side_effect=sqlite3.OperationalError("locked")):
            self.assertFalse(database.connect_db_param(self.db, ["select 1"]))
        self.assertIn("t", self.table_names())


class GetRowsTests(DatabaseTestBase):

    def setUp(self):
        super().setUp()
        database.init_db(self.db, SCHEMA)
        database.connect_db_param(self.db, [
            ("insert into t(name,val) values(?,?)", ["a", 1]),
            ("insert into t(name,val) values(?,?)", ["b", 2]),
        ])

    def test_rows_accessible_by_column_name(self):
        """Callers index rows by name (row['val']); losing the Row factory
        would break every stats/SMS read site at once."""
        rows = database.get_rows(self.db, "select * from t order by id")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["name"], "a")
        self.assertEqual(rows[1]["val"], 2)

    def test_error_returns_empty_list_and_keeps_db(self):
        """Regression guard for the data-loss bug: a read error must return []
        and must NEVER delete the DB file."""
        rows = database.get_rows(self.db, "select * from no_such_table")
        self.assertEqual(rows, [])
        self.assertTrue(os.path.exists(self.db))
        self.assertIn("t", self.table_names())

    def test_unopenable_path_returns_empty_list(self):
        bad = os.path.join(self.tmp, "no-such-dir", "x.db")
        self.assertEqual(database.get_rows(bad, "select 1"), [])

    def test_param_query_filters_by_bound_value(self):
        rows = database.get_rows_param(
            self.db, "select val from t where name = ?", ["b"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["val"], 2)

    def test_param_query_neutralizes_injection(self):
        """A hostile bound value must select nothing, not everything."""
        rows = database.get_rows_param(
            self.db, "select val from t where name = ?", ["' OR '1'='1"])
        self.assertEqual(rows, [])

    def test_param_query_error_returns_empty_list(self):
        rows = database.get_rows_param(
            self.db, "select * from no_such_table where x = ?", [1])
        self.assertEqual(rows, [])
        self.assertIn("t", self.table_names())


if __name__ == "__main__":
    unittest.main()
