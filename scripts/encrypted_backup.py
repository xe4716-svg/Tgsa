"""Streaming AES-256-GCM archive wrapper; key custody belongs to the operator.

Backups never write plaintext dumps. Restore verifies the entire tag before any
database tool runs; its private temporary plaintext is removed on close.
"""
import os
from pathlib import Path
import stat
import subprocess
import tempfile

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from scripts.database_backup import run, validate_destination

MAGIC=b'TGMATEBACKUP\x01'
CHUNK=1024*1024


def load_key(path):
    fd=os.open(path,os.O_RDONLY|getattr(os,'O_NOFOLLOW',0))
    with os.fdopen(fd,'rb') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise ValueError('Backup key must be a private regular file')
        key=stream.read(33)
        if len(key)!=32:
            raise ValueError('Backup key must contain exactly 32 bytes')
        return key


def backup_encrypted(output,key_file,pg_dump='pg_dump'):
    key=load_key(key_file);output=Path(output)
    if output.exists():raise ValueError('Backup destination already exists')
    output.parent.mkdir(parents=True,exist_ok=True)
    fd,temp=tempfile.mkstemp(prefix='.encrypted-backup-',dir=output.parent)
    process=None
    try:
        with os.fdopen(fd,'wb') as stream:
            nonce=os.urandom(12);header=MAGIC+nonce
            encryptor=Cipher(algorithms.AES(key),modes.GCM(nonce)).encryptor()
            encryptor.authenticate_additional_data(header)
            stream.write(header)
            process=subprocess.Popen([pg_dump,'--format=custom'],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
            with process.stdout:
                while block:=process.stdout.read(CHUNK):stream.write(encryptor.update(block))
            if process.wait()!=0:raise RuntimeError('Database dump failed')
            stream.write(encryptor.finalize());stream.write(encryptor.tag)
            stream.flush();os.fsync(stream.fileno())
        os.link(temp,output)
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:process.wait(timeout=5)
            except subprocess.TimeoutExpired:process.kill();process.wait()
        Path(temp).unlink(missing_ok=True)


def restore_encrypted(source,database,key_file,createdb='createdb',pg_restore='pg_restore'):
    validate_destination(database);key=load_key(key_file)
    with Path(source).open('rb') as encrypted, tempfile.TemporaryFile() as plaintext:
        size=os.fstat(encrypted.fileno()).st_size
        if size<len(MAGIC)+12+16:raise ValueError('Invalid encrypted backup')
        header=encrypted.read(len(MAGIC)+12)
        if not header.startswith(MAGIC):raise ValueError('Unsupported backup version')
        encrypted.seek(-16,os.SEEK_END);tag=encrypted.read(16)
        encrypted.seek(len(header))
        decryptor=Cipher(algorithms.AES(key),modes.GCM(header[-12:],tag)).decryptor()
        decryptor.authenticate_additional_data(header)
        remaining=size-len(header)-16
        try:
            while remaining:
                block=encrypted.read(min(CHUNK,remaining))
                if not block:raise ValueError('Truncated backup')
                remaining-=len(block);plaintext.write(decryptor.update(block))
            plaintext.write(decryptor.finalize())
        except InvalidTag:
            raise ValueError('Backup authentication failed') from None
        plaintext.seek(0)
        run([createdb,'--',database])
        run([pg_restore,'--exit-on-error','--single-transaction','--dbname',database,'--'],stdin=plaintext)
