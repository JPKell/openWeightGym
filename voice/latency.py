"""Same-process duration measurements; client queue acknowledgements are not physical audio."""

from collections import OrderedDict
from datetime import datetime, timezone
import math
import time

BASIS = {'input_end_received': 'Explicit input end received on compute',
         'last_loud_frame_received': 'Last loud frame received on compute (inferred endpoint)'}
METRICS = {'endpoint_to_final_seconds': 'Endpoint received → recognition final',
           'end_to_audio_seconds': 'Endpoint received → first PCM sent',
           'final_to_audio_seconds': 'Recognition final → first PCM sent',
           'recognition_block_seconds': 'Final recognition block',
           'final_to_phrase_seconds': 'Recognition final → first speakable phrase ready',
           'synthesis_seconds': 'First phrase synthesis',
           'audio_to_ack_seconds': 'First PCM received on the control node → first playback progress',
           'clear_ack_seconds': 'Clear forwarded on the control node → client queue-cleared acknowledgement'}


class Turn:
    """Compute timestamps stay local; only durations cross the sound socket."""
    def __init__(self, number, endpoint, final, basis, recognition):
        """Capture local endpoint and recognition timing for one voice turn."""
        self.number, self.endpoint, self.final = number, endpoint, final
        self.first_audio = None
        self.values = dict(basis=basis, state='started', recognition_block_seconds=round(recognition, 6),
                           endpoint_to_final_seconds=round(max(0,final-endpoint),6))

    def synthesis(self, seconds):
        """Record the first phrase synthesis duration and speaking state."""
        self.values.setdefault('synthesis_seconds', round(seconds, 6))
        self.values['state'] = 'speaking'

    def audio(self, now):
        """Record the first generated PCM timestamp for this turn."""
        if self.first_audio is None:
            self.first_audio = now
            self.values.update(end_to_audio_seconds=round(max(0, now-self.endpoint), 6),
                               final_to_audio_seconds=round(max(0, now-self.final), 6))

    def event(self, state=None):
        """Return numeric turn measurements with an optional updated state."""
        if state:
            self.values['state'] = state
        return dict(self.values)


class Recent:
    """Last 200 numeric turn records since gateway start, with a 24-hour upper age bound."""
    def __init__(self):
        """Initialize the bounded in-memory latency history and its start timestamp."""
        self.started = time.time()
        self.rows = OrderedDict()

    def prune(self, now):
        """Discard latency records outside the bounded count and age limits."""
        for key,row in list(self.rows.items()):
            if row['checked'] < now-86400:
                del self.rows[key]
        while len(self.rows) > 200:
            self.rows.popitem(last=False)

    def update(self, session, turn, source, mode, values):
        """Merge validated numeric and label evidence for one session turn."""
        if not isinstance(session,str) or not 1 <= len(session) <= 80 or type(turn) is not int or not 1 <= turn <= 1000000 or source not in ('browser','desktop','twilio') or mode not in ('direct','hermes'):
            return
        now = time.time()
        self.prune(now)
        key = session,turn
        row = self.rows.setdefault(key, dict(session=session,turn=turn,source=source,mode=mode,values={}))
        clean = {}
        if isinstance(values,dict):
            for metric in METRICS:
                value = values.get(metric)
                if type(value) in (int,float) and 0 <= value <= 7200 and math.isfinite(value):
                    clean[metric] = round(value,6)
            if isinstance(values.get('basis'),str) and values['basis'] in BASIS:
                clean['basis'] = values['basis']
            for label in ('profile','model','revision'):
                value = values.get(label)
                if isinstance(value,str) and 0 < len(value) <= 160:
                    clean[label] = value
            if values.get('state') in ('started','speaking','completed','interrupted','failed'):
                clean['state'] = values['state']
        row['values'].update(clean)
        row['checked'] = now
        row['at'] = datetime.fromtimestamp(now,timezone.utc).isoformat(timespec='seconds')
        self.prune(now)
        return dict(row, values=dict(row['values']))

    def snapshot(self):
        """Summarize retained latency distributions and recent bounded turn records."""
        self.prune(time.time())
        groups = {}
        for row in self.rows.values():
            values = row['values']
            group = (row['source'],row['mode'],values.get('basis','unavailable'),
                     values.get('profile','unavailable'),values.get('model','unavailable'),values.get('revision','unavailable'))
            for metric in METRICS:
                if metric in values:
                    groups.setdefault((*group,metric),[]).append(values[metric])
        summaries = []
        for (source,mode,basis,profile,model,revision,metric),values in sorted(groups.items()):
            values.sort()
            quantile = lambda p: round(1000*values[max(0,math.ceil(p*len(values))-1)],1)
            summaries.append(dict(source=source,mode=mode,profile=profile,model=model,revision=revision,
                                  basis=BASIS.get(basis,'Endpoint unavailable'),
                                  metric=metric,label=METRICS[metric],count=len(values),p50_ms=quantile(.5),p95_ms=quantile(.95)))
        return dict(started=self.started,count=len(self.rows),limit=200,summaries=summaries,
                    recent=[dict(r,values=dict(r['values']),basis=BASIS.get(r['values'].get('basis'),'Endpoint unavailable'))
                            for r in reversed(self.rows.values())][:20])
