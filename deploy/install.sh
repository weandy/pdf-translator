#!/usr/bin/env bash

# pdf-translator 一键 Docker 部署脚本
# 用法：
#   bash <(curl -fsSL https://raw.githubusercontent.com/weandy/pdf-translator/main/deploy/install.sh)
#
# 可选环境变量：
#   PDFTR_INSTALL_DIR=/opt/pdf-translator
#   PDFTR_REPO=https://github.com/weandy/pdf-translator.git
#   PDFTR_BRANCH=main
#   PDFTR_PORT=8080

set -Eeuo pipefail

readonly DEFAULT_REPO="https://github.com/weandy/pdf-translator.git"
readonly DEFAULT_BRANCH="main"
readonly DEFAULT_INSTALL_DIR="/opt/pdf-translator"

REPO="${PDFTR_REPO:-$DEFAULT_REPO}"
BRANCH="${PDFTR_BRANCH:-$DEFAULT_BRANCH}"
INSTALL_DIR="${PDFTR_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"

log() {
  printf '\033[1;34m[pdf-translator]\033[0m %s\n' "$*"
}

fail() {
  printf '\033[1;31m[pdf-translator] 错误：\033[0m %s\n' "$*" >&2
  exit 1
}

cleanup_on_error() {
  local code=$?
  if [[ $code -ne 0 ]]; then
    printf '\033[1;31m[pdf-translator] 部署未完成，退出码 %s。\033[0m\n' "$code" >&2
  fi
}
trap cleanup_on_error EXIT

command -v git >/dev/null 2>&1 || fail "未找到 git，请先安装 Git。"
command -v docker >/dev/null 2>&1 || fail "未找到 Docker，请先安装 Docker Engine。"
docker compose version >/dev/null 2>&1 || fail "未找到 Docker Compose，请安装 Docker Compose v2。"

if [[ "${EUID}" -eq 0 ]]; then
  :
elif [[ -w "$(dirname "$INSTALL_DIR")" ]]; then
  :
else
  fail "没有权限写入 ${INSTALL_DIR}，请使用 sudo 执行，或设置 PDFTR_INSTALL_DIR 到可写目录。"
fi

if [[ -e "$INSTALL_DIR" && ! -d "$INSTALL_DIR" ]]; then
  fail "安装目录已存在且不是目录：${INSTALL_DIR}"
fi

if [[ -d "$INSTALL_DIR/.git" ]]; then
  log "发现已有安装：${INSTALL_DIR}"
  if [[ -n "$(git -C "$INSTALL_DIR" status --porcelain)" ]]; then
    fail "安装目录有未提交修改，请先处理后再更新：${INSTALL_DIR}"
  fi
  log "同步 ${BRANCH} 分支"
  git -C "$INSTALL_DIR" fetch --depth=1 origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout -q "$BRANCH"
  git -C "$INSTALL_DIR" reset --hard -q "origin/$BRANCH"
else
  if [[ -d "$INSTALL_DIR" && -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    fail "安装目录已存在且不为空：${INSTALL_DIR}"
  fi
  mkdir -p "$(dirname "$INSTALL_DIR")"
  log "下载项目到 ${INSTALL_DIR}"
  git clone --depth=1 --branch "$BRANCH" "$REPO" "$INSTALL_DIR"
fi

cd "$INSTALL_DIR"

if [[ ! -f .env ]]; then
  cp .env.example .env
  chmod 600 .env 2>/dev/null || true
  log "已创建 .env，请在网页设置中填写翻译服务和 MinerU Token"
else
  log "保留现有 .env 配置"
fi

if [[ -n "${PDFTR_PORT:-}" ]]; then
  if ! [[ "$PDFTR_PORT" =~ ^[0-9]+$ ]] || (( PDFTR_PORT < 1 || PDFTR_PORT > 65535 )); then
    fail "PDFTR_PORT 必须是 1–65535 之间的端口号"
  fi
  if grep -qE '^PDFTR_PORT=' .env; then
    sed -i.bak -E "s/^PDFTR_PORT=.*/PDFTR_PORT=${PDFTR_PORT}/" .env
    rm -f .env.bak
  else
    printf '\nPDFTR_PORT=%s\n' "$PDFTR_PORT" >> .env
  fi
fi

log "检查 Docker Compose 配置"
docker compose -f deploy/docker-compose.yml config >/dev/null

log "构建并启动容器"
docker compose -f deploy/docker-compose.yml up -d --build

PORT="$(sed -n 's/^PDFTR_PORT=//p' .env | tail -n 1)"
PORT="${PORT:-8080}"

log "部署完成"
printf '\n访问地址：\033[1;32mhttp://服务器地址:%s\033[0m\n' "$PORT"
printf '安装目录：%s\n' "$INSTALL_DIR"
printf '查看状态：cd %q && docker compose -f deploy/docker-compose.yml ps\n' "$INSTALL_DIR"
printf '查看日志：cd %q && docker compose -f deploy/docker-compose.yml logs -f app\n' "$INSTALL_DIR"
printf '\n首次使用请在网页右上角“设置”中填写翻译服务 API Key 和 MinerU Token。\n'
