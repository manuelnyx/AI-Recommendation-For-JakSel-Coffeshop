import math
import re
import pandas as pd
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional

app = FastAPI(title="AI Smart Café Maps API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Serve index.html di root agar GPS browser bisa berfungsi (file:// tidak support GPS)
@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")

class SearchRequest(BaseModel):
    max_budget: int = 100000
    min_rating: float = 0.0
    wifi_required: bool = False
    power_outlet_required: bool = False
    open_24h_required: bool = False
    open_now: Optional[str] = None      # Format "HH:MM"
    sort_by: str = "rating"             # "rating", "price_low", "price_high", "nearest"
    keyword: str = ""
    user_lat: Optional[float] = None
    user_lon: Optional[float] = None
    max_distance_km: float = 0.0


def load_data():
    try:
        # Load langsung dari apify mentah
        df_raw = pd.read_csv("apify_dataset.csv")
        
        # Buat dataframe bersih di memori (Tanpa perlu script converter terpisah)
        df = pd.DataFrame()
        df['cafe_id'] = range(1, len(df_raw) + 1)
        df['cafe_name'] = df_raw['title'].fillna("Unknown Cafe")
        df['latitude'] = pd.to_numeric(df_raw['location/lat'], errors='coerce').fillna(0.0)
        df['longitude'] = pd.to_numeric(df_raw['location/lng'], errors='coerce').fillna(0.0)
        df['rating'] = pd.to_numeric(df_raw['totalScore'], errors='coerce').fillna(0.0)
        df['address'] = df_raw['address'].fillna("Jakarta")
        df['google_maps_query'] = df_raw['url'].fillna("")
        df['opening_hours'] = "08:00 - 22:00" # fallback dasar
        df['menu_highlights'] = "Kopi Spesial & Makanan Ringan"
        
        # Estimasi harga dari simbol ($, $$ dsb) jika ada di kolom 'price'
        def parse_price(val):
            val = str(val).strip()
            if val == '$$$$': return 150000, 300000
            if val == '$$$': return 100000, 150000
            if val == '$$': return 50000, 100000
            if val == '$': return 25000, 50000
            return 50000, 100000
            
        if 'price' in df_raw.columns:
            prices = df_raw['price'].apply(parse_price)
            df['price_min'] = [p[0] for p in prices]
            df['price_max'] = [p[1] for p in prices]
        else:
            df['price_min'] = 50000
            df['price_max'] = 100000
            
        # Ekstrak Fasilitas dan Jam Buka dari data Google Maps secara akurat
        has_wifi = []
        has_power = []
        is_24 = []
        opening = []
        
        for idx, row in df_raw.iterrows():
            is_wifi = 0
            is_laptop = 0
            open_24 = 0
            hours_str = "08:00 - 22:00"
            
            # Cek jam buka (Hari Senin sebagai patokan dasar)
            if 'openingHours/0/hours' in row and pd.notna(row['openingHours/0/hours']):
                h = str(row['openingHours/0/hours'])
                if "24 hours" in h.lower() or "24 jam" in h.lower():
                    open_24 = 1
                    hours_str = "Buka 24 Jam"
                else:
                    # Rangkum format 'to' Google
                    hours_str = h.replace(' to ', ' - ').replace(' ', ' ')
            
            # Cek amenities dari kolom yang ada (contoh: Free Wi-Fi, Good for working on laptop)
            for col, val in row.items():
                if str(val).strip().lower() in ['true', '1', 'yes']:
                    col_lower = str(col).lower()
                    if 'wi-fi' in col_lower:
                        is_wifi = 1
                    if 'laptop' in col_lower:
                        is_laptop = 1
            
            # Default logic untuk colokan jika label laptop tidak ada tapi tempatnya Coffee Shop
            if is_laptop == 0 and "coffee" in str(row.get('categories/0', '')).lower():
                is_laptop = 1 # Asumsi dasar coffee shop

            # Bisa juga ngecek jika di teks review ada penyebutan 'colokan' (bila diperlukan)
            
            has_wifi.append(is_wifi)
            has_power.append(is_laptop)
            is_24.append(open_24)
            opening.append(hours_str)

        df['has_wifi'] = has_wifi
        df['has_power_outlet'] = has_power
        df['is_24h'] = is_24
        df['opening_hours'] = opening

        # Hanya ambil yang punya kordinat valid
        df = df[df['latitude'] != 0.0]
        return df

    except FileNotFoundError:
        raise HTTPException(status_code=500, detail="apify_dataset.csv tidak ditemukan. Harap pastikan file scraper berada di direktori yang sama.")


def haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Hitung jarak antara dua titik koordinat (km) menggunakan formula Haversine."""
    R = 6371
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(math.radians(lat1))
         * math.cos(math.radians(lat2))
         * math.sin(dlon / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


def normalize_opening_hours(raw: str) -> str:
    """
    Normalisasi format jam buka dari OSM ke format yang ramah user.
    Contoh:
      "Mo-Su 08:00-22:00"  → "08:00 – 22:00"
      "Mo-Fr 09:00-18:00"  → "Sen-Jum  09:00 – 18:00"
      "24/7"               → "Buka 24 Jam"
      "00:00 - 23:59"      → "Buka 24 Jam"
    """
    if not raw or str(raw).strip() == "":
        return "08:00 – 22:00"

    raw = str(raw).strip()

    # 24 jam
    if raw in ("24/7", "00:00 - 23:59", "00:00-23:59"):
        return "Buka 24 Jam 🌙"

    # Ganti separator
    raw = raw.replace(" - ", " – ").replace("-", " – ", 1) if ":" not in raw else raw

    # Pola "Day HH:MM-HH:MM" atau "Day HH:MM – HH:MM"
    day_map = {
        "Mo": "Sen", "Tu": "Sel", "We": "Rab", "Th": "Kam",
        "Fr": "Jum", "Sa": "Sab", "Su": "Min",
        "Mo-Su": "Setiap Hari", "Mo-Fr": "Sen–Jum", "Mo-Sa": "Sen–Sab",
    }

    # Contoh: "Mo-Su 08:00-22:00"
    pattern = re.match(
        r'^(Mo-Su|Mo-Fr|Mo-Sa|Mo|Tu|We|Th|Fr|Sa|Su)\s+(\d{2}:\d{2})[\- –]+(\d{2}:\d{2})$',
        raw
    )
    if pattern:
        day_part = day_map.get(pattern.group(1), pattern.group(1))
        open_t = pattern.group(2)
        close_t = pattern.group(3)
        if pattern.group(1) == "Mo-Su":
            return f"{open_t} – {close_t}"
        return f"{day_part}  {open_t} – {close_t}"

    # Format "HH:MM - HH:MM" sudah bagus
    simple = re.match(r'^(\d{2}:\d{2})\s*[\-–]\s*(\d{2}:\d{2})$', raw)
    if simple:
        open_t = simple.group(1)
        close_t = simple.group(2)
        if open_t == "00:00" and close_t == "23:59":
            return "Buka 24 Jam 🌙"
        return f"{open_t} – {close_t}"

    # Format e.g. "10:00-22:00" (no space)
    compact = re.match(r'^(\d{2}:\d{2})-(\d{2}:\d{2})$', raw)
    if compact:
        return f"{compact.group(1)} – {compact.group(2)}"

    # Jika ada semicolon (multiple schedules), ambil yang pertama
    if ";" in raw:
        parts = raw.split(";")
        return normalize_opening_hours(parts[0].strip())

    # Format panjang lain: buang prefix day jika ada, ambil waktu
    time_match = re.search(r'(\d{2}:\d{2})\s*[\-–]\s*(\d{2}:\d{2})', raw)
    if time_match:
        return f"{time_match.group(1)} – {time_match.group(2)}"

    # Fallback
    return raw if len(raw) <= 20 else "08:00 – 22:00"


def is_open_now(opening_hours_normalized: str, current_time: str) -> bool:
    """Cek apakah kafe sedang buka berdasarkan jam yang sudah dinormalisasi."""
    try:
        oh = opening_hours_normalized
        if "24 Jam" in oh:
            return True
        # Coba parse format "HH:MM – HH:MM" atau dengan prefix hari
        time_match = re.search(r'(\d{2}:\d{2})\s*–\s*(\d{2}:\d{2})', oh)
        if time_match:
            return time_match.group(1) <= current_time <= time_match.group(2)
    except:
        pass
    return True


def get_thumbnail_category(name: str, menu: str, tags_raw: str = "") -> str:
    """
    Tentukan kategori thumbnail berdasarkan nama kafe dan menu.
    Kategori: starbucks | specialty | kopi_susu | warkop | bubble_tea | bakery | dessert | default
    """
    name_lower = name.lower()
    menu_lower = str(menu).lower()
    combined = name_lower + " " + menu_lower

    if "starbucks" in name_lower:
        return "starbucks"
    if any(k in name_lower for k in ["anomali", "toby", "crematology", "tanamera", "selective", "ottens", "liberica", "toca"]):
        return "specialty"
    if any(k in combined for k in ["bubble_tea", "chatime", "xi bo ba", "mixue", "quickly", "haus", "boba", "thai tea"]):
        return "bubble_tea"
    if any(k in combined for k in ["bakery", "roti", "bread", "cake", "pastry", "cakery"]):
        return "bakery"
    if any(k in combined for k in ["dessert", "ice cream", "frozen yogurt", "shake", "juice"]):
        return "dessert"
    if any(k in name_lower for k in ["warkop", "warung", "kedai", "kopi susu", "janji jiwa", "kopi kenangan", "tuku", "fore"]):
        return "kopi_susu"
    if any(k in combined for k in ["kopi", "coffee", "espresso", "latte", "brew", "kopitiam"]):
        return "specialty"
    return "default"


@app.post("/rekomendasi")
def get_recommendation(req: SearchRequest):
    df = load_data()

    # Normalisasi opening_hours dulu
    df['opening_hours_display'] = df['opening_hours'].apply(normalize_opening_hours)

    # === FILTER 1: Budget ===
    df = df[df['price_min'] <= req.max_budget].copy()

    # === FILTER 2: Minimum Rating ===
    if req.min_rating > 0:
        df = df[df['rating'] >= req.min_rating]

    # === FILTER 3: Fasilitas ===
    if req.wifi_required:
        df = df[df['has_wifi'] == 1]
    if req.power_outlet_required:
        df = df[df['has_power_outlet'] == 1]
    if req.open_24h_required:
        df = df[df['is_24h'] == 1]

    # === FILTER 4: Buka sekarang ===
    if req.open_now:
        df = df[df['opening_hours_display'].apply(lambda h: is_open_now(h, req.open_now))]

    # === FILTER 5: Keyword ===
    if req.keyword.strip():
        kw = req.keyword.lower()
        df = df[
            df['cafe_name'].str.lower().str.contains(kw, na=False) |
            df['menu_highlights'].str.lower().str.contains(kw, na=False)
        ]

    # === FILTER 6: Hitung jarak dari lokasi user ===
    has_location = req.user_lat is not None and req.user_lon is not None
    if has_location:
        df['distance_km'] = df.apply(
            lambda row: haversine(req.user_lat, req.user_lon,
                                  row['latitude'], row['longitude']),
            axis=1
        )
        if req.max_distance_km > 0:
            df = df[df['distance_km'] <= req.max_distance_km]
    else:
        df['distance_km'] = None

    if df.empty:
        return {
            "status": "success",
            "message": "Tidak ada kafe yang cocok. Coba perluas filter atau naikkan radius.",
            "total": 0,
            "data": []
        }

    # === SORTING ===
    if req.sort_by == "nearest" and has_location:
        df = df.sort_values(by=['distance_km', 'rating'], ascending=[True, False])
    elif req.sort_by == "price_low":
        df = df.sort_values(by=['price_min', 'rating'], ascending=[True, False])
    elif req.sort_by == "price_high":
        df = df.sort_values(by=['price_max', 'rating'], ascending=[False, False])
    else:
        df = df.sort_values(by=['rating', 'price_min'], ascending=[False, True])

    result = []
    for _, row in df.iterrows():
        # Gunakan google_maps_query langsung sebagai URL (sudah valid dari Apify)
        gmaps_url = str(row['google_maps_query']).strip()

        dist = row['distance_km']
        if dist is not None and not math.isnan(float(dist)):
            dist_val = round(float(dist), 2)
        else:
            dist_val = None

        thumbnail_cat = get_thumbnail_category(
            str(row['cafe_name']),
            str(row['menu_highlights'])
        )

        # Graceful fallback untuk kolom address
        address_val = row['address'] if 'address' in df.columns else "Jakarta Selatan"

        result.append({
            "cafe_id":              int(row['cafe_id']),
            "cafe_name":            row['cafe_name'],
            "latitude":             float(row['latitude']),
            "longitude":            float(row['longitude']),
            "address":              address_val,
            "price_min":            int(row['price_min']),
            "price_max":            int(row['price_max']),
            "rating":               float(row['rating']),
            "has_wifi":             bool(row['has_wifi']),
            "has_power_outlet":     bool(row['has_power_outlet']),
            "is_24h":               bool(row['is_24h']),
            "opening_hours":        row['opening_hours_display'],   # ← sudah dinormalisasi
            "menu_highlights":      row['menu_highlights'],
            "google_maps_url":      gmaps_url,
            "distance_km":          dist_val,
            "thumbnail_category":   thumbnail_cat,                  # ← kategori thumbnail
        })

    msg = f"Ditemukan {len(result)} kafe"
    if has_location and req.sort_by == "nearest":
        msg += " — diurutkan dari yang terdekat 📍"
    elif has_location:
        msg += " (dengan info jarak)"
    else:
        msg += " yang cocok untuk kamu! ☕"

    return {
        "status":  "success",
        "message": msg,
        "total":   len(result),
        "data":    result
    }
