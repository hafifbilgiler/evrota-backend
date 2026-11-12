#!/usr/bin/env bash
# Kullanım: ./build.sh 0.1.0
# İmajı podman ile derler, Docker Hub'a gönderir ve evrota-api deployment'ını günceller.
set -euo pipefail
VER="${1:?Sürüm verin, ör: ./build.sh 0.1.0}"
IMG="docker.io/hafifbilgiler/evrota-backend:${VER}"

podman build -t "$IMG" .
podman push "$IMG"
kubectl -n evrota set image deployment/evrota-api api="$IMG"
kubectl -n evrota rollout status deployment/evrota-api --timeout=120s
echo "Tamam: $IMG"
