# Telegram Taxi Userbot

## Run locally

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python -m app.main
```

`DISCOVERY_ENABLED=true` bo'lsa bot ochiq taksi guruhlarini qidiradi, bazaga yozadi va kunlik join limit ichida avtomatik qo'shiladi.
Topilgan buyurtma target guruhga matn ko'rinishida yuboriladi: buyurtma matni + manba havolasi + hudud hashtag.
Private invite linklar (`https://t.me/+...`) xabarlardan avtomatik topilib bazaga saqlanadi.

## Keyword Management

- Web admin: `http://<server-ip>:8080` (`ADMIN_WEB_*` env bilan boshqariladi).
- Sahifa mobile-friendly va Telegram in-app browser ichida ham ochiladi.
- Keywordlar endi DB orqali boshqariladi (static emas).
- Telegram app ichidan ham boshqarish mumkin (owner private chat):
  - `/kw list`
  - `/kw reload`
  - `/kw add <kind> <value>`
  - `/kw del <kind> <value>`

## Run with Docker

```bash
docker compose up --build -d
```

## Tests

```bash
pytest -q
```
