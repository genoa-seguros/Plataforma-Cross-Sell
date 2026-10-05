#!/usr/bin/env bash
# Publica a versão mais nova do repositório:  bash deploy/atualizar.sh
set -euo pipefail
cd "$(dirname "$0")/.."
git pull --ff-only
cd deploy
docker compose up -d --build web   # ao subir, o container aplica as migrações do banco
docker image prune -f  # apaga as imagens antigas que cada atualização deixa no disco
