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
- `TG_BOT_TOKEN`
- `BOT_ADMIN_USER_IDS`
- `BOT_BROADCAST_SUBSCRIBERS`
- `ADMIN_WEB_TOKEN`
- `REALTIME_ONLY`
- `HISTORY_SYNC_ENABLED`
- `HISTORY_SYNC_INTERVAL_SEC`
- `HISTORY_SYNC_BATCH_SIZE`
- `PRIORITY_GROUP_LINKS`

DB eslatma:

- Agar `DATABASE_URL` ichidagi DB mavjud bo'lmasa, bot uni avtomatik yaratishga urinadi.
- Bu ishlashi uchun DB user'da `CREATEDB` huquqi bo'lishi kerak.
- Huquq bo'lmasa DB'ni oldindan qo'lda yarating.

Docker Compose ishlatayotganda host nomlari:

- `DATABASE_URL=postgresql://postgres:postgres@postgres:5432/userbot`
- `REDIS_URL=redis://redis:6379/0`

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

Docker orqali
```bash
docker run --rm -it \
  --name user_bot_login \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  user_bot
```

### 4) Docker deployment

```bash
docker compose up --build -d
```

Tekshirish:

```bash
docker compose ps
```

```bash
docker run -d \
  --name user_bot \
  --restart unless-stopped \
  --env-file .env \
  -v $(pwd)/data:/app/data \
  -p 1311:1311 \
  user_bot
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

### 3) Forward target (qayerga yuborish)

Public guruh/channel:

- `FORWARD_TARGET=@group_username`

Private guruh:

- `FORWARD_TARGET=-1001234567890` formatida bering.
- Userbot account o'sha private guruhda a'zo bo'lishi va yozish huquqiga ega bo'lishi kerak.

Private guruh `id` ni olish:

1. Guruhdan biror message linkini oling: `https://t.me/c/1234567890/55`
2. `FORWARD_TARGET=-1001234567890` qilib yozing (`c/` dan keyingi son boshiga `-100` qo'shiladi).

Bot orqali publish:

- `TG_BOT_TOKEN` berilsa buyurtmalar Telegram Bot API orqali yuboriladi.
- Bot `FORWARD_TARGET` bo'lgan guruh/channelda admin bo'lishi kerak.
- `BOT_ADMIN_USER_IDS` ichiga bot admin user ID larini yozing (masalan `12345,67890`).
- `PER_GROUP_ACTIONS_HOUR=0` yoki `GLOBAL_ACTIONS_MINUTE=0` bersangiz mos limit o'chadi (tezroq real-time rejim).

### 4) Telegram userbot ishlash tartibi

1. Guruhlardan kelgan xabarlar olinadi.
2. Matn normalize qilinadi (emoji/shovqin belgilar tozalanadi, krill-lotin moslashtiriladi).
3. Fast filter order bo'lish ehtimolini tekshiradi.
4. Decision engine yakuniy qaror beradi:
- kuchli buyurtma patternlari (`...dan ...ga`, `1 kishi/odam bor`, `kim bor`) bo'lsa qabul qiladi
- yo'nalish bo'lmasa ham qisqa `odam bor` / `kishi bor` xabarlari qabul qilinadi
- matnda `yuramiz`/`yuryamiz` bo'lsa buyurtma deb olinmaydi (taklif sifatida rad qilinadi)
- taksi takliflari rad etiladi
- spam/ads kategoriyalar chiqarib tashlanadi
5. Mos xabar forward qilinadi:
- original matn
- hudud hashtag
- `Status: Yangi`
- source link (`Manba`)
6. Agar shu bir xil buyurtma xabari qayta ishlansa (masalan edit bo'lsa):
- yangi post ochilmaydi
- oldingi e'lon `Status: Yangilandi` qilib update qilinadi
7. Bot manba (buyurtma topilgan) guruhlarga reply yozmaydi
8. `REALTIME_ONLY=true` bo'lsa:
- faqat yangi `NewMessage` xabarlar filtrlanadi
- edited/history xabarlar filtrlanmaydi
9. `REALTIME_ONLY=false` va `HISTORY_SYNC_ENABLED=true` bo'lsa:
- startupda guruhdagi eski xabarlar o'qilmaydi (latest message `last_seen` qilib baseline qilinadi)
- history sync faqat shu baseline'dan keyin kelgan yangi xabarlarni o'qiydi
- interval bo'yicha qayta scan bo'ladi
- yangi qo'shilgan guruhlar ham keyingi sync'da eski tarixsiz baseline qilinadi

### 5) Guruh topish va qo'shilish

Private:

- Xabardan `https://t.me/+...` linklar avtomatik topilib bazaga yoziladi.
- Invite manager shu linklar bo'yicha join qilishga harakat qiladi.

Public:

- `DISCOVERY_ENABLED=true` bo'lsa querylar orqali public group discovery ishlaydi.
- Topilgan guruhlar navbat bilan join qilinadi (`join_limit_day` cheklovi bilan).
- `PRIORITY_GROUP_LINKS` dagi guruhlar startupda avtomatik seed qilinadi va join navbatida birinchi o'ringa olinadi.
- Discovery query navbatida Samarqand/Toshkent/Vodiyga oid querylar oldinda ishlaydi.

### 6) Management Bot buyruqlari

`TG_BOT_TOKEN` yoqilganda bot private chat buyruqlari:

- `/start` yoki `/subscribe` -> subscriber'ni yoqadi
- `/stop` yoki `/unsubscribe` -> subscriber'ni o'chiradi
- `/help` -> yordam
- `/stats` (admin) -> publish/error statistikasi
- `/subscribers` (admin) -> subscriber soni va ro'yxati
- `/broadcast <text>` (admin) -> subscriberlarga xabar yuboradi (`BOT_BROADCAST_SUBSCRIBERS=true` bo'lsa)

## Logging

- Loglar JSON formatda chiqadi (`stdout`).
- Harakatlar bo'yicha loglar bor: message receive/filter/queue, decision, publish/publish_edit/join, history sync.
- `message_filtered` va `decision_skip` loglarda `chat_ref`, `chat_title`, `chat_username`, `raw_preview`, `normalized_preview` chiqadi.
- Dockerda ko'rish:

```bash
docker logs -f user_bot
```

## Test

```bash
pytest -q
```
