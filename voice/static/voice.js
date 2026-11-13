/* The gateway reserves a connection and binds it to this browser with an HttpOnly cookie.
   WeightRoom serves this same page under /voice, for its signed-in person: the script tag's data-base says where. */
(() => {
const $=id=>document.getElementById(id);
const base=document.currentScript?.dataset.base||'';
let socket,context,node,microphone,source,muted=false,ready=false;
let playback,queueClearAck=false;
let configuration={},failure='',modelChosen=false;
const speechModelsChosen=new Set();
const studio=!!$('voice-workspace'),speakerDrafts=new Map();
let callLocked=false,configurationReady=false,disposed=false,clock,startedAt=0;
let activeTurn=null,partialMessage=null,assistantMessage=null,following=true;
const transcriptRows=[];
let callLanguages={input:'',output:''};
let startAttempt=0,startRequest=null;
const segments=new Map(),completedTurns=new Set(),stoppedTurns=new Set();

function languageIssue(){
  if(!studio||!$('call-profile'))return '';
  const profile=configuration.profiles.find(p=>p.id===$('call-profile').value)||{};
  const audio=profile.speech==='audiocpp',models=configuration.audio_models||[];
  for(const [field,language] of [['call-recognition','listen-language'],['call-synthesis','response-language']]){
    if(language==='response-language'&&$('mode').value!=='direct')continue;
    const code=$(language).value,model=models.find(m=>m.id===$(field).value);
    const allowed=audio?model?.languages||[]:['en'];
    if(code&&allowed.length&&!allowed.includes(code))return 'The selected speech model does not support '+$(language).selectedOptions[0]?.textContent+'. Choose another model or language.';
  }
  return '';
}
function languageControls(){
  if(!studio)return;
  const profile=configuration.profiles.find(p=>p.id===$('call-profile').value)||{},audio=profile.speech==='audiocpp',direct=$('mode').value==='direct';
  const synthesis=(configuration.audio_models||[]).find(model=>model.id===$('call-synthesis').value);
  const missing=!profile.id?'No speech engine is available. Check System → Voice.':direct&&!$('call-model').value?'No permitted conversation model is available. Check Models and your account access.':audio&&(!$('call-synthesis').value||!$('call-recognition').value)?'Choose available recognition and speech models.':audio&&(!synthesis||synthesis.kind==='clone')&&!$('voice').value?'Choose a saved voice from your audio library, or select a model with built-in voices.':'';
  const languages=configuration.languages||[{id:'en',name:'English'}];
  choices('listen-language',audio?[{id:'',name:'Auto-detect'},...languages]:languages,profile.language||'en');
  choices('response-language',[{id:'',name:'Match listening language'},...languages],'en');
  if(!$('translate').checked)$('response-language').value=$('listen-language').value;
  $('translate').disabled=callLocked||!direct;
  $('listen-language').disabled=callLocked;
  $('response-language').disabled=callLocked||!direct||!$('translate').checked;
  $('swap-languages').disabled=callLocked||!direct||!$('translate').checked;
  $('language-summary').textContent=($('listen-language').selectedOptions[0]?.textContent||'Auto-detect')+' → '+(direct?$('response-language').selectedOptions[0]?.textContent:'Agent-managed reply');
  $('language-help').textContent=missing||languageIssue()||(!direct?'Hermes manages its own response language and instructions.':audio?'Choose languages supported by both speech models.':'The configured CPU speech engine supports English. Choose Audio models for other languages.');
  $('connect').disabled=callLocked||!configurationReady||!!missing||!!languageIssue();
}
function lockControls(on){
  callLocked=on;
  for(const input of $('call-settings')?.querySelectorAll('input,select,textarea')||[])input.disabled=on&&!['call-gain','call-volume'].includes(input.id);
  if(studio){
    $('new-conversation').disabled=on;
    $('setup-note').textContent=on?'End the call to change models or languages.':'Models can be changed before a call.';
    if(on)$('voice-workspace').dataset.callActive='';else delete $('voice-workspace').dataset.callActive;
    languageControls();
    if(!on&&$('call-profile'))engineControls();
  }
}
function transcriptCounts(){
  if(!studio)return;
  $('transcript-empty').hidden=transcriptRows.length>0;
  $('turn-count').textContent=transcriptRows.length+' message'+(transcriptRows.length===1?'':'s');
  $('copy-transcript').disabled=$('export-transcript').disabled=!transcriptRows.length;
}
function scrollTranscript(force=false){
  if(!studio)return;
  if(following||force){$('transcript-scroll').scrollTop=$('transcript-scroll').scrollHeight;$('jump-latest').hidden=true;}
  else $('jump-latest').hidden=false;
}
function message(who,text,turn,partial=false){
  if(!studio){line(who,text);return null;}
  let row=who==='You'?partialMessage:assistantMessage;
  if(!row||row.turn!==turn){
    const element=$('voice-message-template').content.firstElementChild.cloneNode(true);
    row={who,text:'',turn,language:who==='You'?callLanguages.input:callLanguages.output,time:new Date().toISOString(),status:'Complete',element};
    element.dataset.role=who==='You'?'user':'assistant';
    element.querySelector('[data-speaker]').textContent=who;
    element.querySelector('.vc-avatar').textContent=who==='You'?'YOU':'AI';
    element.querySelector('time').textContent=new Date(row.time).toLocaleTimeString([],{hour:'2-digit',minute:'2-digit'});
    element.querySelector('time').dateTime=row.time;
    element.querySelector('[data-language]').textContent=(configuration.languages||[]).find(l=>l.id===row.language)?.name||'';
    element.querySelector('[data-text]').lang=row.language;
    $('transcript').append(element);transcriptRows.push(row);
    if(who==='You')partialMessage=row;else assistantMessage=row;
  }
  row.text=who==='You'?text:row.text+(row.text&&!/\s$/.test(row.text)&&!/^\s/.test(text)?' ':'')+text;
  row.element.querySelector('[data-text]').textContent=row.text;
  if(partial){row.status='Recognising';row.element.dataset.pending='';}
  else {row.status='Complete';delete row.element.dataset.pending;}
  row.element.querySelector('[data-message-state]').textContent=partial?'Recognising…':'';
  transcriptCounts();scrollTranscript();return row;
}
function markInterrupted(turn){
  stoppedTurns.add(turn);
  const row=assistantMessage;
  if(row?.turn===turn){row.status='Interrupted';row.element.querySelector('[data-message-state]').textContent='Interrupted · Reply may have been partly heard.';}
}
function finishTranscript(){
  if(partialMessage?.status==='Recognising'){
    partialMessage.status='Unfinished';delete partialMessage.element.dataset.pending;
    partialMessage.element.querySelector('[data-message-state]').textContent='Unfinished utterance';
  }
}
function playbackProgress(event){
  const segment=segments.get(event.segment);if(segment)segment.played=event.samples;
  if(event.turn===activeTurn&&completedTurns.has(event.turn)&&![...segments.values()].some(s=>s.turn===event.turn&&s.played<s.samples)&&ready)status('Listening');
}
const failures={busy:'The voice service is busy. Try again after the current call finishes.',unauthorized:'Your account cannot use this voice or model.',
  unavailable:'The speech service could not start. Check its status under System → Voice.',model_unavailable:'A selected model is unavailable. Choose another model or check the voice profile.',
  speech_service_permissions:'The speech service cannot load or hold its models. An administrator must restore its permissions.',
  invalid_options:'Some voice settings are not supported by the selected speech engine.',invalid_voice:'Choose a saved voice available to your account.',
  startup_timeout:'The selected models did not become ready in time. Choose a smaller conversation model, or wait for active GPU work to finish.',
  mode_unavailable:'This speech engine does not support that conversation mode.',invalid_profile:'The selected speech engine is no longer configured.'};
/** Build an accessible call control with the same markup in both voice pages. */
function control(id,title,type='select',attributes={}){
  if($(id)){$(id).setAttribute('aria-label',title);return $(id);}
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
  ($('call-settings-extra')||$('call-settings')).append(label);return input;
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
  const profile=configuration.profiles.find(p=>p.id===$('call-profile').value)||{},audio=profile.speech==='audiocpp';
  const audioModels=configuration.audio_models||[];
  const speechModels=audioModels.filter(m=>['speech','clone'].includes(m.kind));
  const recognitionModels=audioModels.filter(m=>m.kind==='transcription');
  if(!modelChosen)$('call-model').value='';
  choices('call-model',configuration.models||[],profile.model);
  for(const id of ['call-synthesis','call-recognition'])if(!speechModelsChosen.has(id))$(id).value='';
  choices('call-synthesis',audio?(speechModels.length?speechModels:configuration.synthesis_models||[]):[{id:'cpu',name:'Configured CPU speech'}],profile.synthesis_model);
  choices('call-recognition',audio?(recognitionModels.length?recognitionModels:configuration.recognition_models||[]):[{id:'cpu',name:'Configured CPU recognition'}],profile.recognition_model);
  const selected=speechModels.find(m=>m.id===$('call-synthesis').value);
  const cloned=audio&&(!selected||selected.kind==='clone');
  $('voice-choice').hidden=$('record-choice').hidden=!cloned;
  $('call-speaker').parentElement.hidden=cloned;
  for(const id of ['call-synthesis','call-recognition']){$(id).parentElement.hidden=!audio&&!studio;$(id).disabled=callLocked||!audio;}
  $('call-pause').parentElement.hidden=!audio;
  for(const id of ['call-model','call-length','call-instructions'])$(id).parentElement.hidden=mode!=='direct';
  choices('call-speaker',[{id:'',name:'Default voice'},...(audio?selected?.voices||[]:configuration.speakers||[])],speakerDrafts.get(audio?$('call-synthesis').value:'cpu')??(audio?selected?.default_voice:profile.speaker));
  const speakerKey=audio?$('call-synthesis').value:'cpu',savedSpeaker=speakerDrafts.get(speakerKey);
  if(speakerDrafts.has(speakerKey)&&[...$('call-speaker').options].some(option=>option.value===savedSpeaker))$('call-speaker').value=savedSpeaker;
  if(cloned)choices('voice',configuration.voices||[]);
  if(studio){
    const voice=$('call-speaker').selectedOptions[0]?.textContent||'Default voice';
    $('voice-summary').textContent=cloned?$('voice').selectedOptions[0]?.textContent||'Choose a saved voice':voice;
    $('voice-description').textContent=audio?'Voices declared by the selected model.':'Installed CPU voice bundle.';
    languageControls();
  }
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
  control('call-profile','Speech engine');choices('call-profile',configuration.profiles.map(p=>({id:p.id,name:p.speech==='audiocpp'?'Audio models · '+p.id:'CPU voices · '+p.id})),configuration.default_profile);
  control('call-model','Conversation model');control('call-synthesis','Speech model');control('call-recognition','Recognition model');control('call-speaker','Built-in voice');
  control('call-speed','Speaking speed','select');choices('call-speed',[.5,.75,1,1.25,1.5,2].map(value=>({id:String(value),name:value+'×'})),'1');
  control('call-length','Reply length','select');choices('call-length',[{id:'64',name:'Brief · 64 tokens'},{id:'128',name:'Standard · 128 tokens'},{id:'256',name:'Longer · 256 tokens'},{id:'512',name:'Detailed · 512 tokens'}],'128');
  control('call-pause','Pause before reply (ms)','number',{min:300,max:3000,step:100,value:700});
  control('call-microphone','Microphone');choices('call-microphone',[{id:'',name:'System default'}]);
  control('call-gain','Microphone gain','range',{min:.5,max:3,step:.1,value:1});
  control('call-volume','Playback volume','range',{min:0,max:1,step:.05,value:1});
  const instructions=control('call-instructions','Instructions for this conversation','textarea',{rows:2,maxLength:4000,placeholder:'Optional: tell the model how you want it to reply.'});instructions.parentElement.classList.remove('col-sm-6');
  $('call-profile').onchange=engineControls;$('mode').onchange=engineControls;
  $('call-model').onchange=()=>{modelChosen=true;if(studio)languageControls();};
  for(const id of ['call-synthesis','call-recognition'])$(id).onchange=()=>{speechModelsChosen.add(id);engineControls();};
  $('call-speaker').onchange=()=>{speakerDrafts.set(configuration.profiles.find(p=>p.id===$('call-profile').value)?.speech==='audiocpp'?$('call-synthesis').value:'cpu',$('call-speaker').value);if(studio)$('voice-summary').textContent=$('call-speaker').selectedOptions[0]?.textContent||'';};
  $('voice').onchange=()=>{if(studio){$('voice-summary').textContent=$('voice').selectedOptions[0]?.textContent||'';languageControls();}};
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
  const profile=configuration.profiles.find(p=>p.id===$('call-profile').value),audio=profile?.speech==='audiocpp';
  const synthesis=(configuration.audio_models||[]).find(m=>m.id===$('call-synthesis').value);
  const cloned=audio&&(!synthesis||synthesis.kind==='clone');
  if(!profile)throw Error('Choose an available speech engine.');
  const options={speed:Number($('call-speed').value)};
  if($('mode').value==='direct'){
    if(!$('call-model').value)throw Error('Choose an available conversation model.');
    options.model=$('call-model').value;options.max_output_tokens=Number($('call-length').value);
    if($('call-instructions').value.trim())options.system_prompt=$('call-instructions').value.trim();
  }
  if(audio){
    if((cloned&&!$('voice').value)||!$('call-synthesis').value||!$('call-recognition').value)throw Error('Choose the speech model, recognition model and any required saved voice.');
    options.synthesis_model=$('call-synthesis').value;options.recognition_model=$('call-recognition').value;options.trailing_silence_ms=Number($('call-pause').value);
    if(!cloned&&$('call-speaker').value)options.speech_voice=$('call-speaker').value;
  }else if($('call-speaker').value)options.speaker=Number($('call-speaker').value);
  if(studio){
    const issue=languageIssue();if(issue)throw Error(issue);
    if($('listen-language').value)options.language=$('listen-language').value;
    if($('mode').value==='direct')options.response_language=$('response-language').value||'auto';
    callLanguages={input:$('listen-language').value,output:$('response-language').value};
  }
  return {profile:profile.id,options,...(cloned?{voice:$('voice').value,record:$('record').checked}:{})};
}
/** Update the visible voice connection status using plain text. */
const status=text=>{
  $('status').textContent=text;
  if(!studio)return;
  const state=text.toLowerCase();$('call-panel').dataset.state=state==='disconnected'?'ended':state;
  const copy={listening:['You have the floor.','Speak naturally. You can interrupt a reply.'],thinking:['A moment to think.','Your words are complete. The model is preparing a reply.'],speaking:['A voice on the other side.','Follow the reply in the transcript.'],disconnected:['A good place to pause.','Your transcript stays here. Start another conversation when you are ready.'],connecting:['Getting connected.','Preparing your microphone and selected models.']};
  if(copy[state]){ $('call-headline').textContent=copy[state][0];$('call-hint').textContent=copy[state][1]; }
};
/** Send one control event only while the voice socket is open. */
function send(value){if(socket?.readyState===1)socket.send(JSON.stringify(value));}
/** Append a labeled transcript entry as text rather than active markup. */
function line(who,text){const div=document.createElement('div');div.className='line';const label=document.createElement('small');label.textContent=who;div.append(label,document.createTextNode(text));$('transcript').append(div);}
/** Stop microphone capture, playback and the current voice connection. */
async function close(){
  startAttempt++;startRequest?.abort();startRequest=null;
  clearInterval(clock);clock=null;finishTranscript();
  if(activeTurn!==null&&!completedTurns.has(activeTurn))markInterrupted(activeTurn);
  ready=false;queueClearAck=false;send({type:'session.cancel'});
  const previousSocket=socket;socket=null;if(previousSocket){previousSocket.onclose=previousSocket.onerror=previousSocket.onmessage=null;previousSocket.close();}
  microphone?.getTracks().forEach(t=>t.stop());microphone=null;
  playback?.close();playback=null;
  node?.disconnect();source?.disconnect();node=source=null;const previous=context;context=null;if(previous&&previous.state!=='closed')await previous.close();
  if(disposed)return;
  for(const id of ['mute','hangup','finish','interrupt'])$(id).disabled=true;
  $('connect').disabled=studio&&!configurationReady;status('Disconnected');
  $('echo').disabled=false;if($('voice'))$('voice').disabled=false;
  if($('record'))$('record').disabled=false;
  $('mode').disabled=false;lockControls(false);
}
$('login').onsubmit=async event=>{
  event.preventDefault();if(callLocked||disposed)return;
  const attempt=++startAttempt;startRequest=new AbortController();
  $('connect').disabled=true;$('hangup').disabled=false;failure='';status('Connecting');$('notice').textContent='Preparing microphone…';
  const current=()=>{if(attempt!==startAttempt||disposed)throw new DOMException('Call canceled','AbortError');};
  try{
    const selected=sessionChoices();
    partialMessage=assistantMessage=null;activeTurn=null;segments.clear();completedTurns.clear();stoppedTurns.clear();
    lockControls(true);
    if(!window.isSecureContext)throw Error('Open this page over HTTPS, or on localhost. Browsers require a secure connection for the microphone.');
    context=new AudioContext({latencyHint:'interactive'});await context.resume();current();
    await context.audioWorklet.addModule(base+'/static/audio.js');
    current();
    node=new AudioWorkletNode(context,'voice-audio',{outputChannelCount:[1]});
    node.port.postMessage({type:'input.gain',value:Number($('call-gain')?.value||1)});
    const cancelEcho=$('echo').checked;
    if(cancelEcho && !navigator.mediaDevices.getSupportedConstraints().echoCancellation)throw Error('This browser does not support echo cancellation. Use a supported browser, or headphones with echo cancellation switched off.');
    const captured=await navigator.mediaDevices.getUserMedia({audio:{channelCount:1,echoCancellation:{exact:cancelEcho},noiseSuppression:cancelEcho,autoGainControl:false,
      ...($('call-microphone')?.value?{deviceId:{exact:$('call-microphone').value}}:{})}});
    if(attempt!==startAttempt||disposed){captured.getTracks().forEach(track=>track.stop());current();}
    microphone=captured;await microphones();current();
    const actual=microphone.getAudioTracks()[0].getSettings().echoCancellation;
    if(cancelEcho && actual===false)throw Error('The microphone did not enable echo cancellation. Try another browser or audio device.');
    $('audio-status').textContent=cancelEcho ? (actual ? 'Echo cancellation active · voice interruption enabled' : 'Echo cancellation requested; browser does not report its status · voice interruption enabled') : 'Echo cancellation off · use headphones';
    $('echo').disabled=true;if($('voice'))$('voice').disabled=true;
    if($('record'))$('record').disabled=true;
    playback=await echoPlayback(context,node);
    current();
    playback.setVolume?.(Number($('call-volume')?.value??1));
    $('mode').disabled=true;lockControls(true);
    source=context.createMediaStreamSource(microphone);source.connect(node);
    node.port.onmessage=({data:m})=>{
      if(m.type==='input' && ready){
        if(socket?.bufferedAmount>16000){$('notice').textContent='Connection too slow. Reconnect and repeat your last sentence.';close();return;}
        // Keep the recognizer sample clock moving while muted without sending microphone samples.
        if(socket?.readyState===1)socket.send(muted ? new ArrayBuffer(m.pcm.byteLength) : m.pcm);
      }else if(m.type==='playback.progress' || (m.type==='playback.cleared' && queueClearAck)){send(m);if(m.type==='playback.progress')playbackProgress(m);}
      else if(m.type==='overload'){$('notice').textContent='Playback could not keep up. Please reconnect.';close();}
    };
    const response=await fetch(base+'/browser/sessions',{method:'POST',signal:startRequest.signal,headers:{'content-type':'application/json'},body:JSON.stringify({conversation_mode:$('mode').value,...selected})});
    const result=await response.json();
    current();
    if(!response.ok)throw Error(result.error?.message||result.error||'Voice could not start');
    const connection=new WebSocket(`${location.protocol==='https:'?'wss:':'ws:'}//${location.host}${base}/browser/sessions/${result.session_id}/stream`);socket=connection;socket.binaryType='arraybuffer';
    muted=false;$('mute').textContent='Mute';$('mute').setAttribute('aria-pressed','false');
    socket.onmessage=({data})=>{
      if(socket!==connection)return;
      if(data instanceof ArrayBuffer){node.port.postMessage({type:'audio',pcm:data},[data]);return;}
      const m=JSON.parse(data);
      if(m.type==='session.ready'){queueClearAck=m.queue_clear_ack===true;ready=true;node.port.postMessage({type:'capture',on:true});status('Listening');$('notice').textContent='Speak naturally. You can interrupt an answer.';for(const id of ['mute','finish','interrupt'])$(id).disabled=false;if(studio){startedAt=Date.now();clearInterval(clock);clock=setInterval(()=>{const seconds=Math.floor((Date.now()-startedAt)/1000);$('call-timer').textContent=String(Math.floor(seconds/60)).padStart(2,'0')+':'+String(seconds%60).padStart(2,'0');},1000);}}
      if(m.type==='turn.started'){node.port.postMessage({type:'turn',turn:m.turn});if(m.turn>=0){activeTurn=m.turn;assistantMessage=null;status('Thinking');}}
      if(m.type==='playback.segment'&&!stoppedTurns.has(m.turn)&&!segments.has(m.segment)){segments.set(m.segment,{turn:m.turn,samples:m.samples,played:0});node.port.postMessage({...m,type:'segment'});status('Speaking');if(m.text&&m.turn>=0)message('Assistant',m.text,m.turn);}
      if(m.type==='playback.clear'){node.port.postMessage({type:'clear',turn:m.turn});markInterrupted(m.turn);status('Listening');}
      if(m.type==='transcript.partial'&&studio)message('You',m.text,m.turn,true);
      if(m.type==='transcript.final')message('You',m.text,m.turn);
      if(m.type==='turn.completed' && ready){completedTurns.add(m.turn);playbackProgress({turn:m.turn});}
      if(m.type==='session.state')status(m.state.replaceAll('_',' '));
      if(m.type==='session.error'){failure=failures[m.code]||'The turn could not finish. Please try again or reconnect.';$('notice').textContent=failure;}
    };
    socket.onclose=async event=>{if(socket!==connection)return;const reason=failure||(event.code===1008?'The call was refused or its sign-in expired. Reload this page and try again.':'The voice connection ended. You can reconnect.');await close();$('notice').textContent=reason;};
    socket.onerror=()=>{if(socket===connection)$('notice').textContent='The voice connection could not be opened. Please reconnect.';};
    $('hangup').disabled=false;
  }catch(error){if(attempt!==startAttempt||disposed)return;await close();$('notice').textContent=error.message;}
};
$('hangup').onclick=close;
$('mute').onclick=()=>{muted=!muted;$('mute').textContent=muted?'Unmute':'Mute';$('mute').setAttribute('aria-pressed',String(muted));if(studio)$('notice').textContent=muted?'Microphone muted. Replies can still play.':'Microphone unmuted. Speak naturally.';};
$('finish').onclick=()=>send({type:'input.end'});
$('interrupt').onclick=()=>{node?.port.postMessage({type:'clear'});markInterrupted(activeTurn);send({type:'session.cancel',interrupt:true});status('Listening');};
async function loadConfiguration(){
try{
  const r=await fetch(base+'/browser/config'),c=await r.json();if(disposed)return;
  if(!r.ok)throw Error(c.error?.message||c.error||'Voice settings are unavailable.');
  configuration=c;
  configurationReady=!c.configuration_error;
  for(const option of $('mode').options)option.disabled=!c.modes.includes(option.value);
  if($('mode').selectedOptions[0]?.disabled)$('mode').value=[...$('mode').options].find(option=>!option.disabled)?.value||'';
  // SoundRoom voices, when the gateway offers them: the call then uses audio.cpp speech in that voice.
  if(!$('voice'))return;
  choices('voice',c.voices||[]);
  $('voice-choice').hidden=$('record-choice').hidden=!(c.voices||[]).length;
  const wanted=new URLSearchParams(location.search).get('voice');
  if(wanted&&[...$('voice').options].some(o=>o.value===wanted))$('voice').value=wanted;
  if(new URLSearchParams(location.search).get('record')==='1')$('record').checked=true;
  if(!$('call-model')||!$('call-profile')?.options.length)settingsControls();else engineControls();
  if(c.configuration_error)throw Error(c.configuration_error);
  if(studio){$('configuration-notice').hidden=true;status('Ready');languageControls();}
}catch(error){if(disposed)return;configurationReady=false;$('notice').textContent=error.message;if(studio){$('connect').disabled=true;$('configuration-notice').hidden=false;$('configuration-error').textContent=error.message;status('Unavailable');}}
}
if(studio){
  $('retry-configuration').onclick=loadConfiguration;
  for(const id of ['listen-language','response-language','translate'])$(id).onchange=languageControls;
  $('swap-languages').onclick=()=>{const value=$('listen-language').value;$('listen-language').value=$('response-language').value;$('response-language').value=value;languageControls();};
  $('new-conversation').onclick=()=>{if(callLocked)return;transcriptRows.length=0;$('transcript').replaceChildren();partialMessage=assistantMessage=null;following=true;transcriptCounts();$('action-status').textContent='New conversation ready. Model and language choices are kept.';};
  $('transcript-scroll').addEventListener('scroll',()=>{const el=$('transcript-scroll');following=el.scrollHeight-el.scrollTop-el.clientHeight<50;$('jump-latest').hidden=following;});
  $('jump-latest').onclick=()=>{following=true;scrollTranscript(true);};
  const rows=()=>transcriptRows.map(({element,...row})=>row);
  const text=()=>rows().map(row=>'['+new Date(row.time).toLocaleTimeString()+'] '+row.who+(row.language?' ('+row.language+')':'')+'\n'+row.text+(row.status==='Complete'?'':'\n['+row.status+']')).join('\n\n');
  $('export-transcript').onclick=()=>{const format=$('export-format').value;const blob=new Blob([format==='json'?JSON.stringify({messages:rows()},null,2):text()],{type:format==='json'?'application/json':'text/plain;charset=utf-8'});const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='voice-transcript.'+format;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000);$('action-status').textContent='Transcript exported.';};
  $('copy-transcript').onclick=async()=>{try{await navigator.clipboard.writeText(text());$('action-status').textContent='Transcript copied.';}catch{$('action-status').textContent='Clipboard unavailable. Use Export to save your transcript.';}};
  transcriptCounts();
}
const leave=()=>{if(disposed)return;disposed=true;document.removeEventListener('htmx:beforeSwap',beforeSwap);close();};
const beforeSwap=event=>{if(event.detail.target?.contains($('login')))leave();};
window.addEventListener('pagehide',leave,{once:true});
document.addEventListener('htmx:beforeSwap',beforeSwap);
loadConfiguration();
})();
