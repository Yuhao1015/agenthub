"""Validation and public metadata for user-owned text API connections."""
from __future__ import annotations
import copy
import math
import re
from urllib.parse import urlsplit, urlunsplit

FORMATS = ('openai_compatible', 'anthropic', 'openrouter')
ROLES = ('manager', 'planning', 'coding', 'review', 'writing', 'analysis', 'light')
FIELDS = {'id', 'agent_id', 'name', 'format', 'base_url', 'model', 'enabled',
          'roles', 'tier', 'max_output_tokens', 'pricing', 'credential_ref'}


def text_value(value, label, limit=500, empty=False):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise ValueError(label + '格式无效')
    value = value.strip()
    if not value and not empty:
        raise ValueError('请填写' + label)
    return value


def validate_connection(raw: dict) -> dict:
    if not isinstance(raw, dict) or set(raw) - FIELDS:
        raise ValueError('连接包含未知字段')
    out = copy.deepcopy(raw)
    for field in ('id', 'agent_id'):
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', str(out.get(field, ''))):
            raise ValueError('连接标识无效')
    out['name'] = text_value(out.get('name'), '连接名称', 120)
    out['model'] = text_value(out.get('model'), '模型 ID', 200)
    if any(c.isspace() for c in out['model']):
        raise ValueError('模型 ID 不能包含空格')
    if out.get('format') not in FORMATS:
        raise ValueError('接口格式无效')
    url = text_value(out.get('base_url'), 'Base URL', 2048)
    try:
        parsed = urlsplit(url)
        _ = parsed.port
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            raise ValueError()
        if parsed.query or parsed.fragment or '?' in url or '#' in url or '\\' in url or ' ' in url:
            raise ValueError()
    except ValueError:
        raise ValueError('Base URL 必须是无账号、查询参数或片段的 HTTP(S) 地址') from None
    path = parsed.path.rstrip('/')
    for suffix in ('/chat/completions', '/messages'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            break
    out['base_url'] = urlunsplit((parsed.scheme, parsed.netloc, path, '', ''))
    if type(out.get('enabled', False)) is not bool:
        raise ValueError('启用状态必须是布尔值')
    out.setdefault('enabled', False)
    roles = out.get('roles', [])
    if not isinstance(roles, list) or any(r not in ROLES for r in roles) or len(set(roles)) != len(roles):
        raise ValueError('自动分配职责无效')
    out['roles'] = roles
    if out.get('tier') not in (None, 'economy', 'mid', 'strong'):
        raise ValueError('档位无效')
    out.setdefault('tier', None)
    limit = out.setdefault('max_output_tokens', 4096)
    if type(limit) is not int or not 1 <= limit <= 1000000:
        raise ValueError('输出上限必须是 1 到 1000000 的整数')
    price = out.setdefault('pricing', None)
    if price is not None:
        if not isinstance(price, dict) or set(price) - {'input', 'output', 'cache_read', 'cache_write'} or not {'input', 'output'} <= set(price):
            raise ValueError('价格需包含每百万 input/output token 的美元单价')
        if any(type(v) not in (int, float) or not math.isfinite(v) or v < 0 for v in price.values()):
            raise ValueError('价格必须为有限非负数')
    if 'credential_ref' in out:
        ref = text_value(out['credential_ref'], '凭据引用', 250, empty=True)
        if ref and not re.fullmatch(r'(?:connection/[A-Za-z0-9_/-]+|env/[A-Za-z_][A-Za-z0-9_]*|legacy/(?:OPENROUTER_API_KEY|DEEPSEEK_API_KEY))', ref):
            raise ValueError('凭据引用无效')
        out['credential_ref'] = ref
    return out


def public_connection(connection: dict, key_present: bool) -> dict:
    out = copy.deepcopy(connection)
    out.pop('credential_ref', None)
    out['has_key'] = bool(key_present)
    out['verified'] = False
    return out


def validate_connections(raw):
    if raw is None:
        raw = {}
    if not isinstance(raw, dict) or set(raw) - {'version', 'revision', 'items'}:
        raise ValueError('API 连接配置无效')
    version, revision, items = raw.get('version', 1), raw.get('revision', 0), raw.get('items', {})
    if type(version) is not int or version != 1 or type(revision) is not int or revision < 0 or not isinstance(items, dict):
        raise ValueError('API 连接版本无效')
    clean, names = {}, set()
    for key, value in items.items():
        item = validate_connection(value)
        if key != item['id'] or item['agent_id'] in names:
            raise ValueError('API 连接标识重复或不匹配')
        clean[key] = item
        names.add(item['agent_id'])
    return {'version': 1, 'revision': revision, 'items': clean}


def propose_legacy_api_migration(matrix: dict, key_presence: dict, user_patch: dict) -> dict:
    """Display-only proposal. Caller explicitly confirms before publishing."""
    matrix = copy.deepcopy(matrix)
    for name, patch in user_patch.get('agents', {}).items():
        matrix.setdefault('agents', {}).setdefault(name, {}).update(patch)
    for section in ('routes', 'tiers'):
        matrix.setdefault(section, {}).update(user_patch.get(section, {}))
    items = {}
    if key_presence.get('openrouter'):
        for name, spec in matrix.get('agents', {}).items():
            if spec.get('kind') != 'openrouter':
                continue
            items[name] = validate_connection({
                'id': name, 'agent_id': name, 'name': str(spec.get('display_name') or name),
                'format': 'openrouter', 'base_url': spec.get('base_url') or 'https://openrouter.ai/api/v1',
                'model': spec['model'], 'enabled': True,
                'roles': [role for role in ROLES if name in matrix.get('routes', {}).get(role, [])],
                'tier': next((tier for tier, names in matrix.get('tiers', {}).items() if name in names), None),
                'max_output_tokens': spec.get('max_output_tokens', 4096), 'pricing': spec.get('pricing'),
                'credential_ref': 'env/OPENROUTER_API_KEY'})
    return {'version': 1, 'revision': 0, 'items': items}


def project_api_connections(matrix: dict, connections: dict) -> dict:
    """Preserve historical IDs/order; new connections join only chosen roles."""
    connections = validate_connections(connections)
    result = copy.deepcopy(matrix)
    specs = result.setdefault('agents', {})
    original = copy.deepcopy(specs)
    for name in list(specs):
        if specs[name].get('kind') in ('openrouter', 'api'):
            del specs[name]
    by_name = {}
    for item in connections['items'].values():
        name = item['agent_id']
        if name in specs:
            raise ValueError('API 标识与本地 Agent 冲突')
        previous = original.get(name, {})
        specs[name] = {**previous, 'kind': 'api', 'connection': copy.deepcopy(item),
                       'model': item['model'], 'display_name': item['name'],
                       'enabled': item['enabled'] and bool(item.get('credential_ref')),
                       'animal': previous.get('animal', 'whale'),
                       'blurb': '用户配置的文本 API · 已保存，未验证'}
        by_name[name] = item
    for section in ('routes', 'tiers'):
        for role, names in result.setdefault(section, {}).items():
            def selected(name):
                if name not in specs:
                    return False
                if name not in by_name:
                    return True
                item = by_name[name]
                return specs[name]['enabled'] and (role in item['roles'] if section == 'routes' else role == item['tier'])
            result[section][role] = [name for name in names if selected(name)]
        for name, item in by_name.items():
            if not specs[name]['enabled']:
                continue
            roles = item['roles'] if section == 'routes' else ([item['tier']] if item['tier'] else [])
            for role in roles:
                chain = result[section].setdefault(role, [])
                if name not in chain:
                    chain.append(name)
    return result
