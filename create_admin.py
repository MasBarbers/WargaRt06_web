import getpass
import re

from werkzeug.security import generate_password_hash

from app import app, get_db


def main():
    email = input("Email Ketua RT: ").strip().lower()
    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise SystemExit("Format email tidak valid.")

    password = getpass.getpass("Password (minimal 12 karakter): ")
    if len(password) < 12:
        raise SystemExit("Password harus memiliki minimal 12 karakter.")
    confirm = getpass.getpass("Ulangi password: ")
    if password != confirm:
        raise SystemExit("Password tidak sama.")

    with app.app_context():
        db = get_db()
        try:
            db.execute(
                "INSERT INTO admins (email, password_hash) VALUES (?, ?)",
                (email, generate_password_hash(password)),
            )
            db.commit()
        except Exception as error:
            db.rollback()
            raise SystemExit(f"Gagal membuat akun admin: {error}") from error
    print("Akun Ketua RT berhasil dibuat.")


if __name__ == "__main__":
    main()
