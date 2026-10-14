# Mahzen / Fedya Bot — Baştan Kurulum

## İçerik
- Discord botu (`discord.py`) ve web dashboard (FastAPI)
- SQLite kalıcı kayıtlar
- OpenRouter AI ve dashboard'dan ayarlanabilir kişilik
- Etkinlik oluşturma, planlı hatırlatma (varsayılan 24 saat / 6 saat / 2 saat), anket butonları
- Katılım cevapları, yönetici onayıyla puan
- Oyuncu profili ve liderlik tablosu
- Oyun bağlantıları/kodları, doğal dille link/kod yanıtları
- Üye tanıtımları, başarımlar, yönetim günlüğü ve hata kayıtları

## Kurulum
1. Python 3.11+ yükle.
2. `.env.example` dosyasını `.env` olarak kopyala ve `DISCORD_TOKEN`, `OWNER_DISCORD_ID`, `DASHBOARD_PASSWORD` alanlarını doldur.
3. Discord Developer Portal'da **Message Content Intent** ve **Server Members Intent** aç.
4. Botu sunucuya `bot` + `applications.commands` kapsamlarıyla davet et. Kanal görüntüleme, mesaj gönderme, embed ve slash komut izinlerini ver.
5. Terminalde:
   `pip install -r requirements.txt`
   `python -m app.main`
6. Dashboard: http://localhost:8000

## Render
`render.yaml` ile servis oluştur; ortam değişkenlerini ekle. Kalıcı disk `/data` bağlayıp `DATABASE_PATH=/data/mahzen.db` ayarla. Aynı anda tek instance çalıştır; birden fazla instance hatırlatmaları iki kere gönderebilir.

## Güvenlik
`.env` dosyanı GitHub'a yükleme. Dashboard parolasını güçlü yap. Üretimde HTTPS kullan. Bu sürüm tek yönetici parolalı başlangıç panelidir; Discord OAuth2 henüz yok.
