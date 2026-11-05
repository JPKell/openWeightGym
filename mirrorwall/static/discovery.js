(() => {
  'use strict';
  const $ = name => document.getElementById('mw-' + name);
  const el = (tag,text,classes) => { const node=document.createElement(tag); if(text!==undefined)node.textContent=text;if(classes)node.className=classes;return node; };
  const message = error => { $('message').textContent=error||'';$('message').hidden=!error; };
  async function api(path,body) {
    const answer=await fetch(path,{method:body?'POST':'GET',headers:body?{'content-type':'application/json'}:{},body:body?JSON.stringify(body):undefined,cache:'no-store'});
    if(answer.redirected){location.href='/sign-in';throw new Error('Please sign in again.');}
    const value=await answer.json();if(!answer.ok)throw new Error(value.error?.message||'Discovery is unavailable.');return value;
  }
  const safe = callback => async event => { try{await callback(event);message('');}catch(error){message(error.message);} };
  async function refreshPreferences() {
    const value=await api('/mirrorwall-api/preferences');
    if($('adult')){$('adult-control').hidden=!value.adult_previews_allowed;$('adult').checked=Boolean(value.adult_previews);}
  }
  if($('adult'))$('adult').onchange=safe(async()=>{try{await api('/mirrorwall-api/preferences',{adult_previews:$('adult').checked});}finally{await refreshPreferences();}$('found').replaceChildren();$('status').textContent='Preview preference saved. Search to refresh results.';});
  let found=[];
  function renderResults() {
    const shown=found.filter(one=>(!$('type').value||one.kind===$('type').value)&&(!$('base').value||one.base_model===$('base').value));
    $('found').replaceChildren();$('status').textContent=shown.length+' of '+found.length+' returned versions';
    shown.forEach(one=>{
      const column=el('div',undefined,'col-sm-6 col-lg-4 col-xl-3');const card=$('discovery-card').content.firstElementChild.cloneNode(true);card.querySelector('.card-heading').textContent=one.name||'Unnamed model version';
      if(one.preview){const image=el('img');image.className='card-img-top';image.style.cssText='height:220px;object-fit:cover';image.src=one.preview;image.alt=one.name;image.referrerPolicy='no-referrer';image.loading='lazy';card.prepend(image);}
      const body=card.querySelector('[data-discovery-body]');body.append(el('p',(one.kind||'Type unavailable')+' · '+(one.base_model||'Base model unavailable'),'text-secondary small'));
      if(one.trigger_words?.length)body.append(el('p','Trigger words: '+one.trigger_words.join(', '),'small'));
      const source=el('a','Model page','btn btn-link btn-sm ps-0');source.href=one.url;source.target='_blank';source.rel='noopener noreferrer';body.append(source);
      const details=el('button','Choose file','btn btn-outline-primary btn-sm');const files=el('div',undefined,'mt-3');
      details.onclick=safe(async()=>{
        const value=await api('/mirrorwall-discovery?repository='+encodeURIComponent(one.repository));files.replaceChildren();
        value.files=value.files.filter(file=>!$('format').value||file.file.toLowerCase().endsWith($('format').value));
        if(!value.files.length){files.append(el('p','No supported file with a SHA-256 checksum is available.','small text-secondary'));return;}
        // Downloads are kept in storage alone; the server refuses a cache destination as well.
        const place=el('select',undefined,'form-select form-select-sm mb-2');value.places.filter(row=>['storage_image','storage_encoders'].includes(row.name)).forEach(row=>place.add(new Option(({storage_image:'Image storage',storage_encoders:'Encoder storage'})[row.name],row.name)));place.ariaLabel='Download destination';const label=el('label','Download destination','form-label small w-100');label.append(place);
        files.append(label,el('p',place.options.length?'Downloads go into storage, never the cache. Files are verified and promoted to the cache before use. Encoder storage keeps detached text encoders.':'Downloads go into model storage, never the cache. Set a model storage folder in Settings → Model file locations to download files.','small text-secondary'));
        value.files.forEach(file=>{const button=el('button',file.file+' · '+(file.size/2**30).toFixed(2)+' GiB','btn btn-outline-secondary btn-sm w-100 mb-2 text-break');button.disabled=!place.options.length;
          button.onclick=safe(async()=>{await api('/mirrorwall-discovery',{repository:one.repository,files:[file.file],place:place.value});$('success').textContent='Download started. Progress appears below and on Files.';$('success').hidden=false;});files.append(button);});
      });body.append(details,files);column.append(card);$('found').append(column);
    });
  }
  for(const name of ['type','base','format'])$(name).onchange=renderResults;
  $('search').onsubmit=safe(async event=>{
    event.preventDefault();$('status').textContent='Searching…';$('found').replaceChildren();found=[];
    await refreshPreferences();
    const value=await api('/mirrorwall-discovery?q='+encodeURIComponent($('q').value));
    const invalid=!Array.isArray(value.found)||value.found.some(one=>
      !one||typeof one.name!=='string'||typeof one.repository!=='string'||!one.repository.startsWith('civitai/'));
    if(invalid){$('status').textContent='Search failed.';throw new Error('Civitai returned incomplete model data. Please retry the search.');}
    found=value.found;
    for(const [name,key,title] of [['type','kind','All types'],['base','base_model','All base models']]){
      const selected=$(name).value;$(name).replaceChildren(new Option(title,''));
      [...new Set(found.map(row=>row[key]).filter(Boolean))].sort().forEach(value=>$(name).add(new Option(value,value)));
      if([...$(name).options].some(row=>row.value===selected))$(name).value=selected;
    }
    renderResults();
  });
  refreshPreferences().catch(error=>message(error.message));
})();
