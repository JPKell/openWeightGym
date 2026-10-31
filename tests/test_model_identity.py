"""Stable model identity across labels, file revisions, old schemas and durable references."""

import hashlib
import json
import sqlite3
import time

from loadcoach.models import Models
from loadcoach.batches import Batches
from loadcoach.log import Log
from benchmarks.store import Benchmarks
from weightroom.chat_store import Conversations
from test_weightroom import desk, sign_in


def test_model_identity_survives_name_and_settings_but_not_deletion(tmp_path):
    path=tmp_path/'models.db'
    models=Models(str(path))
    first,second=tmp_path/'one.gguf',tmp_path/'two.gguf'
    for file in (first,second):
        file.write_bytes(b'identical weights')
        models.keep_hash(str(file),hashlib.sha256(file.read_bytes()).hexdigest(),file.stat().st_size,file.stat().st_mtime)
    settings={'flags':{'model':str(first),'ctx-size':2048}}
    models.put('old',settings)
    identity=models.identity('old');mark=models.fingerprint(models.get('old'),'old')
    models.rename('old','new')
    assert models.identity('new')==identity and models.name_for(identity)=='new'
    assert models.fingerprint(models.get('new'),'new')==mark
    models.put('new',{'flags':{**settings['flags'],'ctx-size':8192}})
    assert models.identity('new')==identity and models.file_revision('new')==0
    context_mark=models.fingerprint(models.get('new'),'new')
    models.put('new',{'flags':{'model':str(second),'ctx-size':8192}})
    assert models.file_revision('new')==1 and models.identity('new')==identity
    assert models.fingerprint(models.get('new'),'new')!=context_mark
    models.remove('new');models.put('new',settings)
    assert models.identity('new')!=identity and models.name_for(identity) is None
    assert Models(str(path)).identity('new')==models.identity('new')


def test_saved_fingerprint_after_restart_does_not_reread_model_metadata(tmp_path, monkeypatch):
    path = tmp_path / 'models.db'
    weights = tmp_path / 'model.gguf'
    weights.write_bytes(b'weights')
    models = Models(str(path))
    models.put('model', {'flags': {'model': str(weights), 'ctx-size': 2048}})
    models.keep_hash(str(weights), hashlib.sha256(weights.read_bytes()).hexdigest(),
                     weights.stat().st_size, weights.stat().st_mtime)
    reads = []
    monkeypatch.setattr('loadcoach.models.read_metadata', lambda file: reads.append(file) or {'general.name': 'original'})
    mark = models.fingerprint(models.get('model'), 'model')
    original = models.fingerprints()
    assert reads == [str(weights)]
    models.db.close()

    reopened = Models(str(path))
    reads.clear()
    assert reopened.fingerprint(reopened.get('model'), 'model') == mark
    assert reads == [] and reopened.fingerprints() == original
    reopened.put('model', {'flags': {'model': str(weights), 'ctx-size': 4096}})
    assert reopened.fingerprint(reopened.get('model'), 'model') != mark
    assert reads == [str(weights)]
    reopened.db.close()


def test_legacy_migration_uses_rename_audit_without_rewriting_it(tmp_path):
    path=tmp_path/'old.db';db=sqlite3.connect(path)
    db.execute('CREATE TABLE models(name TEXT PRIMARY KEY,settings TEXT NOT NULL)')
    db.execute('INSERT INTO models VALUES (?,?)',('current',json.dumps({'flags':{'model':'weights.gguf'}})));db.commit();db.close()
    log=Log(str(path));event=log.add('model','renamed',model='current',was='original')
    old=log.read()[0]
    library=Models(str(path))
    identity=library.identity('current')
    assert library.legacy_identity('original',0)==identity
    assert library.legacy_identity('current',time.time())==identity
    assert library.legacy_identity('original',time.time()) is None
    assert log.read()[0]==old and log.verify()[1] is None
    assert Models(str(path)).identity('current')==identity


def test_queued_batch_resolves_identity_without_modifying_original_body(tmp_path):
    library=Models(str(tmp_path/'data.db'));library.put('old',{'flags':{'model':'file.gguf'}})
    batches=Batches(str(tmp_path/'data.db'));batches.bind_models(library)
    upload=batches.put_file('user','requests.jsonl','batch',b'original bytes')
    batch=batches.start('user','/v1/chat/completions',upload['id'],[{'custom_id':'one','body':{'model':'old','messages':[]}}])
    identity=library.identity('old');library.rename('old','new')
    line=batches.next_line(batch['id'])
    assert line['model_id']==identity and library.name_for(line['model_id'])=='new'
    assert line['body']['model']=='old' and batches.content(upload['id'])==b'original bytes'


def test_legacy_reused_name_does_not_capture_prior_benchmarks_or_chat(tmp_path):
    path=tmp_path/'old.db';db=sqlite3.connect(path)
    db.execute('CREATE TABLE models(name TEXT PRIMARY KEY,settings TEXT NOT NULL)')
    db.execute('INSERT INTO models VALUES (?,?)',('same',json.dumps({'flags':{'model':'weights.gguf'}})))
    db.execute('CREATE TABLE events(id INTEGER PRIMARY KEY,at REAL,kind TEXT,what TEXT,model TEXT,detail TEXT)')
    db.executemany('INSERT INTO events VALUES (?,?,?,?,?,?)',[(1,100,'model','removed','same','{}'),(2,200,'model','added','same','{}')])
    db.commit();db.close()
    library=Models(str(path));identity=library.identity('same')
    assert library.legacy_identity('same',50) is None
    assert library.legacy_identity('same',250)==identity
    catalogue=[{'id':'same','model_id':identity,'name_history':library.name_history('same')}]
    results=Benchmarks(str(tmp_path/'benchmarks.db'))
    old=results.create('user','same','load');new=results.create('user','same','load')
    with results.db:
        results.db.execute('UPDATE benchmark_runs SET created=50 WHERE id=?',(old,))
        results.db.execute('UPDATE benchmark_runs SET created=250 WHERE id=?',(new,))
    results.bind_models(catalogue)
    assert results.get(old)['model_id'] is None and results.get(new)['model_id']==identity
    chats=Conversations(str(tmp_path/'chat.db'));ref=chats.start('user','test','',True)
    chats.say(ref,'assistant','old answer',model='same');chats.say(ref,'assistant','new answer',model='same')
    with chats.db:
        chats.db.execute('UPDATE messages SET at=50 WHERE content="old answer"')
        chats.db.execute('UPDATE messages SET at=250 WHERE content="new answer"')
    chats.bind_models(ref,'user',{'same':catalogue[0]})
    messages=chats.get(ref,'user')['messages']
    assert 'model_id' not in messages[0] and messages[1]['model_id']==identity


def test_benchmark_follows_multiple_renames_and_never_reused_name(desk,tmp_path):
    browser,_,users=desk
    models=Models(str(tmp_path/'loadcoach.db'))
    file=tmp_path/'weights.gguf';file.touch()
    models.put('old-model',{'flags':{'model':str(file),'ctx-size':2048}})
    models.keep_hash(str(file),hashlib.sha256(b'').hexdigest(),0,file.stat().st_mtime)
    sign_in(browser,'jordan')
    identity=models.identity('old-model')
    mark=browser.get('/models/old-model').text.split('href="/fingerprints?start=')[1].split('"')[0]
    store=browser.app.state.benchmarks.state.store
    number=store.create('jordan','old-model','load')  # an old record initially has no ID
    store.activate(number,{'fingerprint':mark,'subject':{'settings':{'flags':{'ctx-size':2048}}},'environment':{'engine':'test','engine_hash':'test','machine':{'host':'test'}}})
    store.keep(number,'one',{'success':True,'context_tokens':2048,'load_seconds':2},'saved output','saved log')
    store.finish(number,'completed');store.synced(number)
    before=store.db.execute('SELECT * FROM benchmark_samples').fetchall()
    assert browser.post('/models/old-model',data={'do':'rename','to':'new-model'}).status_code==200
    result=browser.get('/benchmarks/results?benchmark=load&model=new-model')
    assert result.status_code==200 and number in result.text and '>0.5' in result.text
    assert store.get(number)['model_id']==identity
    assert store.get(number)['model']=='old-model'  # recorded label/evidence is immutable
    assert browser.get('/models/new-model').text.split('href="/fingerprints?start=')[1].split('"')[0]==mark
    assert browser.post('/models/new-model',data={'do':'rename','to':'final-model'}).status_code==200
    assert number in browser.get('/models/final-model?part=benchmark&benchmark=load').text
    models.remove('final-model');models.put('old-model',{'flags':{'model':str(file),'ctx-size':2048}})
    assert models.identity('old-model')!=identity
    assert number not in browser.get('/benchmarks/results?benchmark=load&model=old-model').text
    assert store.db.execute('SELECT * FROM benchmark_samples').fetchall()==before
