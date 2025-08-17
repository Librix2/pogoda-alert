#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, json, os, sys, ssl, urllib.parse, urllib.request

def make_ssl_context(insecure: bool):
    if insecure:
        return ssl._create_unverified_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()

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
    return {"name": r.get("name"), "country": r.get("country"),
            "latitude": r["latitude"], "longitude": r["longitude"],
            "timezone": r.get("timezone", "auto")}

def fetch_forecast(lat: float, lon: float, tz: str, ctx) -> dict:
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,precipitation,precipitation_probability",
        "timezone": tz, "forecast_days": 2,
    }
    url = FORECAST_URL + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=20, context=ctx) as resp:
        return json.load(resp)

def next24_indices(times_iso: list[str], now: dt.datetime) -> list[int]:
    parsed = [dt.datetime.fromisoformat(t) for t in times_iso]
    return [i for i, t in enumerate(parsed) if now <= t <= now + dt.timedelta(hours=24)]

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
    return {"rain_state": None}

def save_state(st: dict):
    with open(state_path(), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

def send_telegram(token: str, chat_id: str, text: str, ctx):
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    params = {"chat_id": chat_id, "text": text, "disable_web_page_preview": "true"}
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(api, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        _ = resp.read()

def main():
    ap = argparse.ArgumentParser(description="Pogoda alert: tylko deszcz")
    ap.add_argument("--miasto", required=True)
    ap.add_argument("--prog-opad", type=int, default=50)
    ap.add_argument("--tg-token", default="")
    ap.add_argument("--tg-chat", default="")
    ap.add_argument("--insecure", action="store_true")
    args = ap.parse_args()

    ctx = make_ssl_context(args.insecure)
    now = dt.datetime.now().replace(microsecond=0)

    if now.hour >= 22 or now.hour < 7:
        print(f"{now} – przerwa nocna (22:00–07:00), skrypt kończy pracę.")
        sys.exit(0)

    st = load_state()
    loc = geocode_city(args.miasto, ctx)
    fc = fetch_forecast(loc["latitude"], loc["longitude"], loc["timezone"], ctx)
    times = fc["hourly"]["time"]
    precip_mm = fc["hourly"].get("precipitation", [0]*len(times))
    precip_prob = fc["hourly"].get("precipitation_probability", [0]*len(times))

    next24 = next24_indices(times, now)
    has_rain = any(((precip_prob[i] or 0) >= args.prog_opad) or ((precip_mm[i] or 0) > 0) for i in next24)

    prev_rain = st.get("rain_state", None)
    send_rain, rain_msg = False, None
    if prev_rain is None:
        st["rain_state"] = has_rain
    elif prev_rain != has_rain:
        send_rain = True
        st["rain_state"] = has_rain
        rain_msg = "Deszcz w prognozie w ciągu 24 godzin." if has_rain else "Brak deszczu przez najbliższe 24h."

    if send_rain and rain_msg and args.tg_token and args.tg_chat:
        try:
            send_telegram(args.tg_token, args.tg_chat, f"[{loc['name']}] {rain_msg}", ctx)
        except Exception as e:
            print(f"Błąd wysyłki (deszcz): {e}", file=sys.stderr)

    save_state(st)
    sys.exit(0)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Błąd: {e}", file=sys.stderr)
        sys.exit(2)
