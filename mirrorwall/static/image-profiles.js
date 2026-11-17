/* Family-aware controls shared by model setup, workflows and image generation. */
window.SuiteImageProfiles = (() => {
  'use strict';
  const labels={'flux2-klein':'Flux 2 klein','flux2-dev':'Flux 2 Dev',sdxl:'SDXL','qwen-image':'Qwen Image','qwen-image-2.1':'Qwen Image 2.1','moss-image':'MOSS-Image (Base / SFT)',krea2:'Krea 2 (Raw / Turbo)'};
  function spec(family) {
    const qwen=family?.startsWith('qwen-image'), qwen21=family==='qwen-image-2.1', moss=family==='moss-image', krea=family==='krea2';
    return {label:labels[family] || family || 'Image model',qwen,moss,krea,textOnly:qwen || moss || krea,negative:family==='sdxl' || qwen || moss || krea,
      guidance:family==='flux2-dev',strength:family==='sdxl',alignment:qwen21?32:16,
      defaults:{width:1024,height:1024,n:1,seed:-1,upscale:1,sampler:'euler',
        scheduler:qwen21?'flux':family==='sdxl' || qwen || moss || krea?'discrete':'flux2',
        steps:qwen || moss?50:family==='sdxl' || family==='flux2-dev'?28:krea?20:6,
        cfg_scale:qwen21?6:qwen?4:moss?3:krea?3.5:family==='sdxl'?7:1,guidance:family==='flux2-dev'?4:1}};
  }
  function help(container,family,apply) {
    container.replaceChildren(); const one=spec(family); container.hidden=!one.textOnly;
    if(!one.textOnly) return;
    const p=document.createElement('p'); p.className='small text-secondary mb-2';
    p.textContent=one.krea?
      'Krea 2 Raw / Turbo: use Qwen3-VL 4B and the Wan 2.1 VAE (Comfy ships it as qwen_image_vae). The text-only Qwen3 4B used by Flux 2 klein does not fit. Output dimensions use multiples of 16.':one.moss?
      'MOSS-Image Base / SFT: use Qwen3.5 9B, the external MOSS tokenizer JSON and the Flux 2 VAE. Output dimensions use multiples of 16.':family==='qwen-image-2.1'?
      'Qwen Image 2.1: use Qwen3-VL 8B and the Qwen Image 2.1 VAE. Output dimensions use multiples of 32. Earlier Qwen Image VAEs and text-only Qwen3 encoders are not interchangeable.':
      'Qwen Image (original / 2512): use Qwen2.5-VL 7B and the Qwen Image VAE. A Qwen3 encoder used by Flux 2 is a different component.';
    container.append(p);
    const note=document.createElement('p'); note.className='small text-secondary mb-2';
    note.textContent=one.krea?'Text-to-image generation with CFG and negative prompts. Raw starts at 20 Euler steps and CFG 3.5; Turbo at 8 steps and CFG 1, where negative prompts have no effect. Reference editing needs a community LoRA and is not enabled by this profile.':one.moss?'Text-to-image generation with CFG and negative prompts. Start with 50 Euler steps and CFG 3. Prompt enhancement is optional and is not applied automatically.':
      'Text-to-image generation only. Qwen Image Edit, Layered and Turbo recipes need additional settings and are not enabled by this profile. Start with one image and measure GPU/RAM use on your node.';
    container.append(note);
    const link=document.createElement('a'); link.className='small'; link.textContent=one.krea?'Krea 2 setup reference':one.moss?'MOSS setup reference':'Qwen setup reference';
    link.href='https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/'+(one.krea?'krea2.md':one.moss?'moss_image.md':family==='qwen-image-2.1'?'qwen_image_2.1.md':'qwen_image.md');
    container.append(link);
    if(apply) {
      const button=document.createElement('button'); button.type='button'; button.className='btn btn-outline-secondary btn-sm ms-2';
      button.textContent=`Suggest ${one.krea?'Raw · ':''}1024² · ${one.defaults.steps} steps · CFG ${one.defaults.cfg_scale}`;
      button.onclick=()=>apply({...one.defaults}); container.append(button);
      if(one.krea) {
        const turbo=button.cloneNode(); turbo.textContent='Suggest Turbo · 1024² · 8 steps · CFG 1';
        turbo.onclick=()=>apply({...one.defaults,steps:8,cfg_scale:1}); container.append(turbo);
      }
    }
  }
  return {spec,help,labels};
})();
