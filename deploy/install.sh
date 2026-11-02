#!/usr/bin/env bash

# pdf-translator 一键 Docker 部署脚本
# 用法：
#   bash <(curl -fsSL https://raw.githubusercontent.com/weandy/pdf-translator/main/deploy/install.sh)
#
# 可选环境变量：
#   PDFTR_INSTALL_DIR=/opt/pdf-translator
#   PDFTR_REPO=https://github.com/weandy/pdf-translator.git
#   PDFTR_BRANCH=main
#   PDFTR_PORT=18765
#   PDFTR_USE_GITHUB_PROXY=yes|no
#   PDFTR_GITHUB_PROXY=https://ghfast.top/
#   PDFTR_AUTO_INSTALL_DOCKER=ask|yes|no

set -Eeuo pipefail

readonly DEFAULT_REPO="https://github.com/weandy/pdf-translator.git"
readonly DEFAULT_BRANCH="main"
readonly DEFAULT_INSTALL_DIR="/opt/pdf-translator"
readonly DEFAULT_PORT="18765"
readonly DEFAULT_GITHUB_PROXY="https://ghfast.top/"

REPO="${PDFTR_REPO:-$DEFAULT_REPO}"
BRANCH="${PDFTR_BRANCH:-$DEFAULT_BRANCH}"
INSTALL_DIR="${PDFTR_INSTALL_DIR:-$DEFAULT_INSTALL_DIR}"
GITHUB_PROXY="${PDFTR_GITHUB_PROXY:-$DEFAULT_GITHUB_PROXY}"

usage() {
  cat <<'EOF'
用法：
  bash deploy/install.sh

环境变量：
  PDFTR_INSTALL_DIR              安装目录，默认 /opt/pdf-translator
  PDFTR_PORT                     公网端口，默认 18765
  PDFTR_USE_GITHUB_PROXY         yes/no，是否通过 GitHub 加速地址下载
  PDFTR_GITHUB_PROXY             加速地址，默认 https://ghfast.top/
  PDFTR_AUTO_INSTALL_DOCKER      ask/yes/no，未找到 Docker 时的处理方式
EOF
}

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

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  usage
  exit 0
fi

command -v git >/dev/null 2>&1 || fail "未找到 git，请先安装 Git。"

ask_yes_no() {
  local prompt="$1" default="$2" noninteractive="${3:-$2}" answer
  if [[ ! -t 0 ]]; then
    [[ "$noninteractive" == "yes" ]]
    return
  fi
  if [[ "$default" == "yes" ]]; then
    read -r -p "$prompt [Y/n] " answer
    answer="${answer:-Y}"
  else
    read -r -p "$prompt [y/N] " answer
    answer="${answer:-N}"
  fi
  [[ "$answer" =~ ^[Yy]([Ee][Ss])?$ ]]
}

install_docker() {
  command -v curl >/dev/null 2>&1 || fail "自动安装 Docker 需要 curl，请先安装 curl。"
  if [[ ! -f /etc/os-release ]]; then
    fail "无法识别 Linux 发行版，请手动安装 Docker Engine 和 Compose v2。"
  fi
  # 只在 Debian/Ubuntu 系列上自动补全，其他发行版避免误改系统包管理器。
  # get.docker.com 是 Docker 官方安装脚本；执行前会由上层逻辑询问用户。
  . /etc/os-release
  case "${ID:-}" in
    ubuntu|debian|linuxmint|pop|raspbian) ;;
    *) fail "当前发行版（${ID:-unknown}）暂不自动安装 Docker，请手动安装 Docker Engine 和 Compose v2。" ;;
  esac
  log "正在运行 Docker 官方安装脚本"
  if [[ "${EUID}" -eq 0 ]]; then
    curl -fsSL https://get.docker.com | sh
  else
    command -v sudo >/dev/null 2>&1 || fail "当前用户不是 root 且未找到 sudo。"
    curl -fsSL https://get.docker.com | sudo sh
  fi
  hash -r
}

ensure_docker() {
  if ! command -v docker >/dev/null 2>&1; then
    local mode="${PDFTR_AUTO_INSTALL_DOCKER:-ask}"
    case "$mode" in
      yes) ;;
      no) fail "未找到 Docker，已按配置停止。" ;;
      ask)
        ask_yes_no "未检测到 Docker，是否自动安装 Docker Engine 和 Compose v2？" yes no \
          || fail "未安装 Docker，部署已停止。"
        ;;
      *) fail "PDFTR_AUTO_INSTALL_DOCKER 只能是 ask、yes 或 no。" ;;
    esac
    install_docker
  fi
  command -v docker >/dev/null 2>&1 || fail "Docker 安装后仍不可用。"
  if ! docker compose version >/dev/null 2>&1; then
    fail "已找到 Docker，但缺少 Compose v2 插件，请先安装 docker compose plugin。"
  fi
}

ensure_docker

if [[ "${PDFTR_USE_GITHUB_PROXY:-}" == "" ]]; then
  if ask_yes_no "是否通过 GitHub 加速地址下载项目？默认使用 ${GITHUB_PROXY}" yes; then
    USE_GITHUB_PROXY="yes"
  else
    USE_GITHUB_PROXY="no"
  fi
else
  case "${PDFTR_USE_GITHUB_PROXY}" in
    yes|no) USE_GITHUB_PROXY="$PDFTR_USE_GITHUB_PROXY" ;;
    *) fail "PDFTR_USE_GITHUB_PROXY 只能是 yes 或 no。" ;;
  esac
fi

if [[ "$USE_GITHUB_PROXY" == "yes" ]]; then
  if [[ -t 0 && -z "${PDFTR_GITHUB_PROXY:-}" ]]; then
    read -r -p "请输入 GitHub 加速地址（直接回车使用 ${DEFAULT_GITHUB_PROXY}）：" custom_proxy
    GITHUB_PROXY="${custom_proxy:-$DEFAULT_GITHUB_PROXY}"
  elif [[ -z "$GITHUB_PROXY" ]]; then
    fail "已选择 GitHub 加速，但 PDFTR_GITHUB_PROXY 为空。"
  fi
  GITHUB_PROXY="${GITHUB_PROXY%/}/"
  CLONE_REPO="${GITHUB_PROXY}${REPO}"
  log "GitHub 下载加速：${GITHUB_PROXY}"
else
  CLONE_REPO="$REPO"
  log "GitHub 直连"
fi

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
  git -C "$INSTALL_DIR" remote set-url origin "$CLONE_REPO"
  git -C "$INSTALL_DIR" fetch --depth=1 origin "$BRANCH"
  git -C "$INSTALL_DIR" checkout -q -B "$BRANCH" "origin/$BRANCH"
  git -C "$INSTALL_DIR" reset --hard -q "origin/$BRANCH"
else
  if [[ -d "$INSTALL_DIR" && -n "$(find "$INSTALL_DIR" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]]; then
    fail "安装目录已存在且不为空：${INSTALL_DIR}"
  fi
  mkdir -p "$(dirname "$INSTALL_DIR")"
  log "下载项目到 ${INSTALL_DIR}"
  git clone --depth=1 --branch "$BRANCH" "$CLONE_REPO" "$INSTALL_DIR"
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
PORT="${PORT:-$DEFAULT_PORT}"

log "部署完成"
printf '\n访问地址：\033[1;32mhttp://服务器地址:%s\033[0m\n' "$PORT"
printf '安装目录：%s\n' "$INSTALL_DIR"
printf '查看状态：cd %q && docker compose -f deploy/docker-compose.yml ps\n' "$INSTALL_DIR"
printf '查看日志：cd %q && docker compose -f deploy/docker-compose.yml logs -f app\n' "$INSTALL_DIR"
printf '\n首次使用请在网页右上角“设置”中填写翻译服务 API Key 和 MinerU Token。\n'
