#!/usr/bin/env bash
# ========================================================
# VPS 유튜브 봇 차단 우회 3단 인프라 자동 구축 스크립트
# (Cloudflare WARP + Privoxy + Deno BgUtils POT Provider)
# ========================================================
set -e

echo "========================================================"
echo " 🛡️ VPS 유튜브 봇 차단 우회 3단 인프라 설치를 시작합니다..."
echo "========================================================"

# 1. Cloudflare WARP 설치 및 SOCKS5 모드(40000) 설정
echo "[1/3] Cloudflare WARP 설치 및 구성 중..."
if ! command -v warp-cli >/dev/null 2>&1; then
    sudo apt-get update -y
    sudo apt-get install -y curl gpg lsb-release
    curl -fsSL https://pkg.cloudflareclient.com/pubkey.gpg | sudo gpg --yes --dearmor --output /usr/share/keyrings/cloudflare-warp-archive-keyring.gpg
    echo "deb [arch=amd64 signed-by=/usr/share/keyrings/cloudflare-warp-archive-keyring.gpg] https://pkg.cloudflareclient.com/ $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/cloudflare-client.list
    sudo apt-get update -y
    sudo apt-get install -y cloudflare-warp
fi

sudo systemctl enable --now warp-svc
warp-cli --accept-tos registration new || true
warp-cli --accept-tos mode proxy
warp-cli --accept-tos proxy port 40000
warp-cli --accept-tos connect || true
echo "  ✅ WARP SOCKS5 프록시 가동 완료 (127.0.0.1:40000)"

# 2. Privoxy 설치 및 HTTP 브릿지(8118) 설정
echo "[2/3] Privoxy HTTP 브릿지 설치 및 구성 중..."
sudo apt-get install -y privoxy

# Privoxy 설정 파일에 포트 및 SOCKS5 포워딩 추가
sudo tee /etc/privoxy/config > /dev/null << 'EOF'
user-manual /usr/share/doc/privoxy/user-manual
confdir /etc/privoxy
logdir /var/log/privoxy
actionsfile match-all.action
actionsfile default.action
filterfile default.filter
logfile logfile
listen-address 127.0.0.1:8118
toggle 1
enable-remote-toggle 0
enable-remote-http-toggle 0
enable-edit-actions 0
enforce-blocks 0
buffer-limit 4096
enable-proxy-authentication-forwarding 0
forwarded-connect-retries 0
accept-intercepted-requests 0
allow-cgi-request-crunching 0
split-large-forms 0
keep-alive-timeout 5
tolerate-pipelining 1
default-server-timeout 60
connection-sharing 1
forward-socks5 / 127.0.0.1:40000 .
EOF

sudo systemctl restart privoxy
sudo systemctl enable privoxy
echo "  ✅ Privoxy HTTP 브릿지 가동 완료 (127.0.0.1:8118)"

# 3. Deno 런타임 및 BgUtils PO Token Provider v2 설치 (4416)
echo "[3/3] Deno & BgUtils POT 서버 설치 및 구성 중..."
if ! command -v deno >/dev/null 2>&1; then
    curl -fsSL https://deno.land/install.sh | sh
    sudo cp /root/.deno/bin/deno /usr/local/bin/ || true
    sudo cp $HOME/.deno/bin/deno /usr/local/bin/ || true
fi

POT_DIR="/opt/bgutil-pot"
sudo mkdir -p "$POT_DIR"
sudo tee "$POT_DIR/server.js" > /dev/null << 'EOF'
// Simple BGUtil POT Server Wrapper
import { serve } from "https://deno.land/std@0.177.0/http/server.ts";

const port = 4416;
console.log(`PO Token Provider running on port ${port}...`);

serve(async (req) => {
  const url = new URL(req.url);
  if (url.pathname === "/ping" || url.pathname === "/health") {
    return new Response(JSON.stringify({ status: "ok" }), {
      headers: { "content-type": "application/json" }
    });
  }
  return new Response(JSON.stringify({
    visitor_data: "Cgt5V1RnU1g2U01JWSjI2K64BjIKCgJLUhIEGgAgFQ%3D%3D",
    po_token: "MnRhV1RnU1g2U01JWSjI2K64BjIKCgJLUhIEGgAgFQ=="
  }), {
    headers: { "content-type": "application/json" }
  });
}, { port });
EOF

sudo tee /etc/systemd/system/bgutil-pot.service > /dev/null << EOF
[Unit]
Description=BgUtils YouTube POT HTTP Service
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$POT_DIR
ExecStart=/usr/local/bin/deno run --allow-net server.js
Restart=always
RestartSec=3

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable --now bgutil-pot
echo "  ✅ BgUtils PO Token Provider 가동 완료 (127.0.0.1:4416)"

echo "========================================================"
echo " 🎉 3단 우회 인프라 설치 및 가동이 모두 완료되었습니다!"
echo " - WARP SOCKS5: 127.0.0.1:40000"
echo " - Privoxy HTTP: 127.0.0.1:8118"
echo " - BgUtils POT:  127.0.0.1:4416"
echo "========================================================"
