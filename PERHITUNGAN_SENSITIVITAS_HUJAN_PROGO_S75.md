# Perhitungan Sensitivitas Durasi Hujan

## Sub-DAS yang digunakan

Contoh perhitungan menggunakan sub-DAS terbesar pada model Progo, yaitu **S75**.

| Parameter | Nilai |
|---|---:|
| Luas sub-DAS | 18,23 km² |
| Curve Number | 76,3 |
| Lag time | 130 menit = 2,167 jam |
| Loss method | SCS |
| Transform method | SCS |
| Debit puncak hujan 6 jam dari hasil model | 18,89 m³/s |

Sumber parameter: `data/source/Progo/Progo.basin`  
Sumber debit 6 jam yang tersedia: `data/hms/Progo/scenarios/T_0002.flow.json.gz`

## Tujuan

Memperkirakan perubahan debit puncak apabila durasi hujan merata diubah dari 6 jam menjadi 12 jam dan 24 jam, dengan asumsi total hujan efektif tetap sama.

Perhitungan ini adalah **estimasi sensitivitas**. Perhitungan ini tidak menjalankan ulang HEC-HMS dan bukan pengganti simulasi resmi menggunakan hyetograph 12 jam atau 24 jam.

## 1. Menghitung waktu menuju puncak

Untuk pendekatan unit hydrograph SCS, waktu menuju puncak dihitung dengan:

```text
Tp = Tlag + D/2
```

dengan:

- `Tp` = waktu menuju puncak, jam;
- `Tlag` = lag time sub-DAS, jam;
- `D` = durasi hujan, jam.

Dengan `Tlag = 2,167 jam`:

| Durasi hujan | Perhitungan | Tp |
|---|---:|---:|
| 6 jam | 2,167 + 6/2 | 5,167 jam |
| 12 jam | 2,167 + 12/2 | 8,167 jam |
| 24 jam | 2,167 + 24/2 | 14,167 jam |

## 2. Menghitung debit puncak estimasi

Untuk hujan efektif dan volume limpasan yang dianggap sama, debit puncak diproyeksikan berbanding terbalik dengan `Tp`:

```text
Qp(D) = Qp(6 jam) × Tp(6 jam) / Tp(D)
```

Debit puncak dasar yang digunakan adalah hasil model yang tersedia:

```text
Qp(6 jam) = 18,89 m³/s
```

### Durasi 12 jam

```text
Qp(12 jam) = 18,89 × (5,167 / 8,167)
            = 11,96 m³/s
```

### Durasi 24 jam

```text
Qp(24 jam) = 18,89 × (5,167 / 14,167)
            = 6,89 m³/s
```

## 3. Ringkasan hasil

| Skenario | Tp | Debit puncak |
|---|---:|---:|
| Hujan 6 jam — hasil model | 5,167 jam | **18,89 m³/s** |
| Hujan 12 jam — estimasi | 8,167 jam | **±11,96 m³/s** |
| Hujan 24 jam — estimasi | 14,167 jam | **±6,89 m³/s** |

## Asumsi

1. Hujan merata secara spasial pada sub-DAS S75.
2. Total hujan efektif sama untuk durasi 6, 12, dan 24 jam.
3. Bentuk respons DAS dianggap cukup linear untuk keperluan estimasi.
4. Nilai 18,89 m³/s digunakan sebagai debit puncak referensi.
5. Pengaruh perubahan distribusi temporal, baseflow, dan routing tidak dihitung ulang.

## Batasan

Hasil 12 jam dan 24 jam dapat berbeda dari HEC-HMS karena model sebenarnya memperhitungkan proses yang tidak sepenuhnya linear, termasuk:

- kehilangan hujan berdasarkan Curve Number SCS;
- perubahan hujan efektif akibat distribusi intensitas;
- baseflow;
- transformasi limpasan;
- routing pada jaringan sungai.

Untuk hasil resmi, buat hyetograph 12 jam dan 24 jam pada **Meteorologic Model** HEC-HMS, gunakan `Progo.basin` yang sama, lalu jalankan simulasi ulang.

## Kesimpulan

Dengan asumsi total hujan efektif tetap sama, peningkatan durasi hujan dari 6 menjadi 12 dan 24 jam diperkirakan menurunkan debit puncak S75 dari 18,89 m³/s menjadi masing-masing sekitar 11,96 m³/s dan 6,89 m³/s. Angka tersebut digunakan sebagai gambaran awal sensitivitas durasi hujan.
