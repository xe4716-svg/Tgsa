import pytest
from scripts.configure_business_reads import grant_sql

@pytest.mark.parametrize('role',["';DROP ROLE postgres;--",'bad-role','', 'a'*64])
def test_rejects_unsafe_role(role):
    with pytest.raises(ValueError): grant_sql(role)

def test_column_grants():
    sql=grant_sql('safe_role')
    assert 'NOT rolsuper AND NOT rolbypassrls' in sql
    assert 'relowner' in sql
    assert 'customer_user_id' not in sql and 'ciphertext' not in sql and 'password' not in sql
    assert 'SELECT(id,tenant_id,config_id' in sql
    assert 'GRANT SELECT ON' not in sql
