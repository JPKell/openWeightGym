"""Portable Tool chat upgrade, exact approvals, backup/transfer and account deletion."""
import pytest

from db import Database, migrate
from db.schemas import CHAT
from db.transfer import copy_store
from db.recovery import RecoveryStore, backup_store
from weightroom.chat_store import Conversations
from test_postgresql import postgres  # noqa: F401


def check_storage(source,tmp_path):
    with source.connection() as connection:
        migrate(source,connection,'weightroom.chat',CHAT[:-1])
        connection.execute('INSERT INTO conversations (user,title,system,thinking,updated) VALUES (?,?,?,?,?)',('owner','Legacy','',1,1))
        connection.commit()
    kept=Conversations(source)
    legacy=kept.listing('owner');assert len(legacy)==1
    assert kept.get(legacy[0]['id'],'owner')['kind']=='text'
    number=kept.start('owner','Tool history',kind='tools')
    exact={'content':[{'type':'text','text':'<exact>'},{'type':'image','data':'YWJj'}],'isError':False}
    state={'started':1.5,'calls':2,'pending':[{'id':'pending','function':{'name':'echo','arguments':'{}'}}]}
    kept.tool_state(number,state,'sensitive');kept.say(number,'tool','exact',exact=exact,decision={'approve':False,'arguments':{'zero':0}})
    expected=kept.get(number,'owner')
    assert kept.listing('owner','tools','normal')==[] and len(kept.listing('owner','tools','sensitive'))==1
    kept.tool_state(number,state,'normal');assert kept.get(number,'owner')['level']=='sensitive'
    kept.db.close()
    backup=backup_store(RecoveryStore('chat',source,('weightroom.chat',)),tmp_path/'payload')
    assert sum(count for name,count in backup['verification']['counts'].items() if name.endswith('conversations'))==2
    target=Database.sqlite(tmp_path/'copied.db');assert copy_store(source,target)['state']=='verified'
    copied=Conversations(target);assert copied.get(number,'owner')==expected
    copied.forget('owner');assert copied.listing('owner','tools')==[] and copied.listing('owner')==[]
    copied.db.close()


def test_sqlite_tool_history_upgrade_backup_and_transfer(tmp_path):
    check_storage(Database.sqlite(tmp_path/'chat.db'),tmp_path)


def test_postgresql_tool_history_upgrade_backup_and_transfer(postgres,tmp_path):
    check_storage(postgres,tmp_path)
