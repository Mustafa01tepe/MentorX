# ExamGuard Railway Kurulum Kılavuzu

Bu kılavuz ExamGuard backend'ini Railway üzerinde çalıştırmak, durum
verilerini Railway PostgreSQL'de saklamak ve öğrenci istemcilerini bu
backend'e bağlamak içindir.

## 1. Hedef Mimari

```text
Öğretmen tarayıcısı ─┐
Chrome eklentisi ────┼── HTTPS / WebSocket ── Railway Backend
Masaüstü ajanı ──────┘                              │
                                                   └── Railway PostgreSQL
```

PostgreSQL bağlantısı yalnızca backend tarafından kullanılır. Öğrenci
bilgisayarına PostgreSQL, SQLite veya başka bir veritabanı kurulmaz.

## 2. Gerekenler

- GitHub hesabı
- Railway hesabı
- Bu projenin GitHub deposu
- İsteğe bağlı Groq API anahtarı
- Öğretmen paneli için güçlü bir yönetici tokenı

Railway'e göndereceğiniz depo içinde şu klasör bulunmalıdır:

```text
mentorx-code/apps/examguard/backend
```

## 3. Kodu GitHub'a Gönderme

Proje henüz GitHub'da değilse depo kökünde:

```powershell
git add mentorx-code
git commit -m "Prepare ExamGuard for Railway PostgreSQL"
git branch -M main
git remote add origin https://github.com/KULLANICI/DEPO.git
git push -u origin main
```

Depo zaten GitHub'a bağlıysa yalnızca değişiklikleri commit edip push edin.
Gerçek `.env` dosyasını veya API anahtarlarını GitHub'a göndermeyin.

## 4. Railway Projesi Oluşturma

1. [Railway](https://railway.com/) hesabınıza giriş yapın.
2. `New Project` seçeneğine basın.
3. `Deploy from GitHub repo` seçeneğini seçin.
4. MentorX GitHub deposunu seçin.
5. Oluşan uygulama servisini açın.
6. `Settings` bölümünde `Root Directory` değerini şu şekilde ayarlayın:

   ```text
   /mentorx-code/apps/examguard/backend
   ```

7. Build ve deploy işlemini yeniden başlatın.

Backend klasöründeki `railway.toml` şu başlatma komutunu kullanır:

```text
gunicorn -w 1 --threads 50 --bind 0.0.0.0:$PORT server:app
```

Tek worker kullanılması önemlidir. ExamGuard'ın Socket.IO ve bellek içi canlı
durum yapısı şu an birden fazla worker için tasarlanmamıştır.

## 5. PostgreSQL Ekleme

1. Railway proje görünümünde `+ New` düğmesine basın.
2. `Database` seçeneğini açın.
3. `Add PostgreSQL` seçeneğini seçin.
4. PostgreSQL servisinin hazır olmasını bekleyin.
5. ExamGuard backend servisini açın.
6. `Variables` bölümüne gidin.
7. `New Variable` ile şu değişkeni ekleyin:

   ```text
   DATABASE_URL=${{Postgres.DATABASE_URL}}
   ```

PostgreSQL servisinizin adı `Postgres` değilse süslü parantez içindeki adı
Railway'de görünen servis adıyla değiştirin.

`DATABASE_URL` değerini elle `monitoragent.railway.internal` gibi uygulama
servisinin adresiyle oluşturmayın. Değişken mutlaka PostgreSQL servisinin
`DATABASE_URL` değişkenine referans vermelidir.

Backend ilk açılışta `app_state` tablosunu otomatik oluşturur. Manuel SQL
çalıştırmanız gerekmez.

## 6. Backend Değişkenlerini Tanımlama

Backend servisinin `Variables` bölümüne aşağıdaki değişkenleri ekleyin:

```text
DATABASE_URL=${{Postgres.DATABASE_URL}}
DATABASE_CONNECT_ATTEMPTS=8
DATABASE_RETRY_DELAY_SECONDS=2
SECRET_KEY=GUCLU_RASTGELE_DEGER
ADMIN_TOKEN=GUCLU_OGRETMEN_TOKENI
GROQ_API_KEY=GROQ_ANAHTARINIZ
GROQ_MODEL=meta-llama/llama-4-scout-17b-16e-instruct
MAX_SCREENSHOT_BYTES=5242880
MAX_REQUEST_BYTES=8388608
MAX_CONCURRENT_ANALYSES=3
RAILPACK_PYTHON_VERSION=3.12
```

Railway `PORT` değişkenini kendisi sağlar. Elle tanımlamayın.

`RAILPACK_PYTHON_VERSION=3.12`, Python bağımlılıklarının üretim ve yerel
ortamda aynı ana sürümle çalışmasını sağlar.

PowerShell ile güçlü değerler üretmek için:

```powershell
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Komutu iki kez çalıştırın:

- İlk sonucu `SECRET_KEY` olarak kullanın.
- İkinci sonucu `ADMIN_TOKEN` olarak kullanın.

`ADMIN_TOKEN` öğretmen yetkisidir. Öğrencilerle paylaşmayın.

`GROQ_API_KEY` boş bırakılırsa ekran görüntüsü alınabilir ancak görsel model
analizi `HATA` durumuna düşer ve manuel inceleme gerekir.

## 7. Public Backend Adresi Oluşturma

1. Railway'de backend servisini açın.
2. `Settings` bölümüne girin.
3. `Networking` veya `Public Networking` alanını bulun.
4. `Generate Domain` düğmesine basın.
5. Oluşan HTTPS adresini kaydedin.

Örnek:

```text
https://examguard-production.up.railway.app
```

Bu adresi tarayıcıda açın. Öğretmen paneli görünmelidir.

## 8. Deploy Loglarını Kontrol Etme

Backend servisinin `Deployments` veya `Logs` bölümünde şunları arayın:

```text
[ExamGuard] Durum deposu: PostgreSQL
```

Şunu görüyorsanız PostgreSQL bağlantısı kullanılmıyordur:

```text
[ExamGuard] Durum deposu: SQLite (...)
```

Bu durumda `DATABASE_URL` değişkeninin backend servisine bağlı olduğunu
kontrol edin ve servisi yeniden deploy edin.

## 9. Backend Sağlık Kontrolü

Railway alan adınızın sonuna `/state` ekleyin:

```text
https://examguard-production.up.railway.app/state
```

Başlangıçta buna benzer bir JSON yanıtı gelmelidir:

```json
{
  "active": false,
  "allowed_urls": [],
  "duration": 90,
  "mode": "web",
  "started_at": null
}
```

`exam_code` güvenlik nedeniyle bu yanıtta gösterilmez.

Servis yapılandırmasını anahtarları göstermeden kontrol etmek için `/health`
adresini açabilirsiniz:

```text
https://examguard-production.up.railway.app/health
```

`database` değeri `postgresql`, `adminTokenConfigured` ve
`visionConfigured` değerleri `true` olmalıdır.

## 10. Masaüstü Ajanını Railway'e Bağlama

Şu dosyayı açın:

```text
mentorx-code/apps/examguard/desktop/desktop_agent.py
```

Bu satırı:

```python
BACKEND_URL = 'http://localhost:5000'
```

Railway adresinizle değiştirin:

```python
BACKEND_URL = 'https://examguard-production.up.railway.app'
```

Adresin sonunda `/` kullanmayın.

Öğrenci bilgisayarında yalnızca masaüstü ajanı bağımlılıkları kurulur:

```powershell
cd mentorx-code\apps\examguard\desktop
python -m pip install -r requirements.txt
python desktop_agent.py
```

Öğrenci bilgisayarı PostgreSQL'e bağlanmaz. Yalnızca Railway backend'e HTTPS
ve WebSocket istekleri gönderir.

## 11. Chrome Eklentisini Railway'e Bağlama

Railway adresi iki dosyada değiştirilmelidir.

### `background.js`

Şu dosyayı açın:

```text
mentorx-code/apps/examguard/extension/background.js
```

Şunu:

```javascript
const BACKEND_URL = 'http://localhost:5000';
```

şuna dönüştürün:

```javascript
const BACKEND_URL = 'https://examguard-production.up.railway.app';
```

### `popup.js`

Şu dosyayı açın:

```text
mentorx-code/apps/examguard/extension/popup.js
```

Aynı `BACKEND_URL` değişikliğini burada da yapın.

`manifest.json` zaten `<all_urls>` host iznine sahip olduğu için Railway alan
adı için ayrıca izin eklemek zorunlu değildir.

## 12. Chrome Eklentisini Yükleme

Her öğrenci bilgisayarında:

1. Chrome'da `chrome://extensions` adresini açın.
2. Sağ üstten `Geliştirici modu` seçeneğini açın.
3. `Paketlenmemiş öğe yükle` düğmesine basın.
4. Şu klasörü seçin:

   ```text
   mentorx-code/apps/examguard/extension
   ```

5. ExamGuard eklentisini araç çubuğuna sabitleyin.

Eklenti kodunda daha sonra değişiklik yaparsanız `chrome://extensions`
sayfasındaki ExamGuard kartından `Yeniden yükle` düğmesine basın.

## 13. Öğretmen Panelini Açma

1. Railway backend adresini tarayıcıda açın.
2. Panel açılırken istenen öğretmen tokenı alanına Railway'deki
   `ADMIN_TOKEN` değerini girin.
3. Token tarayıcının `localStorage` alanında saklanır.
4. Ortak bilgisayarda işiniz bittiğinde tarayıcı site verilerini temizleyin.

## 14. İlk Uçtan Uca Test

1. Öğretmen panelini açın.
2. Mod olarak önce `Web` seçin.
3. Süreyi kısa bir test değeri, örneğin 5 dakika, yapın.
4. Güçlü ve geçici bir sınav kodu belirleyin.
5. Moodle sınav adresini izinli URL olarak ekleyin.
6. `Sınavı Başlat` düğmesine basın.
7. Öğrenci bilgisayarında masaüstü ajanını çalıştırın.
8. Chrome eklentisini açın.
9. Ad, öğrenci numarası ve sınav koduyla giriş yapın.
10. Öğretmen panelinde öğrencinin aktif göründüğünü doğrulayın.
11. İzin verilmeyen bir sekmeye geçerek alarm akışını test edin.
12. Kodlama modunda periyodik masaüstü görüntüsünün geldiğini doğrulayın.
13. Sınavı panelden durdurun.
14. Öğrenci oturumunun kapandığını doğrulayın.

## 15. PostgreSQL Kalıcılık Testi

1. Kısa bir sınav başlatın.
2. Bir öğrenciyi sisteme bağlayın.
3. Railway backend servisini `Restart` ile yeniden başlatın.
4. Loglarda tekrar şu satırı doğrulayın:

   ```text
   [ExamGuard] Durum deposu: PostgreSQL
   ```

5. `/state` adresini açın.
6. Sınav durumunun yeniden yüklendiğini doğrulayın.

Canlı Socket.IO bağlantıları restart sırasında kopar; istemciler otomatik
yeniden bağlanır. Kalıcı sınav ve oturum verileri PostgreSQL'den geri yüklenir.

## 16. Ekran Görüntüsü Saklama Notu

PostgreSQL şu anda şunları saklar:

- Sınav durumu
- Sınav modu, süre ve izinli URL'ler
- Öğrenci kayıtları
- Öğrenci oturum tokenları

Ekran görüntüsü dosyaları PostgreSQL'e yazılmaz. Backend'in yerel dosya
sistemine yazılır ve Railway deploy/restart sonrasında kaybolabilir.

Kalıcı ekran görüntüsü arşivi gerekiyorsa sonraki aşamada Railway Bucket,
S3, Cloudflare R2 veya benzeri bir nesne depolama sistemi bağlanmalıdır.

## 17. Öğrenci Cihazındaki Minimum Bileşenler

Web sınavı için:

- Chrome
- ExamGuard Chrome eklentisi
- ExamGuard masaüstü ajanı

Kodlama sınavı için:

- Yukarıdakiler
- Kullanılacak IDE

Öğrenci cihazında bulunmayacaklar:

- PostgreSQL
- SQLite servisi
- Flask backend
- Groq anahtarı
- Öğretmen `ADMIN_TOKEN` değeri

## 18. Sık Karşılaşılan Sorunlar

### Panel açılmıyor

- Railway deploy loglarını kontrol edin.
- Root Directory değerinin doğru olduğundan emin olun.
- Public domain oluşturulduğunu kontrol edin.

### Loglarda SQLite yazıyor

- `DATABASE_URL` backend servisine eklenmemiştir.
- PostgreSQL servis referansının adı yanlış olabilir.
- Değişkeni düzelttikten sonra backend'i yeniden deploy edin.

### Öğrenci backend'e bağlanamıyor

- İstemci dosyalarında hâlâ `http://localhost:5000` kalmış olabilir.
- Railway adresinin `https://` ile başladığını kontrol edin.
- Adresin sonunda `/` kullanmayın.
- Eklentiyi değişiklikten sonra yeniden yükleyin.

### Öğretmen tokenı geçersiz

- Panelde girilen değer ile Railway `ADMIN_TOKEN` aynı olmalıdır.
- Tarayıcının eski tokenını temizleyip paneli yeniden açın.

Tarayıcı konsolunda:

```javascript
localStorage.removeItem('examguardAdminToken')
```

### Görsel analiz çalışmıyor

- `GROQ_API_KEY` değişkenini kontrol edin.
- Backend loglarında Groq hata mesajını inceleyin.
- Kullanılan görsel modelin Groq hesabınızda erişilebilir olduğunu doğrulayın.

### Deploy sonrası eski ekran görüntüleri kayboldu

Bu mevcut tasarımda beklenen davranıştır. Kalıcı dosya saklama için nesne
depolama servisi bağlanmalıdır.

## 19. Güvenlik Kontrol Listesi

- `SECRET_KEY` güçlü ve rastgele.
- `ADMIN_TOKEN` güçlü, gizli ve öğrencilere verilmedi.
- `.env` GitHub'a gönderilmedi.
- Railway adresi `https://` kullanıyor.
- PostgreSQL public bağlantı bilgileri öğrenci cihazlarına yazılmadı.
- Groq anahtarı yalnızca Railway backend değişkenlerinde bulunuyor.
- Sınav kodu her sınav için yenileniyor.
- Test bittikten sonra gerçek öğrenci verileri ve ekran görüntüsü politikası
  kurumun KVKK süreçlerine göre yönetiliyor.

## 20. Güncelleme Akışı

Kod değişikliğinden sonra:

1. Değişiklikleri GitHub'a push edin.
2. Railway deploy'un tamamlanmasını bekleyin.
3. Backend loglarını kontrol edin.
4. `/state` sağlık kontrolünü açın.
5. Eklenti dosyaları değiştiyse öğrenci cihazlarında eklentiyi yeniden
   yükleyin.
6. Masaüstü ajanı değiştiyse öğrenci cihazlarındaki dosyayı güncelleyin.
