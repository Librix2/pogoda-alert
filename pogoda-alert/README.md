# 🌦️ Pogoda Alert — deszcz (Telegram)

Skrypt wysyła powiadomienia na Telegram **tylko o deszczu** i **tylko przy zmianie stanu**:
- „pojawi się deszcz w ciągu 24h” ↔ „brak deszczu przez najbliższe 24h”.
Nie sprawdza temperatury nocy.

## GitHub Actions
- Uruchomienia: 07:00, 10:00, 13:00, 19:00, 21:00 czasu PL (w pliku użyto godzin UTC odpowiadających CEST).
- Ustaw sekrety repo: `TG_TOKEN` (token bota), `TG_CHAT` (chat_id).
