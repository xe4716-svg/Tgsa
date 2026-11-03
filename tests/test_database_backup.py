from pathlib import Path
import pytest
from scripts import database_backup as module


def test_backup_refuses_existing(tmp_path, monkeypatch):
    target = tmp_path / 'backup.dump'
    target.write_bytes(b'old')
    monkeypatch.setattr(module, 'run', lambda command: pytest.fail('Tool must not run'))
    with pytest.raises(ValueError):
        module.backup(target)
    assert target.read_bytes() == b'old'


def test_backup_success_and_cleanup(tmp_path, monkeypatch):
    target = tmp_path / 'backup.dump'
    monkeypatch.setattr(module, 'run', lambda cmd: Path(cmd[-1]).write_bytes(b'dump'))
    module.backup(target)
    assert target.read_bytes() == b'dump'
    assert list(tmp_path.iterdir()) == [target]


def test_backup_failure_cleans_temporary(tmp_path, monkeypatch):
    def fail(command):
        raise RuntimeError('failed')
    monkeypatch.setattr(module, 'run', fail)
    with pytest.raises(RuntimeError):
        module.backup(tmp_path / 'backup.dump')
    assert list(tmp_path.iterdir()) == []


def test_restore_creation_failure_stops(tmp_path, monkeypatch):
    source = tmp_path / 'backup.dump'; source.write_bytes(b'dump')
    calls = []
    def fail(command):
        calls.append(command)
        raise RuntimeError('exists')
    monkeypatch.setattr(module, 'run', fail)
    with pytest.raises(RuntimeError):
        module.restore(source, 'existing')
    assert calls == [['createdb', '--', 'existing']]


def test_restore_is_atomic_without_clean(tmp_path, monkeypatch):
    source = tmp_path / 'backup.dump'; source.write_bytes(b'dump')
    calls = []
    monkeypatch.setattr(module, 'run', calls.append)
    module.restore(source, 'fresh')
    assert calls[0] == ['createdb', '--', 'fresh']
    assert '--single-transaction' in calls[1]
    assert '--clean' not in calls[1]


@pytest.mark.parametrize('database', [
    'dbname=existing', 'postgresql://localhost/existing', '', '-existing',
    'a' * 64, 'name with spaces', 'a/b',
])
def test_restore_rejects_connection_strings_before_tools(tmp_path, monkeypatch, database):
    source = tmp_path / 'backup.dump'; source.write_bytes(b'dump')
    monkeypatch.setattr(module, 'run', lambda command: pytest.fail('Tool must not run'))
    with pytest.raises(ValueError):
        module.restore(source, database)
