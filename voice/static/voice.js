/* The gateway reserves a connection and binds it to this browser with an HttpOnly cookie.
   WeightRoom serves this same page under /voice, for its signed-in person: the script tag's data-base says where. */
const $=id=>document.getElementById(id);
const base=document.currentScript?.dataset.base||'';
let socket,context,node,microphone,source,muted=false,ready=false;
let playback,queueClearAck=false;
let configuration={},failure='',modelChosen=false;
const speechModelsChosen=new Set();
const failures={busy:'The voice service is busy. Try again after the current call finishes.',unauthorized:'Your account cannot use this voice or model.',
  unavailable:'The speech service could not start. Check its status under System → Voice.',model_unavailable:'A selected model is unavailable. Choose another model or check the voice profile.',
  speech_service_permissions:'The speech service cannot load or hold its models. An administrator must restore its permissions.',
  invalid_options:'Some voice settings are not supported by the selected speech engine.',invalid_voice:'Choose a saved voice available to your account.',
  startup_timeout:'The selected models did not become ready in time. Choose a smaller conversation model, or wait for active GPU work to finish.',
  mode_unavailable:'This speech engine does not support that conversation mode.',invalid_profile:'The selected speech engine is no longer configured.'};
/** Build an accessible call control with the same markup in both voice pages. */
function control(id,title,type='select',attributes={}){
  const label=document.createElement('label');label.textContent=title;label.htmlFor=id;
  if($('call-settings').classList.contains('row'))label.className='col-12 col-sm-6 form-label mb-0';
  const input=document.createElement(type==='select'?'select':type==='textarea'?'textarea':'input');input.id=id;
  input.setAttribute('aria-label',title);
  if(!['select','textarea'].includes(type))input.type=type;
  Object.assign(input,attributes);input.className=type==='select'?'form-select':type==='range'?'form-range':'form-control';
  label.append(input);
  if(type==='range'){
    const value=document.createElement('output');value.setAttribute('aria-hidden','true');
    const update=()=>{value.value=id==='call-volume'?Math.round(Number(input.value)*100)+'%':Number(input.value).toFixed(1)+'×';input.setAttribute('aria-valuetext',value.value);};
    input.addEventListener('input',update);update();label.append(value);
  }
  $('call-settings').append(label);return input;
}
/** Replace public choices while preserving the user's current valid selection. */
function choices(id,values,wanted){
  const input=$(id),old=input.value;input.replaceChildren(...values.map(value=>new Option(value.name??value,value.id??value)));
  input.value=[...input.options].some(option=>option.value===old)?old:String(wanted??input.options[0]?.value??'');
  if(input.selectedIndex<0&&input.options.length)input.selectedIndex=0;
}
/** Apply the selected engine's public defaults and hide controls it cannot use. */
function engineControls(){
  if(!$('call-profile'))return;
  const mode=$('mode').value;
  for(const option of $('call-profile').options){const profile=configuration.profiles.find(p=>p.id===option.value);option.disabled=!(profile.modes||['direct']).includes(mode);}
  if($('call-profile').selectedOptions[0]?.disabled)$('call-profile').value=[...$('call-profile').options].find(option=>!option.disabled)?.value||'';
  const profile=configuration.profiles.find(p=>p.id===$('call-profile').value)||{},cloned=profile.speech==='audiocpp';
  $('voice-choice').hidden=$('record-choice').hidden=!cloned;
  $('call-speaker').parentElement.hidden=cloned;
  for(const id of ['call-synthesis','call-recognition','call-pause'])$(id).parentElement.hidden=!cloned;
  for(const id of ['call-model','call-length','call-instructions'])$(id).parentElement.hidden=mode!=='direct';
  if(!modelChosen)$('call-model').value='';
  choices('call-model',configuration.models||[],profile.model);
  for(const id of ['call-synthesis','call-recognition'])if(!speechModelsChosen.has(id))$(id).value='';
  choices('call-synthesis',configuration.synthesis_models||[],profile.synthesis_model);
  choices('call-recognition',configuration.recognition_models||[],profile.recognition_model);
  choices('call-speaker',[{id:'',name:'Default voice'},...(configuration.speakers||[])],profile.speaker);
  if(cloned)choices('voice',configuration.voices||[]);
}
/** Enumerate only device labels the browser permits this page to see. */
async function microphones(){
  if(!$('call-microphone')||!navigator.mediaDevices?.enumerateDevices)return;
  const devices=await navigator.mediaDevices.enumerateDevices();
  choices('call-microphone',[{id:'',name:'System default'},...devices.filter(d=>d.kind==='audioinput'&&d.deviceId!=='default').map((d,index)=>({id:d.deviceId,name:d.label||'Microphone '+(index+1)}))]);
}
/** Add per-call choices and local microphone/playback controls without changing server defaults. */
function settingsControls(){
  if(!$('call-settings')||!configuration.profiles?.length)return;
  control('call-profile','Speech engine');choices('call-profile',configuration.profiles.map(p=>({id:p.id,name:p.speech==='audiocpp'?'Saved voice · '+p.id:'CPU voices · '+p.id})),configuration.default_profile);
  control('call-model','Conversation model');control('call-synthesis','Speech model');control('call-recognition','Recognition model');control('call-speaker','Built-in voice');
  control('call-speed','Speaking speed','select');choices('call-speed',[.5,.75,1,1.25,1.5,2].map(value=>({id:String(value),name:value+'×'})),'1');
  control('call-length','Reply length','select');choices('call-length',[{id:'64',name:'Brief · 64 tokens'},{id:'128',name:'Standard · 128 tokens'},{id:'256',name:'Longer · 256 tokens'},{id:'512',name:'Detailed · 512 tokens'}],'128');
  control('call-pause','Pause before reply (ms)','number',{min:300,max:3000,step:100,value:700});
  control('call-microphone','Microphone');choices('call-microphone',[{id:'',name:'System default'}]);
  control('call-gain','Microphone gain','range',{min:.5,max:3,step:.1,value:1});
  control('call-volume','Playback volume','range',{min:0,max:1,step:.05,value:1});
  const instructions=control('call-instructions','Instructions for this conversation','textarea',{rows:2,maxLength:4000,placeholder:'Optional: tell the model how you want it to reply.'});instructions.parentElement.classList.remove('col-sm-6');
  $('call-profile').onchange=engineControls;$('mode').onchange=engineControls;
  $('call-model').onchange=()=>{modelChosen=true;};
  for(const id of ['call-synthesis','call-recognition'])$(id).onchange=()=>speechModelsChosen.add(id);
  $('call-gain').oninput=()=>node?.port.postMessage({type:'input.gain',value:Number($('call-gain').value)});
  $('call-volume').oninput=()=>playback?.setVolume?.(Number($('call-volume').value));
  const wanted=new URLSearchParams(location.search).get('voice');
  if(wanted&&configuration.cloned_profile)$('call-profile').value=configuration.cloned_profile;
  engineControls();
  if(wanted&&[...$('voice').options].some(option=>option.value===wanted))$('voice').value=wanted;
  microphones().catch(()=>{});
}
/** Validate local choices and shape the options handed to the existing voice-session API. */
function sessionChoices(){
  if(!$('call-profile'))return $('voice')?.value?{voice:$('voice').value,record:$('record').checked}:{};
  const profile=configuration.profiles.find(p=>p.id===$('call-profile').value),cloned=profile?.speech==='audiocpp';
  const options={speed:Number($('call-speed').value)};
  if($('mode').value==='direct'){
    if(!$('call-model').value)throw Error('Choose an available conversation model.');
    options.model=$('call-model').value;options.max_output_tokens=Number($('call-length').value);
    if($('call-instructions').value.trim())options.system_prompt=$('call-instructions').value.trim();
  }
  if(cloned){
    if(!$('voice').value||!$('call-synthesis').value||!$('call-recognition').value)throw Error('Choose a saved voice, speech model and recognition model.');
    options.synthesis_model=$('call-synthesis').value;options.recognition_model=$('call-recognition').value;options.trailing_silence_ms=Number($('call-pause').value);
  }else if($('call-speaker').value)options.speaker=Number($('call-speaker').value);
  return {profile:profile.id,options,...(cloned?{voice:$('voice').value,record:$('record').checked}:{})};
}
/** Update the visible voice connection status using plain text. */
const status=text=>$('status').textContent=text;
/** Send one control event only while the voice socket is open. */
function send(value){if(socket?.readyState===1)socket.send(JSON.stringify(value));}
/** Append a labeled transcript entry as text rather than active markup. */
function line(who,text){const div=document.createElement('div');div.className='line';const label=document.createElement('small');label.textContent=who;div.append(label,document.createTextNode(text));$('transcript').append(div);}
/** Stop microphone capture, playback and the current voice connection. */
async function close(){
  ready=false;queueClearAck=false;send({type:'session.cancel'});
  const previousSocket=socket;socket=null;if(previousSocket){previousSocket.onclose=previousSocket.onerror=previousSocket.onmessage=null;previousSocket.close();}
  microphone?.getTracks().forEach(t=>t.stop());microphone=null;
  playback?.close();playback=null;
  node?.disconnect();source?.disconnect();node=source=null;const previous=context;context=null;if(previous&&previous.state!=='closed')await previous.close();
  for(const id of ['mute','hangup','finish','interrupt'])$(id).disabled=true;
  $('connect').disabled=false;status('Disconnected');
  $('echo').disabled=false;if($('voice'))$('voice').disabled=false;
  if($('record'))$('record').disabled=false;
  $('mode').disabled=false;for(const input of $('call-settings')?.querySelectorAll('input,select,textarea')||[])input.disabled=false;
}
$('login').onsubmit=async event=>{
  event.preventDefault();$('connect').disabled=true;failure='';status('Connecting');$('notice').textContent='Preparing microphone…';
  try{
    const selected=sessionChoices();
    if(!window.isSecureContext)throw Error('Open this page over HTTPS, or on localhost. Browsers require a secure connection for the microphone.');
    context=new AudioContext({latencyHint:'interactive'});await context.resume();
    await context.audioWorklet.addModule(base+'/static/audio.js');
    node=new AudioWorkletNode(context,'voice-audio',{outputChannelCount:[1]});
    node.port.postMessage({type:'input.gain',value:Number($('call-gain')?.value||1)});
    const cancelEcho=$('echo').checked;
    if(cancelEcho && !navigator.mediaDevices.getSupportedConstraints().echoCancellation)throw Error('This browser does not support echo cancellation. Use a supported browser, or headphones with echo cancellation switched off.');
    microphone=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:{exact:cancelEcho},noiseSuppression:cancelEcho,autoGainControl:false,
      ...($('call-microphone')?.value?{deviceId:{exact:$('call-microphone').value}}:{})}});
    await microphones();
    const actual=microphone.getAudioTracks()[0].getSettings().echoCancellation;
    if(cancelEcho && actual===false)throw Error('The microphone did not enable echo cancellation. Try another browser or audio device.');
    $('audio-status').textContent=cancelEcho ? (actual ? 'Echo cancellation active · voice interruption enabled' : 'Echo cancellation requested; browser does not report its status · voice interruption enabled') : 'Echo cancellation off · use headphones';
    $('echo').disabled=true;if($('voice'))$('voice').disabled=true;
    if($('record'))$('record').disabled=true;
    playback=await echoPlayback(context,node);
    playback.setVolume?.(Number($('call-volume')?.value??1));
    $('mode').disabled=true;for(const input of $('call-settings')?.querySelectorAll('input,select,textarea')||[])input.disabled=!['call-gain','call-volume'].includes(input.id);
    source=context.createMediaStreamSource(microphone);source.connect(node);
    node.port.onmessage=({data:m})=>{
      if(m.type==='input' && ready){
        if(socket?.bufferedAmount>16000){$('notice').textContent='Connection too slow. Reconnect and repeat your last sentence.';close();return;}
        // Keep the recognizer sample clock moving while muted without sending microphone samples.
        if(socket?.readyState===1)socket.send(muted ? new ArrayBuffer(m.pcm.byteLength) : m.pcm);
      }else if(m.type==='playback.progress' || (m.type==='playback.cleared' && queueClearAck)){send(m);}
      else if(m.type==='overload'){$('notice').textContent='Playback could not keep up. Please reconnect.';close();}
    };
    const response=await fetch(base+'/browser/sessions',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({conversation_mode:$('mode').value,...selected})});
    const result=await response.json();
    if(!response.ok)throw Error(result.error?.message||result.error||'Voice could not start');
    const connection=new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}${base}/browser/sessions/${result.session_id}/stream`);socket=connection;socket.binaryType='arraybuffer';
    muted=false;$('mute').textContent='Mute';
    socket.onmessage=({data})=>{
      if(data instanceof ArrayBuffer){node.port.postMessage({type:'audio',pcm:data},[data]);return;}
      const m=JSON.parse(data);
      if(m.type==='session.ready'){queueClearAck=m.queue_clear_ack===true;ready=true;node.port.postMessage({type:'capture',on:true});status('Listening');$('notice').textContent='Speak naturally. You can interrupt an answer.';for(const id of ['mute','finish','interrupt'])$(id).disabled=false;}
      if(m.type==='turn.started'){node.port.postMessage({type:'turn',turn:m.turn});if(m.turn>=0)status('Thinking');}
      if(m.type==='playback.segment'){node.port.postMessage({...m,type:'segment'});status('Speaking');if(m.text)line('Assistant',m.text);}
      if(m.type==='playback.clear'){node.port.postMessage({type:'clear',turn:m.turn});status('Listening');}
      if(m.type==='transcript.final')line('You',m.text);
      if(m.type==='turn.completed' && ready)status('Listening');
      if(m.type==='session.state')status(m.state.replaceAll('_',' '));
      if(m.type==='session.error'){failure=failures[m.code]||'The turn could not finish. Please try again or reconnect.';$('notice').textContent=failure;}
    };
    socket.onclose=async event=>{if(socket!==connection)return;const reason=failure||(event.code===1008?'The call was refused or its sign-in expired. Reload this page and try again.':'The voice connection ended. You can reconnect.');await close();$('notice').textContent=reason;};
    socket.onerror=()=>{if(socket===connection)$('notice').textContent='The voice connection could not be opened. Please reconnect.';};
    $('hangup').disabled=false;
  }catch(error){await close();$('notice').textContent=error.message;}
};
$('hangup').onclick=close;
$('mute').onclick=()=>{muted=!muted;$('mute').textContent=muted?'Unmute':'Mute';};
$('finish').onclick=()=>send({type:'input.end'});
$('interrupt').onclick=()=>{node?.port.postMessage({type:'clear'});send({type:'session.cancel',interrupt:true});};
fetch(base+'/browser/config').then(async r=>{const value=await r.json();if(!r.ok)throw Error(value.error?.message||value.error||'Voice settings are unavailable.');return value;}).then(c=>{
  configuration=c;
  for(const option of $('mode').options)option.disabled=!c.modes.includes(option.value);
  // SoundRoom voices, when the gateway offers them: the call then uses audio.cpp speech in that voice.
  if(!$('voice'))return;
  for(const voice of c.voices||[])$('voice').add(new Option(voice.name,voice.id));
  $('voice-choice').hidden=$('record-choice').hidden=!(c.voices||[]).length;
  const wanted=new URLSearchParams(location.search).get('voice');
  if(wanted&&[...$('voice').options].some(o=>o.value===wanted))$('voice').value=wanted;
  if(new URLSearchParams(location.search).get('record')==='1')$('record').checked=true;
  settingsControls();if(c.configuration_error)$('notice').textContent=c.configuration_error;
}).catch(error=>{$('notice').textContent=error.message;});
