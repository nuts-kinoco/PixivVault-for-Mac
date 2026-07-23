#!/bin/bash
# Mac版の正式配布ビルド(flet build macos)を実行する。
#
# ネットワーク共有(/Volumes/Share/...)上ではFlutter/Dartのビルドフックがファイルロックを
# 取得できず失敗するため、ソースをローカルディスクへ一時コピーしてからビルドし、
# 完成した.appだけをこのリポジトリのdist/へコピーして戻す。
#
# 除外対象(pixiv_vault.db/cookies.txt/cache等の実データ)はpyproject.tomlの
# [tool.flet.app] excludeで管理しているため、ここでは触れない。
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LOCAL_BUILD_DIR="$(mktemp -d /tmp/pixivvault_macos_build.XXXXXX)"

echo "==> ローカルビルド用ディレクトリ: $LOCAL_BUILD_DIR"

rsync -a \
    --exclude='.git' --exclude='.venv' --exclude='__pycache__' \
    --exclude='build' --exclude='dist' --exclude='node_modules' --exclude='*.pyc' \
    --exclude='cache' --exclude='Images' --exclude='pixiv_vault.db' \
    --exclude='pixiv_vault.db-shm' --exclude='pixiv_vault.db-wal' --exclude='cookies.txt' \
    "$REPO_DIR/" "$LOCAL_BUILD_DIR/"

source "$REPO_DIR/.venv/bin/activate"
cd "$LOCAL_BUILD_DIR"
flet build macos "$@"

mkdir -p "$REPO_DIR/dist"
rm -rf "$REPO_DIR/dist/PixivVault.app"
cp -R "$LOCAL_BUILD_DIR/build/macos/PixivVault.app" "$REPO_DIR/dist/PixivVault.app"
rm -rf "$LOCAL_BUILD_DIR"

echo "==> 完成: $REPO_DIR/dist/PixivVault.app"
