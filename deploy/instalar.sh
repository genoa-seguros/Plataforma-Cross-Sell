#!/usr/bin/env bash
# Instala Docker e sobe a plataforma num Ubuntu 22.04/24.04 novo. Rodar como usuário com sudo,
# dentro da pasta do repositório clonado:  bash deploy/instalar.sh
set -euo pipefail
cd "$(dirname "$0")"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi
[ -f .env ] || { echo "Crie deploy/.env (modelo: deploy/env.exemplo) antes de continuar."; exit 1; }
grep -q '^DOMINIO=' .env || { echo "Defina DOMINIO=... em deploy/.env"; exit 1; }
export $(grep '^DOMINIO=' .env | xargs)
sudo docker compose up -d --build
sudo docker compose ps
echo "Pronto. Acesse https://$DOMINIO (o certificado sai em 1-2 minutos, depois que o DNS apontar para este servidor)."
