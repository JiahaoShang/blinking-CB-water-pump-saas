#!/bin/bash
# T4 异地自动备份：独立 OSS 私有桶 + age 公钥加密（以 root 运行，由 systemd b2b-offsite-backup.service 调用）。
#
# 流程（“本次快照”的可靠返回路径：目录由本脚本钉死并逐项校验——不按时间猜、不会误选旧包）：
#   ① 前置检查：公钥/工具/ossutil 配置/与本机备份( b2b-backup.service )不并发——不满足则在停写前拒绝
#   ② 调用现有停写快照 deploy/backup_snapshot.sh（B2B_BACKUP_OUT=本次钉死目录；备份失败即终止，不上传）
#   ③ 确保应用已恢复运行（快照脚本 trap 自恢复 + 本脚本复查兜底）；未恢复则拒绝加密与上传
#   ④ tar -C 快照目录 | age -R 公钥 → 密文（管道直传；进程内不落任何明文 tar）
#   ⑤ 生成本地 sha256 校验文件（相对文件名，下载到任意目录都可复核）
#   ⑥ 云端预检：目标前缀必须为空（拒绝覆盖/同名冲突；fail-closed）
#   ⑦ 仅上传密文与校验文件到 backups/<时间戳>/（--ignore-existing：任何情况下不覆盖已存在对象）
#   ⑧ 云下载到隔离 root 0700 目录，复核与本地 sha256 一致（失败保留证据目录；成功清理下载副本）
#   ⑨ 保留策略（仅本地、仅本工具命名；云端不做任何删除，生命周期保留待用户确认）
#
# 失败语义（分阶段非零退出；任何失败都不上传明文、本地快照与密文保留）：
#   2  前置检查不通过（含同名冲突、拒绝并发、与 b2b-backup 的等待超时）
#   10 本机快照失败          11 应用未恢复或快照产物缺失/不可信
#   12 age 加密失败（清理不完整密文）
#   13 本地校验文件生成/自检失败
#   14 云端预检不通过（无法确认前缀为空，或已存在同名对象——拒绝覆盖）
#   15 上传失败（本地全部保留；云端可能留有未完成对象，本脚本无删除权限，需人工核对）
#   16 云下载校验失败（保留隔离证据目录）
# 应用状态：本脚本自身从不停止应用；停写只发生在快照脚本内部并由其 trap 恢复；
#   上传阶段应用已在运行——上传失败不会把应用留在停机态。
#
# 安全边界：不打印任何密钥或配置文件内容；ossutil 仅以 --config-file 引用 root 0600 配置；
#   age 只用公钥文件（私钥必须留在离线机器/密码管理器，禁止放服务器）；不启用云端删除，
#   不设置生命周期/保留策略（待用户确认后另行处理）。
#
# 环境变量（由 /etc/b2b/b2b.env 或单元文件提供；均有默认值）：
#   B2B_APP_UNIT（默认 b2b-app）、B2B_BACKUP_UNIT（默认 b2b-backup.service）、
#   B2B_BACKUP_ROOT（默认 /var/backups/b2b）、B2B_BACKUP_KEEP_DAYS（默认 14）、
#   B2B_AGE_RECIPIENT_FILE（默认 /etc/b2b/backup-age.pub，公钥；私钥不得在服务器）、
#   B2B_OSSUTIL（默认 /usr/local/bin/ossutil）、B2B_OSSUTIL_CONFIG（默认 /etc/b2b/ossutil.conf，root 0600）、
#   B2B_OSS_BUCKET（默认 alpsaga-pilot-backup-v1）、B2B_OSS_PREFIX（默认 backups）、
#   B2B_OSS_VERIFY_ROOT（默认 <BACKUP_ROOT>/oss-verify）、
#   B2B_OFFSITE_WAIT_LOCAL_S（默认 600；等待正在运行的 b2b-backup.service 的秒数，0=不等待直接拒绝）
set -euo pipefail
umask 077

APP_UNIT="${B2B_APP_UNIT:-b2b-app}"
BACKUP_UNIT="${B2B_BACKUP_UNIT:-b2b-backup.service}"
BACKUP_ROOT="${B2B_BACKUP_ROOT:-/var/backups/b2b}"
KEEP_DAYS="${B2B_BACKUP_KEEP_DAYS:-14}"
RECIPIENT_FILE="${B2B_AGE_RECIPIENT_FILE:-/etc/b2b/backup-age.pub}"
OSSUTIL="${B2B_OSSUTIL:-/usr/local/bin/ossutil}"
OSSUTIL_CONFIG="${B2B_OSSUTIL_CONFIG:-/etc/b2b/ossutil.conf}"
OSS_BUCKET="${B2B_OSS_BUCKET:-alpsaga-pilot-backup-v1}"
OSS_PREFIX="${B2B_OSS_PREFIX:-backups}"
VERIFY_ROOT="${B2B_OSS_VERIFY_ROOT:-$BACKUP_ROOT/oss-verify}"
WAIT_LOCAL_S="${B2B_OFFSITE_WAIT_LOCAL_S:-600}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
SNAP_SCRIPT="$SCRIPT_DIR/backup_snapshot.sh"

# ── ① 前置检查（全部通过才开始停写；失败 = 2，不触碰应用）────────────────────
if [ ! -f "$RECIPIENT_FILE" ] || [ -L "$RECIPIENT_FILE" ]; then
  echo "错误：age 收件人公钥文件不存在或为符号链接：$RECIPIENT_FILE" >&2
  exit 2
fi
recipient="$(tr -d ' \t\r\n' < "$RECIPIENT_FILE")"
case "$recipient" in
  age1[0-9a-z]*) : ;;
  *)
    echo "错误：age 收件人文件内容不是有效公钥（应以 age1 开头）；拒绝把私钥或异常内容当收件人：$RECIPIENT_FILE" >&2
    exit 2
    ;;
esac
AGE_BIN="$(command -v age || true)"
if [ -z "$AGE_BIN" ]; then
  echo "错误：未找到 age 命令（apt install age）。" >&2
  exit 2
fi
for tool in tar sha256sum; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "错误：未找到 $tool。" >&2
    exit 2
  fi
done
if [ ! -x "$OSSUTIL" ]; then
  echo "错误：ossutil 不存在或不可执行：$OSSUTIL" >&2
  exit 2
fi
if [ ! -f "$OSSUTIL_CONFIG" ] || [ ! -r "$OSSUTIL_CONFIG" ]; then
  echo "错误：ossutil 配置不存在或不可读：$OSSUTIL_CONFIG（内容不打印）" >&2
  exit 2
fi
if [ ! -x "$SNAP_SCRIPT" ]; then
  echo "错误：未找到停写快照脚本（应与本脚本同目录）：$SNAP_SCRIPT" >&2
  exit 2
fi

# 与本机每日备份串行：避免两个停写窗口重叠（正常调度已错开；开机补跑/手工触发时在此等待或拒绝）。
if systemctl is-active --quiet "$BACKUP_UNIT"; then
  if [ "$WAIT_LOCAL_S" -le 0 ]; then
    echo "错误：$BACKUP_UNIT 正在运行且等待被禁用（B2B_OFFSITE_WAIT_LOCAL_S=$WAIT_LOCAL_S）；拒绝并发停写（未开始）。" >&2
    exit 2
  fi
  echo "检测到 $BACKUP_UNIT 正在运行，等待其完成（最多 ${WAIT_LOCAL_S} 秒）…"
  waited=0
  while systemctl is-active --quiet "$BACKUP_UNIT"; do
    if [ "$waited" -ge "$WAIT_LOCAL_S" ]; then
      echo "错误：等待 $BACKUP_UNIT 超时（${WAIT_LOCAL_S} 秒）；拒绝并发备份（未开始）。" >&2
      exit 2
    fi
    sleep 1
    waited=$((waited + 1))
  done
  echo "本机备份已结束，继续异地链。"
fi

APP_WAS_ACTIVE=0
if systemctl is-active --quiet "$APP_UNIT"; then
  APP_WAS_ACTIVE=1
fi

STAMP="$(date +%Y%m%d-%H%M%S)"
SNAP_DIR="$BACKUP_ROOT/$STAMP"
CIPHER_NAME="$STAMP.export.tar.gz.age"
CIPHER="$BACKUP_ROOT/$CIPHER_NAME"
CIPHER_SHA="$CIPHER.sha256"
VERIFY_DIR="$VERIFY_ROOT/$STAMP"
OSS_BASE="oss://$OSS_BUCKET/$OSS_PREFIX/$STAMP"

# 本地同名冲突（含符号链接）在开始前拒绝：不依赖时间巧合、不与旧产物混用。
for path in "$SNAP_DIR" "$CIPHER" "$CIPHER_SHA" "$VERIFY_DIR"; do
  if [ -e "$path" ] || [ -L "$path" ]; then
    echo "错误：目标已存在，拒绝同名冲突（未开始任何操作）：$path" >&2
    exit 2
  fi
done

# 中断清理：只在密文尚未完成时移除不完整密文；快照目录与已完成密文一律保留。
CIPHER_FINALIZED=0
cleanup_on_signal() {
  if [ "$CIPHER_FINALIZED" = "0" ] && [ -e "$CIPHER" ]; then
    rm -f "$CIPHER"
  fi
  exit 130
}
trap 'cleanup_on_signal' INT TERM HUP

ensure_app_restored() {
  if [ "$APP_WAS_ACTIVE" != "1" ]; then
    return 0
  fi
  if systemctl is-active --quiet "$APP_UNIT"; then
    return 0
  fi
  echo "警告：快照流程后 $APP_UNIT 未处于运行状态，尝试恢复（快照脚本本应已恢复，此为兜底）…" >&2
  systemctl start "$APP_UNIT" >/dev/null 2>&1 || true
  if systemctl is-active --quiet "$APP_UNIT"; then
    echo "已恢复应用运行：$APP_UNIT"
    return 0
  fi
  echo "错误：应用未能恢复运行，需人工处理：$APP_UNIT" >&2
  return 1
}

echo "异地备份开始：本次快照固定为 $SNAP_DIR；云端目标 $OSS_BASE/；本地保留 $KEEP_DAYS 天。"

# ── ② 停写快照（现有脚本；路径钉死）─────────────────────────────────────────
echo "① 停写快照：调用 $SNAP_SCRIPT"
snap_rc=0
B2B_BACKUP_OUT="$SNAP_DIR" "$SNAP_SCRIPT" || snap_rc=$?
if [ "$snap_rc" -ne 0 ]; then
  echo "错误：本机快照失败（退出码 $snap_rc）；终止异地上传——未加密、未上传任何内容。" >&2
  ensure_app_restored || true
  exit 10
fi
if [ ! -d "$SNAP_DIR" ] || [ -L "$SNAP_DIR" ] || [ ! -f "$SNAP_DIR/manifest.json" ]; then
  echo "错误：快照脚本报告成功，但本次快照产物缺失或不可信（$SNAP_DIR）；拒绝继续（不上传）。" >&2
  ensure_app_restored || true
  exit 11
fi

# ── ③ 应用恢复门：未确认应用已恢复之前，绝不加密与上传 ──────────────────────
echo "② 确认应用已恢复运行"
if ! ensure_app_restored; then
  echo "错误：拒绝加密与上传（本地快照已保留：$SNAP_DIR）。" >&2
  exit 11
fi

# ── ④ 加密：tar 管道直传 age（不落明文 tar）────────────────────────────────
echo "③ 加密：tar | age -R（公钥）→ $CIPHER"
if ! tar -C "$SNAP_DIR" -czf - . | "$AGE_BIN" -R "$RECIPIENT_FILE" -o "$CIPHER"; then
  rm -f "$CIPHER"
  echo "错误：age 加密失败；已清理不完整密文；未上传任何内容（本地快照保留：$SNAP_DIR）。" >&2
  exit 12
fi
chmod 0600 "$CIPHER"
CIPHER_FINALIZED=1

# ── ⑤ 本地 sha256（校验文件与上传对象同名同内容口径）────────────────────────
echo "④ 生成 sha256 校验文件"
if ! ( cd "$BACKUP_ROOT" && sha256sum "$CIPHER_NAME" > "$CIPHER_NAME.sha256" ); then
  echo "错误：本地 sha256 校验文件生成失败；未上传。" >&2
  exit 13
fi
chmod 0600 "$CIPHER_SHA"
LOCAL_SHA="$(sha256sum "$CIPHER" | cut -d' ' -f1)"
if ! ( cd "$BACKUP_ROOT" && sha256sum -c "$CIPHER_NAME.sha256" >/dev/null 2>&1 ); then
  echo "错误：本地校验文件自检失败（$CIPHER_SHA）；未上传。" >&2
  exit 13
fi

# ── ⑥ 云端预检：前缀必须为空（拒绝覆盖/同名冲突；无法确认则 fail-closed）────
# ossutil 2.4.0 无 -s 短标志（以官方 2.4.0 二进制 `ls --help` 实核；短输出是长标志 --short-format）。
# 用默认表格输出预检：空前缀仅一行 "Object Number is: 0"（容忍冒号后空格差异）；
# 若前缀下已有对象，数据行 ObjectName 列含 oss:// ——计数非 0 或出现对象行都按同名冲突拒绝。
echo "⑤ 云端预检：确认 $OSS_BASE/ 为空"
ls_rc=0
ls_out="$("$OSSUTIL" --config-file "$OSSUTIL_CONFIG" ls "$OSS_BASE/" 2>&1)" || ls_rc=$?
if [ "$ls_rc" -ne 0 ]; then
  echo "错误：无法确认云端目标前缀为空（ls 退出码 $ls_rc，fail-closed）；未上传。" >&2
  printf '%s\n' "$ls_out" >&2
  exit 14
fi
object_count="$(printf '%s\n' "$ls_out" | sed -n 's/^Object Number is:[[:space:]]*\([0-9][0-9]*\)[[:space:]]*$/\1/p' | tail -n 1)"
if [ -z "$object_count" ]; then
  echo "错误：无法从 ls 输出确认对象计数（输出格式与预期不符，fail-closed）；未上传。" >&2
  printf '%s\n' "$ls_out" >&2
  exit 14
fi
oss_rows="$(printf '%s\n' "$ls_out" | grep 'oss://' || true)"
if [ "$object_count" -ne 0 ] || [ -n "$oss_rows" ]; then
  echo "错误：云端已存在同名对象，拒绝覆盖或同名冲突；未上传。" >&2
  if [ -n "$oss_rows" ]; then
    printf '%s\n' "$oss_rows" >&2
  fi
  exit 14
fi

# ── ⑦ 上传：仅密文与校验文件；--ignore-existing 防覆盖 ──────────────────────
echo "⑥ 上传密文与校验文件（--ignore-existing）"
if ! "$OSSUTIL" --config-file "$OSSUTIL_CONFIG" cp "$CIPHER" "$OSS_BASE/$CIPHER_NAME" --ignore-existing; then
  echo "错误：密文上传失败。本地快照与密文均已保留（$SNAP_DIR、$CIPHER、$CIPHER_SHA）；云端可能留有未完成对象，本脚本无删除权限，需人工核对。" >&2
  exit 15
fi
if ! "$OSSUTIL" --config-file "$OSSUTIL_CONFIG" cp "$CIPHER_SHA" "$OSS_BASE/$CIPHER_NAME.sha256" --ignore-existing; then
  echo "错误：校验文件上传失败。本地快照与密文均已保留；云端可能留有未完成对象，需人工核对。" >&2
  exit 15
fi

# ── ⑧ 云下载复核：隔离 root 0700 目录；与本机 sha256 一致才通过 ──────────────
echo "⑦ 云下载复核（隔离目录 $VERIFY_DIR）"
if ! install -d -m 0700 "$VERIFY_ROOT"; then
  echo "错误：隔离校验根目录不可用：$VERIFY_ROOT" >&2
  exit 16
fi
chmod 0700 "$VERIFY_ROOT"
if ! mkdir -m 0700 "$VERIFY_DIR"; then
  echo "错误：隔离校验目录已存在（拒绝复用）：$VERIFY_DIR" >&2
  exit 16
fi
if ! "$OSSUTIL" --config-file "$OSSUTIL_CONFIG" cp "$OSS_BASE/$CIPHER_NAME" "$VERIFY_DIR/$CIPHER_NAME"; then
  echo "错误：云下载密文失败；保留隔离证据目录：$VERIFY_DIR" >&2
  exit 16
fi
if ! "$OSSUTIL" --config-file "$OSSUTIL_CONFIG" cp "$OSS_BASE/$CIPHER_NAME.sha256" "$VERIFY_DIR/$CIPHER_NAME.sha256"; then
  echo "错误：云下载校验文件失败；保留隔离证据目录：$VERIFY_DIR" >&2
  exit 16
fi
DL_SHA="$(sha256sum "$VERIFY_DIR/$CIPHER_NAME" | cut -d' ' -f1)"
verify_ok=1
( cd "$VERIFY_DIR" && sha256sum -c "$CIPHER_NAME.sha256" >/dev/null 2>&1 ) || verify_ok=0
if [ "$verify_ok" != "1" ] || [ "$DL_SHA" != "$LOCAL_SHA" ]; then
  echo "错误：云下载校验失败（sha256 不一致）：本地 $LOCAL_SHA / 云端 $DL_SHA；保留隔离证据目录：$VERIFY_DIR" >&2
  exit 16
fi
echo "云下载校验通过：本地与云端 sha256 一致（$LOCAL_SHA）。"
case "$VERIFY_DIR" in
  "$VERIFY_ROOT"/[0-9]*)
    rm -rf -- "$VERIFY_DIR"
    ;;
esac

# ── ⑨ 保留策略：仅本地、仅本工具命名；云端一律不删 ──────────────────────────
echo "⑧ 保留策略（本地 ${KEEP_DAYS} 天；云端不做任何删除）"
find "$BACKUP_ROOT" -maxdepth 1 -type f \
  \( -name '20??????-??????.export.tar.gz.age' -o -name '20??????-??????.export.tar.gz.age.sha256' \) \
  -mtime +"$KEEP_DAYS" -exec rm -f {} +
find "$VERIFY_ROOT" -mindepth 1 -maxdepth 1 -type d -name '20??????-??????' \
  -mtime +"$KEEP_DAYS" -exec rm -rf {} +

echo "异地备份完成：本地快照 $SNAP_DIR；密文 $CIPHER（sha256 $LOCAL_SHA）；云端 $OSS_BASE/$CIPHER_NAME 已下载复核一致；本地按 $KEEP_DAYS 天保留（快照/密文/校验文件）；云端无删除、生命周期未启用（待用户确认）。"
