#!/usr/bin/env bash
# GitHub Release 部署脚本。由宝塔终端/计划任务以 root 执行。
# 只安装 GitHub 已构建的产物，不在服务器执行 npm 或 next build。
set -euo pipefail

readonly CONFIG_FILE="${DEPLOY_CONFIG_FILE:-/etc/zhoubao/deploy.env}"
readonly DEFAULT_REPO="YOUR_GITHUB_OWNER/wechat-weekreport-open"
readonly ASSET_NAME="zhoubao-release.tar.gz"
readonly CHECKSUM_NAME="zhoubao-release.tar.gz.sha256"

usage() {
  cat <<'EOF'
用法：
  baota-pull-release.sh --commit <40位commit SHA> --scope <web|backend|all> [--migration-reviewed]
  baota-pull-release.sh --rollback

scope：
  web      只重启前端 Web
  backend  安装必要的 Python wheels，并重启 API 与 Worker
  all      安装必要的 Python wheels，并重启 API、Worker 与 Web

检测到迁移文件变化时默认停止。只有迁移方案已单独审查，且所需 schema 操作已完成
（或确认切换前无需执行）后才传 --migration-reviewed；
该参数不会自动执行数据库迁移。
EOF
}

action="deploy"
target_commit=""
deploy_scope=""
migration_reviewed="false"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --commit)
      [[ $# -ge 2 ]] || { echo "--commit 缺少参数。" >&2; exit 2; }
      target_commit="${2,,}"
      shift 2
      ;;
    --scope)
      [[ $# -ge 2 ]] || { echo "--scope 缺少参数。" >&2; exit 2; }
      deploy_scope="$2"
      shift 2
      ;;
    --migration-reviewed)
      migration_reviewed="true"
      shift
      ;;
    --rollback)
      action="rollback"
      shift
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      echo "未知参数：$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if [[ "$action" == "deploy" ]]; then
  [[ "$target_commit" =~ ^[0-9a-f]{40}$ ]] || {
    echo "必须通过 --commit 传入完整的 40 位小写 commit SHA。" >&2
    exit 2
  }
  case "$deploy_scope" in
    web|backend|all) ;;
    *)
      echo "必须通过 --scope 指定 web、backend 或 all。" >&2
      exit 2
      ;;
  esac
elif [[ -n "$target_commit" || -n "$deploy_scope" || "$migration_reviewed" == "true" ]]; then
  echo "--rollback 不能与部署参数同时使用。" >&2
  exit 2
fi

if [[ "${EUID}" -ne 0 ]]; then
  echo "请以 root 执行：sudo $0" >&2
  exit 1
fi

if [[ ! -f "$CONFIG_FILE" ]]; then
  echo "缺少部署配置：$CONFIG_FILE。请先按 deploy/README.md 创建。" >&2
  exit 1
fi

# 该文件仅应由服务器管理员写入，内容为 KEY=VALUE 形式，不要提交到 Git。
# shellcheck disable=SC1090
source "$CONFIG_FILE"

readonly REPO="${GITHUB_REPOSITORY:-$DEFAULT_REPO}"
readonly DEPLOY_ROOT="${DEPLOY_ROOT:-/opt/zhoubao}"
readonly RELEASES_DIR="$DEPLOY_ROOT/releases"
readonly SHARED_DIR="$DEPLOY_ROOT/shared"
readonly CURRENT_LINK="$DEPLOY_ROOT/current"
readonly PREVIOUS_LINK="$DEPLOY_ROOT/previous"
readonly VENV_DIR="$SHARED_DIR/venv"
readonly DEPLOY_USER="${DEPLOY_USER:-zhoubao}"
readonly API_SERVICE="${API_SERVICE:-zhoubao-api}"
readonly WORKER_SERVICE="${WORKER_SERVICE:-zhoubao-worker}"
readonly WEB_SERVICE="${WEB_SERVICE:-zhoubao-web}"
readonly API_HEALTH_URL="${API_HEALTH_URL:-http://127.0.0.1:8010/api/health}"
web_port=3000
if [[ -z "${WEB_HEALTH_URL:-}" ]]; then
  web_environment="$(systemctl show "$WEB_SERVICE" --property=Environment --value)"
  if [[ " $web_environment " =~ [[:space:]]PORT=([0-9]+)[[:space:]] ]]; then
    web_port="${BASH_REMATCH[1]}"
  fi
fi
readonly WEB_HEALTH_URL="${WEB_HEALTH_URL:-http://127.0.0.1:${web_port}/login}"

mkdir -p "$DEPLOY_ROOT"
exec 9>"$DEPLOY_ROOT/.deploy.lock"
if ! flock -n 9; then
  echo "已有发布任务正在执行，拒绝并发部署。" >&2
  exit 1
fi

switch_link() {
  local link_path="$1"
  local target_path="$2"
  local temp_link="${link_path}.new"
  ln -sfn "$target_path" "$temp_link"
  mv -Tf "$temp_link" "$link_path"
}

release_version() {
  local release_dir="$1"
  [[ -f "$release_dir/VERSION" ]] || return 1
  tr -d '\r\n' < "$release_dir/VERSION"
}

requirements_changed() {
  local from_dir="$1"
  local to_dir="$2"
  [[ -x "$VENV_DIR/bin/python" ]] || return 0
  [[ -n "$from_dir" && -f "$from_dir/backend/requirements.txt" ]] || return 0
  ! cmp -s "$from_dir/backend/requirements.txt" "$to_dir/backend/requirements.txt"
}

install_backend_dependencies() {
  local release_dir="$1"
  if [[ ! -x "$VENV_DIR/bin/python" ]]; then
    python3 -m venv "$VENV_DIR"
  fi
  "$VENV_DIR/bin/pip" install --upgrade --no-index \
    --find-links "$release_dir/backend/wheelhouse" \
    -r "$release_dir/backend/requirements.txt"
  chown -R "$DEPLOY_USER:$DEPLOY_USER" "$SHARED_DIR"
}

restart_scope() {
  local scope="$1"
  case "$scope" in
    web)
      systemctl restart "$WEB_SERVICE"
      ;;
    backend)
      systemctl restart "$API_SERVICE" "$WORKER_SERVICE"
      ;;
    all)
      systemctl restart "$API_SERVICE" "$WORKER_SERVICE" "$WEB_SERVICE"
      ;;
  esac
}

wait_for_url() {
  local url="$1"
  local label="$2"
  local attempt
  for attempt in {1..30}; do
    if curl --fail --silent --show-error --max-time 5 "$url" >/dev/null; then
      return 0
    fi
    sleep 1
  done
  echo "$label 健康检查失败：$url" >&2
  return 1
}

verify_runtime() {
  local expected_version="$1"
  local scope="$2"
  local actual_version
  actual_version="$(release_version "$(readlink -f "$CURRENT_LINK")")" || {
    echo "current 版本缺少 VERSION。" >&2
    return 1
  }
  [[ "$actual_version" == "$expected_version" ]] || {
    echo "VERSION 不匹配：期望 $expected_version，实际 $actual_version。" >&2
    return 1
  }

  local service
  for service in "$API_SERVICE" "$WORKER_SERVICE" "$WEB_SERVICE"; do
    if ! systemctl is-active --quiet "$service"; then
      echo "服务未运行：$service" >&2
      systemctl --no-pager --full status "$service" || true
      return 1
    fi
  done

  wait_for_url "$API_HEALTH_URL" "API"
  wait_for_url "$WEB_HEALTH_URL" "Web"

  if [[ "$scope" == "web" || "$scope" == "all" ]]; then
    local web_pid web_cwd expected_cwd asset_file asset_relative asset_url
    web_pid="$(systemctl show "$WEB_SERVICE" --property=MainPID --value)"
    web_cwd="$(readlink -f "/proc/$web_pid/cwd" 2>/dev/null || true)"
    expected_cwd="$(readlink -f "$CURRENT_LINK/frontend")"
    if [[ "$web_cwd" != "$expected_cwd" ]]; then
      echo "Web 进程仍在旧目录：$web_cwd（期望 $expected_cwd）。" >&2
      return 1
    fi

    asset_file="$(find "$CURRENT_LINK/frontend/.next/static" -type f \
      \( -name '*.css' -o -name '*.js' \) -print -quit)"
    [[ -n "$asset_file" ]] || { echo "发布包缺少前端静态资源。" >&2; return 1; }
    asset_relative="${asset_file#"$CURRENT_LINK/frontend/.next/static/"}"
    asset_url="${WEB_HEALTH_URL%/*}/_next/static/$asset_relative"
    if ! curl --fail --silent --show-error --max-time 10 "$asset_url" | cmp -s "$asset_file" -; then
      echo "Web 静态资源与发布包不一致：$asset_url" >&2
      return 1
    fi
  fi
}

restore_release() {
  local restore_dir="$1"
  local scope="$2"
  local failed_target="$3"
  local restore_version
  restore_version="$(release_version "$restore_dir")"

  echo "新版本验证失败，开始恢复 $restore_version。" >&2
  switch_link "$CURRENT_LINK" "$restore_dir"
  if [[ "$scope" == "backend" || "$scope" == "all" ]]; then
    if requirements_changed "$failed_target" "$restore_dir"; then
      install_backend_dependencies "$restore_dir"
    fi
  fi
  restart_scope "$scope"
  verify_runtime "$restore_version" "$scope"
  echo "已恢复到：$restore_version" >&2
}

rollback() {
  if [[ ! -L "$CURRENT_LINK" || ! -L "$PREVIOUS_LINK" ]]; then
    echo "缺少 current 或 previous，无法回滚。" >&2
    exit 1
  fi

  local old_current rollback_target current_version rollback_version
  old_current="$(readlink -f "$CURRENT_LINK")"
  rollback_target="$(readlink -f "$PREVIOUS_LINK")"
  current_version="$(release_version "$old_current")"
  rollback_version="$(release_version "$rollback_target")"

  if requirements_changed "$old_current" "$rollback_target"; then
    install_backend_dependencies "$rollback_target"
  fi
  switch_link "$CURRENT_LINK" "$rollback_target"

  if restart_scope all && verify_runtime "$rollback_version" all; then
    switch_link "$PREVIOUS_LINK" "$old_current"
    echo "已回滚：$current_version -> $rollback_version"
    return 0
  fi

  echo "回滚版本验证失败，尝试恢复原版本 $current_version。" >&2
  switch_link "$CURRENT_LINK" "$old_current"
  if requirements_changed "$rollback_target" "$old_current"; then
    install_backend_dependencies "$old_current"
  fi
  restart_scope all
  verify_runtime "$current_version" all
  echo "回滚未生效，已恢复原版本：$current_version" >&2
  return 1
}

if [[ "$action" == "rollback" ]]; then
  rollback
  exit 0
fi

if [[ -e "$DEPLOY_ROOT/backend/data" && ! -e "$SHARED_DIR/data" ]]; then
  cat >&2 <<EOF
检测到旧 SQLite 数据目录 $DEPLOY_ROOT/backend/data。
为避免首次发布丢失数据，请先执行一次：
  mkdir -p $SHARED_DIR && mv $DEPLOY_ROOT/backend/data $SHARED_DIR/data
然后重新运行本脚本。
EOF
  exit 1
fi

mkdir -p "$RELEASES_DIR" "$SHARED_DIR/data"

auth_headers=()
if [[ -n "${GITHUB_TOKEN:-}" ]]; then
  auth_headers=(-H "Authorization: Bearer $GITHUB_TOKEN")
fi

expected_tag="build-$target_commit"
release_json="$(curl --fail --silent --show-error --location \
  -H 'Accept: application/vnd.github+json' \
  "${auth_headers[@]}" \
  "https://api.github.com/repos/$REPO/releases/tags/$expected_tag")"

asset_id="$({ printf '%s' "$release_json" | python3 -c '
import json, sys
for asset in json.load(sys.stdin).get("assets", []):
    if asset.get("name") == "zhoubao-release.tar.gz":
        print(asset["id"])
        break
else:
    raise SystemExit("目标 Release 中没有 zhoubao-release.tar.gz")
'; })"
checksum_id="$({ printf '%s' "$release_json" | python3 -c '
import json, sys
for asset in json.load(sys.stdin).get("assets", []):
    if asset.get("name") == "zhoubao-release.tar.gz.sha256":
        print(asset["id"])
        break
else:
    raise SystemExit("目标 Release 中没有 zhoubao-release.tar.gz.sha256")
'; })"
release_tag="$(printf '%s' "$release_json" | python3 -c 'import json, sys; print(json.load(sys.stdin)["tag_name"])')"
[[ "$release_tag" == "$expected_tag" ]] || {
  echo "Release 标签不匹配：期望 $expected_tag，实际 $release_tag。" >&2
  exit 1
}

stage_dir="$(mktemp -d "$DEPLOY_ROOT/.release-stage.XXXXXX")"
cleanup() { rm -rf "$stage_dir"; }
trap cleanup EXIT

download_asset() {
  local id="$1"
  local output="$2"
  curl --fail --silent --show-error --location \
    -H 'Accept: application/octet-stream' \
    "${auth_headers[@]}" \
    "https://api.github.com/repos/$REPO/releases/assets/$id" \
    --output "$output"
}

download_asset "$asset_id" "$stage_dir/$ASSET_NAME"
download_asset "$checksum_id" "$stage_dir/$CHECKSUM_NAME"
(cd "$stage_dir" && sha256sum --check "$CHECKSUM_NAME")

release_dir="$RELEASES_DIR/$release_tag"
if [[ ! -d "$release_dir" ]]; then
  unpack_dir="$stage_dir/unpacked"
  mkdir -p "$unpack_dir"
  tar -xzf "$stage_dir/$ASSET_NAME" -C "$unpack_dir"
  unpacked_version="$(release_version "$unpack_dir")" || {
    echo "发布包缺少 VERSION。" >&2
    exit 1
  }
  [[ "$unpacked_version" == "$target_commit" ]] || {
    echo "发布包 VERSION 不匹配：期望 $target_commit，实际 $unpacked_version。" >&2
    exit 1
  }
  ln -sfn "$SHARED_DIR/data" "$unpack_dir/backend/data"
  chown -R "$DEPLOY_USER:$DEPLOY_USER" "$unpack_dir"
  mv "$unpack_dir" "$release_dir"
else
  existing_version="$(release_version "$release_dir")" || {
    echo "已存在的发布目录不完整：$release_dir" >&2
    exit 1
  }
  [[ "$existing_version" == "$target_commit" ]] || {
    echo "已存在的发布目录 VERSION 不匹配：$existing_version" >&2
    exit 1
  }
fi

old_current=""
old_version=""
if [[ -L "$CURRENT_LINK" ]]; then
  old_current="$(readlink -f "$CURRENT_LINK")"
  old_version="$(release_version "$old_current")"
fi

if [[ "$old_current" == "$release_dir" ]]; then
  verify_runtime "$target_commit" "$deploy_scope"
  echo "目标版本已在运行且健康：$target_commit"
  exit 0
fi

# Ignore Python bytecode caches created when Alembic imports migration modules;
# only source migration changes require the explicit schema review gate.
if [[ -n "$old_current" && -d "$old_current/backend/migrations" ]] \
  && ! diff -qr --exclude='__pycache__' --exclude='*.py[co]' \
    "$old_current/backend/migrations" "$release_dir/backend/migrations" >/dev/null; then
  if [[ "$migration_reviewed" != "true" ]]; then
    echo "检测到数据库迁移文件变化。请先单独审查迁移，再传 --migration-reviewed。脚本不会自动执行迁移。" >&2
    exit 3
  fi
  if [[ "$deploy_scope" == "web" ]]; then
    echo "迁移变化不能使用 --scope web。" >&2
    exit 3
  fi
fi

if [[ "$deploy_scope" == "backend" || "$deploy_scope" == "all" ]]; then
  if requirements_changed "$old_current" "$release_dir"; then
    echo "Python 依赖有变化，安装 Release 内 wheels。"
    install_backend_dependencies "$release_dir"
  else
    echo "Python 依赖未变化，跳过安装。"
  fi
fi

# 兼容部署切换前已打开页面仍请求上一版哈希分块的情况。
if [[ ("$deploy_scope" == "web" || "$deploy_scope" == "all") \
  && -n "$old_current" \
  && -d "$old_current/frontend/.next/static" \
  && -d "$release_dir/frontend/.next/static" ]]; then
  cp -a -n "$old_current/frontend/.next/static/." "$release_dir/frontend/.next/static/"
  echo "已保留上一版本前端静态资源，兼容旧页面缓存。"
fi

switch_link "$CURRENT_LINK" "$release_dir"

if restart_scope "$deploy_scope" && verify_runtime "$target_commit" "$deploy_scope"; then
  if [[ -n "$old_current" ]]; then
    switch_link "$PREVIOUS_LINK" "$old_current"
  fi
  echo "部署完成：$old_version -> $target_commit（scope=$deploy_scope）"
  exit 0
fi

if [[ -n "$old_current" ]]; then
  restore_release "$old_current" "$deploy_scope" "$release_dir"
else
  echo "首次部署验证失败，没有 previous 可以自动恢复。" >&2
fi
exit 1
