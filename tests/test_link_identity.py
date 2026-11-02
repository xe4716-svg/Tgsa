from types import SimpleNamespace
from uuid import uuid4
import pytest
from scripts import link_identity as module

def args():
    return [str(uuid4()) for _ in range(4)]

def test_default_plan_checks_without_mapping_writes():
    script=module.plan(*args())
    assert 'Identity mapping conflict' in script
    assert 'Root membership unavailable' in script
    assert 'Business membership unavailable' in script
    assert 'INSERT INTO' not in script
    assert 'email' not in script

def test_apply_is_atomic_and_idempotent():
    script=module.plan(*args(),apply=True)
    assert script.count('INSERT INTO')==3
    assert script.count('ON CONFLICT DO NOTHING')==3
    assert script.index('BEGIN;') < script.index('LOCK ') < script.index('Identity mapping conflict') < script.index('INSERT INTO') < script.index('COMMIT;')

@pytest.mark.parametrize('value',[None,'not-a-uuid',"';DROP TABLE users;--"])
def test_invalid_input_never_builds_sql(value):
    with pytest.raises((ValueError,TypeError,AttributeError)):
        module.plan(value,*args()[1:])

def test_runtime_grants_are_column_scoped():
    script=module.runtime_plan()
    assert 'NOT rolsuper AND NOT rolbypassrls' in script
    assert 'relowner' in script
    assert 'SELECT(id,status)' in script
    assert 'password' not in script and 'session' not in script
    assert 'GRANT SELECT ON' not in script

def test_cli_check_only_and_safe_rejection(monkeypatch,capsys):
    captured=[]
    def run(command,**kwargs):
        captured.append(kwargs['input'])
        return SimpleNamespace(returncode=1)
    monkeypatch.setattr(module.subprocess,'run',run)
    values=args()
    assert module.main(['--tenant',values[0],'--user',values[1],'--business-tenant',values[2],'--business-user',values[3]])==1
    assert 'INSERT INTO' not in captured[0]
    assert 'no mapping changes committed' in capsys.readouterr().err
