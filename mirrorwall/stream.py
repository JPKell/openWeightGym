"""Bounded SSE parsing for image progress, previews and terminal results."""
import json

MAX_EVENT=192*1024*1024


async def events(chunks, *, max_event=MAX_EVENT):
    """Decode complete data events across arbitrary transport chunk boundaries."""
    pending=bytearray()
    async for chunk in chunks:
        pending.extend(chunk.replace(b"\r",b""))
        if len(pending)>max_event:
            raise ValueError("Image stream event exceeds its size limit.")
        while b"\n\n" in pending:
            frame,_,rest=pending.partition(b"\n\n")
            pending=bytearray(rest)
            lines=[line[5:].strip() for line in frame.replace(b"\r",b"").split(b"\n") if line.startswith(b"data:")]
            if not lines:
                continue
            value=b"\n".join(lines)
            if value==b"[DONE]":
                continue
            decoded=json.loads(value)
            if not isinstance(decoded,dict):
                raise ValueError("Invalid image stream event.")
            yield decoded
    if pending.strip():
        raise ValueError("Image stream ended during an event.")


def encode(event):
    """Encode one JSON event for OpenAI-compatible image clients."""
    kind=event.get("type","error")
    return ("event: "+kind+"\ndata: "+json.dumps(event,separators=(",",":"))+"\n\n").encode()


def completed(whole):
    """Recognize image terminal events independently of chat's [DONE] convention."""
    for line in whole.splitlines():
        if line.startswith(b"data:"):
            try:
                value=json.loads(line[5:])
            except ValueError:
                continue
            if isinstance(value,dict) and value.get("type") in ("image_generation.completed","image_edit.completed","video_generation.completed"):
                return True
    return False


def metadata(value):
    """Keep reproducibility/settings/error facts without duplicating image bytes in logs."""
    if isinstance(value,dict):
        return {key:metadata(item) for key,item in value.items() if key not in ('b64_json','poster_b64','references','image','mask','init_image','end_image','reference_images','control_video')}
    if isinstance(value,list):
        return [metadata(item) for item in value]
    return value


class Capture:
    """Bound diagnostic memory independently of preview count and image batch size."""
    def __init__(self,content_type,*,max_event=MAX_EVENT):
        """Initialize private image state without loading model weights."""
        self.sse='text/event-stream' in content_type
        self.max_event=max_event
        self.buffer=bytearray()
        self.records=[]
        self.overflow=False
        self.count=0
        self.error=False

    def accept(self,value):
        """Retain terminal metadata without duplicating encoded image pixels."""
        if not isinstance(value,dict):
            return
        if value.get('error'):
            self.error=True
        if value.get('type') in ('image_generation.completed','image_edit.completed','video_generation.completed'):
            self.count+=1
        if value.get('type') in ('image_generation.completed','image_edit.completed','video_generation.completed','error') or 'error' in value:
            self.records.append(metadata(value))

    def feed(self,chunk):
        """Consume bounded transport chunks and decode complete image events."""
        if self.overflow:
            return
        self.buffer.extend(chunk.replace(b"\r",b"") if self.sse else chunk)
        if len(self.buffer)>self.max_event:
            self.overflow=True
            self.buffer.clear()
            return
        if self.sse:
            while b'\n\n' in self.buffer:
                frame,_,rest=self.buffer.partition(b'\n\n')
                self.buffer=bytearray(rest)
                for line in frame.splitlines():
                    if line.startswith(b'data:'):
                        try:
                            self.accept(json.loads(line[5:]))
                        except ValueError:
                            pass

    def snapshot(self):
        """Return compact image diagnostics after consuming the response."""
        if self.sse:
            return b''.join(encode(value) for value in self.records)
        if self.overflow:
            return b'{"image_capture_truncated":true}'
        try:
            value=json.loads(self.buffer)
            self.error=bool(value.get('error'))
            self.count=len(value.get('data',[]))
            if not self.records:
                for row in value.get('data',[]):
                    if row.get('mime_type')=='video/webm':
                        self.records.append({'type':'video_generation.completed',**metadata(row)})
            return json.dumps(metadata(value)).encode()
        except (ValueError,AttributeError):
            return bytes(self.buffer[:4096])

    def outcome(self):
        """Classify a completed image response without chat token conventions."""
        if self.error:
            return 'error'
        return 'success' if self.count else 'unscored' if self.overflow else 'error'

    def video_usage(self):
        """Count actual completed clips without fabricating language-token usage."""
        clips=[row for row in self.records if row.get('type')=='video_generation.completed']
        return {'video_clips':len(clips),'video_seconds':sum(row.get('seconds',0) for row in clips),
                'video_frames':sum(row.get('frame_count',0) for row in clips),
                'video_pixels':sum(row.get('width',0)*row.get('height',0) for row in clips),
                'gpu_seconds':sum(row.get('gpu_seconds',0) for row in clips)} if clips else {}
