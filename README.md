# Penelusuran Banjir — Versi 1.0.1.0

WebGIS untuk membaca dan menelusuri hasil simulasi banjir HEC-HMS pada jaringan sungai. Aplikasi membantu pengguna melihat hidrograf, debit puncak, waktu puncak, hubungan hulu–hilir, serta perubahan aliran pada beberapa skenario model.

Aplikasi ini merupakan **viewer hasil model**, bukan mesin simulasi hidrologi yang menghitung ulang hujan-limpasan atau routing di browser. Data HEC-HMS diproses terlebih dahulu menjadi data ringan yang siap dibaca oleh web.

## Riwayat versi

### 1.0.1.0 — Pembaruan metodologi dan dokumentasi

- Modal **Metodologi & Sumber Data** disusun ulang dengan alur pemodelan yang lebih mudah dipahami oleh pengguna hidrologi pemula.
- Metodologi menjelaskan tahapan hujan, kehilangan hujan, hujan efektif, transformasi limpasan, aliran dasar, routing, dan outlet.
- Sumber data dipisahkan menjadi Basin Model `.basin`, database spasial `.sqlite`, hasil HEC-DSS `.dss`, data runtime *precomputed*, dan data referensi peta.
- Ditambahkan penjelasan kontrak `FLOW` sebagai outflow dan `FLOW-COMBINE` sebagai inflow Reach.
- README dirapikan menjadi dokumentasi utama rilis dan dokumentasi non-audit digabungkan ke dalamnya.
- `.gitignore` diperbarui untuk mengecualikan data lokal, `.cache`, `.venv`, dan dokumen audit.

### 1.0.0.0 — Rilis awal

- Rilis awal WebGIS Penelusuran Banjir berbasis hasil precompute HEC-HMS.
- Menyediakan peta jaringan sungai, Titik Kontrol, hidrograf, penelusuran upstream, skenario banjir, backend lokal, dan backend Cloudflare R2.
## Fitur utama

- Memilih DAS dan skenario model.
- Menampilkan batas DAS, jaringan sungai, sub-DAS, dan jaringan routing.
- Menambahkan Titik Kontrol pada jaringan sungai.
- Membaca hidrograf dan debit puncak pada lokasi yang dipilih.
- Membandingkan debit serta waktu puncak antar-Titik Kontrol.
- Menelusuri jaringan dari titik paling hilir ke seluruh jaringan hulu yang berkontribusi.
- Menampilkan fase perubahan debit secara visual: debit awal/rendah, kenaikan, mendekati puncak, puncak, dan resesi.
- Mengunduh hidrograf Titik Kontrol dalam format `.xlsx`.
- Menyediakan backend lokal atau Cloudflare R2 untuk data runtime.

## Cara membaca hasil

Peta dan grafik menampilkan keluaran model. Nilai pada aplikasi bukan pengukuran langsung di sungai dan tidak menunjukkan kedalaman, kecepatan, tinggi muka air, atau luas genangan.

Skenario `2`, `10`, dan `25` tahun adalah label skenario pada hasil HEC-HMS. Label tersebut tidak dengan sendirinya membuktikan periode ulang statistik, analisis frekuensi, atau tingkat ketidakpastian.

Istilah **selisih waktu puncak** berarti perbedaan waktu terjadinya puncak pada dua hidrograf. Metrik ini bukan waktu tempuh partikel maupun waktu tempuh hidraulik formal.

## Metodologi pemodelan

Model sumber menggunakan HEC-HMS. Secara umum alurnya adalah:

```text
Hujan
  ↓
Kehilangan hujan (loss)
  ↓
Hujan efektif
  ↓
Transformasi menjadi limpasan langsung
  ↓
Aliran dasar dan penggabungan aliran
  ↓
Routing pada reach
  ↓
Outlet
```

Metode yang digunakan mengikuti konfigurasi masing-masing Basin Model HEC-HMS. Pada dataset yang tersedia, komponen utamanya mencakup:

- **Loss:** SCS Curve Number.
- **Transform:** SCS Unit Hydrograph.
- **Baseflow:** Recession.
- **Routing:** Muskingum–Cunge.
- **Geometri saluran:** penampang trapezoidal.

Parameter tidak dianggap sebagai satu nilai yang mewakili seluruh DAS. Nilai parameter dapat berbeda antar-sub-DAS dan antar-reach sesuai model sumber.

Preprocessing hanya mengubah format dan struktur penyimpanan agar efisien untuk web. Preprocessing tidak mengganti metode atau menghitung ulang hasil hidrologi HEC-HMS.

## Sumber dan kontrak data

### Berkas sumber HEC-HMS

Setiap model pada `data/source/<model>/` memerlukan berkas berikut:

```text
data/source/Oyo/
├── Oyo.basin
├── Oyo.sqlite
├── T_0002.dss
├── T_0010.dss
└── T_0025.dss
```

- `*.basin` adalah sumber elemen dan topologi HEC-HMS. Parser membaca Subbasin, Reach, Junction, Sink/outlet, serta hubungan downstream.
- `*.sqlite` adalah sumber data spasial. Layer `reach2d` digunakan sebagai centerline routing dan `subbasin2d` digunakan sebagai poligon sub-DAS.
- `T_xxxx.dss` adalah sumber seri waktu debit untuk setiap skenario.
- Tidak diperlukan SHP eksternal atau `longest_flowpath` terpisah untuk preprocessing routing.

### Kontrak seri debit Reach

Untuk setiap Reach, record DSS dibaca dengan aturan:

```text
//Rxx/FLOW/...          = debit keluaran (outflow) Reach
//Rxx/FLOW-COMBINE/...  = debit masukan (inflow) Reach
```

Keduanya berasal dari Reach yang sama. Jika `FLOW-COMBINE` tidak tersedia, sistem tidak menggantinya dengan `FLOW` sebagai inflow.

Untuk centerline yang mewakili Subbasin, seri yang digunakan adalah `Subbasin/FLOW`.

### Data runtime precomputed

Hasil preprocessing disimpan dalam `data/hms/<model>/`:

```text
data/hms/Oyo/
├── model.json
├── topology.json
├── source_manifest.json
├── reaches.geojson
├── subbasins.geojson
├── modeled_area.geojson
├── model_rivers.geojson
└── scenarios/
    ├── T_0002.flow.json.gz
    ├── T_0010.flow.json.gz
    └── T_0025.flow.json.gz
```

`model.json` berisi metadata model, daftar skenario, interval waktu, dan jumlah seri. `topology.json` berisi hubungan jaringan. GeoJSON digunakan untuk peta, sedangkan berkas `.flow.json.gz` digunakan untuk seri debit.

Data runtime dapat disimpan lokal atau diunggah ke R2. Folder `data` berisi data model berukuran besar dan diatur terpisah dari source code saat distribusi/deployment.

### Data referensi kartografi

Layer referensi digunakan untuk peta dan penamaan, bukan untuk menghitung routing:

- `toponim.sqlite` untuk penamaan otomatis dan geocoding lokal.
- `official_reference.gpkg` untuk batas DAS dan referensi resmi.
- `official_rivers_original.gpkg` untuk jaringan sungai dengan atribut sumber.

Layer **official rivers** pada halaman Penelusuran Banjir menggunakan geometri jaringan sungai resmi yang telah di-clip ke modeled area HEC-HMS. Endpoint `/api/hec-routing/modeled-rivers` menyediakan tier tampilan berdasarkan zoom, sehingga jaringan di luar cakupan model tidak ikut ditampilkan dan tidak memengaruhi routing.

Parameter tampilan official rivers mengikuti kontrak visual Delineasi DTA: batas zoom orde 1–3 dan `other`, generalisasi berbasis tier, ketebalan garis berdasarkan orde, label sungai dengan prioritas collision, serta parameter GeoJSON yang menjaga presisi dan kualitas garis. Kesamaan ini hanya berlaku untuk kartografi/rendering; algoritma penelusuran, chainage, pemilihan seri debit, dan topologi HEC-HMS tetap memakai mekanisme Penelusuran Banjir.

Geometri sungai referensi dan proses kartografi tidak mengubah hasil simulasi.

## Preprocessing HEC-HMS

Pada Windows, siapkan `data/source/<model>/`, lalu jalankan:

```bat
preprocess_hms.bat
```

Untuk satu model saja:

```bat
preprocess_hms.bat Oyo
```

Tahapan preprocessing:

```text
.basin
  ↓ membaca topologi
.sqlite
  ↓ memilih reach2d dan subbasin2d
pencocokan reach2d.name dengan elemen .basin
  ↓
orientasi centerline upstream → downstream
  ↓
penyusunan GeoJSON dan metadata runtime
.dss
  ↓ membaca FLOW, FLOW-COMBINE, dan seri Subbasin
  ↓
data/hms/<model>
```

Field `reach2d.name` dicocokkan langsung dengan ID Reach atau Subbasin pada `.basin`. Centerline yang tidak memiliki pasangan pada topologi model tidak dipublikasikan sebagai jaringan routing.

Arah centerline ditentukan dari koordinat upstream/downstream SQLite. Arah ini menjadi dasar perhitungan chainage dan penelusuran hulu–hilir. `strmorder` digunakan untuk membentuk ketebalan dasar garis sungai agar sungai berorde lebih tinggi terlihat lebih tebal.

Untuk kompatibilitas `pydsstools`, Python 3.11 atau 3.12 direkomendasikan.

## Snapping dan penelusuran jaringan

Saat pengguna menambahkan Titik Kontrol, sistem:

1. Memeriksa apakah titik berada di dalam modeled area.
2. Mencari centerline routing terdekat dalam radius snap.
3. Mencocokkan titik dengan Reach atau Subbasin.
4. Menentukan seri debit sesuai jenis elemen dan posisi titik.
5. Mengikuti topologi `.basin` untuk mengambil jaringan upstream.

Selama proses penambahan titik, peta menampilkan titik pilihan asli berwarna putih, titik hasil snapping berwarna biru tua, dan garis penghubung putus-putus. Jarak snapping ditampilkan pada dialog; garis penghubung hanya ditampilkan bila jaraknya lebih dari 0,25 m. Peringatan pergeseran jauh memakai ambang adaptif yang dibatasi 150–500 m.

Pratinjau titik dibuat sejak peta selesai dimuat, sebelum request HEC-HMS pertama, sehingga titik pertama langsung terlihat. Jika model kala ulang tidak tersedia, request dibatalkan dan seluruh marker pratinjau dibersihkan sebelum modal peringatan ditampilkan.

Pada Reach, posisi titik menggunakan aturan proxy sederhana:

```text
chainage < 50% panjang Reach  → seri inflow
chainage ≥ 50% panjang Reach  → seri outflow
```

Aturan ini memilih seri endpoint terdekat; sistem belum melakukan interpolasi hidraulik sepanjang Reach. Dua titik pada cabang yang berbeda sebelum bertemu di junction tidak memiliki jalur langsung satu sama lain.

## Visualisasi aliran

Fase tampilan ditentukan dari rasio `Q/Qp` dan posisi relatif terhadap puncak hidrograf:

| Fase | Kondisi umum |
|---|---|
| Debit awal/rendah | `Q/Qp < 0,20` sebelum puncak |
| Rising Limb | `0,20–0,50` sebelum puncak |
| Mendekati Puncak | `0,50–0,85` sebelum puncak |
| Peak Discharge | `0,85–1,00` di sekitar puncak |
| Falling Limb / Resesi | setelah puncak ketika debit menurun |

Klasifikasi ini adalah cara membaca bentuk hidrograf pada peta, bukan pemisahan baseflow secara fisik. Warna dan ketebalan garis membantu membedakan fase aliran serta mengikuti orde sungai.

## Menjalankan aplikasi

Salin `.env.example` menjadi `.env`, lalu pilih backend yang sesuai.

### Backend lokal

Gunakan:

```text
DATA_BACKEND=local
```

Jalankan aplikasi:

```bat
run.bat
```

Atau jalankan langsung:

```bat
py -3 -m uvicorn api.app:app --host 127.0.0.5 --port 8000
```

Buka [http://127.0.0.5:8000](http://127.0.0.5:8000).

### Backend Cloudflare R2

Gunakan:

```text
DATA_BACKEND=r2
R2_RUNTIME_BUCKET=flood-routing
R2_REFERENCE_BUCKET=dta-map-assets
```

Setelah preprocessing selesai, unggah runtime dengan:

```bat
preprocess_hms_r2.bat
```

`preprocess_hms_r2.bat` tidak menjalankan preprocessing ulang. Skrip tersebut memakai isi `data/hms` yang sudah tersedia dan mengunggah runtime ke bucket R2. Official basin/river tetap menggunakan bucket referensi terpisah.

Semua launcher menentukan root berdasarkan lokasi script. Folder repository dapat dipindahkan atau diganti nama tanpa mengubah path secara manual.

## Endpoint utama

- `GET /api/hec-routing/info`
- `GET /api/hec-routing/reaches?scenario=T_0002`
- `GET /api/hec-routing/modeled-rivers?scenario=T_0002`
- `GET /api/hec-routing/modeled-area?scenario=T_0002`
- `GET /api/hec-routing/series?scenario=T_0002&reach_ids=Oyo:R5,Oyo:S10`
- `POST /api/hec-routing/snap`
- `POST /api/hec-routing/observe`

Nama endpoint `reaches` dipertahankan untuk kompatibilitas; isinya mencakup centerline routing Reach dan Subbasin yang tersedia pada runtime.

## Struktur repository

```text
api/                 aplikasi FastAPI dan service backend
scripts/              preprocessing, upload, dan persiapan toponim
static/               JavaScript, CSS, dan aset antarmuka
templates/            template halaman web
data/source/          sumber HEC-HMS lokal (opsional/terpisah)
data/hms/             hasil preprocessing runtime (opsional/terpisah)
data/reference/       data referensi spasial
preprocess_hms.bat    preprocessing Windows
preprocess_hms_r2.bat upload runtime ke R2
run.bat               launcher aplikasi Windows
run_linux_mac.sh      launcher aplikasi Linux/macOS
```

## Pemeriksaan dan validasi

Perintah pemeriksaan yang digunakan pada pengembangan:

```bat
pytest -q
node --check static\js\spatial.js
node --check static\js\flood-routing.js
python -m py_compile api\app.py api\core.py
```

Pemeriksaan parity tampilan dan interaksi Penelusuran Banjir:

```bat
pytest -q tests/test_dta_parity.py tests/test_hec_routing.py tests/test_shell_performance.py
```

Pemeriksaan struktur dataset mencakup kecocokan elemen `.basin` dengan centerline SQLite, kelengkapan seri DSS, arah centerline, topologi downstream, dan konsistensi metadata runtime. Pemeriksaan tersebut tidak menggantikan kalibrasi, validasi terhadap data observasi, analisis sensitivitas, atau pemeriksaan ketidakpastian model.

Pada dataset Oyo yang tervalidasi, model memiliki 196 Subbasin, 98 Reach, 98 Junction, dan 1 Sink. Sebanyak 227 baris `reach2d` menghasilkan 197 centerline routing yang cocok dengan topologi, terdiri dari 98 Reach dan 99 Subbasin.

## Batasan penggunaan

Aplikasi ini belum melakukan:

- perhitungan hidrologi atau routing secara live;
- perhitungan muka air, kedalaman, kecepatan, atau luas genangan;
- interpolasi hidraulik pada posisi interior Reach;
- kalibrasi dan validasi terhadap observasi secara otomatis;
- analisis frekuensi untuk membuktikan label kala ulang;
- analisis sensitivitas dan ketidakpastian;
- pemeriksaan kontinuitas massa/volume sebagai bagian dari viewer.

Gunakan aplikasi sebagai alat inspeksi dan komunikasi hasil model. Untuk keputusan operasional, tetap diperlukan pemeriksaan teknis, data lapangan, dan sumber informasi resmi yang berlaku.

## Dokumentasi audit

Dokumen audit teknis sengaja dipertahankan terpisah karena berfungsi sebagai catatan pemeriksaan internal:

- `AUDIT_TEKNIS_HIDROLOGI.md`
- `AUDIT_TOPONIM_PENAMAAN_TITIK.md`
- `AUDIT_UX_UI_DESKTOP_MOBILE.md`

Dokumen tersebut bukan panduan penggunaan utama dan tidak digabungkan ke README rilis.

## Lisensi dan atribusi

Periksa lisensi setiap data spasial, library, dan sumber model sebelum distribusi publik. Dokumentasi HEC-HMS tersedia pada [USACE Hydrologic Engineering Center](https://www.hec.usace.army.mil/software/hec-hms/documentation.aspx).

---

**Rilis:** `1.0.1.0`
