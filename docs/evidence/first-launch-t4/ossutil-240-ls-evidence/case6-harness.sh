#!/bin/bash
# 以真机捕获的官方 ossutil 2.4.0 输出（空/非空/失败）驱动 offsite_backup_oss.sh 的⑥预检块：
# 从修复后的脚本里原样提取该块并 eval（不启动任何网络）；验证解析结论：
#   空输出（仅 "Object Number is: 0"）→ 放行；表格含对象行 → 拒绝（14）；ls 非零 → fail-closed（14）。
export LC_ALL=C
export LANG=C
set -u
REPO="/Users/jiahaoshang/.codex/worktrees/196c/b2b 售前客户管理平台"
SCRATCH="/Users/jiahaoshang/.hermes/cache/scratch/t4-ossutil"
BLOCK="$(awk '/^# ── ⑥ 云端预检/,/^# ── ⑦/' "$REPO/deploy/offsite_backup_oss.sh" | grep -v '^# ── ⑦')"
if [ -z "$BLOCK" ]; then
  echo "错误：未能从脚本提取⑥预检块"
  exit 9
fi

stub_dir="$(mktemp -d)"
for name in empty nonempty fail; do
  case "$name" in
    empty)    file="$SCRATCH/ls-mock-empty.txt";    rc=0; toerr=0 ;;
    nonempty) file="$SCRATCH/ls-mock-nonempty.txt"; rc=0; toerr=0 ;;
    fail)     file="$SCRATCH/ls-mock-fail.txt";     rc=2; toerr=1 ;;
  esac
  stub="$stub_dir/$name"
  cat > "$stub" <<EOF
#!/bin/bash
if [ "$toerr" = "1" ]; then cat "$file" >&2; else cat "$file"; fi
exit $rc
EOF
  chmod +x "$stub"
  echo "===== 场景: ${name} （注入真输出 $(basename "$file") ；注入退出码 ${rc}）====="
  (
    set -euo pipefail
    OSSUTIL="$stub"
    OSSUTIL_CONFIG="/dev/null"
    OSS_BASE="oss://mock-bucket/backups/20990101-000000/"
    eval "$BLOCK"
    echo "[解析结论] 放行：前缀为空，继续上传路径"
  ) 2>&1
  echo "[块退出码] $?"
done
rm -rf "$stub_dir"
