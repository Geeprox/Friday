"""Offline system TTS and bounded playback on a configured HA speaker."""

import asyncio
from array import array
import secrets
import re
import sys
import time
import wave
from urllib.parse import urlencode

from .paths import DATA, PRIVATE, configuration


def _validate_wav(path):
    """An audio container being decodable does not mean it contains speech."""
    try:
        with wave.open(str(path), 'rb') as source:
            rate, frames = source.getframerate(), source.getnframes()
            channels, width = source.getnchannels(), source.getsampwidth()
            if rate != 44100 or channels not in {1, 2} or width != 2 or not rate*.05 <= frames <= rate*120:
                raise ValueError('speech_synthesis_unavailable')
            pcm = source.readframes(frames)
        if len(pcm) != frames*channels*width:
            raise ValueError('speech_synthesis_unavailable')
        samples = array('h', pcm)
        if sys.byteorder != 'little':
            samples.byteswap()
        if not samples or max(abs(value) for value in samples) < 16:
            raise ValueError('speech_synthesis_unavailable')
    except (wave.Error, EOFError):
        raise ValueError('speech_synthesis_unavailable') from None


async def synthesize(text, *, voice="Meijia", file_format="m4a"):
    if (not isinstance(text, str) or not 1 <= len(text) <= 1200 or voice not in {"Meijia", "Tingting"}
            or file_format not in {"m4a", "wav"}):
        raise ValueError("invalid_reply")
    folder = DATA / "speech"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    for old in folder.glob("reply-*"):
        if old.is_file() and time.time() - old.stat().st_mtime > 300:
            old.unlink()
    token = secrets.token_hex(16)
    raw = folder / ("reply-" + token + ".aiff")
    pcm = folder / ("reply-" + token + ".wav")
    audio = folder / ("reply-" + token + "." + file_format)
    complete = False

    async def run(*command):
        process = await asyncio.create_subprocess_exec(
            *command, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        try:
            code = await asyncio.wait_for(process.wait(), 25)
        except BaseException:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        if code:
            raise ValueError("speech_synthesis_unavailable")

    try:
        await run("/usr/bin/say", "-v", voice, "-o", str(raw), "--", text)
        await run("/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@44100", str(raw), str(pcm))
        if not pcm.is_file() or pcm.stat().st_size > 8_000_000:
            raise ValueError("speech_synthesis_unavailable")
        _validate_wav(pcm)
        # Validate one common PCM payload before encoding either destination.
        # A valid-but-empty AIFF must never become an accepted M4A container.
        if file_format == 'm4a':
            await run("/usr/bin/afconvert", "-f", "m4af", "-d", "aac", str(pcm), str(audio))
        if not audio.is_file() or audio.stat().st_size > 8_000_000:
            raise ValueError("speech_synthesis_unavailable")
        audio.chmod(0o600)
        complete = True
        return audio, token
    finally:
        raw.unlink(missing_ok=True)
        if pcm != audio:
            pcm.unlink(missing_ok=True)
        if not complete:
            audio.unlink(missing_ok=True)


async def play_media_player(ha, text):
    """Only the configured speaker; preserve volume and don't interrupt media."""
    if (PRIVATE / "voice-paused").exists():
        return dict(status="paused")
    config = configuration()
    entity = config.get("speaker_entity")
    if not isinstance(entity, str) or not re.fullmatch(r'media_player\.[a-z0-9_]+', entity):
        return dict(status="unavailable")
    before = await asyncio.to_thread(ha.request, "states/" + entity)
    if before.get("state") not in {"idle", "off", "paused"}:
        return dict(status="busy")
    volume = before.get("attributes", {}).get("volume_level")
    if type(volume) not in {int, float} or not 0 <= volume <= 1:
        return dict(status="unavailable")
    audio, token = await synthesize(text)
    query = urlencode(dict(k=(PRIVATE / "viewer-key").read_text().strip()))
    url = f"http://{config['viewer_host']}:{config['viewer_port']}/audio/{token}?{query}"
    changed = False
    try:
        if (PRIVATE / "voice-paused").exists():
            return dict(status="paused")
        if volume < 0.08:
            await asyncio.to_thread(
                ha.request, "services/media_player/volume_set", dict(entity_id=entity, volume_level=0.12)
            )
            changed = True
        await asyncio.to_thread(
            ha.request,
            "services/media_player/play_media",
            dict(entity_id=entity, media_content_id=url, media_content_type="audio/mp4"),
        )
        started = False
        for _ in range(40):
            await asyncio.sleep(0.5)
            state = await asyncio.to_thread(ha.request, "states/" + entity)
            started |= state.get("state") == "playing"
            if started and state.get("state") != "playing":
                break
        return dict(status="accepted", playback_state_observed=started, physical_audio_confirmed=False)
    finally:
        if changed:
            await asyncio.to_thread(
                ha.request, "services/media_player/volume_set", dict(entity_id=entity, volume_level=volume)
            )
        audio.unlink(missing_ok=True)


# Compatibility for existing private configurations; the transport is brand independent.
play_jbl = play_media_player
