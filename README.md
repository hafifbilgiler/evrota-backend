# EVrota backend

Elektrikli araç uygulaması **EVrota**'nın API'si. FastAPI + Redis, Kubernetes'te kendi
namespace'inde (`evrota`) çalışır; başka hiçbir servisle kaynak paylaşmaz.

## Uçlar

| Uç | Açıklama |
|---|---|
| `GET /health` | Durum, Redis ve Places anahtarı var mı |
| `GET /vehicles` | Araç kataloğu (`data/vehicles.json`, uygulamadaki dosyayla aynı biçim) |
| `GET /prices` | Elektrik / akaryakıt / şarj fiyatları (ConfigMap'ten) |
| `GET /stations?lat=&lng=&radius=` | Yakındaki şarj istasyonları, soketler, anlık doluluk + kullanıcı bildirim özeti |
| `POST /stations/{id}/reports` | Bildirim: `Calisiyor`, `Kuyruk`, `Arizali`, `Kapali` |
| `GET /stations/{id}/reports` | Son 6 saatin bildirim özeti |

İstasyon verisi Google Places API (New) `evChargeOptions` alanından gelir. Maliyet için
3 dk önbellek, günlük çağrı sınırı (`DAILY_PLACES_CAP`, varsayılan 1500) ve Google'a
ulaşılamazsa son bilinen sonuç (`stale: true`) var.

## İlk kurulum

```bash
cd /opt/GIT/evrota-backend
kubectl apply -f k8s/00-namespace.yaml

# Places anahtarı (EVrota için ayrı anahtar önerilir; IP kısıtlaması: sunucu IP'leri)
kubectl -n evrota create secret generic evrota-secrets \
  --from-literal=GOOGLE_PLACES_API_KEY=xxxxx

podman build -t docker.io/hafifbilgiler/evrota-backend:0.1.0 .
podman push docker.io/hafifbilgiler/evrota-backend:0.1.0
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
curl -s "localhost:18000/stations?lat=40.99&lng=29.03" | head -c 600
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
