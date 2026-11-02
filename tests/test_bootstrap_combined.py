from types import SimpleNamespace
from pathlib import Path
from scripts import bootstrap_combined as module

def test_order_and_argument_boundaries():
    plan = module.commands('tool with spaces')
    assert [Path(command[1]).name for command in plan] == [
        'migrate.py', 'migrate_business.py', 'link_identity.py', 'configure_business_reads.py']
    assert all(command[command.index('--psql')+1] == 'tool with spaces' for command in plan)

def test_failure_stops_before_grants(monkeypatch, capsys):
    calls = []
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=1 if len(calls)==2 else 0)
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert module.main([]) == 1
    assert len(calls) == 2
    assert 'step 2 rejected' in capsys.readouterr().err

def test_all_stages_run_without_echoing_output(monkeypatch, capsys):
    calls=[]
    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(returncode=0, stdout=b'private', stderr=b'private')
    monkeypatch.setattr(module.subprocess, 'run', run)
    assert module.main([]) == 0
    assert len(calls)==4 and 'private' not in capsys.readouterr().out

def test_missing_tool_is_safe(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise OSError('private')
    monkeypatch.setattr(module.subprocess, 'run', fail)
    assert module.main([]) == 1
    assert 'private' not in capsys.readouterr().err
