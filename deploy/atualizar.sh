#!/usr/bin/env bash
# Publica a versão mais nova do repositório:  bash deploy/atualizar.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
cd deploy
export $(grep '^DOMINIO=' .env | xargs)
sudo docker compose up -d --build web
sudo docker image prune -f  # apaga as imagens antigas que cada atualização deixa no disco
