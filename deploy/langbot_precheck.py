#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""LangBot 受管容器停写预检（deploy/backup_snapshot.sh 在停写之前调用；只读）。

仅在 <数据目录>/connection-runtime/langbot/managed 标记存在时被调用：核对两个固定容器
（b2b-langbot / b2b-langbot-plugin）确实部署在本数据目录、状态可确认，然后输出
「state name」（state ∈ running / stopped）供停写脚本决定停哪些容器。

fail-closed 边界（绝不把“无法确认”当成“无需停写”）：
- 容器不存在、被替换（缺 b2b.langbot=1 标签）、挂载不属于本数据目录（解析后路径比较，
  如另一数据根或恢复副本的同名容器）→ 报错退出；
- docker 不可用或 docker inspect 失败（守护进程异常等）→ 同样报错退出。

用法（由备份脚本以 root 内部调用，不面向日常运维）：
  python3 deploy/langbot_precheck.py --data-dir /var/lib/b2b/data
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

CORE = 'b2b-langbot'
PLUGIN = 'b2b-langbot-plugin'
LABEL_MARKER = 'b2b.langbot'
CORE_TARGET = '/app/data'
PLUGIN_TARGET = '/app/data/plugins'


class PrecheckError(Exception):
    """预检失败（fail-closed：调用方不得继续停写）。"""


def run_docker(args, timeout=30):
    """Docker 调用唯一入口（命令行只含容器名与固定模板，绝不含密钥）。"""
    try:
        return subprocess.run(['docker', *args], capture_output=True, text=True, timeout=timeout)
    except OSError as error:
        raise PrecheckError('Docker 不可用（%s）' % error) from None


def _inspect(name, template):
    """docker inspect 取单个字段；容器不存在（stderr 含 No such…）返回 None，其他失败抛错。"""
    result = run_docker(['inspect', '--format', template, name], timeout=15)
    if result.returncode:
        stderr = result.stderr or ''
        if 'no such' in stderr.lower():
            return None
        tail = stderr.strip().splitlines()
        raise PrecheckError('无法确认容器状态（docker inspect 失败）：%s：%s'
                            % (name, tail[-1][:160] if tail else ''))
    return result.stdout.strip()


def _same_path(left, right):
    """路径同一性：两侧先解析符号链接（如 macOS /var→/private/var），再退化为字面比较。"""
    left = str(left or '')
    right = str(right or '')
    if not left or not right:
        return False
    try:
        if os.path.realpath(left) == os.path.realpath(right):
            return True
    except OSError:
        pass
    return os.path.normpath(left) == os.path.normpath(right)


def _container_state(name):
    """返回 {'running','labels','mounts'} 或 None（容器不存在）；容器存在但读不完整抛错。"""
    running = _inspect(name, '{{.State.Running}}')
    if running is None:
        return None
    labels_raw = _inspect(name, '{{json .Config.Labels}}')
    mounts_raw = _inspect(name, '{{json .Mounts}}')
    try:
        labels = json.loads(labels_raw or '{}') or {}
        mounts = json.loads(mounts_raw or '[]') or []
    except json.JSONDecodeError:
        raise PrecheckError('无法解析容器标签/挂载（拒绝停写）：%s' % name) from None
    if not isinstance(labels, dict) or not isinstance(mounts, list):
        raise PrecheckError('容器信息异常（拒绝停写）：%s' % name)
    return {'running': running == 'true', 'labels': labels, 'mounts': mounts}


def check(data_dir):
    """核对两个受管容器；返回 [(name, 'running'|'stopped')]（固定顺序：核心 → 插件）。"""
    root = Path(data_dir)
    expected = (
        (CORE, root / 'connection-runtime' / 'langbot' / 'core', CORE_TARGET),
        (PLUGIN, root / 'connection-runtime' / 'langbot' / 'plugin', PLUGIN_TARGET),
    )
    results = []
    for name, source_dir, target in expected:
        state = _container_state(name)
        if state is None:
            raise PrecheckError('受管 LangBot 容器不存在（标记存在即应已部署，拒绝停写）：%s' % name)
        if state['labels'].get(LABEL_MARKER) != '1':
            raise PrecheckError('同名容器缺少 %s=1 标签（可能被替换），拒绝停写：%s' % (LABEL_MARKER, name))
        mount = None
        for candidate in state['mounts']:
            if isinstance(candidate, dict) and str(candidate.get('Destination')) == target:
                mount = candidate
                break
        if (not mount or str(mount.get('Type') or 'bind') != 'bind'
                or not _same_path(mount.get('Source'), source_dir)):
            raise PrecheckError('同名容器挂载不属于本数据目录（可能来自恢复副本/另一数据根），拒绝停写：%s' % name)
        results.append((name, 'running' if state['running'] else 'stopped'))
    return results


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description='LangBot 受管容器停写预检（只读；供 deploy/backup_snapshot.sh 在停写前调用）')
    parser.add_argument('--data-dir', required=True,
                        help='本部署数据目录（B2B_DATA_DIR），用于定位 connection-runtime/langbot')
    args = parser.parse_args(argv)
    try:
        for name, state in check(args.data_dir):
            print('%s %s' % (state, name))
        return 0
    except PrecheckError as error:
        print('错误：%s' % error, file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
