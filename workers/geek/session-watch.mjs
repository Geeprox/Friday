// The worker's one session loop is also exercised with explicit test doubles.
import {setTimeout as delay} from 'node:timers/promises';
import {classifyFailure} from './diagnostics.mjs';
import {pollInventory, saveInventory, graphChanges} from './session-reader.mjs';

export async function watchSession({clientFactory, passcode, output, emit, signal,
  poll=pollInventory, save=saveInventory, wait=delay, clock=()=>performance.now()}) {
  let reconnects = 0, failedReconnects = 0, previous = null, client, lastSaved = null;
  const close = () => { void client?.close(); };
  signal.addEventListener('abort',close);
  try {
    while (!signal.aborted) {
      let phase = 'websocket_connect';
      try {
        client = clientFactory();
        await emit(reconnects ? 'reconnecting' : 'starting',{reconnects});
        await client.connect();
        phase = 'authenticate'; await client.authenticate(passcode);
        const authenticatedAt = clock();
        let resumed = reconnects > 0;
        while (!signal.aborted) {
          phase = 'inventory';
          let deadline, snapshot;
          try {
            snapshot = await Promise.race([
              poll(client),
              new Promise((_,reject)=>{deadline=setTimeout(()=>reject(Error('inventory_deadline')),120000);}),
            ]);
          } finally { clearTimeout(deadline); }
          if (signal.aborted) break;
          const changes = graphChanges(previous?.graphs,snapshot.graphs);
          // Refresh the private raw snapshot daily without disconnecting the
          // gateway. Four retained snapshots keep age/capacity bounded even
          // when opaque content never changes during a long-lived session.
          if (!previous || previous.fingerprint !== snapshot.fingerprint || lastSaved === null || clock()-lastSaved >= 86400000) {
            await save(output,snapshot); lastSaved=clock();
          }
          if (signal.aborted) break;
          await emit('online',{reconnects,device_count:snapshot.device_count,graph_count:snapshot.graph_count,
            scope_count:snapshot.scope_count,fingerprint:snapshot.fingerprint,changes,gap:resumed,
            session_seconds:Math.floor((clock()-authenticatedAt)/1000)});
          previous = {fingerprint:snapshot.fingerprint,graphs:snapshot.graphs};
          failedReconnects = 0;
          resumed = false;
          await wait(60000,undefined,{signal});
        }
      } catch (error) {
        if (signal.aborted) break;
        const failure_code = classifyFailure(error);
        const network_errno = ['ECONNREFUSED','ETIMEDOUT','EHOSTUNREACH','ENETUNREACH','EPERM','EACCES'].includes(error?.code) ? error.code : undefined;
        await emit('blocked',{reconnects,failure_phase:phase,failure_code,...(network_errno?{network_errno}:{})});
        await client?.close();
        // An unsuccessful handshake is never retried. Reconnect requires a
        // prior complete inventory and at most three consecutive failures.
        // A complete poll resets the outage budget, not the total counter.
        if (phase === 'authenticate' || !previous || failedReconnects >= 3 || !['gateway_closed_connection','session_not_connected','readonly_request_timeout','connection_refused','connection_timeout','network_unreachable'].includes(failure_code)) break;
        failedReconnects++;
        reconnects++;
        await emit('reconnecting',{reconnects,failure_phase:phase,failure_code,gap:true});
        await wait([1000,5000,15000][failedReconnects-1],undefined,{signal});
      } finally { await client?.close(); }
    }
  } finally { signal.removeEventListener('abort',close); await client?.close(); }
}
