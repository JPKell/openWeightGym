"""Private front-owned image files and metadata, using the existing accounts store.

The batch executor remains openWeightGym's existing front executor; this store binds its
image jobs to immutable account IDs and keeps binary renders out of the database.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import time

from db import migrate
from mirrorwall.schema import MIRRORWALL

ASSET_ID=re.compile(r"image_[a-f0-9]{32}")
MAX_BYTES=20*2**20
MAX_PIXELS=33554432


class Store:
    """Image history/assets scoped to stable owner IDs; ephemeral assets never touch disk/SQL."""
    def __init__(self,connection,root):
        """Initialize private image state without loading model weights."""
        self.db=connection
        migrate(connection.database,connection,"mirrorwall.front",MIRRORWALL)
        self.root=Path(root)/"mirrorwall-assets"
        self.root.mkdir(parents=True,exist_ok=True,mode=0o700)
        self.temporary={}

    def path(self,identifier,thumbnail=False):
        """Map generated IDs only to private files; caller filenames never become paths."""
        if not isinstance(identifier,str) or not ASSET_ID.fullmatch(identifier):
            raise ValueError("Invalid image asset ID.")
        return self.root/"assets"/identifier[6:8]/(identifier+(".thumb.png" if thumbnail else ".image"))

    @staticmethod
    def decoded(content,output=False):
        """Validate size/format and generate an orientation-correct bounded PNG thumbnail."""
        from PIL import Image,ImageOps
        maximum=128*2**20 if output else MAX_BYTES
        if not isinstance(content,bytes) or not content or len(content)>maximum:
            raise ValueError("Image exceeds its upload/output byte limit.")
        with Image.open(io.BytesIO(content)) as source:
            if source.format not in ("PNG","JPEG","WEBP") or source.width*source.height>MAX_PIXELS:
                raise ValueError("Use a PNG, JPEG or WebP image up to 32 megapixels.")
            source.load()
            image=ImageOps.exif_transpose(source).convert("RGBA")
            width,height=image.size
            image.thumbnail((384,384),Image.Resampling.LANCZOS)
            output=io.BytesIO()
            image.save(output,format="PNG")
            return width,height,output.getvalue()

    def expire(self):
        """Bound temporary no-log assets by age and aggregate memory."""
        now=time.monotonic()
        for key in [key for key,value in self.temporary.items() if value["until"]<now]:
            self.temporary.pop(key)

    def save(self,owner,content,*,kind="reference",filename="image.png",metadata=None,temporary=False,job=None):
        """Atomically retain one validated asset, or hold it only in bounded temporary memory."""
        if not owner or kind not in ("reference","output","mask"):
            raise ValueError("Invalid image owner/kind.")
        width,height,thumb=self.decoded(content,kind=='output')
        identifier="image_"+secrets.token_hex(16)
        filename=Path(str(filename).replace("\\","/")).name[:160] or "image.png"
        record={"id":identifier,"kind":kind,"filename":filename,"sha256":hashlib.sha256(content).hexdigest(),
                "width":width,"height":height,"bytes":len(content),"created":time.time(),"metadata":metadata or {},"temporary":temporary}
        if temporary:
            self.expire()
            if sum(len(row["content"])+len(row["thumbnail"]) for row in self.temporary.values())+len(content)+len(thumb)>256*2**20:
                raise ValueError("Temporary image memory is full; remove temporary images and retry.")
            self.temporary[identifier]={"owner":owner,"record":record,"content":content,"thumbnail":thumb,"until":time.monotonic()+3600}
            return record
        path=self.path(identifier)
        path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
        if shutil.disk_usage(self.root).free<len(content)+len(thumb)+2**30:
            raise ValueError("Front image storage needs at least 1 GiB free headroom.")
        made=[]
        try:
            for destination,data in ((path,content),(self.path(identifier,True),thumb)):
                with destination.open("xb") as stream:
                    os.chmod(destination,0o600)
                    made.append(destination)
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            with self.db:
                self.db.execute("INSERT INTO mirrorwall_assets VALUES (?,?,?,?,?,?,?,?,?,?)",(identifier,owner,kind,filename,record["sha256"],width,height,len(content),record["created"],json.dumps(metadata or {})))
                if job:
                    self.db.execute("INSERT INTO mirrorwall_job_assets VALUES (?,?,?)",(job,identifier,"output"))
        except BaseException:
            for destination in made:
                destination.unlink(missing_ok=True)
            raise
        return record

    def asset(self,owner,identifier):
        """Read only the caller's asset; absent/foreign IDs have the same result."""
        self.expire()
        temporary=self.temporary.get(identifier)
        if temporary:
            return dict(temporary["record"]) if temporary["owner"]==owner else None
        row=self.db.execute("SELECT kind,filename,sha256,width,height,bytes,created,metadata FROM mirrorwall_assets WHERE id=? AND owner_id=?",(identifier,owner)).fetchone()
        return {"id":identifier,**dict(zip(("kind","filename","sha256","width","height","bytes","created"),row[:7])),"metadata":json.loads(row[7]),"temporary":False} if row else None

    def content(self,owner,identifier,thumbnail=False):
        """Authorize before reading either the original or its thumbnail derivative."""
        record=self.asset(owner,identifier)
        if not record:
            return None
        if record["temporary"]:
            return self.temporary[identifier]["thumbnail" if thumbnail else "content"]
        path=self.path(identifier,thumbnail)
        return path.read_bytes() if path.is_file() else None

    def listing(self,owner,*,kind=None,limit=100,before=None,job=None):
        """Return bounded private gallery metadata without loading full image bytes."""
        limit=max(1,min(int(limit),200))
        query="SELECT a.id FROM mirrorwall_assets a WHERE a.owner_id=?"
        values=[owner]
        if kind:
            query+=" AND a.kind=?"
            values.append(kind)
        if before is not None:
            query+=" AND a.created<?"
            values.append(float(before))
        if job:
            query+=" AND EXISTS (SELECT 1 FROM mirrorwall_job_assets j WHERE j.asset=a.id AND j.job=?)"
            values.append(job)
        query+=" ORDER BY a.created DESC LIMIT ?"
        values.append(limit)
        records=[self.asset(owner,identifier) for identifier, in self.db.execute(query,tuple(values)).fetchall()]
        if before is None:
            self.expire()
            records=[row["record"] for row in self.temporary.values() if row["owner"]==owner and (not kind or row["record"]["kind"]==kind) and (not job or row["record"]["metadata"].get("job")==job)]+records
        return sorted(records,key=lambda record:record["created"],reverse=True)[:limit]

    def delete(self,owner,identifier):
        """Delete a private asset only when no active image batch still references it."""
        record=self.asset(owner,identifier)
        if not record:
            return False
        if record["temporary"]:
            self.temporary.pop(identifier)
            return True
        active=self.db.execute("SELECT 1 FROM mirrorwall_job_assets j JOIN batches b ON b.id=j.job WHERE j.asset=? AND b.status IN ('in_progress','paused','cancelling') LIMIT 1",(identifier,)).fetchone()
        if active:
            raise ValueError("This image is used by an unfinished job; stop or finish that job first.")
        with self.db:
            self.db.execute("DELETE FROM mirrorwall_job_assets WHERE asset=?",(identifier,))
            self.db.execute("DELETE FROM mirrorwall_assets WHERE id=? AND owner_id=?",(identifier,owner))
        self.path(identifier).unlink(missing_ok=True)
        self.path(identifier,True).unlink(missing_ok=True)
        return True

    def bind_job(self,identifier,owner,model_id,model_name,payload,references=()):
        """Bind the existing durable batch to stable account/model identities."""
        for asset in references:
            if not self.asset(owner,asset):
                raise ValueError("There is no such reference image of yours.")
        with self.db:
            self.db.execute("INSERT INTO mirrorwall_jobs VALUES (?,?,?,?,?,?)",(identifier,owner,model_id or "",model_name,json.dumps(payload),time.time()))
            for asset in set(references):
                self.db.execute("INSERT INTO mirrorwall_job_assets VALUES (?,?,?)",(identifier,asset,"reference"))

    def owner(self,identifier):
        """Resolve stable ownership independently of mutable account names."""
        row=self.db.execute("SELECT owner_id FROM mirrorwall_jobs WHERE id=?",(identifier,)).fetchone()
        return row[0] if row else None

    def bind_upload(self,identifier,owner):
        """Bind new batch input files to stable identities before exposing their bytes."""
        with self.db:
            self.db.execute('INSERT INTO mirrorwall_uploads VALUES (?,?)',(identifier,owner))

    def visible(self,owner,row):
        """Protect image batches and their files across account renames/recreation."""
        owners=self.db.execute('SELECT owner_id FROM mirrorwall_jobs WHERE id=? UNION '
            'SELECT j.owner_id FROM mirrorwall_jobs j JOIN batches b ON b.id=j.id WHERE ? IN (b.input_file,b.output_file,b.error_file) UNION '
            'SELECT owner_id FROM mirrorwall_uploads WHERE file_id=?',(row['id'],row['id'],row['id'])).fetchall()
        if owners:
            return all(identifier==owner for identifier, in owners)
        return not row.get('endpoint','').startswith('/v1/images/')

    def jobs(self,owner,limit=40):
        """Return image batch IDs and saved settings for the signed-in owner."""
        rows=self.db.execute("SELECT id,model_id,model_name,payload,created FROM mirrorwall_jobs WHERE owner_id=? ORDER BY created DESC LIMIT ?",(owner,min(100,max(1,int(limit))))).fetchall()
        return [{"id":r[0],"model_id":r[1],"model":r[2],"settings":json.loads(r[3]),"created":r[4]} for r in rows]

    def preferences(self,owner,settings=None):
        """Persist only validated non-secret display preferences."""
        if settings is not None:
            if settings.keys()-{"adult_previews"} or type(settings.get("adult_previews",False)) is not bool:
                raise ValueError("Invalid image preferences.")
            with self.db:
                self.db.execute("INSERT INTO mirrorwall_preferences VALUES (?,?) ON CONFLICT(owner_id) DO UPDATE SET settings=excluded.settings",(owner,json.dumps(settings)))
        row=self.db.execute("SELECT settings FROM mirrorwall_preferences WHERE owner_id=?",(owner,)).fetchone()
        return json.loads(row[0]) if row else {"adult_previews":False}

    def presets(self,owner):
        """List private and published workflow presets as separate versioned records."""
        rows=self.db.execute("SELECT id,owner_id,name,version,model_id,model_name,settings,updated FROM mirrorwall_presets WHERE owner_id IN (?, '*') ORDER BY name",(owner,)).fetchall()
        return [{"id":r[0],"published":r[1]=="*","name":r[2],"version":r[3],"model_id":r[4],"model":r[5],"settings":json.loads(r[6]),"updated":r[7]} for r in rows]

    def preset_history(self,owner,identifier):
        """Read revisions only through a currently visible private/shared workflow."""
        if not self.db.execute("SELECT 1 FROM mirrorwall_presets WHERE id=? AND owner_id IN (?, '*')",(identifier,owner)).fetchone():
            return None
        return [{"version":r[0],"name":r[1],"model_id":r[2],"model":r[3],"settings":json.loads(r[4]),"updated":r[5]}
                for r in self.db.execute("SELECT version,name,model_id,model_name,settings,updated FROM mirrorwall_preset_versions WHERE id=? ORDER BY version DESC",(identifier,))]

    def save_preset(self,owner,name,model_name,model_id,settings,identifier=None,expected_version=None):
        """Create/update a preset while incrementing its version and preserving ownership."""
        if not isinstance(name,str) or not 1<=len(name.strip())<=100:
            raise ValueError("Preset name must contain 1–100 characters.")
        if identifier is not None and (not isinstance(identifier,str) or not re.fullmatch(r'workflow_[a-f0-9]{24}',identifier)):
            raise ValueError('Invalid workflow ID.')
        identifier=identifier or "workflow_"+secrets.token_hex(12)
        existing=self.db.execute("SELECT owner_id,version,model_id FROM mirrorwall_presets WHERE id=?",(identifier,)).fetchone()
        if existing and existing[0]!=owner:
            raise ValueError("There is no such workflow of yours.")
        if expected_version is not None and (type(expected_version) is not int or not existing or expected_version!=existing[1]):
            raise ValueError("This workflow changed elsewhere. Reload its latest version or save a copy.")
        if existing and existing[2] and existing[2]!=model_id:
            raise ValueError("This workflow belongs to another model identity. Save a copy for this model.")
        version=existing[1]+1 if existing else 1
        updated=time.time()
        with self.db:
            if existing:
                changed=self.db.execute("UPDATE mirrorwall_presets SET name=?,version=?,model_id=?,model_name=?,settings=?,updated=? WHERE id=? AND owner_id=? AND version=?",
                    (name.strip(),version,model_id or "",model_name,json.dumps(settings),updated,identifier,owner,existing[1]))
                if changed.rowcount!=1:
                    raise ValueError("This workflow changed elsewhere. Reload its latest version or save a copy.")
            else:
                self.db.execute("INSERT INTO mirrorwall_presets VALUES (?,?,?,?,?,?,?,?)",
                                (identifier,owner,name.strip(),version,model_id or "",model_name,json.dumps(settings),updated))
            self.db.execute("INSERT INTO mirrorwall_preset_versions VALUES (?,?,?,?,?,?,?)",
                            (identifier,version,name.strip(),model_id or "",model_name,json.dumps(settings),updated))
        return next(row for row in self.presets(owner) if row["id"]==identifier)
