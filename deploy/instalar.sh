#!/usr/bin/env bash
# Sobe a plataforma pela primeira vez na zeca-server (Docker já instalado). Rodar dentro da pasta do
# repositório clonado (/opt/crosssell):  bash deploy/instalar.sh
set -euo pipefail
cd "$(dirname "$0")"
command -v docker >/dev/null || { echo "Docker não encontrado."; exit 1; }
[ -f .env ] || { echo "Crie deploy/.env (modelo: deploy/env.exemplo) antes de continuar."; exit 1; }
grep -qE '^POSTGRES_PASSWORD=.+' .env || { echo "Defina POSTGRES_PASSWORD=... em deploy/.env"; exit 1; }
chmod 600 .env
pasta=$(grep -E '^PASTA_BACKUP=' .env | cut -d= -f2-)
pasta=${pasta:-/opt/crosssell-backups}
sudo mkdir -p "$pasta" && sudo chmod 700 "$pasta"
docker compose up -d --build
docker compose ps
porta=$(grep -E '^PORTA_LOCAL=' .env | cut -d= -f2-)
echo "Plataforma em http://127.0.0.1:${porta:-8000}. Falta o Nginx e o certificado (deploy/LEIA-ME.md, passo 5)."
