// Fixed diagnostic codes only. Never forward gateway messages or household data.
export function classifyFailure(error) {
  const message = typeof error?.message === 'string' ? error.message : '';
  if (error?.code === 'ECONNREFUSED') return 'connection_refused';
  if (['ETIMEDOUT', 'EHOSTUNREACH', 'ENETUNREACH'].includes(error?.code)) return 'network_unreachable';
  if (/^Gateway connection closed \(\d+\)$/.test(message)) return 'gateway_closed_connection';
  if (message === 'Opening handshake has timed out') return 'connection_timeout';
  if (/^Unexpected server response: \d+$/.test(message)) return 'websocket_upgrade_rejected';
  if (message === 'Gateway authentication timed out') return 'authentication_timeout';
  if (message === 'Protocol selection failed') return 'protocol_selection_failed';
  if (/^(Session key exchange failed|ZKP verification failed for (g_x3|g_x4|round two))$/.test(message)) return 'authentication_handshake_failed';
  // ERROR=4 is defined by the exact pinned client. It does not identify why pairing failed.
  if (message === 'Unexpected response type: 4') return 'gateway_error_frame';
  if (/^Unexpected response type: \d+$/.test(message)) return 'unexpected_handshake_frame';
  if (/^API call timeout: (getDevList|getGraphList|getGraph|getVarScopeList|getVarList)$/.test(message)) return 'readonly_request_timeout';
  if (message === 'Secure Session not established') return 'session_not_connected';
  return 'check_failed';
}
