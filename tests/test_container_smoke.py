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
