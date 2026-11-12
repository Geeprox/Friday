"""Voice-worker bridge to the owning Friday application session.

No ARL host, provider process, model selection or extra queue is created here.
Device actions retain their separate command and authority checks.
"""
import re
from urllib.parse import urlsplit

import httpx

from .paths import PRIVATE, configuration


class IntentRuntime:
    def __init__(self, *, client=None, config=configuration, private=PRIVATE):
        self.client = client or httpx.Client(timeout=35, trust_env=False, follow_redirects=False)
        self.config, self.private = config, private
        self.state = 'not_requested'
        self.admission = None

    def report(self):
        return dict(route=getattr(self, 'route', 'arl_application_scenario'), state=self.state, scenario_id='voice_intent',
                    package_id='low', tools=False, device_authority=False, admission=self.admission)

    def complete(self, prompt, *, request_id, voice_revision=None):
        self.state,self.admission='unavailable',None
        if not isinstance(request_id, str) or not re.fullmatch(r'[a-z0-9-]{1,128}', request_id):
            raise ValueError('model_request_id_required')
        origin = self.config()['main_origin'].rstrip('/')
        address = urlsplit(origin)
        port=address.port if address.port is not None else 80
        if (address.scheme != 'http' or address.hostname != '127.0.0.1'
                or not 1 <= port <= 65535
                or address.path or address.query or address.fragment or address.username or address.password):
            raise ValueError('model_owner_origin_invalid')
        origin='http://127.0.0.1'+(f':{port}' if port!=80 else '')
        key = (self.private / 'control-key').read_text().strip()
        try:
            response = self.client.post(origin + '/api/embodied/model',
                headers={'Origin': origin, 'X-Friday-Worker': key},
                json={'request_id': request_id, 'input': prompt,
                      **({'voice_revision':voice_revision} if voice_revision is not None else {})})
            if response.status_code != 200:
                raise ValueError('application_model_unavailable')
            result = response.json()
            from homeplane.intelligence.runtime import verified_planning_result
            if not verified_planning_result(result, request_id, 'voice_intent'):
                raise ValueError('model_result_unverified')
            admission = result.get('api_receipt') if result.get('provider_id') == 'openai_responses' else result.get('application_admission', {})
            self.admission = admission
            self.route = 'openai_api' if result.get('provider_id') == 'openai_responses' else 'arl_application_scenario'
            self.state = 'completed'
            return result
        except (httpx.HTTPError, ValueError):
            self.state = 'unavailable'
            raise ValueError('application_model_unavailable') from None

    def close(self):
        self.client.close()
