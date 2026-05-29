#!/usr/bin/env bash
# deploy.sh – Deploy DataHarmonizer on the Infomaniak VPS
# Usage: ./deploy.sh
# Prerequisites: SSH key configured for ubuntu@govtech.watts-drive.ch

set -euo pipefail

VPS_HOST="govtech.watts-drive.ch"
VPS_USER="ubuntu"
REMOTE_DIR="/home/ubuntu/dataharmonizer"
BRANCH="Infomaniak-VPS"

echo "==> Deploying DataHarmonizer to ${VPS_USER}@${VPS_HOST}"

ssh "${VPS_USER}@${VPS_HOST}" bash <<ENDSSH
set -euo pipefail

# ---- Install Docker if not present ----
if ! command -v docker &>/dev/null; then
  echo "Installing Docker..."
  curl -fsSL https://get.docker.com | sh
  sudo usermod -aG docker \$USER
  echo "Docker installed. You may need to re-login for group changes to take effect."
fi

# ---- Clone or update repo ----
if [ -d "${REMOTE_DIR}/.git" ]; then
  echo "Pulling latest changes..."
  cd "${REMOTE_DIR}"
  git fetch origin
  git checkout ${BRANCH}
  git pull origin ${BRANCH}
else
  echo "Cloning repository..."
  git clone --branch ${BRANCH} https://github.com/YOUR_ORG/DataHarmonizer.git "${REMOTE_DIR}"
  cd "${REMOTE_DIR}"
fi

cd "${REMOTE_DIR}"

# ---- Ensure .env exists ----
if [ ! -f ".env" ]; then
  echo "ERROR: .env file not found at ${REMOTE_DIR}/.env"
  echo "Copy .env.example to .env and fill in the values, then re-run deploy.sh"
  exit 1
fi

# ---- First-run: obtain TLS certificate ----
if [ ! -d "nginx/certs/live/govtech.watts-drive.ch" ]; then
  echo "Obtaining Let's Encrypt certificate..."
  mkdir -p nginx/certs

  # Start nginx on port 80 only for the ACME challenge
  docker compose up -d nginx

  docker compose run --rm certbot \
    certbot certonly \
    --webroot \
    --webroot-path=/var/www/certbot \
    --email admin@watts-drive.ch \
    --agree-tos \
    --no-eff-email \
    -d govtech.watts-drive.ch

  docker compose restart nginx
fi

# ---- Build & start (or restart) services ----
echo "Building and starting containers..."
docker compose pull nginx certbot
docker compose up -d --build

echo ""
echo "==> Deployment complete!"
echo "    App: https://govtech.watts-drive.ch"
docker compose ps
ENDSSH
