#!/bin/bash
# 真脚本 + 真 ossutil 2.4.0 二进制 + 真 age 的本机端到端（“云”= 127.0.0.1 mock，不接真实云）。
# 用法: e2e-run.sh <success|stale|fail-list>
export LC_ALL=C
export LANG=C
set -u
SCEN="$1"
SCRATCH="/Users/jiahaoshang/.hermes/cache/scratch/t4-ossutil"
E2E="$SCRATCH/e2e"
REPO="/Users/jiahaoshang/.codex/worktrees/196c/b2b 售前客户管理平台"
case "$SCEN" in
  success)   MODE="" ;;
  stale)     MODE="stale" ;;
  fail-list) MODE="fail-list" ;;
  *) echo "unknown scenario: $SCEN"; exit 9 ;;
esac
printf '%s' "$MODE" > "$SCRATCH/mock-mode.txt"
> "$SCRATCH/mock2-requests.log"
rm -rf "$E2E/$SCEN"
mkdir -p "$E2E/$SCEN/backups" "$E2E/$SCEN/oss-verify"
mkdir -p "$E2E/state/units"
touch "$E2E/state/units/b2b-app.active"
rm -f "$E2E/state/units/b2b-backup.service.active"

export PATH="$E2E/shims:/usr/bin:/bin:/sbin:/opt/homebrew/bin"
export FAKE_STATE_DIR="$E2E/state"
export FAKE_LOG="$E2E/$SCEN/calls.log"
export B2B_APP_UNIT="b2b-app"
export B2B_BACKUP_UNIT="b2b-backup.service"
export B2B_APP_DIR="$E2E/app"
export B2B_PY="$(command -v python3)"
export B2B_DATA_DIR="$E2E/data"
export B2B_BACKUP_ROOT="$E2E/$SCEN/backups"
export B2B_BACKUP_KEEP_DAYS="14"
export B2B_ENV_FILE="$E2E/b2b.env"
export B2B_AGE_RECIPIENT_FILE="$E2E/backup-age.pub"
export B2B_OSSUTIL="$SCRATCH/ossutil-2.4.0-mac-arm64/ossutil"
export B2B_OSSUTIL_CONFIG="$SCRATCH/mock2.conf"
export B2B_OSS_BUCKET="mock-bucket"
export B2B_OSS_PREFIX="backups"
export B2B_OSS_VERIFY_ROOT="$E2E/$SCEN/oss-verify"

echo "==== E2E 场景: $SCEN (mock mode='$MODE') ===="
echo "-- 运行: bash $REPO/deploy/offsite_backup_oss.sh"
bash "$REPO/deploy/offsite_backup_oss.sh"
rc=$?
echo "-- 退出码: $rc"
echo "-- 本地备份根（含密文与校验文件）:"
ls -la "$E2E/$SCEN/backups" 2>/dev/null
echo "-- 云端对象（mock 存储树）:"
find "$SCRATCH/mock-cloud/mock-bucket/backups" -type f 2>/dev/null | sort
echo "-- systemctl 调用记录（FAKE_LOG）:"
cat "$E2E/$SCEN/calls.log" 2>/dev/null
echo "-- mock 请求日志（最近 25 行）:"
tail -n 25 "$SCRATCH/mock2-requests.log"
exit 0
