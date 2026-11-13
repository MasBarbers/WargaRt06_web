import tempfile
import unittest
from pathlib import Path

from werkzeug.security import check_password_hash, generate_password_hash

import app as site


class AdminAccountSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        site.DATABASE = Path(self.temp_dir.name) / "rt006.sqlite3"
        site.app.config.update(TESTING=True)
        with site.app.app_context():
            site.init_db()
            site.get_db().execute(
                "INSERT INTO admins (email, password_hash) VALUES (?, ?)",
                ("ketua@example.com", generate_password_hash("password-lama")),
            )
            site.get_db().commit()
        self.client = site.app.test_client()
        with self.client.session_transaction() as browser_session:
            browser_session["admin_id"] = 1
            browser_session["csrf_token"] = "test-csrf-token"

    def tearDown(self):
        self.temp_dir.cleanup()

    @staticmethod
    def _form_data(**overrides):
        data = {
            "csrf_token": "test-csrf-token",
            "email": "ketua@example.com",
            "current_password": "password-lama",
            "new_password": "password-baru",
            "confirm_password": "password-baru",
        }
        data.update(overrides)
        return data

    def test_account_settings_page_is_available_to_logged_in_admin(self):
        response = self.client.get("/admin/account")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Akun Ketua RT", response.data)
        self.assertIn(b"ketua@example.com", response.data)

    def test_account_settings_are_in_hamburger_menu_not_admin_panel(self):
        response = self.client.get("/data-warga")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Pengaturan akun", response.data)
        self.assertNotIn(b'id="akun"', response.data)

    def test_admin_can_update_email_and_password(self):
        response = self.client.post(
            "/admin/account",
            data=self._form_data(email="ketua-baru@example.com"),
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"berhasil diperbarui", response.data)
        with site.app.app_context():
            admin = site.get_db().execute(
                "SELECT email, password_hash FROM admins WHERE id = 1"
            ).fetchone()
        self.assertEqual(admin["email"], "ketua-baru@example.com")
        self.assertTrue(check_password_hash(admin["password_hash"], "password-baru"))

    def test_wrong_current_password_does_not_change_credentials(self):
        response = self.client.post(
            "/admin/account",
            data=self._form_data(
                email="different@example.com", current_password="salah"
            ),
            follow_redirects=True,
        )
        self.assertIn(b"Password saat ini tidak sesuai", response.data)
        with site.app.app_context():
            admin = site.get_db().execute(
                "SELECT email, password_hash FROM admins WHERE id = 1"
            ).fetchone()
        self.assertEqual(admin["email"], "ketua@example.com")
        self.assertTrue(check_password_hash(admin["password_hash"], "password-lama"))

    def test_short_or_mismatched_new_password_is_rejected(self):
        for data, message in (
            (self._form_data(new_password="short", confirm_password="short"), b"minimal 8 karakter"),
            (self._form_data(confirm_password="tidak-sama"), b"tidak cocok"),
        ):
            response = self.client.post(
                "/admin/account", data=data, follow_redirects=True
            )
            self.assertIn(message, response.data)
        with site.app.app_context():
            admin = site.get_db().execute(
                "SELECT email, password_hash FROM admins WHERE id = 1"
            ).fetchone()
        self.assertEqual(admin["email"], "ketua@example.com")
        self.assertTrue(check_password_hash(admin["password_hash"], "password-lama"))

    def test_duplicate_email_is_rejected_without_changing_password(self):
        with site.app.app_context():
            site.get_db().execute(
                "INSERT INTO admins (email, password_hash) VALUES (?, ?)",
                ("lain@example.com", generate_password_hash("password-lain")),
            )
            site.get_db().commit()
        response = self.client.post(
            "/admin/account",
            data=self._form_data(email="lain@example.com"),
            follow_redirects=True,
        )
        self.assertIn(b"sudah digunakan", response.data)
        with site.app.app_context():
            admin = site.get_db().execute(
                "SELECT email, password_hash FROM admins WHERE id = 1"
            ).fetchone()
        self.assertEqual(admin["email"], "ketua@example.com")
        self.assertTrue(check_password_hash(admin["password_hash"], "password-lama"))


if __name__ == "__main__":
    unittest.main()
