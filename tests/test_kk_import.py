import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

import app as site


class KartuKeluargaImportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        site.DATABASE = Path(self.temp_dir.name) / "rt006.sqlite3"
        site.app.config.update(TESTING=True)

        db = sqlite3.connect(site.DATABASE)
        try:
            db.executescript(
                """
                CREATE TABLE admins (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    password_hash TEXT NOT NULL,
                    google_sub TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO admins (email, password_hash)
                VALUES ('ketua@example.com', 'test-hash');
                CREATE TABLE residents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    address TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE rukma_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    address TEXT NOT NULL,
                    contribution_status TEXT NOT NULL DEFAULT 'Aktif',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE letter_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    name TEXT NOT NULL,
                    address TEXT NOT NULL,
                    phone TEXT NOT NULL,
                    purpose TEXT NOT NULL,
                    birth_place TEXT NOT NULL DEFAULT '',
                    birth_date TEXT NOT NULL DEFAULT '',
                    gender TEXT NOT NULL DEFAULT '',
                    religion TEXT NOT NULL DEFAULT '',
                    nationality TEXT NOT NULL DEFAULT '',
                    national_id TEXT NOT NULL DEFAULT '',
                    family_card_number TEXT NOT NULL DEFAULT '',
                    occupation TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL DEFAULT 'Menunggu',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                """
            )
        finally:
            db.close()
        with site.app.app_context():
            site.init_db()
        self.client = site.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _login_admin(self):
        with self.client.session_transaction() as browser_session:
            browser_session["admin_id"] = 1
            browser_session["csrf_token"] = "test-csrf-token"

    @staticmethod
    def _form_data(**overrides):
        data = {
            "csrf_token": "test-csrf-token",
            "family_card_number": "3174000000000009",
            "address": "Jl. Cilincing No. 6",
            "phone": "08123456789",
            "resident_name": ["Anggota Satu", "Anggota Dua"],
            "national_id": ["3174000000000001", "3174000000000002"],
        }
        data.update(overrides)
        return data

    def test_import_requires_admin_login(self):
        with self.client.session_transaction() as browser_session:
            browser_session["csrf_token"] = "test-csrf-token"
        response = self.client.post(
            "/admin/residents/import-kk", data=self._form_data()
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/data-warga", response.location)

    def test_admin_can_import_all_members_as_residents(self):
        self._login_admin()
        page = self.client.get("/data-warga")
        self.assertEqual(page.status_code, 200)
        self.assertIn(b"Impor dari Kartu Keluarga", page.data)
        self.assertIn(b"capture=\"environment\"", page.data)

        response = self.client.post(
            "/admin/residents/import-kk",
            data=self._form_data(),
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2 anggota keluarga berhasil disimpan", response.data)
        with site.app.app_context():
            rows = site.get_db().execute(
                """
                SELECT name, address, phone, national_id, family_card_number,
                    is_family_head
                FROM residents ORDER BY name
                """
            ).fetchall()
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0]["national_id"], "3174000000000002")
        self.assertEqual(rows[1]["family_card_number"], "3174000000000009")
        self.assertEqual(rows[0]["is_family_head"], 0)
        self.assertEqual(rows[1]["is_family_head"], 1)

    def test_duplicate_and_invalid_ids_are_rejected(self):
        self._login_admin()
        data = self._form_data(national_id=["3174000000000001", "3174000000000001"])
        response = self.client.post(
            "/admin/residents/import-kk", data=data, follow_redirects=True
        )
        self.assertIn(b"NIK duplikat", response.data)

        data = self._form_data(national_id=["123", "3174000000000002"])
        response = self.client.post(
            "/admin/residents/import-kk", data=data, follow_redirects=True
        )
        self.assertIn(b"Setiap NIK harus terdiri dari 16 angka", response.data)

        response = self.client.post(
            "/admin/residents/import-kk",
            data=self._form_data(),
            follow_redirects=True,
        )
        self.assertIn(b"2 anggota keluarga berhasil disimpan", response.data)
        response = self.client.post(
            "/admin/residents/import-kk",
            data=self._form_data(),
            follow_redirects=True,
        )
        self.assertIn(b"Sebagian NIK sudah terdaftar", response.data)
        with site.app.app_context():
            self.assertEqual(
                site.get_db().execute("SELECT COUNT(*) FROM residents").fetchone()[0],
                2,
            )

    def test_only_first_imported_member_is_head_for_each_family_card(self):
        self._login_admin()
        data = self._form_data()
        response = self.client.post(
            "/admin/residents/import-kk", data=data, follow_redirects=True
        )
        self.assertIn(b"2 anggota keluarga berhasil disimpan", response.data)

        data["resident_name"] = ["Anggota Tambahan"]
        data["national_id"] = ["3174000000000099"]
        response = self.client.post(
            "/admin/residents/import-kk", data=data, follow_redirects=True
        )
        self.assertIn(b"1 anggota keluarga berhasil disimpan", response.data)
        with site.app.app_context():
            rows = site.get_db().execute(
                """
                SELECT name, is_family_head
                FROM residents
                WHERE family_card_number = ?
                ORDER BY id
                """,
                ("3174000000000009",),
            ).fetchall()
        self.assertEqual([row["is_family_head"] for row in rows], [1, 0, 0])
