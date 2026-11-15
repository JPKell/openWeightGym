/* Starting values come from the same Python profile contracts as request validation. */
window.SuiteVideoProfiles=(()=>{
  'use strict';
  const profiles=JSON.parse(document.getElementById('image-studio')?.dataset.videoProfiles || '{}');
  const labels=Object.fromEntries(Object.entries(profiles).map(([key,value])=>[key,value.label]));
  const spec=family=>({...profiles[family],label:labels[family] || family || 'Video model',negative:true,guidance:true,strength:false,alignment:family==='minimax-h3'?32:16});
  function help(container,family) {
    container.replaceChildren();container.hidden=false;
    const note=document.createElement('p');note.className='small text-secondary mb-0';
    note.textContent='Video profiles use exclusive GPU admission and batch priority. Inputs are enabled by measured runtime capabilities. Start with a short clip and qualify cancellation and browser playback.';
    container.append(note);
  }
  return {spec,help,labels};
})();
