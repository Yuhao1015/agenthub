"""Execution choices are data, never permissions or shell commands."""
import re
import unicodedata
import subprocess
from functools import lru_cache
import yaml

GROUPS = {'claude_code': 'Claude Code', 'codex': 'Codex',
          'dsh': 'DeepSeek Harness', 'codebuddy': 'WorkBuddy / CodeBuddy',
          'openrouter': 'OpenRouter', 'api': 'API 连接', 'mock': '模拟 Agent'}
EFFORTS = {'claude_code': ['low', 'medium', 'high', 'xhigh', 'max'],
           'codex': ['minimal', 'low', 'medium', 'high', 'xhigh'],
           'dsh': ['off', 'low', 'high', 'max']}

def agent_group(kind: str) -> str:
    return GROUPS.get(kind, kind)

def suggested_efforts(kind: str) -> list:
    return list(EFFORTS.get(kind, []))


def selection_capabilities(kind: str, model: str, declared: dict, runtime: dict) -> dict:
    """Parameter capability, never proof of account access. No model-name guessing."""
    unknown = {'effort_control': 'unverified' if kind in EFFORTS else 'unsupported',
               'effort_levels': [], 'source': 'unknown'}
    if kind not in EFFORTS:
        return unknown
    for source, catalog in (('runtime', runtime), ('configuration', declared)):
        row = catalog.get(model) if isinstance(catalog, dict) else None
        if not isinstance(row, dict):
            continue
        control = row.get('effort_control')
        levels = row.get('effort_levels', [])
        if control not in ('supported', 'unsupported', 'unverified') or not isinstance(levels, list):
            continue
        if any(not isinstance(x, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}', x) for x in levels):
            continue
        if control == 'supported' and not levels:
            continue
        return {'effort_control': control,
                'effort_levels': list(dict.fromkeys(levels)) if control == 'supported' else [],
                'source': source}
    return unknown


def validate_effort(options: dict, capabilities: dict) -> dict:
    effort = options.get('effort')
    if effort:
        control = capabilities.get('effort_control')
        if control == 'unsupported':
            raise ValueError('selected model does not support effort')
        if control == 'supported' and effort not in capabilities.get('effort_levels', []):
            raise ValueError('selected model does not support that effort level')
    return dict(options)

def normalize_options(kind: str, raw: dict) -> dict:
    if not isinstance(raw, dict) or set(raw) - {'model', 'effort'}:
        raise ValueError('execution options must contain only model and effort')
    out = {}
    for key, value in raw.items():
        if not isinstance(value, str):
            raise ValueError(f'{key} must be a string')
        if key == 'model':
            if not value and kind not in ('claude_code', 'codex', 'mock'):
                raise ValueError('this agent requires an explicit model')
            if len(value) > 200 or value.startswith('-') or any(
                    c.isspace() or unicodedata.category(c).startswith('C') for c in value):
                raise ValueError('model must be at most 200 characters without whitespace or control characters')
        elif value and (kind not in EFFORTS or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,31}', value)):
            raise ValueError('this agent does not support that effort setting')
        out[key] = value
    return out

@lru_cache(maxsize=8)
def dsh_effort_supported(command: tuple) -> bool:
    """Conservative, offline evidence only. Never return or log CLI config."""
    if not command:
        return False
    try:
        result = subprocess.run(list(command) + ['--profile', 'headless', '--dump-config'],
                                capture_output=True, timeout=3, text=True, encoding='utf-8')
        if result.returncode:
            return False
        config = yaml.safe_load(result.stdout)
        rows = config if isinstance(config, list) else []
        return any(isinstance(row, dict) and row.get('id') == 'agent-default-model'
                   and isinstance(row.get('config'), dict)
                   and 'reasoningEffort' in row['config'] for row in rows)
    except (OSError, subprocess.TimeoutExpired, ValueError, yaml.YAMLError):
        return False
