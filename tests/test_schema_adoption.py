import json
from pathlib import Path
import pytest
from scripts.adopt_business_schema import plain_database,release,digest,adopt_sql
from scripts.adopt_business_schema import differences

@pytest.mark.parametrize('value',[None,'host=remote dbname=old','postgresql://remote/old','-x','db name','x'*64])
def test_target_must_be_plain_identifier(value):
    with pytest.raises(ValueError):plain_database(value)

def test_identifier_and_fingerprints_are_deterministic():
    assert plain_database('legacy_v2')=='legacy_v2'
    assert digest({'a':1,'b':2})==digest({'b':2,'a':1})

def test_release_checks_import_manifest(tmp_path):
    from scripts.migrate_business import BASELINE_FILES as FILES
    import hashlib
    manifest={}
    for name in FILES:
        path=tmp_path/name;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_bytes(b'BEGIN;\nSELECT 1;\nCOMMIT;')
        manifest[name]=hashlib.sha256(path.read_bytes()).hexdigest()
    (tmp_path/'UPLOAD_MANIFEST.json').write_text(json.dumps(manifest))
    values=release(tmp_path)
    assert len(values)==12 and next(iter(values)).startswith('001_')
    (tmp_path/FILES[0]).write_bytes(b'modified')
    with pytest.raises(ValueError,match='manifest'):release(tmp_path)

def test_adoption_only_records_verified_baseline():
    statement=adopt_sql({'001_original.sql':'a'*64},{'relations':[]},'b'*64)
    assert 'pg_advisory_xact_lock' in statement
    assert 'Business schema already tracked' in statement
    assert 'Root identity migration required' in statement
    assert 'INSERT INTO public.business_schema_adoptions' in statement
    assert 'DROP ' not in statement and 'UPDATE tgmate.' not in statement

def test_difference_summary_never_emits_function_bodies():
    summary=differences({'functions':[{'name':'f','definition':'PRIVATE-OLD'}]},
                        {'functions':[{'name':'f','definition':'PRIVATE-NEW'}]})
    assert summary['functions'][0]['fields']==['definition']
    assert 'PRIVATE' not in json.dumps(summary)
