# EVrota backend

Elektrikli araç uygulaması **EVrota**'nın API'si. FastAPI + Redis, Kubernetes'te kendi
namespace'inde (`evrota`) çalışır; başka hiçbir servisle kaynak paylaşmaz.

## Güvenlik

- `POST /auth/device {"deviceId": "<uuid>"}` → imzalı cihaz token'ı (JWT, 30 gün).
  Aşağıdaki tüm uçlar `Authorization: Bearer <token>` ister; yoksa / sahteyse / süresi
  dolmuşsa **401**.
- IP başına saatte en fazla `TOKENS_PER_IP_HOUR` (10) token; token başına dakikada
  `REQUESTS_PER_MINUTE` (60), günde `REQUESTS_PER_DAY` (2000) istek.
- Aynı cihaz aynı istasyona 10 dakikada bir bildirim verebilir; bildirimler cihaza bağlı.
- `/docs` ve `/openapi.json` kapalı, gövde boyutu sınırlı, hata mesajlarında iç bilgi yok.
- Bir cihazı engellemek: `redis-cli SET blockdev:<deviceId> 1`
- `EVROTA_JWT_SECRET` (en az 32 karakter) yoksa servis başlamaz.
- Play Integrity: `PLAY_INTEGRITY_REQUIRED=true` ile açılacak (doğrulayıcı sonraki sürümde).

## Uçlar

| Uç | Açıklama |
|---|---|
| `GET /health` | Durum (açık) |
| `POST /auth/device` | Cihaz token'ı (açık) |
| `GET /vehicles` | Araç kataloğu (`data/vehicles.json`, uygulamadaki dosyayla aynı biçim) |
| `GET /prices` | Elektrik / akaryakıt / şarj fiyatları (ConfigMap'ten) |
| `GET /stations?lat=&lng=&radius=&operator=` | Yakındaki şarj istasyonları, soketler, anlık doluluk, operatör (ZES, Eşarj, Trugo…) + kullanıcı bildirim özeti |
| `GET /operators` | Firma filtresi için operatör listesi |
| `POST /stations/{id}/reports` | Bildirim: `Calisiyor`, `Kuyruk`, `Arizali`, `Kapali` |
| `GET /stations/{id}/reports` | Son 6 saatin bildirim özeti |
| `GET /news?limit=&category=&before=` | EV haberleri: özet, "neden önemli", öne çıkan bilgiler, katalogdaki araç + kaynak linki |
| `POST /agent/chat` | AI EV asistanı: Gemini fonksiyon çağrısıyla istasyon, katalog, kıyas, yolculuk ve maliyet verisini kullanır; cihaz başına günlük sınır (`AGENT_DAILY_LIMIT`) |
| `GET /vehicles/{id}/insight` | AI araç değerlendirmesi (artılar, eksiler, kime uygun, rakipler); katalog verisine dayalı, 30 gün önbellek |

İstasyon verisi Google Places API (New) `evChargeOptions` alanından gelir. Maliyet için
3 dk önbellek, günlük çağrı sınırı (`DAILY_PLACES_CAP`, varsayılan 1500) ve Google'a
ulaşılamazsa son bilinen sonuç (`stale: true`) var.

## İlk kurulum

```bash
cd /opt/GIT/evrota-backend
kubectl apply -f k8s/00-namespace.yaml

# Places anahtarı (EVrota için ayrı anahtar önerilir; IP kısıtlaması: sunucu IP'leri)
kubectl -n evrota create secret generic evrota-secrets \
  --from-literal=GOOGLE_PLACES_API_KEY=xxxxx \
  --from-literal=EVROTA_JWT_SECRET="$(openssl rand -hex 32)"

podman build -t docker.io/hafifbilgiler/evrota-backend:0.2.0 .
podman push docker.io/hafifbilgiler/evrota-backend:0.2.0
kubectl apply -f k8s/
kubectl -n evrota get pods
```

## Güncelleme

```bash
cd /opt/GIT/evrota-backend && git pull
./build.sh 0.1.1          # build + push + set image + rollout
kubectl apply -f k8s/     # manifest değiştiyse
```

## Test

```bash
kubectl -n evrota port-forward svc/evrota-api 18000:8000 &
curl -s localhost:18000/health
T=$(curl -s -X POST localhost:18000/auth/device -H 'content-type: application/json' \
  -d '{"deviceId":"test-0000-0000-0000-000000000001"}' | python3 -c 'import sys,json;print(json.load(sys.stdin)["token"])')
curl -s -H "Authorization: Bearer $T" "localhost:18000/stations?lat=40.99&lng=29.03" | head -c 600
```

## Dışarı açma

Uygulamanın erişeceği adres (`ApiConfig.BASE_URL`) için nginx'te bir yönlendirme gerekir.
Servise node üzerinden ulaşmak için `k8s/31-api-nodeport.yaml.example` (port 30880)
kullanılabilir. Örnek nginx bloğu:

```nginx
location /evrota/ {
    proxy_pass http://127.0.0.1:30880/;
    proxy_set_header Host $host;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

## Fiyatlar

`k8s/20-configmap-prices.yaml` içindeki değerleri doldurup `kubectl apply` yapın; pod
dosyayı her istekte okur. `null` bırakılan alanlar uygulamada otomatik doldurulmaz.
Otomatik toplama (EPDK tarifesi, akaryakıt, operatör tarifeleri) sonraki adım.

## Haberler

`evrota-news` CronJob'ı saatte bir `k8s/21-configmap-news.yaml` içindeki RSS akışlarını tarar:
tekrarları birleştirir, `GEMINI_API_KEY` varsa EV dışı haberleri eler, haberi kendi
cümlelerimizle özetler ve kategori/marka/model/fiyat çıkarır (yoksa anahtar kelime filtresi).
Yeni model / fiyat haberleri Redis'te `news:drafts` listesine katalog taslağı olarak düşer.
Haber metni kopyalanmaz; uygulama başlık, özet, kaynak adı ve orijinal linki gösterir.

```bash
# Gemini anahtarı (isteğe bağlı ama önerilir)
kubectl -n evrota patch secret evrota-secrets -p '{"stringData":{"GEMINI_API_KEY":"xxxx"}}'
# Elle bir tarama başlat
kubectl -n evrota create job --from=cronjob/evrota-news news-manual-$(date +%s)
kubectl -n evrota logs -l job-name --tail=20
# Kapalı yayıncı akışlarını test et (200 ve <rss/<feed görmelisiniz)
for u in https://www.webtekno.com/rss.xml https://shiftdelete.net/feed https://tr.motor1.com/rss/news/all/ https://www.donanimhaber.com/rss/tum/; do
  printf "%s → " "$u"; curl -s -m 10 -o /tmp/f -w "%{http_code} " "$u"; head -c 300 /tmp/f | grep -o -m1 -E "<rss|<feed" || echo "RSS değil"; done
```
