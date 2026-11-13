import unittest

import app as site


class FakeCursor:
    def __init__(self, rows=()):
        self.rows = list(rows)
        self.rowcount = 0

    def fetchone(self):
        return self.rows.pop(0) if self.rows else None

    def fetchall(self):
        rows, self.rows = self.rows, []
        return rows

    def executemany(self, _query, _parameters):
        return None


class FakePostgresConnection:
    def __init__(self):
        self.statements = []

    def execute(self, query, parameters=()):
        self.statements.append((query, parameters))
        if "information_schema.columns" in query:
            return FakeCursor([{"column_name": "id"}, {"column_name": "email"}])
        return FakeCursor()

    def cursor(self):
        return FakeCursor()

    def commit(self):
        return None

    def rollback(self):
        return None

    def close(self):
        return None


class DatabaseAdapterTests(unittest.TestCase):
    def setUp(self):
        self.connection = FakePostgresConnection()
        self.db = site.Database(self.connection, "postgres")

    def test_postgres_parameters_and_dictionary_rows_are_compatible(self):
        cursor = self.db.execute(
            "SELECT id, email FROM admins WHERE email = ?",
            ("ketua@example.com",),
        )
        self.assertIn("%s", self.connection.statements[-1][0])
        self.assertEqual(self.connection.statements[-1][1], ("ketua@example.com",))

        self.db.execute(
            "UPDATE admins SET created_at = CURRENT_TIMESTAMP WHERE email = ?",
            ("ketua@example.com",),
        )
        self.assertIn(
            "(CURRENT_TIMESTAMP::text)", self.connection.statements[-1][0]
        )
        self.assertEqual(
            site.HybridRow({"id": 8, "email": "ketua@example.com"})[0],
            8,
        )
        self.assertIsNotNone(cursor)

    def test_postgres_schema_translates_sqlite_identity_and_defaults(self):
        self.db.executescript(
            """
            CREATE TABLE admins (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        statement = self.connection.statements[0][0]
        self.assertIn("id SERIAL PRIMARY KEY", statement)
        self.assertNotIn("COLLATE NOCASE", statement)
        self.assertIn("DEFAULT (CURRENT_TIMESTAMP::text)", statement)

    def test_postgres_column_discovery_uses_information_schema(self):
        self.assertEqual(self.db.table_columns("admins"), {"id", "email"})
        self.assertIn(
            "information_schema.columns", self.connection.statements[-1][0]
        )


if __name__ == "__main__":
    unittest.main()
