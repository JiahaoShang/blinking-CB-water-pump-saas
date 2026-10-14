#!/bin/sh
# T4 停写窗口备份（单机部署；以 root 运行，由 systemd b2b-backup.service 调用）。
# 流程：保存原活跃状态 → 停止应用与受管桥 → 一致性快照（库 + 全部原件 + 私有模型配置
# + 桥登录状态 + 受限运维配置 /etc/b2b/b2b.env）→ 恢复“原本活跃”的服务。
# 任何失败都经 EXIT trap 恢复原状态，绝不留下“应用被停掉而备份没做成”的现场。
#
# 边界（不伪装原子快照）：先停写再复制；数据库用 SQLite 在线备份接口取事务一致快照。
# 备份产物为 root 私有（目录 0700/文件 0600）：包内含业务数据与密钥（b2b.env、企业模型
# 配置），因此不进 b2b 用户可读范围，也不允许明文上传到云——异地方案必须先加密
# （见 deploy/offsite-backup-r2-plan.md）。
#
# 受管桥（三类，均只处理本数据目录对应容器，绝不触碰其他容器）：
# 1) 旧账号容器（WhatsApp 个人版，名称固定 b2b-account-<24 位账号 ID>）：
#    只处理本库 <数据目录>/connection-runtime/ 下登记过账号对应的容器；
# 2) 企业专属桥容器（运营预部署，b2b-enterprise-<租户哈希 24hex>）：
#    由注册表 <数据目录>/connection-runtime/enterprise-bridges.json（或
#    WHATSAPP_PERSONAL_BRIDGES_FILE 指向的文件）登记；预检用部署工具 --check-registered
#    核对标签与 sessions/media 卷属于本库数据目录、并区分运行状态——恢复副本/另一数据根
#    的同名容器或被替换的容器一律失败退出（exit 3）；任何“无法确认”（含 docker inspect
#    失败）同样失败，绝不宣称一致性成功。
# 3) LangBot 传输栈（opt-in：仅当 <数据目录>/connection-runtime/langbot/managed 普通文件存在）：
#    两个固定容器 b2b-langbot / b2b-langbot-plugin；停写前用本目录 langbot_precheck.py 核对
#    b2b.langbot=1 标签与 connection-runtime/langbot/{core,plugin} 挂载属于本数据目录（解析后
#    路径）并区分运行状态——缺失/被替换/挂载不符/docker 错误一律失败退出（exit 3）；标记
#    不存在（未部署 LangBot）时行为与原来完全一致。
# 旧账号容器：docker 不可用或容器不存在时告警跳过、不猜不停。本部署形态下应用没有 Docker 权限。
#
# 环境变量（由 /etc/b2b/b2b.env 或单元文件提供；均有默认值）：
#   B2B_APP_UNIT、B2B_APP_DIR、B2B_PY、B2B_DATA_DIR、B2B_BACKUP_ROOT、
#   B2B_BACKUP_KEEP_DAYS、B2B_ENV_FILE
#   B2B_BACKUP_OUT（可选）：把本次快照目录固定为该绝对路径。异地链
#   （deploy/offsite_backup_oss.sh）用它把“本次生成的快照”钉死为可确定的返回路径：
#   不靠时间巧合、不会误选旧包。未设置时按原行为生成 <BACKUP_ROOT>/<时间戳>。
set -eu

APP_UNIT="${B2B_APP_UNIT:-b2b-app}"
APP_DIR="${B2B_APP_DIR:-/opt/b2b/app}"
PY="${B2B_PY:-/opt/b2b/venv/bin/python3}"
DATA_DIR="${B2B_DATA_DIR:-/var/lib/b2b/data}"
BACKUP_ROOT="${B2B_BACKUP_ROOT:-/var/backups/b2b}"
KEEP_DAYS="${B2B_BACKUP_KEEP_DAYS:-14}"
OPS_ENV_FILE="${B2B_ENV_FILE:-/etc/b2b/b2b.env}"

DB_PATH="$DATA_DIR/app.db"
UPLOADS_DIR="$DATA_DIR/uploads"
RUNTIME_DIR="$DATA_DIR/connection-runtime"

[ -x "$PY" ] || PY="$(command -v python3)"   # 未建 venv 时用系统 python3（纯文本场景可行）

# ── 原状态保存与恢复（任何退出路径都恢复，只动本来就活跃的东西）────────────
APP_WAS_ACTIVE=0
STOPPED_BRIDGES=""
RESTORED=0

restore_services() {
  if [ "$RESTORED" = "1" ]; then
    return 0
  fi
  RESTORED=1
  for name in $STOPPED_BRIDGES; do
    if docker start "$name" >/dev/null 2>&1; then
      echo "已恢复受管桥：$name"
    else
      echo "警告：受管桥容器未能恢复启动（请人工处理）：$name" >&2
    fi
  done
  if [ "$APP_WAS_ACTIVE" = "1" ]; then
    if systemctl start "$APP_UNIT"; then
      echo "已恢复服务：$APP_UNIT"
    else
      echo "警告：应用未能恢复启动（请人工处理）：$APP_UNIT" >&2
    fi
  fi
}
trap 'restore_services' EXIT
trap 'exit 130' INT TERM HUP

if systemctl is-active --quiet "$APP_UNIT"; then
  APP_WAS_ACTIVE=1
fi

# 备份根目录：root 私有；已经存在但权限过宽时收紧。
if [ ! -d "$BACKUP_ROOT" ]; then
  install -d -m 0700 "$BACKUP_ROOT"
fi
chmod 0700 "$BACKUP_ROOT"
STAMP="$(date +%Y%m%d-%H%M%S)"
# 本次快照目录：默认时间戳；B2B_BACKUP_OUT 显式固定（异地链的“本次快照”可靠返回路径）。
# 同名目标在停写之前就拒绝（fail-closed，避免白停一轮还失败）。
if [ -n "${B2B_BACKUP_OUT:-}" ]; then
  case "$B2B_BACKUP_OUT" in
    /*)
      OUT="$B2B_BACKUP_OUT"
      ;;
    *)
      echo "错误：B2B_BACKUP_OUT 必须是绝对路径：$B2B_BACKUP_OUT" >&2
      exit 2
      ;;
  esac
else
  OUT="$BACKUP_ROOT/$STAMP"
fi
if [ -e "$OUT" ] || [ -L "$OUT" ]; then
  echo "错误：备份目标已存在，拒绝同名冲突（未停写）：$OUT" >&2
  exit 2
fi

# ── 企业桥注册表预检（在停写之前完成；用部署工具核对归属与运行状态）──────────────
# 注册表位置与应用的读取口径一致：WHATSAPP_PERSONAL_BRIDGES_FILE 优先，默认取本数据目录。
# --check-registered 由部署工具核对每个登记容器：标签（bridge=1 + 本企业）、sessions/media
# 卷必须属于本库数据目录（恢复副本/另一数据根的同名容器会被拒绝），并区分运行状态；
# 任何无法核对（含 docker inspect 失败）一律失败——绝不把“无法确认”当成“无需停写”。
ENTERPRISE_REGISTRY="${WHATSAPP_PERSONAL_BRIDGES_FILE:-$RUNTIME_DIR/enterprise-bridges.json}"
ENTERPRISE_TOOL="$APP_DIR/deploy/provision_enterprise_bridge.py"
ENTERPRISE_STATES=""
if [ -e "$ENTERPRISE_REGISTRY" ]; then
  if [ ! -f "$ENTERPRISE_REGISTRY" ]; then
    echo "错误：企业桥注册表存在但不是普通文件，拒绝备份：$ENTERPRISE_REGISTRY" >&2
    exit 3
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "错误：已登记企业桥（$ENTERPRISE_REGISTRY）但 Docker 不可用，无法停写企业桥容器——拒绝在可能半写状态下备份" >&2
    exit 3
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "错误：已登记企业桥（$ENTERPRISE_REGISTRY）但 Docker 守护进程不可用，无法停写企业桥容器——拒绝在可能半写状态下备份" >&2
    exit 3
  fi
  if [ ! -f "$ENTERPRISE_TOOL" ]; then
    echo "错误：缺少企业桥注册表工具（$ENTERPRISE_TOOL），无法核对已登记容器的停写范围" >&2
    exit 3
  fi
  if ! ENTERPRISE_STATES="$("$PY" "$ENTERPRISE_TOOL" --check-registered --db "$DB_PATH")"; then
    echo "错误：企业桥归属/状态核对失败（fail-closed，未停写；恢复副本或被替换的容器不会被动）" >&2
    exit 3
  fi
fi

# ── LangBot 受管容器预检（opt-in：仅当标记存在；同样在停写之前完成）────────────────
# 标记为普通文件时：两个固定容器必须存在、带 b2b.langbot=1 标签、挂载来自本数据目录
# connection-runtime/langbot/{core,plugin}（解析后路径），且 docker 可核对；任何“无法确认”
# （缺失、被替换、另一数据根/恢复副本的同名容器、docker 不可用或 inspect 失败）一律失败退出
# （exit 3）——绝不把停不了的容器留在停写窗口之外。标记缺失时以下逻辑完全不参与。
LANGBOT_STATES=""
LANGBOT_MARKER="$RUNTIME_DIR/langbot/managed"
LANGBOT_TOOL="$APP_DIR/deploy/langbot_precheck.py"
if [ -e "$LANGBOT_MARKER" ]; then
  if [ ! -f "$LANGBOT_MARKER" ]; then
    echo "错误：LangBot 受管标记存在但不是普通文件，拒绝备份：$LANGBOT_MARKER" >&2
    exit 3
  fi
  if ! command -v docker >/dev/null 2>&1; then
    echo "错误：LangBot 受管标记存在（$LANGBOT_MARKER）但 Docker 不可用，无法停写 LangBot 容器——拒绝在可能半写状态下备份" >&2
    exit 3
  fi
  if ! docker info >/dev/null 2>&1; then
    echo "错误：LangBot 受管标记存在但 Docker 守护进程不可用，无法停写 LangBot 容器——拒绝在可能半写状态下备份" >&2
    exit 3
  fi
  if [ ! -f "$LANGBOT_TOOL" ]; then
    echo "错误：缺少 LangBot 停写预检工具（$LANGBOT_TOOL），无法核对已部署容器的停写范围" >&2
    exit 3
  fi
  if ! LANGBOT_STATES="$("$PY" "$LANGBOT_TOOL" --data-dir "$DATA_DIR")"; then
    echo "错误：LangBot 容器归属/状态核对失败（fail-closed，未停写；缺失/被替换/挂载不符的容器不会被动）" >&2
    exit 3
  fi
fi

# ── 停写：应用 → 受管桥 ───────────────────────────────────────────────────
if [ "$APP_WAS_ACTIVE" = "1" ]; then
  echo "停写窗口：停止 $APP_UNIT"
  systemctl stop "$APP_UNIT"
else
  echo "提示：$APP_UNIT 原本未运行，不停不启（备份后保持原状）"
fi

if [ -d "$RUNTIME_DIR" ] || [ -n "$ENTERPRISE_STATES" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    echo "警告：Docker 未安装或不在 PATH，跳过受管桥停写；如桥正在运行，本次备份可能包含半写会话" >&2
  elif ! docker info >/dev/null 2>&1; then
    echo "警告：Docker 不可用，跳过受管桥停写；如桥正在运行，本次备份可能包含半写会话" >&2
  else
    # 旧账号容器（既有模式；docker 不可用或容器不存在时告警跳过、不猜不停）
    if [ -d "$RUNTIME_DIR" ]; then
      for account_dir in "$RUNTIME_DIR"/*/; do
        [ -d "$account_dir" ] || continue
        account="$(basename "$account_dir")"
        if ! printf '%s' "$account" | grep -Eq '^[0-9a-f]{24}$'; then
          continue
        fi
        name="b2b-account-$account"
        state="$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || true)"
        if [ "$state" = "true" ]; then
          echo "停写窗口：停止受管桥容器 $name"
          docker stop "$name" >/dev/null
          STOPPED_BRIDGES="$STOPPED_BRIDGES $name"
        fi
      done
    fi
    # 企业桥容器（预检已核对归属：标签 + 卷属于本库数据目录；去重；停不下来即失败）
    while IFS=' ' read -r state name; do
      [ -n "${name:-}" ] || continue
      if ! printf '%s' "$name" | grep -Eq '^b2b-enterprise-[0-9a-f]{24}$'; then
        echo "错误：预检返回了无效容器名，拒绝停写：$name" >&2
        exit 3
      fi
      case "$state" in
        running)
          case " $STOPPED_BRIDGES " in
            *" $name "*) continue ;;
          esac
          echo "停写窗口：停止企业桥容器 $name"
          if ! docker stop "$name" >/dev/null; then
            echo "错误：企业桥容器未能停止，终止本次备份（将恢复原状态）：$name" >&2
            exit 3
          fi
          STOPPED_BRIDGES="$STOPPED_BRIDGES $name"
          ;;
        stopped)
          echo "提示：注册企业桥容器不在运行，无需停写：$name"
          ;;
        absent)
          echo "提示：注册企业桥容器不存在（尚未部署或已移除），无需停写：$name"
          ;;
        *)
          echo "错误：企业桥状态无法识别，拒绝停写：$state $name" >&2
          exit 3
          ;;
      esac
    done <<EOF
$ENTERPRISE_STATES
EOF
    # LangBot 容器（opt-in；预检已核对标签与挂载属于本数据目录；只停原本运行的，绝不启动原本未运行的）
    if [ -n "$LANGBOT_STATES" ]; then
      while IFS=' ' read -r state name; do
        [ -n "${name:-}" ] || continue
        case "$name" in
          b2b-langbot|b2b-langbot-plugin)
            ;;
          *)
            echo "错误：LangBot 预检返回了未登记容器名，拒绝停写：$name" >&2
            exit 3
            ;;
        esac
        case "$state" in
          running)
            case " $STOPPED_BRIDGES " in
              *" $name "*) continue ;;
            esac
            echo "停写窗口：停止 LangBot 容器 $name"
            if ! docker stop "$name" >/dev/null; then
              echo "错误：LangBot 容器未能停止，终止本次备份（将恢复原状态）：$name" >&2
              exit 3
            fi
            STOPPED_BRIDGES="$STOPPED_BRIDGES $name"
            ;;
          stopped)
            echo "提示：LangBot 容器不在运行，无需停写：$name"
            ;;
          *)
            echo "错误：LangBot 状态无法识别，拒绝停写：$state $name" >&2
            exit 3
            ;;
        esac
      done <<EOF
$LANGBOT_STATES
EOF
    fi
  fi
fi

# ── 快照（含受限运维配置；root 私有产物）─────────────────────────────────
echo "快照：$OUT"
if [ -f "$OPS_ENV_FILE" ]; then
  "$PY" "$APP_DIR/tools/backup_restore.py" backup \
    --db "$DB_PATH" --uploads "$UPLOADS_DIR" --out "$OUT" --ops-config "$OPS_ENV_FILE"
else
  echo "警告：未找到运维配置 $OPS_ENV_FILE，本次备份不含它；密钥材料请按 deploy/offsite-backup-r2-plan.md 单独加密保管" >&2
  "$PY" "$APP_DIR/tools/backup_restore.py" backup \
    --db "$DB_PATH" --uploads "$UPLOADS_DIR" --out "$OUT"
fi

# ── 保留策略：只清理本工具命名的“YYYYmmdd-HHMMSS”目录，绝不触碰其他文件 ──
find "$BACKUP_ROOT" -maxdepth 1 -type d -name '20??????-??????' -mtime +"$KEEP_DAYS" -exec rm -rf {} +

echo "备份完成：$OUT（保留 $KEEP_DAYS 天；异地方案见 deploy/offsite-backup-r2-plan.md）"
# 退出时由 EXIT trap 恢复原本活跃的服务。
