/* Keep speaker audio on WebRTC's remote playback path so it is an AEC reference.
   Both peers are in this page: no STUN/TURN server and no external media connection. */
async function echoPlayback(context, node) {
  const outgoing = new RTCPeerConnection({iceServers: []});
  const incoming = new RTCPeerConnection({iceServers: []});
  const destination = context.createMediaStreamDestination();
  const player = document.createElement('audio');
  player.autoplay = true;
  player.setAttribute('playsinline', '');
  document.body.append(player);
  /** Release the local peer connections, tracks and audio routing nodes. */
  function close() {
    node.disconnect(destination);
    outgoing.close(); incoming.close();
    destination.stream.getTracks().forEach(track => track.stop());
    player.pause(); player.srcObject = null; player.remove();
  }
  /** Wait for local ICE gathering with a finite connection deadline. */
  async function gathered(peer) {
    if (peer.iceGatheringState === 'complete') return;
    await new Promise((resolve, reject) => {
      /** Remove ICE listeners and clear the connection deadline. */
      const done = () => {
        clearTimeout(timer);
        peer.removeEventListener('icegatheringstatechange', changed);
      };
      /** Resolve gathering only after the peer reports completion. */
      const changed = () => {
        if (peer.iceGatheringState === 'complete') {done(); resolve();}
      };
      const timer = setTimeout(() => {done(); reject(Error('Local echo-cancellation audio connection timed out.'));}, 5000);
      peer.addEventListener('icegatheringstatechange', changed);
      changed();
    });
  }
  try {
    node.connect(destination);
    incoming.ontrack = event => {player.srcObject = event.streams[0];};
    for (const track of destination.stream.getTracks()) outgoing.addTrack(track, destination.stream);
    await outgoing.setLocalDescription(await outgoing.createOffer());
    await gathered(outgoing);
    await incoming.setRemoteDescription(outgoing.localDescription);
    await incoming.setLocalDescription(await incoming.createAnswer());
    await gathered(incoming);
    await outgoing.setRemoteDescription(incoming.localDescription);
    await Promise.race([
      player.play(),
      new Promise((_, reject) => setTimeout(() => reject(Error('Speaker playback could not start.')), 5000))
    ]);
    return {close, outgoing, incoming, setVolume: value => {player.volume = value;}};
  } catch (error) {close(); throw error;}
}
