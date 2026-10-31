"""Verified encoder relocation with durable recovery and unchanged historical evidence.

Run only after every configured node has reported usage and affected services have drained.
The manifest is data, never executable instructions. Current file revisions intentionally
change; old path-keyed measurements and benchmark fingerprints remain historical.
"""
import json
import os
from pathlib import Path
import shutil

from loadcoach.files import SPARE, carry, sha256_of
from turnstile.model_configuration import relocate


def manifest(nodes, models):
    """Plan complete encoder packages from configured consumers and metadata, fail closed."""
    if not nodes or any(not node.get('answering') for node in nodes):
        raise ValueError('Every configured node must report its files and usage before relocation.')
    signatures = {}
    for node in nodes:
        for model in models.get(node['name'], []):
            settings = model.get('settings',model)
            if settings.get('flags',{}).get('embedding'):
                file = next((row for row in node['files'] if row['path']==settings['flags']['model']),None)
                if file and model.get('sha256'):
                    facts=file.get('facts',{})
                    signatures[(file['size'],facts.get('name'),facts.get('architecture'))]=model['sha256']
    result = {'version':1, 'state':'planned', 'nodes':[]}
    for node in nodes:
        entries, consumers = {}, []
        places = {place['name']:place for place in node['places']}
        for model in models.get(node['name'], []):
            settings = model.get('settings', model)
            selected = []
            if settings.get('engine','llama_cpp')=='llama_cpp' and settings.get('flags',{}).get('embedding'):
                selected.append(settings['flags']['model'])
            if settings.get('engine')=='sdcpp':
                selected.extend(path for slot,path in settings['sdcpp'].get('components',{}).items() if slot in ('text_encoder','clip_l','clip_g'))
            for old in selected:
                source = next((row for row in node['files'] if row['path']==old),None)
                if source is None or source.get('moving'):
                    raise ValueError('An encoder is unavailable or moving: '+old)
                source_place = places[source['place']]
                root = Path(source_place['folder'])
                if root.name=='encoders':
                    continue
                target_root = root.parent/'encoders'
                relative = Path(old).relative_to(root)
                # Nested model directories are complete packages, including tokenizer/config files.
                package = relative.parts[0] if len(relative.parts)>1 else None
                bundle = [row for row in node['files'] if row['place']==source['place'] and
                          (Path(row['path']).relative_to(root).parts[0]==package if package else row['path']==old)]
                for file in bundle:
                    if file.get('moving'):
                        raise ValueError('A package file is moving: '+file['path'])
                    new = str(target_root/Path(file['path']).relative_to(root))
                    previous = entries.get(file['path'])
                    entry = {'old':file['path'], 'new':new, 'size':file['size'], 'place':source['place'], 'sha256':None}
                    if previous and previous['new']!=new:
                        raise ValueError('Conflicting encoder destinations.')
                    entries[file['path']]=entry
            if selected:
                consumers.append({'name':model['name'], 'model_id':model.get('model_id'), 'before':settings})
        for file in node['files']:
            facts=file.get('facts',{})
            signature=(file['size'],facts.get('name'),facts.get('architecture'))
            image_encoder=facts.get('image_component') or facts.get('kind') in ('text_encoder','clip_l','clip_g')
            if signature not in signatures and not image_encoder:
                continue
            root=Path(places[file['place']]['folder'])
            if root.name=='encoders':
                continue
            if file.get('moving'):
                raise ValueError('An encoder-library file is moving.')
            entries.setdefault(file['path'],{'old':file['path'],'new':str(root.parent/'encoders'/Path(file['path']).relative_to(root)),
                'size':file['size'],'place':file['place'],'sha256':None,'expected_sha256':signatures.get(signature)})
        result['nodes'].append({'name':node['name'], 'files':list(entries.values()), 'consumers':consumers})
    return result


def save(path, value):
    """Atomically retain progress before changing references or deleting superseded files."""
    target = Path(path)
    target.parent.mkdir(parents=True,exist_ok=True)
    temporary = target.with_suffix('.tmp')
    with temporary.open('w') as stream:
        stream.write(json.dumps(value,indent=2)+'\n')
        stream.flush()
        os.fsync(stream.fileno())
    temporary.chmod(0o600)
    os.replace(temporary,target)


def prepare(node, record):
    """Verify conflicts and room before copying; preserve originals until native checks pass."""
    allocations = {}
    for row in node['files']:
        source,target = Path(row['old']),Path(row['new'])
        if source.is_symlink() or not source.is_file() or source.stat().st_size!=row['size']:
            raise ValueError('Encoder source changed or is not a regular file: '+str(source))
        row['sha256']=sha256_of(str(source))
        if row.get('expected_sha256') and row['sha256']!=row['expected_sha256']:
            raise ValueError('A proposed library copy differs from its configured encoder.')
        if target.exists():
            if target.is_symlink() or not target.is_file() or sha256_of(str(target))!=row['sha256']:
                raise ValueError('Conflicting encoder destination: '+str(target))
        else:
            parent=target.parent
            while not parent.exists():
                parent=parent.parent
            device=parent.stat().st_dev
            allocations.setdefault(device,{'folder':parent,'bytes':0})['bytes']+=row['size']
    for allocation in allocations.values():
        if shutil.disk_usage(allocation['folder']).free < allocation['bytes']+SPARE:
            raise ValueError('Insufficient space for the complete encoder package.')
    save(record,node)
    for row in node['files']:
        source,target=Path(row['old']),Path(row['new'])
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            carry(source,target,{'of':row['size'],'done':0})
        if sha256_of(str(target))!=row['sha256']:
            raise ValueError('Encoder copy failed SHA-256 verification.')
        row['copied']=True
        save(record,node)
    return node


def rewrite(node, library, record, rollback=False):
    """Use the model store's normal revision authority; never rewrite retained measurements."""
    paths={row['old']:row['new'] for row in node['files']}
    for consumer in node['consumers']:
        before=consumer['before']
        after=relocate(before,paths)
        if library.identity(consumer['name'])!=consumer['model_id'] or library.stored(consumer['name']) not in (before,after):
            raise ValueError('A consumer changed after the manifest was prepared: '+consumer['name'])
    for consumer in node['consumers']:
        library.put(consumer['name'],consumer['before'] if rollback else relocate(consumer['before'],paths))
    node['references']='restored' if rollback else 'relocated'
    save(record,node)


def finish(node, record):
    """Remove only verified superseded files after every consumer passed native checks."""
    if not node.get('verified_consumers') or set(node['verified_consumers'])!={one['name'] for one in node['consumers']}:
        raise ValueError('Every affected consumer needs a recorded native verification before removal.')
    for row in node['files']:
        if Path(row['new']).is_symlink() or sha256_of(row['new'])!=row['sha256']:
            raise ValueError('The installed encoder changed; preserve its original.')
        source=Path(row['old'])
        if source.exists() and sha256_of(str(source))!=row['sha256']:
            raise ValueError('The old encoder changed; do not remove it.')
    for row in node['files']:
        Path(row['old']).unlink(missing_ok=True)
        row['removed']=True
        save(record,node)
    node['state']='complete'
    save(record,node)


def restore_files(node, record):
    """Restore old paths from verified new files before restoring consumer references."""
    for row in node['files']:
        source,target=Path(row['new']),Path(row['old'])
        if source.is_symlink() or target.is_symlink() or sha256_of(str(source))!=row['sha256']:
            raise ValueError('The recovery source failed verification.')
        target.parent.mkdir(parents=True,exist_ok=True)
        if not target.exists():
            carry(source,target,{'of':row['size'],'done':0})
        if sha256_of(str(target))!=row['sha256']:
            raise ValueError('The old path conflicts with verified recovery data.')
    save(record,node)
