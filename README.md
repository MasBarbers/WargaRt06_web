# Warga RT 006

Aplikasi web untuk pengajuan surat pengantar, informasi RUKMA (Rukun Kematian), dan pengelolaan data warga RT 006. Tampilan menggunakan HTML/Jinja dan backend Flask. Untuk penggunaan lokal, data disimpan di SQLite `instance/rt006.sqlite3`; bila `DATABASE_URL` diatur, aplikasi menggunakan PostgreSQL.

## Menjalankan aplikasi

Gunakan Python 3.10 atau lebih baru.

```powershell
py -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python create_admin.py
python app.py
```

Buka `http://127.0.0.1:5000`. Pembuatan akun admin meminta email dan password secara interaktif; password tidak disimpan dalam bentuk teks biasa. Jalankan `python create_admin.py` lagi untuk membuat akun admin tambahan.

## Fitur

- **Surat Pengantar:** warga mengisi NIK (tanpa nomor KK) serta data sesuai formulir RT, lalu dapat melihat pratinjau surat dan mengunduh PDF A4 setelah memeriksanya. Lambang Jakarta digunakan pada kop surat dan sebagai watermark latar. Ketua RT tetap dapat melihat surat melalui tautan referensi pribadi dan memperbarui status. PDF berstatus draf sampai disetujui; formulir ini adalah layanan RT, bukan surat yang diterbitkan Kelurahan, dan tetap memerlukan tanda tangan pengurus berwenang sebelum digunakan.
- **Data RUKMA dan iuran:** informasi umum tetap dapat dilihat warga, tetapi daftar KK dan pencatatan iuran hanya dapat diakses Ketua RT setelah masuk. Daftar penagihan diambil dari data warga dan hanya menyertakan Kepala Keluarga. Saat impor KK, anggota pertama yang dimasukkan ditetapkan sebagai Kepala Keluarga; anggota berikutnya dari nomor KK yang sama tidak ditagihkan lagi. Ketua RT mengatur satu nominal iuran untuk setiap KK pada tiap bulan, mencatat status lunas, melihat jumlah KK yang sudah/belum membayar dan total kas terkumpul, serta membatalkan pencatatan lunas yang keliru. Riwayat iuran bulanan tersimpan di database yang dikonfigurasi; data KK yang keluar tidak dihapus sehingga riwayat pembayaran tetap ada.
- **Data Warga:** memerlukan email dan password Ketua RT. Ketua RT dapat mengubah email dan password di panel akun (password baru minimal 8 karakter, dengan konfirmasi password saat ini), mengelola data warga, serta memperbarui status pengajuan.
- Ketua RT dapat memakai login Google untuk email yang sama dengan akun admin. Tautan Google hanya tersedia setelah OAuth dikonfigurasi.
- Formulir menggunakan token CSRF. Password admin disimpan sebagai hash.

## Mengaktifkan login Google

1. Buat OAuth Client ID bertipe **Web application** di Google Cloud Console dan tambahkan Authorized redirect URI `http://127.0.0.1:5000/auth/google/callback` untuk pengembangan lokal.
2. Buat akun Ketua RT dengan `python create_admin.py`. Gunakan alamat Gmail/Google yang sama untuk akun tersebut.
3. Isi kredensial OAuth di PowerShell sebelum menjalankan aplikasi:

   ```powershell
   $env:GOOGLE_CLIENT_ID = "Client-ID-dari-Google"
   $env:GOOGLE_CLIENT_SECRET = "Client-Secret-dari-Google"
   python app.py
   ```

Kredensial Google disimpan sebagai environment variables dan jangan dimasukkan ke kode atau dibagikan di chat. Untuk situs online, daftarkan URI HTTPS sesuai domain situs di Google Cloud dan atur `SECRET_KEY` acak yang kuat serta `COOKIE_SECURE=1`. Jangan aktifkan `FLASK_DEBUG=1` di produksi.

Formulir surat menyimpan biodata termasuk NIK dan nomor KK ke database yang dikonfigurasi. Tautan surat memakai token acak rahasia dan tidak di-cache; warga harus merahasiakan tautan tersebut. Batasi akses perangkat/server dan gunakan HTTPS bila aplikasi dipublikasikan.

Di panel Ketua RT, **Impor dari Kartu Keluarga** menerima foto kamera atau file gambar. OCR Bahasa Indonesia berjalan di browser untuk mendeteksi nomor 16 digit; OCR tidak mengisi nama secara andal, sehingga Ketua RT harus memeriksa/mengisi nama, nomor KK, alamat, dan NIK pada daftar sebelum menyimpannya. Foto tidak dikirim ke server atau disimpan. Browser memuat pustaka OCR dan data bahasa dari CDN; teks OCR tetap diproses di perangkat. Anggota disimpan sebagai data warga hanya setelah dikonfirmasi, satu record per NIK. NIK unik mencegah impor ganda.

## Deploy publik di Render

Repositori ini menyertakan `render.yaml` untuk membuat web service Flask dan database PostgreSQL persisten. Paket database dan web service Render yang didefinisikan di sana adalah paket berbayar; biaya dan ketersediaan paket dapat berubah, jadi tinjau biaya pada dashboard Render sebelum membuat Blueprint.

1. Buat repositori GitHub baru dan unggah kode proyek. Jangan unggah `.env`, folder virtual environment, atau file database lokal; aturan `.gitignore` sudah mengecualikan file tersebut.
2. Di Render, pilih **New > Blueprint**, hubungkan repositori GitHub, lalu tinjau resource dan biaya dari `render.yaml` sebelum menyetujui pembuatan.
3. Render akan memasang dependency dan menghubungkan web service ke PostgreSQL melalui `DATABASE_URL`. Migrasi tabel dijalankan saat aplikasi mulai. `SECRET_KEY` dibuat oleh Render dan cookie sesi HTTPS diaktifkan.
4. Setelah deploy berhasil, buka Shell untuk web service dan jalankan `python create_admin.py`. Gunakan email Ketua RT serta password kuat; perintah meminta password secara tersembunyi.
5. Uji halaman utama, pengajuan surat, login Ketua RT, dan pencatatan iuran. Tambahkan domain produksi ke pengaturan OAuth Google bila fitur login Google digunakan.

Untuk lokal, `DATABASE_URL` boleh tidak diatur agar SQLite tetap digunakan. Jangan pernah mengunggah kredensial database, `SECRET_KEY`, file `.env`, atau salinan database berisi NIK ke repositori publik.

## Menjalankan tes

```powershell
python -m unittest discover -s tests -p "test_*.py" -v
```
