import pytest
from solders.keypair import Keypair

from memecoin_bot.v12_prearmed_ops import OperationLedger, lamports, validate

OWNER='123456789012345678'


@pytest.mark.parametrize('amount', ['0', '-1', 'NaN', 'Infinity', '1e-3', '0.0000000001', '1; reboot', ' 1', True])
def test_invalid_amounts_are_rejected(amount):
    with pytest.raises(ValueError): lamports(amount)


def test_transfer_is_exact_and_has_no_command_fields():
    destination=str(Keypair().pubkey())
    assert validate('transfer',{'destination':destination,'amount_sol':'0.001'}) == {
        'destination':destination,'amount_lamports':1_000_000}
    with pytest.raises(ValueError): validate('transfer',{'destination':destination,'amount_sol':'0.1','shell':'anything'})


def test_owner_checks_and_single_use_survive_restart(tmp_path):
    path=tmp_path/'ops.sqlite'
    ledger=OperationLedger(path,owner_id=OWNER)
    with pytest.raises(PermissionError): ledger.propose('999999999999999999','stop',{})
    intent=ledger.propose(OWNER,'stop',{})
    with pytest.raises(PermissionError): ledger.claim('999999999999999999',intent['id'])
    assert ledger.claim(OWNER,intent['id'])['action']=='stop'
    ledger.db.close()
    ledger=OperationLedger(path,owner_id=OWNER)
    with pytest.raises(ValueError): ledger.claim(OWNER,intent['id'])
    second=ledger.propose(OWNER,'start',{})
    with pytest.raises(RuntimeError): ledger.claim(OWNER,second['id'])
    ledger.finish(intent['id'],'COMPLETE',{'message':'Stopped'})
    assert ledger.claim(OWNER,second['id'])['action']=='start'
    ledger.db.close()


def test_expired_or_clock_reversed_confirmation_cannot_execute(tmp_path):
    clock=[10**18]
    ledger=OperationLedger(tmp_path/'ops.sqlite',owner_id=OWNER,clock=lambda:clock[0])
    intent=ledger.propose(OWNER,'stop',{})
    clock[0]+=121_000_000_000
    with pytest.raises(ValueError): ledger.claim(OWNER,intent['id'])
    clock[0]=1
    with pytest.raises(ValueError): ledger.claim(OWNER,intent['id'])
    ledger.db.close()


def test_limits_and_release_identifiers_cannot_escape_supported_scope():
    for value in (0,11,True,'5'):
        with pytest.raises(ValueError): validate('configure',{'setting':'position_percent','value':value})
    for release in ('main','../../etc/passwd','https://example.com/code', 'f'*63):
        with pytest.raises(ValueError): validate('update',{'release_id':release})
    assert validate('live_test',{})['maximum_total_lamports']==10_000_000


def test_gateway_restart_never_replays_running_command(tmp_path):
    path=tmp_path/'operations.sqlite'
    ledger=OperationLedger(path,owner_id=OWNER)
    intent=ledger.propose(OWNER,'transfer',{'destination':str(Keypair().pubkey()),'amount_sol':'0.001'})
    ledger.claim(OWNER,intent['id']);ledger.db.close()
    ledger=OperationLedger(path,owner_id=OWNER,recover_interrupted=True)
    assert ledger.history(OWNER)[0]['state']=='UNKNOWN'
    with pytest.raises(ValueError):ledger.claim(OWNER,intent['id'])
    with pytest.raises(PermissionError):ledger.acknowledge('999',intent['id'])
    ledger.acknowledge(OWNER,intent['id'])
    assert ledger.history(OWNER)[0]['state']=='ACKNOWLEDGED'
    with pytest.raises(ValueError):ledger.acknowledge(OWNER,intent['id'])
    ledger.db.close()
