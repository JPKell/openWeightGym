/* Family-aware controls shared by model setup, workflows and image generation. */
window.SuiteImageProfiles = (() => {
  'use strict';
  const labels={'flux2-klein':'Flux 2 klein','flux2-dev':'Flux 2 Dev',sdxl:'SDXL','qwen-image':'Qwen Image','qwen-image-2.1':'Qwen Image 2.1'};
  function spec(family) {
    const qwen=family?.startsWith('qwen-image'), qwen21=family==='qwen-image-2.1';
    return {label:labels[family] || family || 'Image model',qwen,negative:family==='sdxl' || qwen,
      guidance:family==='flux2-dev',strength:family==='sdxl',alignment:qwen21?32:16,
      defaults:{width:1024,height:1024,n:1,seed:-1,upscale:1,sampler:'euler',
        scheduler:qwen21?'flux':family==='sdxl' || qwen?'discrete':'flux2',
        steps:qwen?50:family==='sdxl' || family==='flux2-dev'?28:6,
        cfg_scale:qwen21?6:qwen?4:family==='sdxl'?7:1,guidance:family==='flux2-dev'?4:1}};
  }
  function help(container,family,apply) {
    container.replaceChildren(); const one=spec(family); container.hidden=!one.qwen;
    if(!one.qwen) return;
    const p=document.createElement('p'); p.className='small text-secondary mb-2';
    p.textContent=family==='qwen-image-2.1'?
      'Qwen Image 2.1: use Qwen3-VL 8B and the Qwen Image 2.1 VAE. Output dimensions use multiples of 32. Earlier Qwen Image VAEs and text-only Qwen3 encoders are not interchangeable.':
      'Qwen Image (original / 2512): use Qwen2.5-VL 7B and the Qwen Image VAE. A Qwen3 encoder used by Flux 2 is a different component.';
    container.append(p);
    const note=document.createElement('p'); note.className='small text-secondary mb-2';
    note.textContent='Text-to-image generation only. Qwen Image Edit, Layered and Turbo recipes need additional settings and are not enabled by this profile. Start with one image and measure GPU/RAM use on your node.';
    container.append(note);
    const link=document.createElement('a'); link.className='small'; link.textContent='Qwen setup reference';
    link.href='https://github.com/leejet/stable-diffusion.cpp/blob/95ff76230889898e1bf40058ba272a3699b50dc6/docs/'+(family==='qwen-image-2.1'?'qwen_image_2.1.md':'qwen_image.md');
    container.append(link);
    if(apply) {
      const button=document.createElement('button'); button.type='button'; button.className='btn btn-outline-secondary btn-sm ms-2';
      button.textContent=`Suggest 1024² · ${one.defaults.steps} steps · CFG ${one.defaults.cfg_scale}`;
      button.onclick=()=>apply({...one.defaults}); container.append(button);
    }
  }
  return {spec,help,labels};
})();
