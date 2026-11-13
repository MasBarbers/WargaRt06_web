import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path

from werkzeug.security import generate_password_hash

import app as site


class RukmaDuesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        site.DATABASE = Path(self.temp_dir.name) / "rt006.sqlite3"
        site.app.config.update(TESTING=True)
        with site.app.app_context():
            site.init_db()
            db = site.get_db()
            db.execute(
                "INSERT INTO admins (email, password_hash) VALUES (?, ?)",
                ("ketua@example.com", generate_password_hash("password-lama")),
            )
            db.execute(
                """
                INSERT INTO residents (
                    name, address, family_card_number, is_family_head, status
                ) VALUES ('Kepala Keluarga Aktif', 'Jl. Mawar 1', '3174000000000009', 1, 'Aktif')
                """
            )
            db.execute(
                """
                INSERT INTO residents (
                    name, address, family_card_number, is_family_head, status
                ) VALUES ('Anggota Bukan Kepala', 'Jl. Mawar 1', '3174000000000009', 0, 'Aktif')
                """
            )
            db.commit()
        self.client = site.app.test_client()
        self.period = date.today().strftime("%Y-%m")
        with self.client.session_transaction() as browser_session:
            browser_session["admin_id"] = 1
            browser_session["csrf_token"] = "test-csrf-token"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _active_member_id(self):
        with site.app.app_context():
            return site.get_db().execute(
                "SELECT id FROM residents WHERE name = 'Kepala Keluarga Aktif'"
            ).fetchone()["id"]

    def _save_fee(self, amount, period=None, follow_redirects=False):
        return self.client.post(
            "/admin/rukma-fees",
            data={
                "csrf_token": "test-csrf-token",
                "period": period or self.period,
                "amount": str(amount),
            },
            follow_redirects=follow_redirects,
        )

    def test_existing_rukma_data_is_preserved_and_iuran_tables_are_added(self):
        legacy_path = Path(self.temp_dir.name) / "legacy.sqlite3"
        db = sqlite3.connect(legacy_path)
        try:
            db.execute(
                """
                CREATE TABLE rukma_members (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    address TEXT NOT NULL,
                    contribution_status TEXT NOT NULL DEFAULT 'Aktif',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                """
                CREATE TABLE residents (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    address TEXT NOT NULL,
                    phone TEXT NOT NULL DEFAULT '',
                    national_id TEXT NOT NULL DEFAULT '',
                    family_card_number TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            db.execute(
                """
                INSERT INTO rukma_members (name, address)
                VALUES ('Data RUKMA Lama', 'Jl. Lama')
                """
            )
            db.execute(
                """
                INSERT INTO residents (
                    name, address, family_card_number, national_id
                ) VALUES
                    ('Kepala Lama', 'Jl. Lama', '3174000000000010', '3174000000000011'),
                    ('Anggota Lama', 'Jl. Lama', '3174000000000010', '3174000000000012')
                """
            )
            db.commit()
        finally:
            db.close()

        site.DATABASE = legacy_path
        with site.app.app_context():
            site.init_db()
            db = site.get_db()
            columns = {
                row["name"] for row in db.execute("PRAGMA table_info(rukma_members)")
            }
            tables = {
                row["name"]
                for row in db.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                )
            }
            existing_member = db.execute(
                "SELECT name FROM rukma_members WHERE id = 1"
            ).fetchone()
            marked_heads = db.execute(
                "SELECT name FROM residents WHERE is_family_head = 1"
            ).fetchall()
        self.assertIn("inactive_at", columns)
        self.assertTrue({"rukma_monthly_fees", "rukma_dues"}.issubset(tables))
        self.assertEqual(existing_member["name"], "Data RUKMA Lama")
        self.assertEqual([row["name"] for row in marked_heads], ["Kepala Lama"])

    def test_fee_and_payment_are_only_accessible_to_chairperson(self):
        public_page = self.client.get("/data-rukma")
        self.assertEqual(public_page.status_code, 200)
        self.assertNotIn(b"Kepala Keluarga Aktif", public_page.data)
        self.assertNotIn(b"Rp ", public_page.data)

        with self.client.session_transaction() as browser_session:
            browser_session.clear()
            browser_session["csrf_token"] = "test-csrf-token"
        response = self.client.post(
            "/admin/rukma-fees",
            data={
                "csrf_token": "test-csrf-token",
                "period": self.period,
                "amount": "15000",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response.location.endswith("/data-warga"))

    def test_monthly_fee_payment_summary_and_cancellation(self):
        response = self.client.get(
            "/data-warga", query_string={"iuran_bulan": self.period}
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"KK ditagihkan", response.data)
        self.assertIn(b"Kepala Keluarga Aktif", response.data)
        dues_section = response.data.split(b"Pencatatan iuran bulanan", 1)[1]
        self.assertNotIn(b"Anggota Bukan Kepala", dues_section)
        self.assertIn(b"Belum lunas", response.data)

        saved_fee = self._save_fee(15000)
        self.assertEqual(saved_fee.status_code, 302)
        page = self.client.get(
            "/data-warga", query_string={"iuran_bulan": self.period}
        )
        self.assertIn(b"Rp 15.000", page.data)

        member_id = self._active_member_id()
        paid = self.client.post(
            f"/admin/residents/{member_id}/iuran/{self.period}/bayar",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(paid.status_code, 302)
        with site.app.app_context():
            payment = site.get_db().execute(
                "SELECT amount, status, paid_at FROM rukma_resident_dues "
                "WHERE resident_id = ? AND period = ?",
                (member_id, self.period),
            ).fetchone()
        self.assertEqual(payment["amount"], 15000)
        self.assertEqual(payment["status"], "Lunas")
        self.assertIsNotNone(payment["paid_at"])

        page = self.client.get(
            "/data-warga", query_string={"iuran_bulan": self.period}
        )
        self.assertIn(b"Total terkumpul bulan ini", page.data)
        self.assertIn(b"Rp 15.000", page.data)
        self.assertIn(b">Lunas<", page.data)

        canceled = self.client.post(
            f"/admin/residents/{member_id}/iuran/{self.period}/batalkan",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(canceled.status_code, 302)
        with site.app.app_context():
            payment = site.get_db().execute(
                "SELECT amount, status, paid_at, recorded_by FROM rukma_resident_dues "
                "WHERE resident_id = ? AND period = ?",
                (member_id, self.period),
            ).fetchone()
        self.assertEqual(payment["amount"], 15000)
        self.assertEqual(payment["status"], "Belum")
        self.assertIsNone(payment["paid_at"])
        self.assertIsNone(payment["recorded_by"])

        self._save_fee(20000)
        with site.app.app_context():
            payment = site.get_db().execute(
                "SELECT amount FROM rukma_resident_dues "
                "WHERE resident_id = ? AND period = ?",
                (member_id, self.period),
            ).fetchone()
        self.assertEqual(payment["amount"], 20000)

    def test_invalid_fee_and_month_are_rejected(self):
        for value in ("0", "-1", "abc", "1000000001"):
            response = self._save_fee(value, follow_redirects=True)
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Nominal iuran", response.data)

        invalid_month = self.client.get(
            "/data-warga", query_string={"iuran_bulan": "2026-13"}
        )
        self.assertEqual(invalid_month.status_code, 400)

    def test_archiving_household_preserves_old_monthly_records(self):
        self._save_fee(10000)
        member_id = self._active_member_id()
        self.client.post(
            f"/admin/residents/{member_id}/iuran/{self.period}/bayar",
            data={"csrf_token": "test-csrf-token"},
        )

        archived = self.client.post(
            f"/admin/residents/{member_id}/delete",
            data={"csrf_token": "test-csrf-token"},
        )
        self.assertEqual(archived.status_code, 302)
        with site.app.app_context():
            member = site.get_db().execute(
                "SELECT status FROM residents WHERE id = ?",
                (member_id,),
            ).fetchone()
            payment_count = site.get_db().execute(
                "SELECT COUNT(*) FROM rukma_resident_dues WHERE resident_id = ?",
                (member_id,),
            ).fetchone()[0]
        self.assertEqual(member["status"], "Keluar")
        self.assertEqual(payment_count, 1)

        old_month = self.client.get(
            "/data-warga", query_string={"iuran_bulan": self.period}
        )
        self.assertIn(b"Kepala Keluarga Aktif", old_month.data)
        self.assertIn(b">Lunas<", old_month.data)


if __name__ == "__main__":
    unittest.main()
