/* AudioWorklet: continuous filtered capture and a one-second bounded playback ring. */
/** Filter microphone frames and play ordered audio through a bounded real-time ring. */
class VoiceAudio extends AudioWorkletProcessor {
  /** Initialize capture filtering, playback storage and ordered control-message handling. */
  constructor() {
    super();
    this.capture = false;
    this.inputGain = 1;
    this.input = new Float32Array(64);
    this.at = 0; this.clock = 0; this.next = 32;
    this.frame = new Int16Array(320); this.filled = 0;
    this.ring = new Float32Array(24000); this.read = 0; this.write = 0; this.queued = 0;
    this.fraction = 0; this.played = 0; this.segments = []; this.accept = false; this.turn = 0;
    this.port.onmessage = ({data:m}) => {
      if (m.type === 'capture') this.capture = m.on;
      if (m.type === 'input.gain' && Number.isFinite(m.value)) this.inputGain = Math.min(3, Math.max(.5, m.value));
      if (m.type === 'clear') {
        this.read = this.write = this.queued = this.fraction = this.played = 0;
        this.segments = []; this.accept = false;
        if(Number.isInteger(m.turn) && m.turn>=0)this.port.postMessage({type:'playback.cleared',turn:m.turn});
      }
      if (m.type === 'turn') {this.accept = true; this.turn = m.turn;}
      if (m.type === 'segment' && this.accept) this.segments.push({...m, played:0});
      if (m.type === 'audio' && this.accept) {
        const pcm = new Int16Array(m.pcm);
        if (pcm.length + this.queued > this.ring.length) {
          this.port.postMessage({type:'overload'}); return;
        }
        for (const x of pcm) {this.ring[this.write] = x / 32768; this.write = (this.write+1)%this.ring.length;}
        this.queued += pcm.length;
      }
    };
  }
  /** Filter and resample one input sample into ordered 16 kHz microphone frames. */
  captureSample(x) {
    this.input[this.at] = x; this.at = (this.at+1)%64; this.clock++;
    if (this.clock < this.next) return;
    this.next += sampleRate/16000;
    // Windowed sinc low-pass, cutoff below the 16 kHz output Nyquist frequency.
    const cutoff = Math.min(.45, 7200/sampleRate);
    let value=0, sum=0;
    for(let i=0;i<31;i++) {
      const d=i-15, sinc=d===0?2*cutoff:Math.sin(2*Math.PI*cutoff*d)/(Math.PI*d);
      const weight=sinc*(.54-.46*Math.cos(2*Math.PI*i/30));
      value += this.input[(this.at-1-i+64)%64]*weight; sum+=weight;
    }
    this.frame[this.filled++] = Math.round(Math.max(-1,Math.min(1,value/sum))*32767);
    if(this.filled===320) {
      if(this.capture) this.port.postMessage({type:'input',pcm:this.frame.buffer},[this.frame.buffer]);
      this.frame=new Int16Array(320);this.filled=0;
    }
  }
  /** Account for one played source sample and acknowledge segment progress. */
  consumed() {
    this.played++;
    const segment=this.segments[0];
    if(!segment) return;
    segment.played++;
    if(segment.played%960===0 || segment.played===segment.samples) {
      this.port.postMessage({type:'playback.progress',turn:segment.turn,segment:segment.segment,samples:segment.played});
    }
    if(segment.played===segment.samples) this.segments.shift();
  }
  /** Capture input and fill each output quantum without exceeding the playback ring. */
  process(inputs,outputs) {
    const output=outputs[0][0], input=inputs[0]?.[0];
    for(let i=0;i<output.length;i++) {
      this.captureSample((input?.[i]||0)*this.inputGain);
      if(!this.queued) {output[i]=0;continue;}
      const a=this.ring[this.read], b=this.ring[(this.read+1)%this.ring.length];
      output[i]=a+(b-a)*this.fraction;
      this.fraction+=24000/sampleRate;
      while(this.fraction>=1 && this.queued) {
        this.fraction--;this.read=(this.read+1)%this.ring.length;this.queued--;this.consumed();
      }
    }
    return true;
  }
}
registerProcessor('voice-audio',VoiceAudio);
