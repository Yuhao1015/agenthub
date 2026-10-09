"""Explicit project-local consent, independent of discovery and execution."""
from __future__ import annotations
import copy
import os

KINDS = ('claude_code', 'codex', 'codebuddy')
LABELS = {'claude_code': 'Claude Code', 'codex': 'Codex', 'codebuddy': 'WorkBuddy'}
PATH_FIELDS = {'claude_code': {'executable': 'claude_path'},
               'codex': {'executable': 'codex_path'},
               'codebuddy': {'node': 'node_path', 'script': 'script_path'}}


def validate_local_settings(raw: dict, platform: str) -> dict:
    if not isinstance(raw, dict) or set(raw) - {'version', 'revision', 'confirmed', 'platform', 'agents'}:
        raise ValueError('接入配置格式无效')
    version = raw.get('version', 1)
    revision = raw.get('revision', 0)
    confirmed = raw.get('confirmed', False)
    if type(version) is not int or version != 1:
        raise ValueError('不支持的接入配置版本')
    if type(revision) is not int or revision < 0 or type(confirmed) is not bool:
        raise ValueError('接入配置状态无效')
    stored_platform = raw.get('platform', platform)
    if not isinstance(stored_platform, str):
        raise ValueError('接入平台无效')
    agents = raw.get('agents', {})
    if not isinstance(agents, dict) or set(agents) - set(KINDS):
        raise ValueError('不支持的本机 Agent')
    out = {'version': 1, 'revision': revision, 'confirmed': confirmed and stored_platform == platform,
           'platform': platform, 'agents': {}}
    for kind in KINDS:
        entry = agents.get(kind, {})
        if not isinstance(entry, dict) or set(entry) - {'enabled', 'paths'}:
            raise ValueError('Agent 设置无效')
        enabled, paths = entry.get('enabled', False), entry.get('paths', {})
        if type(enabled) is not bool or not isinstance(paths, dict) or set(paths) - set(PATH_FIELDS[kind]):
            raise ValueError('Agent 选择或路径无效')
        clean = {}
        for key, value in paths.items():
            if not isinstance(value, str) or len(value) > 32767 or any(ord(c) < 32 for c in value):
                raise ValueError('路径必须是有效文本')
            if value.strip():
                if not os.path.isabs(value):
                    raise ValueError('请填写绝对路径')
                clean[key] = value.strip()
        out['agents'][kind] = {'enabled': enabled, 'paths': clean}
    return out


def configured_paths(matrix: dict, kind: str) -> dict:
    """Return shared explicit paths when profiles agree; surface conflicts."""
    out = {}
    for spec in matrix.get('agents', {}).values():
        if spec.get('kind') != kind:
            continue
        for key, field in PATH_FIELDS[kind].items():
            if spec.get(field):
                if key in out and out[key] != spec[field]:
                    raise ValueError(LABELS[kind] + ' 的模型配置使用不同安装路径，请先统一路径')
                out[key] = spec[field]
    return out


def apply_local_policy(matrix: dict, settings: dict, platform: str) -> dict:
    cfg = validate_local_settings(settings, platform)
    out = copy.deepcopy(matrix)
    enabled = [k for k, v in cfg['agents'].items() if v['enabled']] if cfg['confirmed'] else []
    fallback = []
    for name, spec in out.get('agents', {}).items():
        kind = spec.get('kind')
        if kind not in KINDS:
            continue
        for key, field in PATH_FIELDS[kind].items():
            if cfg['agents'][kind]['paths'].get(key) and not spec.get(field):
                spec[field] = cfg['agents'][kind]['paths'][key]
        if kind == 'codex' and kind in enabled:
            fallback.append(name)
    out['local_agent_policy'] = {'confirmed': cfg['confirmed'], 'enabled_kinds': enabled,
                                 'generic_fallback': fallback,
                                 'paths': {k: v['paths'] for k, v in cfg['agents'].items()}}
    return out
