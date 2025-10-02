#!/usr/bin/env python3
from __future__ import annotations
import argparse, datetime as dt, json, os, sys, ssl, urllib.parse, urllib.request

# ---------- SSL ----------
def make_ssl_context(insecure: bool):
    """Tworzy kontekst SSL. --insecure wyłącza weryfikację (awaryjnie)."""
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
    return [i for i, t in enumerate(parsed) if now <= t <= now + dt.timedelta(hours=24)]

def _to_float(x, default: float = 0.0) -> float:
    """Bezpieczna konwersja na float (radzi sobie z None/str)."""
    try:
        return float(x)
    except Exception:
        return float(default)

# ---------- Stan / pliki ----------
def state_path() -> str:
    base = os.path.expanduser("~")
    return os.path.join(base, ".pogoda_alert_state.json")

DEFAULT_STATE = {
    "rain_state": None,       # True/False lub None (brak)
    "subscribers": [],        # lista chat_id (int)
    "last_update_id": None,   # ostatnio przetworzony update_id z getUpdates
    "last_status_text": None, # ostatni wysłany tekst (np. "[Szczecin] Brak deszczu...")
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
    ap = argparse.ArgumentParser(description="Pogoda alert: próg % lub próg mm + subskrypcje (/start, /stop)")
    ap.add_argument("--miasto", required=True)
    ap.add_argument("--prog-opad", type=int, default=50, help="Próg prawd. opadu w % (alert, gdy >= próg)")
    ap.add_argument("--prog-opad-mm", type=float, default=0.3, help="Próg godzinowego opadu w mm (alert, gdy >= próg)")
    ap.add_argument("--tg-token", default="", help="Token bota Telegram")
    ap.add_argument("--tg-chat", default="", help="Początkowe chat ID (po przecinku, opcjonalnie)")
    ap.add_argument("--insecure", action="store_true", help="Wyłącz weryfikację SSL (awaryjnie)")
    args = ap.parse_args()

    ctx = make_ssl_context(args.insecure)
    now = dt.datetime.now().replace(microsecond=0)

    # Przerwa nocna do 06:59 (run o 07:00 ma działać)
    if (now.hour >= 22) or (now.hour < 6) or (now.hour == 6 and now.minute <= 59):
        print(f"{now} – przerwa nocna (22:00–06:59), skrypt kończy pracę.")
        sys.exit(0)

    if not args.tg_token:
        print("Brak tokenu Telegram (--tg-token). Kończę.", file=sys.stderr)
        sys.exit(2)

    st = load_state()

    # 0) Zasiej subskrybentów z parametru --tg-chat (jeśli podano)
    seed_ids: list[int] = []
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

    # 1) Obsłuż /start i /stop
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
        text_l = text.lower()
        chat_id = chat.get("id")

        if not isinstance(chat_id, int):
            continue

        if text_l == "/start":
            if unique_add(st["subscribers"], chat_id):
                new_subs.append(chat_id)
        elif text_l == "/stop":
            if chat_id in st["subscribers"]:
                st["subscribers"].remove(chat_id)
                removed_subs.append(chat_id)

    if max_update_id is not None:
        st["last_update_id"] = max_update_id

    # Potwierdzenia /stop
    for cid in removed_subs:
        try:
            send_telegram(args.tg_token, cid, "Zostałeś wypisany z alertów.", ctx)
            print(f"Wypisano subskrybenta {cid}")
        except Exception as e:
            print(f"Błąd wysyłki (stop) do {cid}: {e}", file=sys.stderr)

    # 2) Prognoza i decyzja na 24h
    loc = geocode_city(args.miasto, ctx)
    fc = fetch_forecast(loc["latitude"], loc["longitude"], loc["timezone"], ctx)
    times = fc["hourly"]["time"]
    precip_mm = [_to_float(x, 0.0) for x in fc["hourly"].get("precipitation", [0]*len(times))]
    precip_prob = [int(_to_float(x, 0.0)) for x in fc["hourly"].get("precipitation_probability", [0]*len(times))]

    header = f"Lokalizacja: {loc['name']}, {loc['country']} — {now.strftime('%d.%m.%Y %H:%M')}"
    print(header)
    print("-" * len(header))

    idx24 = next24_indices(times, now)
    vals = []
    for i in idx24:
        p = precip_prob[i] if i < len(precip_prob) else 0
        m = precip_mm[i] if i < len(precip_mm) else 0.0
        vals.append((i, p, m))

    max_prob = max((p for _, p, _ in vals), default=0)
    max_mm   = max((m for _, _, m in vals), default=0.0)

    # ZŁOTY ŚRODEK: reaguj gdy (prawdopodobieństwo ≥ próg) LUB (opad ≥ próg_mm)
    prob_trigger = any(precip_prob[i] >= args.prog_opad for i in idx24)
    mm_trigger   = any(precip_mm[i]   >= args.prog_opad_mm for i in idx24)
    has_rain = prob_trigger or mm_trigger

    print(
        f"Status deszczu 24h: {'będzie' if has_rain else 'brak'} "
        f"(max prawd={max_prob}%, max opad={max_mm:.1f} mm, próg%={args.prog_opad}, próg_mm={args.prog_opad_mm:g})"
    )

    # DEBUG – szczegóły godzinowe
    for i, p, m in [(i, precip_prob[i], precip_mm[i]) for i in idx24]:
        print(f"  {times[i]}: opad={m:.1f} mm, prawd={p}%")

    # Tekst bieżącego statusu (dla nowych /start i dla alertów)
    core = "Deszcz w prognozie w ciągu 24 godzin." if has_rain else "Brak deszczu przez najbliższe 24h."
    current_status_text = f"[{loc['name']}] {core}"

    # Przywitaj świeżych /start bieżącym stanem
    if new_subs:
        for cid in new_subs:
            try:
                send_telegram(args.tg_token, cid, current_status_text, ctx)
                print(f"Wysłano bieżący status do nowego subskrybenta: {cid}")
            except Exception as e:
                print(f"Błąd wysyłki (welcome) do {cid}: {e}", file=sys.stderr)

    # Aktualizuj ostatni wpis
    st["last_status_text"] = current_status_text

    # Zmiana stanu / pierwsze uruchomienie
    prev_rain = st.get("rain_state", None)
    send_rain, rain_msg = False, None

    if prev_rain is None:
        st["rain_state"] = has_rain
        rain_msg = current_status_text
        print("Pierwsze uruchomienie – wysyłam stan początkowy.")
        send_rain = True
    elif prev_rain != has_rain:
        st["rain_state"] = has_rain
        rain_msg = current_status_text
        print("Zmiana stanu →", "pojawił się deszcz" if has_rain else "zniknął deszcz")
        send_rain = True
    else:
        print("Brak zmiany stanu – nie wysyłam powiadomienia.")

    # Wysyłka do wszystkich subskrybentów (plus seed_ids)
    if send_rain and rain_msg:
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

    # (opcjonalnie) pokaż listę subskrybentów
    print("Lista subskrybentów zapisanych w stanie:")
    for cid in st["subscribers"]:
        print(" -", cid)

    # Zapis stanu
    save_state(st)
    sys.exit(0)

if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(f"Błąd: {e}", file=sys.stderr)
        sys.exit(2)
