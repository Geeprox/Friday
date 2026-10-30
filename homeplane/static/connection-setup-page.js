import {mountConnectionSetup,mountCameraSetup} from './connection-setup.js';
mountConnectionSetup(document.getElementById('connection-setup'),{standalone:true,onCameraSetup:slot=>mountCameraSetup(slot)});
