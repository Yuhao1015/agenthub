import os, sys, tempfile, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serve
from test_gate_serve import _snap, _restore, _start, _req

def test_settings():
    saved = _snap()
    with tempfile.TemporaryDirectory() as td:
        server, port = _start(td, with_run=False)
        try:
            cfg = serve._load_config()
            cfg['matrix_patch'] = {'agents': {'cc-sonnet': {'allowed_tools':'Read'}}}
            serve._save_config(cfg)
            serve._write_merged_matrix()
            code, data = _req(port, 'POST', '/settings/agent-options', {'agent':'cc-sonnet', 'options':{'model':'custom/model', 'effort':'high'}})
            assert code == 200, (code, data)
            patch = serve._load_config()['matrix_patch']['agents']['cc-sonnet']
            assert patch == {'allowed_tools':'Read', 'model':'custom/model', 'reasoning_effort':'high'}, patch
            before = open(serve.CONFIG_PATH, 'rb').read()
            code, _ = _req(port, 'POST', '/settings/agent-options', {'agent':'cc-sonnet', 'options':{'model':'bad model'}})
            assert code == 400 and open(serve.CONFIG_PATH, 'rb').read() == before
            code, data = _req(port, 'GET', '/agents')
            one = next(x for x in data['agents'] if x['name'] == 'cc-sonnet')
            assert one['group'] == 'Claude Code' and one['model'] == 'custom/model'
            assert 'custom/model' in one['model_choices'] and one['effort_supported']
            code, data = _req(port, 'POST', '/settings/tools', {'agent':'cc-sonnet', 'allowed':serve._base_allowed_tools('cc-sonnet')})
            assert code == 200, (code,data)
            assert serve._load_config()['matrix_patch']['agents']['cc-sonnet'] == {'model':'custom/model','reasoning_effort':'high'}
            errors = []
            def save(name):
                code, data = _req(port, 'POST', '/settings/agent-options', {'agent':name, 'options':{'model':name+'-custom'}})
                if code != 200: errors.append((code,data))
            threads = [threading.Thread(target=save, args=(name,)) for name in ('cc-sonnet','cc-opus')]
            for t in threads: t.start()
            for t in threads: t.join()
            assert not errors, errors
            agents = serve._load_config()['matrix_patch']['agents']
            assert agents['cc-sonnet']['model'] == 'cc-sonnet-custom'
            assert agents['cc-opus']['model'] == 'cc-opus-custom'
        finally:
            server.shutdown(); server.server_close(); _restore(saved)

if __name__ == '__main__':
    test_settings()
    print('Agent settings tests passed')
