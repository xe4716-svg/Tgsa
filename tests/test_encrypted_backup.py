import os
from pathlib import Path
import sys

import pytest

from scripts import encrypted_backup as module


@pytest.fixture
def archive(tmp_path):
    key=tmp_path/'key';key.write_bytes(os.urandom(32));key.chmod(0o600)
    fake=tmp_path/'fake-dump'
    fake.write_text('#!'+sys.executable+"\nimport sys\nsys.stdout.buffer.write(b'private-data'*200000)\n")
    fake.chmod(0o700)
    target=tmp_path/'archive.enc'
    module.backup_encrypted(target,key,str(fake))
    return target,key


def test_streamed_archive_roundtrip_and_private_permissions(archive,monkeypatch):
    target,key=archive;calls=[]
    assert b'private-data' not in target.read_bytes()
    assert target.stat().st_mode & 0o077==0
    def run(command,*,stdin=None):
        calls.append(command)
        if stdin is not None:assert stdin.read()==b'private-data'*200000
    monkeypatch.setattr(module,'run',run)
    module.restore_encrypted(target,'fresh',key)
    assert calls[0]==['createdb','--','fresh'] and '--single-transaction' in calls[1]
    assert not list(target.parent.glob('.encrypted-backup-*'))


@pytest.mark.parametrize('corrupt',['ciphertext','tag','header','truncated','wrong-key'])
def test_authentication_failure_never_creates_database(archive,monkeypatch,corrupt):
    target,key=archive;raw=bytearray(target.read_bytes())
    if corrupt=='wrong-key':key.write_bytes(os.urandom(32))
    else:
        if corrupt=='ciphertext':raw[len(module.MAGIC)+20]^=1
        elif corrupt=='tag':raw[-1]^=1
        elif corrupt=='header':raw[0]^=1
        elif corrupt=='truncated':raw=raw[:10]
        target.write_bytes(raw)
    monkeypatch.setattr(module,'run',lambda *args,**kwargs:pytest.fail('Database tool ran before authentication'))
    with pytest.raises(ValueError):module.restore_encrypted(target,'fresh',key)


def test_key_mode_symlink_length_and_existing_archive_are_rejected(archive,tmp_path):
    target,key=archive
    with pytest.raises(ValueError):module.backup_encrypted(target,key)
    key.chmod(0o644)
    with pytest.raises(ValueError):module.load_key(key)
    key.chmod(0o600);key.write_bytes(b'invalid')
    with pytest.raises(ValueError):module.load_key(key)
    link=tmp_path/'link';link.symlink_to(key)
    with pytest.raises(OSError):module.load_key(link)


def test_dump_failure_leaves_no_archive_or_temporary_file(tmp_path):
    key=tmp_path/'key';key.write_bytes(os.urandom(32));key.chmod(0o600)
    fake=tmp_path/'failed-dump';fake.write_text('#!'+sys.executable+'\nraise SystemExit(1)\n');fake.chmod(0o700)
    target=tmp_path/'backup.enc'
    with pytest.raises(RuntimeError):module.backup_encrypted(target,key,str(fake))
    assert not target.exists() and not list(tmp_path.glob('.encrypted-backup-*'))
