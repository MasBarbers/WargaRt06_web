import re
import sqlite3
import tempfile
import unittest
from pathlib import Path

import app as site


class LetterWorkflowTests(unittest.TestCase):
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
                    status TEXT NOT NULL DEFAULT 'Menunggu',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                INSERT INTO letter_requests (reference, name, address, phone, purpose)
                VALUES ('RT006-OLD123', 'Pengaju Lama', 'Alamat Lama', '0800000000', 'Keperluan lama');
                """
            )
        finally:
            db.close()

        with site.app.app_context():
            site.init_db()
        self.client = site.app.test_client()

    def tearDown(self):
        self.temp_dir.cleanup()

    def _csrf_token(self):
        page = self.client.get("/surat-pengantar")
        self.assertEqual(page.status_code, 200)
        match = re.search(rb'name="csrf_token" value="([^"]+)"', page.data)
        self.assertIsNotNone(match)
        return match.group(1).decode()

    @staticmethod
    def _form_data(csrf_token):
        return {
            "csrf_token": csrf_token,
            "name": "Siti Warga",
            "address": "Jl. Cilincing No. 6",
            "phone": "08123456789",
            "purpose": "Pengurusan administrasi",
            "birth_place": "Jakarta",
            "birth_date": "1990-03-08",
            "gender": "PEREMPUAN",
            "religion": "ISLAM",
            "marital_status": "KAWIN",
            "nationality": "WNI",
            "national_id": "3174000000000001",
            "occupation": "Wiraswasta",
        }

    def test_existing_database_migrates_without_losing_old_requests(self):
        with site.app.app_context():
            columns = {
                row["name"]
                for row in site.get_db().execute("PRAGMA table_info(letter_requests)")
            }
            self.assertTrue(
                {
                    "birth_place",
                    "birth_date",
                    "gender",
                    "religion",
                    "marital_status",
                    "nationality",
                    "national_id",
                    "family_card_number",
                    "occupation",
                    "approved_at",
                }.issubset(columns)
            )
            self.assertEqual(
                site.get_db().execute("SELECT COUNT(*) FROM letter_requests").fetchone()[0],
                1,
            )

    def test_invalid_identity_data_is_rejected(self):
        data = self._form_data(self._csrf_token())
        data["national_id"] = "12345"
        response = self.client.post("/surat-pengantar", data=data)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"NIK harus terdiri dari 16 angka", response.data)

        data = self._form_data(self._csrf_token())
        data["birth_date"] = "2099-01-01"
        response = self.client.post("/surat-pengantar", data=data)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Tanggal lahir tidak boleh di masa depan", response.data)

        data = self._form_data(self._csrf_token())
        data["marital_status"] = "STATUS TIDAK VALID"
        response = self.client.post("/surat-pengantar", data=data)
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Status perkawinan tidak valid", response.data)

    def test_letter_preview_is_shown_before_download_without_family_card_number(self):
        form_data = self._form_data(self._csrf_token())
        form_data["purpose"] = "Pengurusan administrasi — keperluan warga"
        response = self.client.post("/surat-pengantar", data=form_data)
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.startswith("/surat/"))
        self.assertFalse(response.location.endswith("/unduh"))
        reference = response.location.rstrip("/").split("/")[-1]
        self.assertRegex(reference, r"RT06-SP-[0-9]{4}-[A-F0-9]{32}")

        letter_url = response.location
        letter = self.client.get(letter_url)
        self.assertEqual(letter.status_code, 200)
        self.assertEqual(letter.mimetype, "text/html")
        self.assertIn(b"Siti Warga", letter.data)
        self.assertIn(b"Unduh PDF", letter.data)
        self.assertIn(b"window.print()", letter.data)

        pending_download = self.client.get(f"{letter_url}/unduh")
        self.assertEqual(pending_download.status_code, 200)
        self.assertEqual(pending_download.mimetype, "application/pdf")
        self.assertIn("attachment;", pending_download.headers["Content-Disposition"])
        self.assertTrue(pending_download.data.startswith(b"%PDF-"))

        self.assertIn(b"DRAF", letter.data)
        self.assertIn(b"Siti Warga", letter.data)
        self.assertIn(b"3174000000000001", letter.data)
        self.assertNotIn(b"Nomor Kartu Keluarga", letter.data)
        self.assertIn(b"Unduh PDF", letter.data)
        with site.app.app_context():
            saved_letter = site.get_db().execute(
                "SELECT national_id, family_card_number, status FROM letter_requests "
                "WHERE reference = ?",
                (reference,),
            ).fetchone()
        self.assertEqual(saved_letter["national_id"], "3174000000000001")
        self.assertEqual(saved_letter["family_card_number"], "")
        self.assertEqual(saved_letter["status"], "Menunggu")

        with self.client.session_transaction() as browser_session:
            browser_session["admin_id"] = 1
            browser_session["csrf_token"] = "test-csrf-token"
        with site.app.app_context():
            letter_id = site.get_db().execute(
                "SELECT id FROM letter_requests WHERE reference = ?", (reference,)
            ).fetchone()["id"]
        approval = self.client.post(
            f"/admin/letters/{letter_id}",
            data={"csrf_token": "test-csrf-token", "status": "Diterima"},
        )
        self.assertEqual(approval.status_code, 302)

        admin_panel = self.client.get("/data-warga")
        self.assertIn(letter_url.encode(), admin_panel.data)
        approved_letter = self.client.get(letter_url)
        self.assertEqual(approved_letter.status_code, 200)
        for expected in (
            b"Pengajuan diterima oleh Ketua RT pada",
            b"Unduh PDF",
            b"RUKUN TETANGGA 006/01",
            b"Kelurahan Cilincing",
            b"jakarta-seal.png",
            b"Status Perkawinan",
            b"SURAT PENGANTAR",
            b"Siti Warga",
            b"3174000000000001",
            b"PENGAJUAN DITERIMA KETUA RT",
            b"window.print()",
        ):
            self.assertIn(expected, approved_letter.data)
        self.assertEqual(
            approved_letter.headers["Cache-Control"], "no-store, private"
        )
        self.assertEqual(approved_letter.headers["Referrer-Policy"], "no-referrer")
        pdf = self.client.get(f"{letter_url}/unduh")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.mimetype, "application/pdf")
        self.assertIn("attachment;", pdf.headers["Content-Disposition"])
        self.assertTrue(pdf.data.startswith(b"%PDF-"))
        self.assertIn(b"/Subtype /Image", pdf.data)
        self.assertEqual(
            self.client.get("/surat/RT006-OLD123").status_code,
            404,
        )
