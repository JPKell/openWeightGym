"""Telephone proofs and revocation share the portable accounts-store contract."""
import asyncio
import pytest
from loadcoach.groups import Group
from loadcoach.users import Users
from turnstile.phone_store import PhoneStore
from test_postgresql import postgres


def test_postgresql_telephone_proofs_pin_sessions_and_account_cascade(postgres,tmp_path):
    users=Users(postgres,{'plain':Group(can=frozenset({'front-phone'}),models=('small',)),
        'app':Group(can=frozenset({'app-sign-in','front-voice','front-phone'}),models=None)})
    users.add('person','plain');users.add('voice-gateway','app')
    store=PhoneStore(users,tmp_path)
    owner=users.identity('person')
    async def verify():
        code=await store.start_verification(owner,'+15555550999')
        row=store.db.execute('SELECT verifier FROM telephone_verifications').fetchone()
        assert row[0]!=code and code not in row[0]
        await store.verify(owner,'+15555550999',code)
        await store.set_pin('+15555550123','123456')
        assert await store.check_pin('+15555550123','123456')
        assert not await store.check_pin('+15555550123','123457')
    asyncio.run(verify())
    person=store.registered('+15555550999');app=users.by_name('voice-gateway')
    key=store.mint_session(person,app,'+15555550999',registered=True)
    scoped=store.scope_session(users.by_key(key),key)
    assert scoped.name=='person' and scoped.group.models==('small',)
    assert scoped.can('front-voice') and not scoped.can('app-sign-in')
    store.remove_number(owner,store.own(owner)[0]['id'])
    assert users.by_key(key) is None
    users.remove('person')
    for table in ('telephone_numbers','telephone_verifications','telephone_sms_attempts','telephone_sessions'):
        assert store.db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
    store.db.close()
