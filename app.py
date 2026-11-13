import io
import os
import re
import secrets
import sqlite3
from datetime import date
from functools import wraps
from pathlib import Path
from xml.sax.saxutils import escape

import reportlab
from flask import (
    Flask,
    abort,
    flash,
    g,
    make_response,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from authlib.integrations.base_client.errors import OAuthError
from authlib.integrations.flask_client import OAuth
from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_JUSTIFY, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.lib.utils import ImageReader
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    Image as ReportLabImage,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)
from werkzeug.security import check_password_hash, generate_password_hash

try:
    import psycopg
    from psycopg.rows import dict_row
except ImportError:
    psycopg = None
    dict_row = None


BASE_DIR = Path(__file__).resolve().parent
DATABASE = BASE_DIR / "instance" / "rt006.sqlite3"
LETTER_LOGO = BASE_DIR / "static" / "jakarta-seal.png"
REPORTLAB_FONTS = Path(reportlab.__file__).resolve().parent / "fonts"
pdfmetrics.registerFont(TTFont("Vera", str(REPORTLAB_FONTS / "Vera.ttf")))
pdfmetrics.registerFont(TTFont("Vera-Bold", str(REPORTLAB_FONTS / "VeraBd.ttf")))

app = Flask(__name__)
RESIDENT_STATUSES = {"Aktif", "Pindah", "Keluar", "Meninggal"}
MARITAL_STATUSES = {"BELUM KAWIN", "KAWIN", "CERAI HIDUP", "CERAI MATI"}
app.config.update(
    SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.environ.get("COOKIE_SECURE") == "1",
)
google_login_enabled = bool(
    os.environ.get("GOOGLE_CLIENT_ID") and os.environ.get("GOOGLE_CLIENT_SECRET")
)
DB_INTEGRITY_ERRORS = (sqlite3.IntegrityError,)
if psycopg is not None:
    DB_INTEGRITY_ERRORS += (psycopg.IntegrityError,)
oauth = OAuth(app)
if google_login_enabled:
    oauth.register(
        name="google",
        client_id=os.environ["GOOGLE_CLIENT_ID"],
        client_secret=os.environ["GOOGLE_CLIENT_SECRET"],
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )


class DatabaseResult:
    def __init__(self, cursor):
        self.cursor = cursor
        self.rowcount = cursor.rowcount

    @staticmethod
    def _row(row):
        if row is None or isinstance(row, sqlite3.Row):
            return row
        return HybridRow(row)

    def fetchone(self):
        return self._row(self.cursor.fetchone())

    def fetchall(self):
        return [self._row(row) for row in self.cursor.fetchall()]

    def __iter__(self):
        return iter(self.fetchall())


class HybridRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            key = tuple(self.keys())[key]
        return super().__getitem__(key)


class Database:
    def __init__(self, connection, dialect):
        self.connection = connection
        self.dialect = dialect

    def execute(self, query, parameters=()):
        if self.dialect == "postgres":
            query = re.sub(r"\?", "%s", query)
            query = re.sub(
                r"\bCURRENT_TIMESTAMP\b",
                "(CURRENT_TIMESTAMP::text)",
                query,
                flags=re.IGNORECASE,
            )
        return DatabaseResult(self.connection.execute(query, parameters))

    def executemany(self, query, parameters):
        if self.dialect == "postgres":
            query = re.sub(r"\?", "%s", query)
            query = re.sub(
                r"\bCURRENT_TIMESTAMP\b",
                "(CURRENT_TIMESTAMP::text)",
                query,
                flags=re.IGNORECASE,
            )
        cursor = self.connection.cursor()
        cursor.executemany(query, parameters)
        return DatabaseResult(cursor)

    def executescript(self, script):
        if self.dialect == "sqlite":
            self.connection.executescript(script)
            return
        script = re.sub(
            r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
            "SERIAL PRIMARY KEY",
            script,
            flags=re.IGNORECASE,
        )
        script = re.sub(
            r"\bTEXT\s+NOT\s+NULL\s+UNIQUE\s+COLLATE\s+NOCASE\b",
            "TEXT NOT NULL UNIQUE",
            script,
            flags=re.IGNORECASE,
        )
        script = re.sub(
            r"\bDEFAULT\s+CURRENT_TIMESTAMP\b",
            "DEFAULT (CURRENT_TIMESTAMP::text)",
            script,
            flags=re.IGNORECASE,
        )
        for statement in script.split(";"):
            if statement.strip():
                self.connection.execute(statement)

    def table_columns(self, table_name):
        if self.dialect == "sqlite":
            rows = self.execute(f"PRAGMA table_info({table_name})").fetchall()
            return {row["name"] for row in rows}
        rows = self.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = current_schema() AND table_name = ?
            """,
            (table_name,),
        ).fetchall()
        return {row["column_name"] for row in rows}

    def commit(self):
        self.connection.commit()

    def rollback(self):
        self.connection.rollback()

    def close(self):
        self.connection.close()


def get_db():
    if "db" not in g:
        database_url = os.environ.get("DATABASE_URL")
        if database_url:
            if psycopg is None:
                raise RuntimeError(
                    "DATABASE_URL is set, but the psycopg PostgreSQL driver is missing."
                )
            connection = psycopg.connect(database_url, row_factory=dict_row)
            g.db = Database(connection, "postgres")
        else:
            DATABASE.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(DATABASE)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            g.db = Database(connection, "sqlite")
    return g.db


def init_db():
    db = get_db()
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS admins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL UNIQUE COLLATE NOCASE,
            password_hash TEXT NOT NULL,
            google_sub TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS residents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT NOT NULL,
            phone TEXT NOT NULL DEFAULT '',
            national_id TEXT NOT NULL DEFAULT '',
            family_card_number TEXT NOT NULL DEFAULT '',
            birth_place TEXT NOT NULL DEFAULT '',
            birth_date TEXT NOT NULL DEFAULT '',
            gender TEXT NOT NULL DEFAULT '',
            religion TEXT NOT NULL DEFAULT '',
            nationality TEXT NOT NULL DEFAULT '',
            occupation TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Aktif',
            status_note TEXT NOT NULL DEFAULT '',
            is_family_head INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS rukma_members (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            address TEXT NOT NULL,
            contribution_status TEXT NOT NULL DEFAULT 'Aktif',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS rukma_monthly_fees (
            period TEXT PRIMARY KEY,
            amount INTEGER NOT NULL CHECK (amount > 0),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS rukma_dues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            member_id INTEGER NOT NULL REFERENCES rukma_members(id) ON DELETE RESTRICT,
            period TEXT NOT NULL,
            amount INTEGER NOT NULL CHECK (amount > 0),
            status TEXT NOT NULL CHECK (status IN ('Belum', 'Lunas')),
            paid_at TEXT,
            recorded_by INTEGER REFERENCES admins(id),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (member_id, period),
            CHECK (
                (status = 'Lunas' AND paid_at IS NOT NULL)
                OR (status = 'Belum' AND paid_at IS NULL)
            )
        );
        CREATE TABLE IF NOT EXISTS rukma_resident_dues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            resident_id INTEGER NOT NULL REFERENCES residents(id) ON DELETE RESTRICT,
            period TEXT NOT NULL,
            amount INTEGER NOT NULL CHECK (amount > 0),
            status TEXT NOT NULL CHECK (status IN ('Belum', 'Lunas')),
            paid_at TEXT,
            recorded_by INTEGER REFERENCES admins(id),
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (resident_id, period),
            CHECK (
                (status = 'Lunas' AND paid_at IS NOT NULL)
                OR (status = 'Belum' AND paid_at IS NULL)
            )
        );
        CREATE TABLE IF NOT EXISTS letter_requests (
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
            marital_status TEXT NOT NULL DEFAULT '',
            nationality TEXT NOT NULL DEFAULT '',
            national_id TEXT NOT NULL DEFAULT '',
            family_card_number TEXT NOT NULL DEFAULT '',
            occupation TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'Menunggu',
            approved_at TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        """
    )
    admin_columns = db.table_columns("admins")
    if "google_sub" not in admin_columns:
        db.execute("ALTER TABLE admins ADD COLUMN google_sub TEXT")
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_admins_google_sub "
        "ON admins(google_sub)"
    )
    rukma_columns = db.table_columns("rukma_members")
    if "inactive_at" not in rukma_columns:
        db.execute("ALTER TABLE rukma_members ADD COLUMN inactive_at TEXT")
    letter_columns = db.table_columns("letter_requests")
    new_letter_columns = {
        "birth_place": "TEXT NOT NULL DEFAULT ''",
        "birth_date": "TEXT NOT NULL DEFAULT ''",
        "gender": "TEXT NOT NULL DEFAULT ''",
        "religion": "TEXT NOT NULL DEFAULT ''",
        "marital_status": "TEXT NOT NULL DEFAULT ''",
        "nationality": "TEXT NOT NULL DEFAULT ''",
        "national_id": "TEXT NOT NULL DEFAULT ''",
        "family_card_number": "TEXT NOT NULL DEFAULT ''",
        "occupation": "TEXT NOT NULL DEFAULT ''",
        "approved_at": "TEXT",
    }
    for column, definition in new_letter_columns.items():
        if column not in letter_columns:
            db.execute(f"ALTER TABLE letter_requests ADD COLUMN {column} {definition}")
    resident_columns = db.table_columns("residents")
    new_resident_columns = {
        "national_id": "TEXT NOT NULL DEFAULT ''",
        "family_card_number": "TEXT NOT NULL DEFAULT ''",
        "birth_place": "TEXT NOT NULL DEFAULT ''",
        "birth_date": "TEXT NOT NULL DEFAULT ''",
        "gender": "TEXT NOT NULL DEFAULT ''",
        "religion": "TEXT NOT NULL DEFAULT ''",
        "nationality": "TEXT NOT NULL DEFAULT ''",
        "occupation": "TEXT NOT NULL DEFAULT ''",
        "status": "TEXT NOT NULL DEFAULT 'Aktif'",
        "status_note": "TEXT NOT NULL DEFAULT ''",
        "is_family_head": "INTEGER NOT NULL DEFAULT 0",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
    }
    for column, definition in new_resident_columns.items():
        if column not in resident_columns:
            db.execute(f"ALTER TABLE residents ADD COLUMN {column} {definition}")
    db.execute(
        "UPDATE residents SET updated_at = CURRENT_TIMESTAMP WHERE updated_at = ''"
    )
    db.execute(
        """
        UPDATE residents
        SET is_family_head = 1
        WHERE family_card_number != ''
            AND id = (
                SELECT MIN(head_candidate.id)
                FROM residents AS head_candidate
                WHERE head_candidate.family_card_number = residents.family_card_number
            )
            AND NOT EXISTS (
                SELECT 1
                FROM residents AS marked_head
                WHERE marked_head.family_card_number = residents.family_card_number
                    AND marked_head.is_family_head = 1
            )
        """
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_residents_one_family_head "
        "ON residents(family_card_number) "
        "WHERE family_card_number != '' AND is_family_head = 1"
    )
    db.execute(
        """
        INSERT INTO rukma_resident_dues (
            resident_id, period, amount, status, paid_at, recorded_by, updated_at
        )
        SELECT
            resident.id, old_due.period, old_due.amount, old_due.status,
            old_due.paid_at, old_due.recorded_by, old_due.updated_at
        FROM rukma_dues AS old_due
        JOIN rukma_members AS old_member ON old_member.id = old_due.member_id
        JOIN residents AS resident
            ON resident.is_family_head = 1
            AND lower(trim(resident.name)) = lower(trim(old_member.name))
            AND lower(trim(resident.address)) = lower(trim(old_member.address))
        WHERE true
        ON CONFLICT (resident_id, period) DO NOTHING
        """
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_residents_national_id "
        "ON residents(national_id) WHERE national_id != ''"
    )
    db.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_admins_email_case_insensitive "
        "ON admins(lower(email))"
    )
    db.commit()


@app.teardown_appcontext
def close_db(_error=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


@app.before_request
def protect_post_requests():
    if request.method == "POST":
        submitted = request.form.get("csrf_token", "")
        expected = session.get("csrf_token", "")
        if not expected or not secrets.compare_digest(submitted, expected):
            abort(400, description="Formulir kedaluwarsa. Silakan muat ulang halaman.")


@app.context_processor
def inject_template_helpers():
    def csrf_token():
        if "csrf_token" not in session:
            session["csrf_token"] = secrets.token_urlsafe(32)
        return session["csrf_token"]

    return {
        "csrf_token": csrf_token,
        "logged_in": bool(session.get("admin_id")),
        "google_login_enabled": google_login_enabled,
    }


@app.template_filter("rupiah")
def format_rupiah(amount):
    return f"Rp {int(amount):,}".replace(",", ".")


def admin_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("admin_id"):
            flash("Silakan masuk sebagai Ketua RT untuk melihat Data Warga.", "info")
            return redirect(url_for("data_warga"))
        return view(*args, **kwargs)

    return wrapped


@app.get("/")
def home():
    db = get_db()
    stats = {
        "residents": db.execute("SELECT COUNT(*) FROM residents").fetchone()[0],
        "rukma": db.execute(
            "SELECT COUNT(*) FROM residents "
            "WHERE status = 'Aktif' AND is_family_head = 1"
        ).fetchone()[0],
        "letters": db.execute(
            "SELECT COUNT(*) FROM letter_requests WHERE status = 'Menunggu'"
        ).fetchone()[0],
    }
    return render_template("home.html", stats=stats)


@app.get("/healthz")
def health_check():
    get_db().execute("SELECT 1")
    return {"status": "ok"}


@app.route("/surat-pengantar", methods=["GET", "POST"])
def surat_pengantar():
    if request.method == "POST":
        fields = {
            "name": request.form.get("name", "").strip(),
            "address": request.form.get("address", "").strip(),
            "phone": request.form.get("phone", "").strip(),
            "purpose": request.form.get("purpose", "").strip(),
            "birth_place": request.form.get("birth_place", "").strip(),
            "birth_date": request.form.get("birth_date", "").strip(),
            "gender": request.form.get("gender", "").strip(),
            "religion": request.form.get("religion", "").strip(),
            "marital_status": request.form.get("marital_status", "").strip().upper(),
            "nationality": request.form.get("nationality", "").strip(),
            "national_id": request.form.get("national_id", "").strip(),
            "family_card_number": "",
            "occupation": request.form.get("occupation", "").strip(),
        }
        if not all(
            value for key, value in fields.items() if key != "family_card_number"
        ):
            flash("Semua kolom wajib diisi.", "error")
        elif any(
            len(fields[key]) > limit
            for key, limit in {
                "name": 100,
                "address": 250,
                "phone": 30,
                "purpose": 500,
                "birth_place": 100,
                "religion": 40,
                "nationality": 50,
                "occupation": 100,
            }.items()
        ):
            flash("Ada isian yang terlalu panjang. Periksa kembali formulir.", "error")
        elif fields["gender"] not in {"LAKI-LAKI", "PEREMPUAN"}:
            flash("Jenis kelamin tidak valid.", "error")
        elif fields["religion"] not in {
            "ISLAM",
            "KRISTEN PROTESTAN",
            "KATOLIK",
            "HINDU",
            "BUDDHA",
            "KONGHUCU",
            "LAINNYA",
        }:
            flash("Agama tidak valid.", "error")
        elif fields["marital_status"] not in MARITAL_STATUSES:
            flash("Status perkawinan tidak valid.", "error")
        elif not re.fullmatch(r"[0-9]{16}", fields["national_id"]):
            flash("NIK harus terdiri dari 16 angka.", "error")
        else:
            try:
                birth_date = date.fromisoformat(fields["birth_date"])
            except ValueError:
                flash("Tanggal lahir tidak valid.", "error")
            else:
                if birth_date > date.today():
                    flash("Tanggal lahir tidak boleh di masa depan.", "error")
                else:
                    today = date.today()
                    reference = f"RT06-SP-{today.year}-{secrets.token_hex(16).upper()}"
                    db = get_db()
                    db.execute(
                        """
                        INSERT INTO letter_requests (
                            reference, name, address, phone, purpose, birth_place,
                            birth_date, gender, religion, marital_status,
                            nationality, national_id, family_card_number, occupation
                        ) VALUES (
                            :reference, :name, :address, :phone, :purpose, :birth_place,
                            :birth_date, :gender, :religion, :marital_status,
                            :nationality, :national_id, :family_card_number, :occupation
                        )
                        """,
                        {"reference": reference, **fields},
                    )
                    db.commit()
                    return redirect(url_for("print_letter", reference=reference))
    return render_template("surat_pengantar.html")


@app.get("/surat/<reference>")
def print_letter(reference):
    if not re.fullmatch(r"RT06-SP-[0-9]{4}-[A-F0-9]{32}", reference):
        abort(404)
    letter = get_db().execute(
        "SELECT * FROM letter_requests WHERE reference = ?", (reference,)
    ).fetchone()
    if letter is None or not letter["birth_date"]:
        abort(404)

    date_parts = date.fromisoformat(letter["birth_date"])
    month_names = (
        "Januari", "Februari", "Maret", "April", "Mei", "Juni",
        "Juli", "Agustus", "September", "Oktober", "November", "Desember",
    )
    birth_date = (
        f"{date_parts.day} {month_names[date_parts.month - 1]} {date_parts.year}"
    )
    is_approved = letter["status"] in {"Diterima", "Selesai"} and bool(
        letter["approved_at"]
    )
    date_source = letter["approved_at"] if is_approved else letter["created_at"]
    approved_date = date.fromisoformat(date_source[:10])
    issued_date = (
        f"{approved_date.day} {month_names[approved_date.month - 1]} "
        f"{approved_date.year}"
    )
    response = make_response(
        render_template(
            "print_letter.html",
            letter=letter,
            birth_date=birth_date,
            issued_date=issued_date,
            is_approved=is_approved,
            download_url=url_for("download_letter", reference=reference),
        )
    )
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@app.get("/surat/<reference>/unduh")
def download_letter(reference):
    if not re.fullmatch(r"RT06-SP-[0-9]{4}-[A-F0-9]{32}", reference):
        abort(404)
    letter = get_db().execute(
        "SELECT * FROM letter_requests WHERE reference = ?", (reference,)
    ).fetchone()
    if letter is None or not letter["birth_date"]:
        abort(404)
    date_parts = date.fromisoformat(letter["birth_date"])
    month_names = (
        "Januari", "Februari", "Maret", "April", "Mei", "Juni",
        "Juli", "Agustus", "September", "Oktober", "November", "Desember",
    )
    birth_date = (
        f"{date_parts.day} {month_names[date_parts.month - 1]} {date_parts.year}"
    )
    is_approved = letter["status"] in {"Diterima", "Selesai"} and bool(
        letter["approved_at"]
    )
    date_source = letter["approved_at"] if is_approved else letter["created_at"]
    approved_date = date.fromisoformat(date_source[:10])
    issued_date = (
        f"{approved_date.day} {month_names[approved_date.month - 1]} "
        f"{approved_date.year}"
    )
    pdf_buffer = io.BytesIO()
    document = SimpleDocTemplate(
        pdf_buffer,
        pagesize=A4,
        rightMargin=22 * mm,
        leftMargin=22 * mm,
        topMargin=16 * mm,
        bottomMargin=20 * mm,
        title=f"Surat Pengantar {letter['reference']}",
        author="Rukun Tetangga 006 / Rukun Warga 001",
    )
    styles = getSampleStyleSheet()
    header_title_style = ParagraphStyle(
        "HeaderTitle",
        parent=styles["Normal"],
        fontName="Vera-Bold",
        fontSize=19,
        leading=22,
        alignment=TA_CENTER,
    )
    authority_style = ParagraphStyle(
        "Authority",
        parent=styles["Normal"],
        fontName="Vera-Bold",
        fontSize=8,
        leading=11,
        alignment=TA_CENTER,
    )
    address_style = ParagraphStyle(
        "OfficeAddress",
        parent=styles["Normal"],
        fontName="Vera",
        fontSize=8,
        leading=10,
        alignment=TA_CENTER,
    )
    title_style = ParagraphStyle(
        "LetterTitle",
        parent=styles["Heading2"],
        fontName="Vera-Bold",
        fontSize=13,
        leading=16,
        alignment=TA_CENTER,
        spaceAfter=2,
    )
    body_style = ParagraphStyle(
        "LetterBody",
        parent=styles["BodyText"],
        fontName="Vera",
        fontSize=10,
        leading=15,
        alignment=TA_JUSTIFY,
        spaceAfter=8,
    )
    label_style = ParagraphStyle(
        "LetterLabel",
        parent=body_style,
        alignment=0,
        spaceAfter=0,
    )
    value_style = ParagraphStyle(
        "LetterValue",
        parent=body_style,
        alignment=0,
        spaceAfter=0,
    )
    small_style = ParagraphStyle(
        "LetterSmall",
        parent=styles["Normal"],
        fontName="Vera",
        fontSize=8,
        leading=11,
        alignment=TA_CENTER,
        textColor=colors.HexColor("#555555"),
    )

    def safe_paragraph(value, style):
        return Paragraph(escape(str(value)).replace("\n", "<br/>"), style)

    emblem = ReportLabImage(
        str(LETTER_LOGO),
        width=23 * mm,
        height=23 * mm,
    )
    header_text = [
        Paragraph("RUKUN TETANGGA 006/01", header_title_style),
        Paragraph(
            "KELURAHAN CILINCING · KECAMATAN CILINCING · "
            "KOTA ADMINISTRASI JAKARTA UTARA",
            authority_style,
        ),
        Paragraph("Sekretariat: Jl. Baru RT 006 RW 01 · Kotak Pos 14120", address_style),
    ]
    header_table = Table(
        [[emblem, header_text]],
        colWidths=[24 * mm, 142 * mm],
        hAlign="LEFT",
    )
    header_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story = [header_table, Spacer(1, 3 * mm)]
    letterhead_rule = Table([[""]], colWidths=[166 * mm], rowHeights=[1.5 * mm])
    letterhead_rule.setStyle(
        TableStyle(
            [
                ("LINEABOVE", (0, 0), (-1, -1), 1.5, colors.black),
                ("LINEBELOW", (0, 0), (-1, -1), 0.5, colors.black),
            ]
        )
    )
    story.extend(
        [
            letterhead_rule,
            Spacer(1, 5 * mm),
            Table(
                [[Paragraph("SURAT PENGANTAR", title_style)]],
                colWidths=[77 * mm],
                hAlign="CENTER",
                style=TableStyle(
                    [
                        ("BOX", (0, 0), (-1, -1), 1, colors.black),
                        ("BOX", (0, 0), (-1, -1), 0.4, colors.black),
                        ("LEFTPADDING", (0, 0), (-1, -1), 3),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                        ("TOPPADDING", (0, 0), (-1, -1), 2),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                    ]
                ),
            ),
            Spacer(1, 5 * mm),
            safe_paragraph(
                "Yang bertanda tangan di bawah ini Pengurus RT. 006 / RW. 01 "
                "Kelurahan Cilincing, Kecamatan Cilincing, Jakarta Utara "
                "menerangkan bahwa:",
                body_style,
            ),
        ]
    )
    details = [
        ("Nama", letter["name"]),
        ("Tempat / Tgl. Lahir", f"{letter['birth_place']}, {birth_date}"),
        ("Jenis Kelamin", letter["gender"]),
        ("Agama", letter["religion"]),
        ("Status Perkawinan", letter["marital_status"] or "—"),
        ("Pekerjaan", letter["occupation"]),
        ("No. KTP / NIK", letter["national_id"]),
        ("Kewarganegaraan", letter["nationality"]),
        ("Alamat", letter["address"]),
    ]
    details_table = Table(
        [
            [
                safe_paragraph(label, label_style),
                safe_paragraph(":", label_style),
                safe_paragraph(value, value_style),
            ]
            for label, value in details
        ],
        colWidths=[39 * mm, 4 * mm, 123 * mm],
        hAlign="LEFT",
    )
    details_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ("LINEBELOW", (2, 0), (2, -1), 0.4, colors.HexColor("#777777")),
            ]
        )
    )
    story.extend(
        [
            details_table,
            Spacer(1, 3 * mm),
            Table(
                [[
                    safe_paragraph("Keperluan", label_style),
                    safe_paragraph(":", label_style),
                    safe_paragraph(letter["purpose"], value_style),
                ]],
                colWidths=[39 * mm, 4 * mm, 123 * mm],
                style=TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 2),
                        ("TOPPADDING", (0, 0), (-1, -1), 4),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                        ("LINEBELOW", (2, 0), (2, -1), 0.4, colors.HexColor("#777777")),
                    ]
                ),
            ),
        ]
    )
    signature_date_style = ParagraphStyle(
        "SignatureDate",
        parent=body_style,
        alignment=TA_RIGHT,
        spaceAfter=0,
    )
    signature_style = ParagraphStyle(
        "Signature",
        parent=styles["Normal"],
        fontName="Vera",
        fontSize=10,
        leading=14,
        alignment=TA_CENTER,
    )
    signature_table = Table(
        [
            [
                Paragraph(
                    "Mengetahui,<br/>Pengurus RW. 01 Kel. Cilincing",
                    signature_style,
                ),
                Paragraph(
                    "Mengetahui,<br/>Pengurus RT. 006/01 Kel. Cilincing",
                    signature_style,
                ),
            ],
            ["", ""],
            [
                Paragraph("(........................................)", signature_style),
                Paragraph("(........................................)", signature_style),
            ],
        ],
        colWidths=[83 * mm, 83 * mm],
        rowHeights=[14 * mm, 30 * mm, 7 * mm],
    )
    signature_table.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    story.extend(
        [
            Table(
                [[
                    Paragraph("No. " + escape(letter["reference"]), label_style),
                    Paragraph("Jakarta, " + escape(issued_date), signature_date_style),
                ]],
                colWidths=[83 * mm, 83 * mm],
                style=TableStyle(
                    [
                        ("VALIGN", (0, 0), (-1, -1), "BOTTOM"),
                        ("LEFTPADDING", (0, 0), (-1, -1), 0),
                        ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                        ("LINEBELOW", (0, 0), (0, 0), 0.4, colors.HexColor("#777777")),
                        ("LINEBELOW", (1, 0), (1, 0), 0.4, colors.HexColor("#777777")),
                    ]
                ),
            ),
            Spacer(1, 4 * mm),
            signature_table,
            Spacer(1, 5 * mm),
            safe_paragraph(
                (
                    "Formulir layanan administrasi RT. Pengajuan telah diterima "
                    "Ketua RT; dokumen tetap memerlukan tanda tangan pengurus "
                    "berwenang agar sah digunakan."
                    if is_approved
                    else "DRAF — BELUM DISETUJUI KETUA RT. Formulir layanan "
                    "administrasi RT ini bukan surat yang diterbitkan Kelurahan "
                    "dan belum sah digunakan sebelum ditandatangani pengurus "
                    "berwenang."
                ),
                small_style,
            ),
        ]
    )

    def draw_letter_background(canvas, _document):
        canvas.saveState()
        watermark_size = 120 * mm
        canvas.setFillAlpha(0.055)
        canvas.drawImage(
            ImageReader(str(LETTER_LOGO)),
            (A4[0] - watermark_size) / 2,
            (A4[1] - watermark_size) / 2,
            width=watermark_size,
            height=watermark_size,
            mask="auto",
        )
        canvas.restoreState()

    def draw_page_number(canvas, _document):
        canvas.saveState()
        canvas.setFont("Vera", 8)
        canvas.setFillColor(colors.HexColor("#555555"))
        canvas.drawCentredString(A4[0] / 2, 12 * mm, f"Halaman {canvas.getPageNumber()}")
        canvas.restoreState()

    def draw_page(canvas, doc):
        draw_letter_background(canvas, doc)
        draw_page_number(canvas, doc)

    document.build(story, onFirstPage=draw_page, onLaterPages=draw_page)
    pdf_buffer.seek(0)
    response = send_file(
        pdf_buffer,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"Surat_Pengantar_{reference}.pdf",
    )
    response.headers["Cache-Control"] = "no-store, private"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    return response


@app.get("/data-rukma")
def data_rukma():
    db = get_db()
    stats = {
        "members": db.execute(
            "SELECT COUNT(*) FROM residents WHERE status = 'Aktif' AND is_family_head = 1"
        ).fetchone()[0],
        "active": db.execute(
            "SELECT COUNT(*) FROM residents WHERE status = 'Aktif' AND is_family_head = 1"
        ).fetchone()[0],
    }
    return render_template("data_rukma.html", stats=stats)


@app.route("/data-warga", methods=["GET", "POST"])
def data_warga():
    if request.method == "POST":
        email = request.form.get("email", "").strip().lower()
        password = request.form.get("password", "")
        admin = get_db().execute(
            "SELECT id, password_hash FROM admins WHERE email = ?", (email,)
        ).fetchone()
        if admin and check_password_hash(admin["password_hash"], password):
            session.clear()
            session["admin_id"] = admin["id"]
            session["csrf_token"] = secrets.token_urlsafe(32)
            flash("Berhasil masuk. Selamat datang, Ketua RT.", "success")
            return redirect(url_for("data_warga"))
        flash("Email atau password tidak sesuai.", "error")

    if not session.get("admin_id"):
        has_admin = get_db().execute("SELECT EXISTS(SELECT 1 FROM admins)").fetchone()[0]
        return render_template(
            "login.html",
            has_admin=bool(has_admin),
            google_login_enabled=google_login_enabled,
        )

    period = request.args.get("iuran_bulan", date.today().strftime("%Y-%m"))
    if not is_valid_rukma_period(period):
        abort(400, description="Bulan iuran tidak valid.")

    db = get_db()
    residents = db.execute(
        "SELECT * FROM residents ORDER BY CASE WHEN status = 'Aktif' THEN 0 ELSE 1 END, name"
    ).fetchall()
    rukma_heads = db.execute(
        """
        SELECT id, name, address, family_card_number, status, created_at, updated_at
        FROM residents
        WHERE is_family_head = 1
            AND status = 'Aktif'
            AND substr(created_at, 1, 7) <= ?
        ORDER BY name
        """,
        (period,),
    ).fetchall()
    rukma_fee_row = db.execute(
        "SELECT amount FROM rukma_monthly_fees WHERE period = ?", (period,)
    ).fetchone()
    rukma_dues = db.execute(
        """
        SELECT
            head.id,
            head.name,
            head.address,
            head.family_card_number,
            head.status AS resident_status,
            due.status AS payment_status,
            due.paid_at,
            COALESCE(due.amount, fee.amount) AS amount
        FROM residents AS head
        LEFT JOIN rukma_monthly_fees AS fee ON fee.period = ?
        LEFT JOIN rukma_resident_dues AS due
            ON due.resident_id = head.id AND due.period = ?
        WHERE head.is_family_head = 1
            AND substr(head.created_at, 1, 7) <= ?
            AND (
                head.status = 'Aktif'
                OR substr(head.updated_at, 1, 7) >= ?
                OR EXISTS (
                    SELECT 1 FROM rukma_resident_dues AS history
                    WHERE history.resident_id = head.id AND history.period = ?
                )
            )
        ORDER BY head.name
        """,
        (period, period, period, period, period),
    ).fetchall()
    rukma_paid_count = sum(
        1 for member in rukma_dues if member["payment_status"] == "Lunas"
    )
    rukma_collected = sum(
        member["amount"] or 0
        for member in rukma_dues
        if member["payment_status"] == "Lunas"
    )
    letters = db.execute(
        "SELECT * FROM letter_requests ORDER BY created_at DESC, id DESC"
    ).fetchall()
    return render_template(
        "admin.html",
        residents=residents,
        rukma_heads=rukma_heads,
        rukma_period=period,
        rukma_fee=rukma_fee_row["amount"] if rukma_fee_row else None,
        rukma_dues=rukma_dues,
        rukma_paid_count=rukma_paid_count,
        rukma_collected=rukma_collected,
        letters=letters,
    )


@app.get("/auth/google")
def google_login():
    if not google_login_enabled:
        flash("Login Google belum dikonfigurasi.", "info")
        return redirect(url_for("data_warga"))
    return oauth.google.authorize_redirect(url_for("google_callback", _external=True))


@app.get("/auth/google/callback")
def google_callback():
    if not google_login_enabled:
        abort(404)

    try:
        oauth.google.authorize_access_token()
    except OAuthError:
        flash("Login Google dibatalkan atau gagal. Silakan coba lagi.", "error")
        return redirect(url_for("data_warga"))

    response = oauth.google.get("https://openidconnect.googleapis.com/v1/userinfo")
    response.raise_for_status()
    profile = response.json()
    google_sub = profile.get("sub")
    email = profile.get("email", "").strip().lower()
    if not google_sub or not email or profile.get("email_verified") is not True:
        flash("Akun Google harus memiliki email yang sudah terverifikasi.", "error")
        return redirect(url_for("data_warga"))

    db = get_db()
    admin = db.execute(
        "SELECT id, email, google_sub FROM admins WHERE google_sub = ?",
        (google_sub,),
    ).fetchone()
    if admin is None:
        admin = db.execute(
            "SELECT id, email, google_sub FROM admins WHERE email = ?", (email,)
        ).fetchone()
        if admin is None:
            flash(
                "Email Google ini belum terdaftar sebagai akun Ketua RT. "
                "Buat akun email/password terlebih dahulu.",
                "error",
            )
            return redirect(url_for("data_warga"))
        if admin["google_sub"] is not None:
            flash("Akun Google lain sudah terhubung ke email Ketua RT ini.", "error")
            return redirect(url_for("data_warga"))
        result = db.execute(
            "UPDATE admins SET google_sub = ? WHERE id = ? AND google_sub IS NULL",
            (google_sub, admin["id"]),
        )
        if result.rowcount != 1:
            flash("Akun Google gagal ditautkan. Silakan coba lagi.", "error")
            return redirect(url_for("data_warga"))
        db.commit()

    session.clear()
    session["admin_id"] = admin["id"]
    session["csrf_token"] = secrets.token_urlsafe(32)
    flash("Berhasil masuk dengan Google. Selamat datang, Ketua RT.", "success")
    return redirect(url_for("data_warga"))


def is_valid_rukma_period(period):
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", period):
        return False
    try:
        date.fromisoformat(f"{period}-01")
    except ValueError:
        return False
    return True


@app.post("/admin/logout")
@admin_required
def logout():
    session.clear()
    flash("Anda sudah keluar.", "info")
    return redirect(url_for("home"))


@app.get("/admin/account")
@admin_required
def account_settings():
    admin = get_db().execute(
        "SELECT email FROM admins WHERE id = ?", (session["admin_id"],)
    ).fetchone()
    if admin is None:
        session.clear()
        flash("Akun Ketua RT tidak ditemukan. Silakan masuk kembali.", "error")
        return redirect(url_for("data_warga"))
    return render_template("account.html", admin_email=admin["email"])


@app.post("/admin/account")
@admin_required
def update_admin_account():
    email = request.form.get("email", "").strip().lower()
    current_password = request.form.get("current_password", "")
    new_password = request.form.get("new_password", "")
    confirm_password = request.form.get("confirm_password", "")

    if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        flash("Format email tidak valid.", "error")
        return redirect(url_for("account_settings"))
    if not current_password:
        flash("Masukkan password saat ini untuk mengonfirmasi perubahan.", "error")
        return redirect(url_for("account_settings"))
    if new_password and len(new_password) < 8:
        flash("Password baru minimal 8 karakter.", "error")
        return redirect(url_for("account_settings"))
    if len(new_password) > 128:
        flash("Password baru terlalu panjang (maksimal 128 karakter).", "error")
        return redirect(url_for("account_settings"))
    if new_password != confirm_password:
        flash("Konfirmasi password baru tidak cocok.", "error")
        return redirect(url_for("account_settings"))

    db = get_db()
    admin = db.execute(
        "SELECT password_hash FROM admins WHERE id = ?", (session["admin_id"],)
    ).fetchone()
    if admin is None:
        session.clear()
        flash("Akun Ketua RT tidak ditemukan. Silakan masuk kembali.", "error")
        return redirect(url_for("data_warga"))
    if not check_password_hash(admin["password_hash"], current_password):
        flash("Password saat ini tidak sesuai.", "error")
        return redirect(url_for("account_settings"))

    password_hash = (
        generate_password_hash(new_password) if new_password else admin["password_hash"]
    )
    try:
        db.execute(
            "UPDATE admins SET email = ?, password_hash = ? WHERE id = ?",
            (email, password_hash, session["admin_id"]),
        )
        db.commit()
    except DB_INTEGRITY_ERRORS:
        db.rollback()
        flash("Email tersebut sudah digunakan akun admin lain.", "error")
        return redirect(url_for("account_settings"))

    session["csrf_token"] = secrets.token_urlsafe(32)
    flash("Data akun Ketua RT berhasil diperbarui.", "success")
    return redirect(url_for("account_settings"))


@app.post("/admin/residents")
@admin_required
def add_resident():
    name = request.form.get("name", "").strip()
    address = request.form.get("address", "").strip()
    phone = request.form.get("phone", "").strip()
    status = request.form.get("status", "Aktif")
    status_note = request.form.get("status_note", "").strip()
    if status not in RESIDENT_STATUSES:
        flash("Status warga tidak valid.", "error")
    elif not name or not address or len(name) > 100 or len(address) > 250 or len(phone) > 30 or len(status_note) > 250:
        flash("Nama, alamat, status, dan catatan status harus valid; periksa batas panjang isian.", "error")
    else:
        db = get_db()
        db.execute(
            "INSERT INTO residents (name, address, phone, status, status_note) VALUES (?, ?, ?, ?, ?)",
            (name, address, phone, status, status_note),
        )
        db.commit()
        flash("Data warga berhasil ditambahkan.", "success")
    return redirect(url_for("data_warga") + "#warga")


@app.post("/admin/residents/<int:resident_id>/edit")
@admin_required
def edit_resident(resident_id):
    name = request.form.get("name", "").strip()
    address = request.form.get("address", "").strip()
    phone = request.form.get("phone", "").strip()
    status = request.form.get("status", "Aktif")
    status_note = request.form.get("status_note", "").strip()
    if status not in RESIDENT_STATUSES:
        flash("Status warga tidak valid.", "error")
    elif not name or not address or len(name) > 100 or len(address) > 250 or len(phone) > 30 or len(status_note) > 250:
        flash("Nama, alamat, status, dan catatan status harus valid; periksa batas panjang isian.", "error")
    else:
        db = get_db()
        result = db.execute(
            """
            UPDATE residents
            SET name = ?, address = ?, phone = ?, status = ?, status_note = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (name, address, phone, status, status_note, resident_id),
        )
        db.commit()
        if result.rowcount == 0:
            abort(404)
        flash("Data warga berhasil diperbarui.", "success")
    return redirect(url_for("data_warga") + "#warga")


@app.post("/admin/residents/import-kk")
@admin_required
def import_kk_residents():
    family_card_number = request.form.get("family_card_number", "").strip()
    address = request.form.get("address", "").strip()
    phone = request.form.get("phone", "").strip()
    names = [name.strip() for name in request.form.getlist("resident_name")]
    national_ids = [
        national_id.strip() for national_id in request.form.getlist("national_id")
    ]
    if not re.fullmatch(r"[0-9]{16}", family_card_number):
        flash("Nomor KK harus terdiri dari 16 angka.", "error")
    elif not address or len(address) > 250 or len(phone) > 30:
        flash("Alamat wajib diisi; periksa juga batas panjang isian.", "error")
    elif not 1 <= len(names) <= 20 or len(names) != len(national_ids):
        flash("Daftar anggota KK tidak valid. Maksimal 20 anggota per impor.", "error")
    elif any(not name or len(name) > 100 for name in names):
        flash("Isi nama setiap anggota dengan benar sebelum menyimpan.", "error")
    elif any(not re.fullmatch(r"[0-9]{16}", value) for value in national_ids):
        flash("Setiap NIK harus terdiri dari 16 angka.", "error")
    elif len(set(national_ids)) != len(national_ids):
        flash("Ada NIK duplikat dalam hasil scan. Periksa kembali daftar.", "error")
    else:
        db = get_db()
        existing_family = db.execute(
            "SELECT 1 FROM residents WHERE family_card_number = ? LIMIT 1",
            (family_card_number,),
        ).fetchone()
        placeholders = ",".join("?" for _ in national_ids)
        duplicates = db.execute(
            f"SELECT national_id FROM residents WHERE national_id IN ({placeholders})",
            national_ids,
        ).fetchall()
        if duplicates:
            flash(
                "Sebagian NIK sudah terdaftar: "
                + ", ".join(row["national_id"] for row in duplicates),
                "error",
            )
        else:
            try:
                db.executemany(
                    """
                    INSERT INTO residents (
                        name, address, phone, national_id, family_card_number,
                        status, is_family_head
                    ) VALUES (?, ?, ?, ?, ?, 'Aktif', ?)
                    """,
                    [
                        (
                            name,
                            address,
                            phone,
                            national_id,
                            family_card_number,
                            int(index == 0 and existing_family is None),
                        )
                        for index, (name, national_id) in enumerate(
                            zip(names, national_ids)
                        )
                    ],
                )
                db.commit()
            except DB_INTEGRITY_ERRORS:
                db.rollback()
                flash(
                    "NIK sudah terdaftar atau ada data yang tidak valid. "
                    "Periksa kembali daftar anggota.",
                    "error",
                )
            else:
                flash(f"{len(names)} anggota keluarga berhasil disimpan.", "success")
    return redirect(url_for("data_warga") + "#warga")


@app.post("/admin/residents/<int:resident_id>/delete")
@admin_required
def delete_resident(resident_id):
    db = get_db()
    result = db.execute(
        """
        UPDATE residents
        SET status = 'Keluar',
            status_note = CASE
                WHEN status_note = '' THEN 'Dihapus dari daftar aktif oleh Ketua RT'
                ELSE status_note || ' | Dihapus dari daftar aktif oleh Ketua RT'
            END,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ?
        """,
        (resident_id,),
    )
    db.commit()
    if result.rowcount == 0:
        abort(404)
    flash("Data warga dipindahkan ke status Keluar agar riwayat tetap tersimpan.", "success")
    return redirect(url_for("data_warga") + "#warga")


@app.post("/admin/rukma-members")
@admin_required
def add_rukma_member():
    name = request.form.get("name", "").strip()
    address = request.form.get("address", "").strip()
    status = request.form.get("contribution_status", "Aktif")
    if (
        not name
        or not address
        or len(name) > 100
        or len(address) > 250
        or status not in {"Aktif", "Perlu konfirmasi"}
    ):
        flash("Periksa kembali nama, alamat, dan status anggota Rukma.", "error")
    else:
        db = get_db()
        db.execute(
            """
            INSERT INTO rukma_members (name, address, contribution_status)
            VALUES (?, ?, ?)
            """,
            (name, address, status),
        )
        db.commit()
        flash("Data anggota RUKMA berhasil ditambahkan.", "success")
    return redirect(url_for("data_warga") + "#rukma")


@app.post("/admin/rukma-fees")
@admin_required
def update_rukma_fee():
    period = request.form.get("period", "")
    amount_text = request.form.get("amount", "").strip()
    if not is_valid_rukma_period(period):
        abort(400, description="Bulan iuran tidak valid.")
    if not re.fullmatch(r"[0-9]{1,10}", amount_text):
        flash("Nominal iuran harus berupa angka rupiah yang valid.", "error")
        return redirect(url_for("data_warga", iuran_bulan=period) + "#rukma")

    amount = int(amount_text)
    if not 1 <= amount <= 1_000_000_000:
        flash("Nominal iuran harus lebih dari Rp 0 dan maksimal Rp 1.000.000.000.", "error")
        return redirect(url_for("data_warga", iuran_bulan=period) + "#rukma")

    db = get_db()
    db.execute(
        """
        INSERT INTO rukma_monthly_fees (period, amount)
        VALUES (?, ?)
        ON CONFLICT(period) DO UPDATE SET
            amount = excluded.amount,
            updated_at = CURRENT_TIMESTAMP
        """,
        (period, amount),
    )
    db.execute(
        """
        UPDATE rukma_resident_dues
        SET amount = ?, updated_at = CURRENT_TIMESTAMP
        WHERE period = ? AND status = 'Belum'
        """,
        (amount, period),
    )
    db.commit()
    flash("Nominal iuran untuk bulan tersebut berhasil disimpan.", "success")
    return redirect(url_for("data_warga", iuran_bulan=period) + "#rukma")


def get_rukma_member_for_period(member_id, period):
    return get_db().execute(
        """
        SELECT id, status
        FROM residents
        WHERE id = ?
            AND is_family_head = 1
            AND substr(created_at, 1, 7) <= ?
            AND (
                status = 'Aktif'
                OR substr(updated_at, 1, 7) >= ?
                OR EXISTS (
                    SELECT 1 FROM rukma_resident_dues AS history
                    WHERE history.resident_id = residents.id AND history.period = ?
                )
            )
        """,
        (member_id, period, period, period),
    ).fetchone()


@app.post("/admin/residents/<int:resident_id>/iuran/<period>/bayar")
@admin_required
def record_rukma_payment(resident_id, period):
    if not is_valid_rukma_period(period):
        abort(400, description="Bulan iuran tidak valid.")

    db = get_db()
    member = get_rukma_member_for_period(resident_id, period)
    if member is None or member["status"] != "Aktif":
        abort(404)
    fee = db.execute(
        "SELECT amount FROM rukma_monthly_fees WHERE period = ?", (period,)
    ).fetchone()
    if fee is None:
        flash("Tetapkan nominal iuran bulan ini sebelum mencatat pembayaran.", "error")
        return redirect(url_for("data_warga", iuran_bulan=period) + "#rukma")

    existing = db.execute(
        "SELECT status FROM rukma_resident_dues WHERE resident_id = ? AND period = ?",
        (resident_id, period),
    ).fetchone()
    if existing is not None and existing["status"] == "Lunas":
        flash("Iuran KK ini untuk bulan tersebut sudah tercatat lunas.", "info")
        return redirect(url_for("data_warga", iuran_bulan=period) + "#rukma")

    db.execute(
        """
        INSERT INTO rukma_resident_dues (
            resident_id, period, amount, status, paid_at, recorded_by
        ) VALUES (?, ?, ?, 'Lunas', CURRENT_TIMESTAMP, ?)
        ON CONFLICT(resident_id, period) DO UPDATE SET
            amount = excluded.amount,
            status = 'Lunas',
            paid_at = CURRENT_TIMESTAMP,
            recorded_by = excluded.recorded_by,
            updated_at = CURRENT_TIMESTAMP
        """,
        (resident_id, period, fee["amount"], session["admin_id"]),
    )
    db.commit()
    flash("Pembayaran iuran berhasil dicatat.", "success")
    return redirect(url_for("data_warga", iuran_bulan=period) + "#rukma")


@app.post("/admin/residents/<int:resident_id>/iuran/<period>/batalkan")
@admin_required
def cancel_rukma_payment(resident_id, period):
    if not is_valid_rukma_period(period):
        abort(400, description="Bulan iuran tidak valid.")
    if get_rukma_member_for_period(resident_id, period) is None:
        abort(404)

    db = get_db()
    result = db.execute(
        """
        UPDATE rukma_resident_dues
        SET status = 'Belum',
            paid_at = NULL,
            recorded_by = NULL,
            updated_at = CURRENT_TIMESTAMP
        WHERE resident_id = ? AND period = ? AND status = 'Lunas'
        """,
        (resident_id, period),
    )
    db.commit()
    if result.rowcount:
        flash("Pencatatan lunas dibatalkan; status iuran kembali belum lunas.", "success")
    else:
        flash("Belum ada pembayaran lunas yang perlu dibatalkan.", "info")
    return redirect(url_for("data_warga", iuran_bulan=period) + "#rukma")


@app.post("/admin/rukma-members/<int:member_id>/delete")
@admin_required
def delete_rukma_member(member_id):
    db = get_db()
    result = db.execute(
        """
        UPDATE rukma_members
        SET contribution_status = 'Nonaktif',
            inactive_at = CURRENT_TIMESTAMP
        WHERE id = ? AND contribution_status != 'Nonaktif'
        """,
        (member_id,),
    )
    db.commit()
    if result.rowcount == 0:
        abort(404)
    flash("KK diarsipkan; riwayat iuran sebelumnya tetap tersimpan.", "success")
    return redirect(url_for("data_warga") + "#rukma")


@app.post("/admin/letters/<int:letter_id>")
@admin_required
def update_letter_status(letter_id):
    status = request.form.get("status", "")
    if status not in {"Menunggu", "Diproses", "Diterima", "Selesai"}:
        abort(400, description="Status pengajuan tidak dikenal.")
    db = get_db()
    result = db.execute(
        """
        UPDATE letter_requests
        SET status = ?,
            approved_at = CASE
                WHEN ? IN ('Diterima', 'Selesai') THEN COALESCE(approved_at, CURRENT_TIMESTAMP)
                ELSE NULL
            END
        WHERE id = ?
        """,
        (status, status, letter_id),
    )
    db.commit()
    if result.rowcount == 0:
        abort(404)
    flash("Status pengajuan surat diperbarui.", "success")
    return redirect(url_for("data_warga") + "#pengajuan")


with app.app_context():
    init_db()


if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=os.environ.get("FLASK_DEBUG") == "1",
    )