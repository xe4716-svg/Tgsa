from scripts import container_smoke as module

def test_existing_runtime_file_is_preserved(tmp_path,monkeypatch):
    monkeypatch.setattr(module,'__file__',str(tmp_path/'scripts'/'container_smoke.py'))
    target=tmp_path/'.env.runtime'
    target.write_text('existing private configuration')
    monkeypatch.setattr(module.subprocess,'run',lambda *a,**k: (_ for _ in ()).throw(AssertionError('must not start tools')))
    assert module.main()==1
    assert target.read_text()=='existing private configuration'

def test_missing_docker_cleans_only_its_runtime_file(tmp_path,monkeypatch,capsys):
    monkeypatch.setattr(module,'__file__',str(tmp_path/'scripts'/'container_smoke.py'))
    def fail(*args,**kwargs):
        raise FileNotFoundError('private diagnostic')
    monkeypatch.setattr(module.subprocess,'run',fail)
    assert module.main()==1
    assert not (tmp_path/'.env.runtime').exists()
    assert 'private diagnostic' not in capsys.readouterr().err


import json
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize('correct_ledger', [True, False])
def test_compose_checks_current_migration_release_and_cleans_runtime(tmp_path, monkeypatch, correct_ledger):
    monkeypatch.setattr(module, '__file__', str(tmp_path/'scripts'/'container_smoke.py'))
    migrations = tmp_path/'migrations'
    migrations.mkdir()
    (migrations/'001.sql').write_text('SELECT 1;')
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        if 'config' in command:
            stdout = json.dumps({'services':{'api':{'depends_on':{'migrate':{'condition':'service_completed_successfully'}}},'billing':{},
                'bot':{'environment':{'BOT_WORKER_DATABASE_URL':'postgresql://tgs_bot_worker:synthetic@db/tgs'}},'mtproto':{}}})
        elif 'psql' in command:
            count = len(module.FILES) if correct_ledger else len(module.FILES)-1
            stdout = f'1|{count}\n'
        else:
            stdout = ''
        return SimpleNamespace(returncode=0, stdout=stdout)
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert module.main() == (0 if correct_ledger else 1)
    assert not (tmp_path/'.env.runtime').exists()
    assert not (tmp_path/'.env.bot.runtime').exists()
    assert not (tmp_path/'.env.mtproto.runtime').exists()
    assert any('down' in command for command in calls)
    assert any('run' in command for command in calls) == correct_ledger
