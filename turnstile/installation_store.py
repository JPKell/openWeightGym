"""Durable model-installation requests and progress; never store a caller's API key."""
import json
import time

from db import database, migrate
from db.schemas import MODEL_INSTALLATIONS as MIGRATIONS
TERMINAL=frozenset({'complete','failed','cancelled'})


class Installations:
    """Keep exact retry settings and explicit interruptions in the front accounts store."""
    def __init__(self,source):
        self.source=database(source)
        self.db=self.source.connect(check_same_thread=False)
        migrate(self.source,self.db,'turnstile.installations',MIGRATIONS)
        self.saved={}

    @staticmethod
    def owner(user):
        return user.identity or 'name:'+user.name

    def add(self,job,user,asked):
        request={key:asked[key] for key in ('name','settings','source_machine','source_model_id','generation','move') if key in asked}
        request['machine']=job['destination']
        progress=json.dumps(job)
        with self.db:
            self.db.execute('INSERT INTO model_installations(id,owner_id,request,progress,updated) VALUES(?,?,?,?,?)',
                (job['id'],self.owner(user),json.dumps(request),progress,time.time()))
        self.saved[job['id']]=progress

    def checkpoint(self,jobs):
        changed=[]
        for job in jobs:
            progress=json.dumps(job)
            if self.saved.get(job['id'])!=progress:changed.append((progress,time.time(),job['id']))
        if changed:
            with self.db:
                self.db.executemany('UPDATE model_installations SET progress=?,updated=? WHERE id=?',changed)
            self.saved.update({id:progress for progress,_,id in changed})

    def recover(self):
        jobs={id:json.loads(progress) for id,progress in self.db.execute('SELECT id,progress FROM model_installations').fetchall()}
        for job in jobs.values():
            if job['state'] not in TERMINAL:
                job.update(state='failed',interrupted=True,why='Installation interrupted by a service restart. Retry to verify retained files and continue with the saved settings.')
        self.checkpoint(jobs.values())
        return jobs

    def can_retry(self,id,user):
        row=self.db.execute('SELECT owner_id FROM model_installations WHERE id=?',(id,)).fetchone()
        return bool(row and (row[0]==self.owner(user) or user.can('superuser')))

    def retry_request(self,id,user):
        row=self.db.execute('SELECT request,progress FROM model_installations WHERE id=?',(id,)).fetchone()
        if row is None:raise KeyError('That installation is not recorded.')
        if not self.can_retry(id,user):raise PermissionError('Only its owner or a superuser may retry this installation.')
        if json.loads(row[1])['state'] not in ('failed','cancelled'):
            raise ValueError('This installation is running or already complete.')
        return json.loads(row[0])
