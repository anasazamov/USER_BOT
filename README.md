# Telegram Taxi Userbot

Asinxron Telethon userbot: taksi buyurtmalarini topadi, filtrlaydi, va belgilangan targetga yuboradi.

## Deployment

### 1) Talablar

- Python `3.11+` (local run uchun)
- PostgreSQL
- Telegram `api_id` va `api_hash`

### 2) Environment sozlash

Majburiy:

- `TG_API_ID`
- `TG_API_HASH`

Tavsiya etiladi:

- `DATABASE_URL`
- `FORWARD_TARGET`
- `OWNER_USER_ID`
- `ADMIN_WEB_TOKEN`

Nusxa olish:

Linux/macOS:

```bash
cp .env.example .env
```

Windows PowerShell:

```powershell
Copy-Item .env.example .env
```

### 3) Local ishga tushirish

Linux/macOS:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m app.main
```

Windows PowerShell:

```powershell
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m app.main
```

Birinchi ishga tushishda Telethon session avtorizatsiyasi so'raladi (SMS/login kod).

### 4) Docker deployment

```bash
docker compose up --build -d
```

Tekshirish:

```bash
docker compose ps
```

## Foydalanish

### 1) Admin Web UI

URL:

- `http://<server-ip>:1311`
- token yoqilgan bo'lsa: `http://<server-ip>:1311?token=<ADMIN_WEB_TOKEN>`

Web UI orqali:

- Keyword qo'shish/o'chirish (`transport`, `request`, `offer`, `exclude`, `location`, `route`)
- Private invite link qo'shish/o'chirish/yoqish/o'chirish
- Public group username qo'shish/o'chirish/yoqish/o'chirish
- Runtime config o'zgartirish:
- `forward_target`, `min_text_length`
- action/reply/join limitlar
- delay parametrlar
- discovery query va discovery limitlar

### 2) Admin Telegram buyruqlari

`OWNER_USER_ID` ga tegishli akkaunt private chatdan boshqaradi:

```text
/kw list
/kw reload
/kw add <kind> <value>
/kw del <kind> <value>
```

### 3) Telegram userbot ishlash tartibi

1. Guruhlardan kelgan xabarlar olinadi.
2. Matn normalize qilinadi (emoji/shovqin belgilar tozalanadi, krill-lotin moslashtiriladi).
3. Fast filter order bo'lish ehtimolini tekshiradi.
4. Decision engine yakuniy qaror beradi:
- kontakt (`telefon` yoki `@username`) bo'lishi shart
- taksi takliflari rad etiladi
- spam/ads kategoriyalar chiqarib tashlanadi
5. Mos xabar forward qilinadi:
- original matn
- hudud hashtag
- source link (`Manba`)

### 4) Guruh topish va qo'shilish

Private:

- Xabardan `https://t.me/+...` linklar avtomatik topilib bazaga yoziladi.
- Invite manager shu linklar bo'yicha join qilishga harakat qiladi.

Public:

- `DISCOVERY_ENABLED=true` bo'lsa querylar orqali public group discovery ishlaydi.
- Topilgan guruhlar navbat bilan join qilinadi (`join_limit_day` cheklovi bilan).

## Test

```bash
pytest -q
```
