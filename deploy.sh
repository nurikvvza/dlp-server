#!/usr/bin/env bash
# Деплой dlp-server БЕЗ sudo (user-site pip, статические бинари в ~/.local).
# Запускать на ЦЕЛЕВОМ сервере под пользователем administrator:
#   bash /tmp/dlp-server/deploy.sh
set -euo pipefail

APP_DIR="$HOME/dlp-server"
LOCAL="$HOME/.local"
BIN="$LOCAL/bin"
PORT=8000
SERVICE="dlp-server"

mkdir -p "$APP_DIR" "$LOCAL" "$BIN"

echo "==[1/7]== копируем файлы проекта"
cp -r /tmp/dlp-server/. "$APP_DIR/" 2>/dev/null || cp -r "$(dirname "$0")/dlp-server/." "$APP_DIR/"
chmod 700 "$APP_DIR"

echo "==[2/7]== pip user-site установка зависимостей"
export PATH="$BIN:$HOME/.local/bin:$PATH"
python3 -m pip install --user --quiet --upgrade pip --break-system-packages 2>/dev/null || true
python3 -m pip install --user --quiet -r "$APP_DIR/requirements.txt" --break-system-packages

echo "==[3/7]== ffmpeg (статический бинарь в ~/.local/bin)"
if ! command -v ffmpeg >/dev/null 2>&1 && [ ! -x "$BIN/ffmpeg" ]; then
  curl -fsSL https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/ffmpeg-master-latest-linux64-gpl.tar.xz -o /tmp/ffmpeg.tar.xz
  mkdir -p /tmp/ffmpeg_extract
  tar -xf /tmp/ffmpeg.tar.xz -C /tmp/ffmpeg_extract
  FFMPEG_DIR=$(find /tmp/ffmpeg_extract -maxdepth 1 -type d -name 'ffmpeg-master-latest-linux64-gpl*' | head -1)
  cp "$FFMPEG_DIR/bin/ffmpeg" "$FFMPEG_DIR/bin/ffprobe" "$BIN/"
  chmod +x "$BIN/ffmpeg" "$BIN/ffprobe"
fi
"$BIN/ffmpeg" -version | head -1 || ffmpeg -version | head -1

echo "==[4/7]== node.js v20 (статический в ~/.local/node)"
if ! command -v node >/dev/null 2>&1 && [ ! -x "$LOCAL/node/bin/node" ]; then
  curl -fsSL https://nodejs.org/dist/v20.11.1/node-v20.11.1-linux-x64.tar.xz -o /tmp/node.tar.xz
  mkdir -p "$LOCAL/node"
  tar -xf /tmp/node.tar.xz -C "$LOCAL/node" --strip-components=1
fi
NODE_BIN="$(dirname "$(command -v node 2>/dev/null || echo "$LOCAL/node/bin/node")")"
node --version || "$LOCAL/node/bin/node" --version

echo "==[5/7]== запуск (user-systemd если есть, иначе nohup)"
cat > "$HOME/start-dlp.sh" <<EOF
#!/usr/bin/env bash
export PATH="$BIN:$NODE_BIN:$HOME/.local/bin:\$PATH"
export AUTH_TOKEN=dlp_secret_2024
cd "$APP_DIR"
exec python3 "$APP_DIR/server.py"
EOF
chmod +x "$HOME/start.sh" 2>/dev/null || true
chmod +x "$HOME/start-dlp.sh"

mkdir -p "$HOME/.config/systemd/user"
cat > "$HOME/.config/systemd/user/${SERVICE}.service" <<EOF
[Unit]
Description=dlp-server (yt-dlp downloader, user)
After=network.target

[Service]
Environment=PATH=$BIN:$NODE_BIN:$HOME/.local/bin:/usr/bin:/bin
Environment=AUTH_TOKEN=dlp_secret_2024
WorkingDirectory=$APP_DIR
ExecStart=$HOME/start-dlp.sh
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
EOF

if command -v systemctl >/dev/null 2>&1; then
  systemctl --user daemon-reload 2>/dev/null || true
  systemctl --user enable "$SERVICE" 2>/dev/null || true
  systemctl --user restart "$SERVICE" 2>/dev/null || true
  sleep 3
  if systemctl --user is-active --quiet "$SERVICE" 2>/dev/null; then
    echo "systemd(user): сервис АКТИВЕН"
  else
    echo "systemd(user) не поднялся — nohup"
    nohup "$HOME/start-dlp.sh" > "$APP_DIR/nohup.out" 2>&1 &
    sleep 3
  fi
else
  nohup "$HOME/start-dlp.sh" > "$APP_DIR/nohup.out" 2>&1 &
  sleep 3
fi

echo "==[6/7]== проверка порта $PORT"
if command -v ss >/dev/null 2>&1; then
  ss -tlnp 2>/dev/null | grep ":$PORT" || echo "порт $PORT не слушается"
fi

echo "==[7/7]== healthcheck"
curl -s -m 8 "http://127.0.0.1:${PORT}/" | head -c 300 || echo "HTTP не прошёл"
echo
echo "DONE. Внешний адрес: http://$(curl -s -m 5 ifconfig.me 2>/dev/null):${PORT}"
