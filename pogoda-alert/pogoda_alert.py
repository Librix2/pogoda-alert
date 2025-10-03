#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, json, os, sys, ssl, urllib.parse, urllib.request

# ---------- SSL ----------
def make_ssl_context(insecure: bool):
    if insecure:
        return ssl._create_unverified_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

# ---------- API ----------
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

def geocode_city(city: str, ctx) -> dict:
    params = {"name": city, "count": 1, "language": "pl", "format": "json"}
    url = GEO_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=15, context=ctx) as resp:
        data = json.load(resp)
    results = data.get("results") or []
    if not results:
        raise SystemExit(f"Nie znaleziono lokalizacji dla: {city}")
    r = results[0]
    return {
        "name": r.get("name"),
        "country": r.get("country"),
        "latitude": r["latitude"],
        "longitude": r["longitude"],
        "timezone": r.get("timezone", "auto"),
    }

def fetch_forecast(lat: float, lon: float, tz: str, ctx) -> dict:
    params = {
        "latitude": lat,
        "longitude": lon,
        "hourly": "temperature_2m,precipitation,precipitation_probability",
        "timezone": tz,
        "forecast_days": 2,
    }
    url = FORECAST_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20, context=ctx) as resp:
        return json.load(resp)

# ---------- Pomocnicze ----------
def next24_indices(times_iso: list[str], now: dt.datetime) -> list[int]:
    parsed = [dt.datetime.fromisoformat(t) for t in times_iso]
    end = now + dt.timedelta(hours=24)
    return [i for i, t in enumerate(parsed) if now <= t <= end]

def _to_float(x, default=0.0):
    try:
        return float(x)
    except Exception:
        return float(default)

# ---------- Stan ----------
def state_path() -> str:
    base = os.path.expanduser("~")
    return os.path.join(base, ".pogoda_alert_state.json")

def load_state() -> dict:
    p = state_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    # domyślny stan – None => pierwsze uruchomienie
    return {"rain_state": None, "rain_count": 0}

def save_state(st: dict):
    with open(state_path(), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

# ---------- Telegram ----------
def send_telegram(token: str, chat_id: str, text: str, ctx):
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(api, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        _ = resp.read()

# ---------- Główna logika ----------
def main():
    ap = argparse.ArgumentParser(description="Pogoda alert: deszcz (z debounce i status na 1. uruchomieniu)")
    ap.add_argument("--miasto", required=True)
    ap.add_argument("--prog-opad", type=int, default=50, help="Próg prawd. opadu w % (alert, gdy >= próg)")
    ap.add_argument("--prog-mm", type=float, default=0.5, help="Próg sumy opadu w mm (alert, gdy >= próg)")
    ap.add_argument("--tg-token", default="", help="Token bota Telegram")
    ap.add_argument("--tg-chat", default="", help="Chat ID Telegram")
    ap.add_argument("--insecure", action="store_true", help="Wyłącz weryfikację SSL (awaryjnie)")
    args = ap.parse_args()

    ctx = make_ssl_context(args.insecure)
    now = dt.datetime.now().replace(microsecond=0)

    # Przerwa nocna 22:00–06:59 (żeby bieg 07:00 już działał)
    if now.hour >= 22 or now.hour < 7:
        print(f"{now} – przerwa nocna (22:00–06:59), skrypt kończy pracę.")
        sys.exit(0)

    # parametry "złotego środka"
    DEBOUNCE_NEED = 2       # ile kolejnych wykryć "deszcz" potrzeba, by wysłać alert
    IMMEDIATE_MM  = 2.0     # jeśli max_mm >= IMMEDIATE_MM -> wyślij natychmiast

    # Stan
    st = load_state()
    prev_rain  = st.get("rain_state", None)   # None => pierwsze uruchomienie
    rain_count = int(st.get("rain_count", 0))

    # Prognoza
    loc = geocode_city(args.miasto, ctx)
    fc = fetch_forecast(loc["latitude"], loc["longitude"], loc["timezone"], ctx)
    times = fc["hourly"]["time"]
    precip_mm   = fc["hourly"].get("precipitation", [0]*len(times))
    precip_prob = fc["hourly"].get("precipitation_probability", [0]*len(times))

    header = f"Lokalizacja: {loc['name']}, {loc['country']} — {now.strftime('%d.%m.%Y %H:%M')}"
    print(header)
    print("-" * len(header))

    # Okno 24h
    idxs = next24_indices(times, now)
    vals = []
    for i in idxs:
        p = _to_float(precip_prob[i] if i < len(precip_prob) else 0.0, 0.0)
        m = _to_float(precip_mm[i]   if i < len(precip_mm)   else 0.0, 0.0)
        vals.append((p, m))

    max_prob = max((p for p, _ in vals), default=0.0)
    max_mm   = max((m for _, m in vals), default=0.0)

    detected_now = (max_prob >= float(args.prog_opad)) or (max_mm >= float(args.prog_mm))
    print(f"Status deszczu 24h: {'będzie' if detected_now else 'brak'} "
          f"(max prawd={int(max_prob)}%, max opad={max_mm:.1f} mm, progi: {int(args.prog_opad)}%, {args.prog_mm} mm)")

    # szczegóły (krótko)
    for i in idxs[:24]:  # wypisz max 24 wiersze, żeby nie zalewać logów
        prob = _to_float(precip_prob[i] if i < len(precip_prob) else 0.0, 0.0)
        mm   = _to_float(precip_mm[i]   if i < len(precip_mm)   else 0.0, 0.0)
        print(f"  {times[i]}: opad={mm:.1f} mm, prawd={int(prob)}%")

    # Debounce i decyzja powiadomienia
    send_rain = False
    msg = None

    first_run = (prev_rain is None)

    if first_run:
        # Na pierwszym uruchomieniu – zawsze wyślij stan
        send_rain = True
        st["rain_state"] = bool(detected_now)
        st["rain_count"] = 1 if detected_now else 0
        msg = "Deszcz w prognozie w ciągu 24 godzin." if detected_now else "Brak deszczu przez najbliższe 24h."
        print("Pierwsze uruchomienie – wysyłam stan początkowy.")
    else:
        # aktualizuj licznik "deszcz widoczny"
        if detected_now:
            rain_count += 1
        else:
            rain_count = 0

        # zmiana na DESZCZ
        if (not prev_rain) and detected_now:
            if (max_mm >= IMMEDIATE_MM) or (rain_count >= DEBOUNCE_NEED):
                send_rain = True
                st["rain_state"] = True
                msg = "Deszcz w prognozie w ciągu 24 godzin."
                print("Zmiana stanu → pojawił się deszcz (warunek spełniony).")
            else:
                print("Wykryto kandydat na deszcz, ale czekam na potwierdzenie (debounce).")

        # zmiana na BRAK DESZCZU – bez debounce, żeby szybciej „otworzyć namiot”
        elif prev_rain and (not detected_now):
            send_rain = True
            st["rain_state"] = False
            msg = "Brak deszczu przez najbliższe 24h."
            print("Zmiana stanu → zniknął deszcz.")

        # brak zmiany
        else:
            print("Brak zmiany stanu – nie wysyłam powiadomienia.")
            st["rain_state"] = bool(detected_now)

        st["rain_count"] = rain_count

    # Wysyłka
    if send_rain and msg and args.tg_token and args.tg_chat:
        try:
            send_telegram(args.tg_token, args.tg_chat, f"[{loc['name']}] {msg}", ctx)
            print(f"Wysłano powiadomienie do {args.tg_chat}: {msg}")
        except Exception as e:
            print(f"Błąd wysyłki (Telegram): {e}", file=sys.stderr)

    save_state(st)
    sys.exit(0)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Błąd: {e}", file=sys.stderr)
        sys.exit(2)
