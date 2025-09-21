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

# ---------- Pomocnicze ----------
def next24_indices(times_iso: list[str], now: dt.datetime) -> list[int]:
    parsed = [dt.datetime.fromisoformat(t) for t in times_iso]
    return [i for i, t in enumerate(parsed) if now <= t <= now + dt.timedelta(hours=24)]

# ---------- Stan / pliki ----------
def state_path() -> str:
    base = os.path.expanduser("~")
    return os.path.join(base, ".pogoda_alert_state.json")

DEFAULT_STATE = {
    "rain_state": None,             # True/False lub None (brak)
    "subscribers": [],              # lista chat_id (int)
    "last_update_id": None,         # ostatnio przetworzony update_id z getUpdates
    "last_status_text": None,       # ostatni wysłany tekst (np. "[Szczecin] Brak deszczu...")
}

def load_state() -> dict:
    p = state_path()
    if os.path.exists(p):
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            for k, v in DEFAULT_STATE.items():
                if k not in data:
                    data[k] = v
            return data
        except Exception:
            pass
    return DEFAULT_STATE.copy()

def save_state(st: dict):
    with open(state_path(), "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)

# ---------- Telegram ----------
def send_telegram(token: str, chat_id: str | int, text: str, ctx):
    api = f"https://api.telegram.org/bot{token}/sendMessage"
    params = {"chat_id": str(chat_id), "text": text, "disable_web_page_preview": "true"}
    data = urllib.parse.urlencode(params).encode("utf-8")
    req = urllib.request.Request(api, data=data, method="POST")
    with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
        _ = resp.read()

def get_updates(token: str, offset: int | None, ctx) -> dict:
    api = f"https://api.telegram.org/bot{token}/getUpdates"
    params = {}
    if offset is not None:
        params["offset"] = offset
    url = api + ("?" + urllib.parse.urlencode(params) if params else "")
    with urllib.request.urlopen(url, timeout=15, context=ctx) as resp:
        return json.load(resp)

def unique_add(lst: list[int], item: int) -> bool:
    if item not in lst:
        lst.append(item)
        return True
    return False

# ---------- Główna logika ----------
def main():
    ap = argparse.ArgumentParser(description="Pogoda alert: deszcz + auto-subskrypcje /start i /stop")
    ap.add_argument("--miasto", required=True)
    ap.add_argument("--prog-opad", type=int, default=50, help="Próg prawd. opadu w % (alert, gdy >= próg)")
    ap.add_argument("--tg-token", default="", help="Token bota Telegram")
    ap.add_argument("--tg-chat", default="", help="Początkowe chat ID (po przecinku, opcjonalnie)")
    ap.add_argument("--insecure", action="store_true", help="Wyłącz weryfikację SSL (awaryjnie)")
    args = ap.parse_args()

    ctx = make_ssl_context(args.insecure)
    now = dt.datetime.now().replace(microsecond=0)

    # Przerwa nocna 22:00–07:00 (również nie odpowiadamy na /start /stop)
    if now.hour >= 22 or now.hour < 7:
        print(f"{now} – przerwa nocna (22:00–07:00), skrypt kończy pracę.")
        sys.exit(0)

    if not args.tg_token:
        print("Brak tokenu Telegram (--tg-token). Kończę.", file=sys.stderr)
        sys.exit(2)

    st = load_state()

    # 0) Zasiej subskrybentów z parametru --tg-chat (jeśli podano)
    seed_ids = []
    if args.tg_chat:
        for part in args.tg_chat.split(","):
            s = part.strip()
            if s:
                try:
                    seed_ids.append(int(s))
                except ValueError:
                    print(f"Ostrzeżenie: pominięto nieprawidłowy chat_id: {s}", file=sys.stderr)
    added_seed = 0
    for cid in seed_ids:
        if unique_add(st["subscribers"], cid):
            added_seed += 1
    if added_seed:
        print(f"Dodano {added_seed} subskrybent(ów) z --tg-chat.")

    # 1) Obsłuż /start i /stop: pobierz getUpdates od ostatniego update_id
    try:
        offset = st["last_update_id"] + 1 if st["last_update_id"] is not None else None
    except Exception:
        offset = None
    try:
        upd = get_updates(args.tg_token, offset, ctx)
    except Exception as e:
        print(f"Ostrzeżenie: getUpdates nie powiodło się: {e}", file=sys.stderr)
        upd = {"ok": False, "result": []}

    new_subs: list[int] = []
    removed_subs: list[int] = []
    max_update_id = st["last_update_id"]

    for item in upd.get("result", []):
        uid = item.get("update_id")
        if max_update_id is None or (isinstance(uid, int) and uid > max_update_id):
            max_update_id = uid
        msg = item.get("message") or item.get("edited_message") or {}
        chat = msg.get("chat") or {}
        text = (msg.get("text") or "").strip()
        chat_id = chat.get("id")

        if not isinstance(chat_id, int):
            continue

        if text == "/start":
            if unique_add(st["subscribers"], chat_id):
                new_subs.append(chat_id)

        elif text == "/stop":
            if chat_id in st["subscribers"]:
                st["subscribers"].remove(chat_id)
                removed_subs.append(chat_id)

    if max_update_id is not None:
        st["last_update_id"] = max_update_id

    # Wyślij potwierdzenia
    for cid in removed_subs:
        try:
            send_telegram(args.tg_token, cid, "Zostałeś wypisany z alertów.", ctx)
            print(f"Wypisano subskrybenta {cid}")
        except Exception as e:
            print(f"Błąd wysyłki (stop) do {cid}: {e}", file=sys.stderr)

    if new_subs and st.get("last_status_text"):
        for cid in new_subs:
            try:
                send_telegram(args.tg_token, cid, st["last_status_text"], ctx)
                print(f"Wysłano ostatni wpis do nowego subskrybenta: {cid}")
            except Exception as e:
                print(f"Błąd wysyłki do {cid}: {e}", file=sys.stderr)

    # 2) Zbierz prognozę i policz status (24h)
    loc = geocode_city(args.miasto, ctx)
    fc = fetch_forecast(loc["latitude"], loc["longitude"], loc["timezone"], ctx)
    times = fc["hourly"]["time"]
    precip_mm = fc["hourly"].get("precipitation", [0]*len(times))
    precip_prob = fc["hourly"].get("precipitation_probability", [0]*len(times))

    header = f"Lokalizacja: {loc['name']}, {loc['country']} — {now.strftime('%d.%m.%Y %H:%M')}"
    print(header)
    print("-" * len(header))

    def _to_float(x, default=0.0):
    try:
        # czasem przychodzi None lub string – zamień bezpiecznie na float
        return float(x)
    except Exception:
        return float(default)

next24 = next24_indices(times, now)

# zbierz wartości z 24h okna
vals = []
for i in next24:
    p = _to_float(precip_prob[i] if i < len(precip_prob) else 0)
    m = _to_float(precip_mm[i]   if i < len(precip_mm)   else 0)
    vals.append((i, p, m))

max_prob = max((p for _, p, _ in vals), default=0.0)
max_mm   = max((m for _, _, m in vals), default=0.0)

# jednoznaczna decyzja: będzie deszcz, jeśli choć jeden z maksimów przekracza próg
has_rain = (max_prob >= float(args.prog_opad)) or (max_mm > 0.0)

print(f"Status deszczu 24h: {'będzie' if has_rain else 'brak'} "
      f"(max prawd={max_prob:.0f}%, max opad={max_mm:.1f} mm)")

# >>> DEBUG: pokaż szczegóły prognozy na 24h <<<
for i, p, m in vals:
    print(f"  {times[i]}: opad={m:.1f} mm, prawd={p:.0f}%")


    prev_rain = st.get("rain_state", None)
    send_rain, rain_msg = False, None

    if prev_rain is None:
        st["rain_state"] = has_rain
        core = "Deszcz w prognozie w ciągu 24 godzin." if has_rain else "Brak deszczu przez najbliższe 24h."
        rain_msg = f"[{loc['name']}] {core}"
        print("Pierwsze uruchomienie – wysyłam stan początkowy.")
        send_rain = True
    elif prev_rain != has_rain:
        st["rain_state"] = has_rain
        core = "Deszcz w prognozie w ciągu 24 godzin." if has_rain else "Brak deszczu przez najbliższe 24h."
        rain_msg = f"[{loc['name']}] {core}"
        print("Zmiana stanu →", "pojawił się deszcz" if has_rain else "zniknął deszcz")
        send_rain = True
    else:
        print("Brak zmiany stanu – nie wysyłam powiadomienia.")

    # 3) Wysyłka do wszystkich subskrybentów (oraz seed_ids)
    if send_rain and rain_msg:
        st["last_status_text"] = rain_msg
        all_ids = list({*(st['subscribers']), *seed_ids})
        if not all_ids:
            print("Brak subskrybentów – pomijam wysyłkę.")
        else:
            for cid in all_ids:
                try:
                    send_telegram(args.tg_token, cid, rain_msg, ctx)
                    print(f"Wysłano powiadomienie do {cid}: {rain_msg}")
                except Exception as e:
                    print(f"Błąd wysyłki do {cid}: {e}", file=sys.stderr)

    # 4) Zapis stanu
    save_state(st)
    sys.exit(0)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Błąd: {e}", file=sys.stderr)
        sys.exit(2)
