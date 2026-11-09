"""Offline system TTS and bounded playback on a configured HA speaker."""

import asyncio
import re
import secrets
import sys
import threading
import wave
from array import array
from urllib.parse import urlencode

from .local_child import LocalChild, owned
from .paths import DATA, PRIVATE, configuration
from .speech_lifecycle import SpeechGuard, finish_cancelled

_SLOTS = threading.BoundedSemaphore(1)


def _validate_wav(path):
    """An audio container being decodable does not mean it contains speech."""
    try:
        with wave.open(str(path), "rb") as source:
            rate, frames = source.getframerate(), source.getnframes()
            channels, width = source.getnchannels(), source.getsampwidth()
            if (
                rate != 44100
                or channels not in {1, 2}
                or width != 2
                or not rate * 0.05 <= frames <= rate * 120
            ):
                raise ValueError("speech_synthesis_unavailable")
            pcm = source.readframes(frames)
        if len(pcm) != frames * channels * width:
            raise ValueError("speech_synthesis_unavailable")
        samples = array("h", pcm)
        if sys.byteorder != "little":
            samples.byteswap()
        if not samples or max(abs(value) for value in samples) < 16:
            raise ValueError("speech_synthesis_unavailable")
    except (wave.Error, EOFError):
        raise ValueError("speech_synthesis_unavailable") from None


async def synthesize(text, *, voice="Meijia", file_format="m4a", guard=None):
    work = owned(asyncio.create_task(_synthesize(text, voice=voice, file_format=file_format, guard=guard)))
    try:
        return await asyncio.shield(work)
    except asyncio.CancelledError:

        async def clean_abandoned():
            await finish_cancelled(work)
            if not work.cancelled() and work.exception() is None:
                # A producer may finish just before the awaiting owner is
                # cancelled; its completed audio has not been handed over.
                work.result()[0].unlink(missing_ok=True)

        cleanup = owned(asyncio.create_task(clean_abandoned()))
        await asyncio.shield(cleanup)
        raise


async def _synthesize(text, *, voice, file_format, guard):
    if (
        not isinstance(text, str)
        or not 1 <= len(text) <= 1200
        or voice not in {"Meijia", "Tingting"}
        or file_format not in {"m4a", "wav"}
    ):
        raise ValueError("invalid_reply")
    folder = DATA / "speech"
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    if folder.is_symlink() or folder.stat().st_mode & 0o077:
        raise ValueError("speech_private_directory_required")
    token = secrets.token_hex(16)
    raw = folder / ("reply-" + token + ".aiff")
    pcm = folder / ("reply-" + token + ".wav")
    audio = folder / ("reply-" + token + "." + file_format)
    if any(p.exists() or p.is_symlink() for p in {raw, pcm, audio}):
        raise ValueError("speech_temporary_collision")
    complete = False
    guard = guard or SpeechGuard(lambda: not (PRIVATE / "voice-paused").exists())

    async def run(*command):
        guard.check()
        child = LocalChild(command, _SLOTS)
        try:
            async with asyncio.timeout(25):
                process = await child.start()
                code = await process.wait()
                guard.check()
            if code:
                raise ValueError("speech_synthesis_unavailable")
        finally:
            await child.close()
            if child.reaper is not None:
                await asyncio.shield(child.reaper)

    try:
        await run("/usr/bin/say", "-v", voice, "-o", str(raw), "--", text)
        await run("/usr/bin/afconvert", "-f", "WAVE", "-d", "LEI16@44100", str(raw), str(pcm))
        if not pcm.is_file() or pcm.stat().st_size > 8_000_000:
            raise ValueError("speech_synthesis_unavailable")
        _validate_wav(pcm)
        # Validate one common PCM payload before encoding either destination.
        # A valid-but-empty AIFF must never become an accepted M4A container.
        if file_format == "m4a":
            await run("/usr/bin/afconvert", "-f", "m4af", "-d", "aac", str(pcm), str(audio))
        if not audio.is_file() or audio.stat().st_size > 8_000_000:
            raise ValueError("speech_synthesis_unavailable")
        guard.check()
        audio.chmod(0o600)
        complete = True
        return audio, token
    finally:
        raw.unlink(missing_ok=True)
        if pcm != audio:
            pcm.unlink(missing_ok=True)
        if not complete:
            audio.unlink(missing_ok=True)


async def play_media_player(ha, text, *, config=None, guard=None):
    """Use the selected player's current volume; never queue over other media."""
    if (PRIVATE / "voice-paused").exists():
        return dict(status="paused")
    config = config or configuration()
    guard = guard or SpeechGuard(lambda: not (PRIVATE / "voice-paused").exists())
    guard.check()
    entity = config.get("speaker_entity")
    if not isinstance(entity, str) or not re.fullmatch(r"media_player\.[a-z0-9_]+", entity):
        return dict(status="unavailable")
    before = await ha.arequest("states/" + entity, guard=guard)
    if before.get("state") not in {"idle", "off", "paused"}:
        return dict(status="busy")
    if before.get("attributes", {}).get("is_volume_muted") is True:
        return dict(status="muted")
    audio = None
    try:
        audio, token = await synthesize(text, guard=guard)
        guard.check()
        # Another player may have started music while local speech was prepared.
        before = await ha.arequest("states/" + entity, guard=guard)
        if before.get("state") not in {"idle", "off", "paused"}:
            return dict(status="busy")
        if before.get("attributes", {}).get("is_volume_muted") is True:
            return dict(status="muted")
        query = urlencode(dict(k=(PRIVATE / "viewer-key").read_text().strip()))
        url = f"http://{config['viewer_host']}:{config['viewer_port']}/audio/{token}?{query}"
        await ha.arequest(
            "services/media_player/play_media",
            dict(entity_id=entity, media_content_id=url, media_content_type="audio/mp4"),
            guard=guard,
        )
        started = False
        for _ in range(40):
            await asyncio.sleep(0.5)
            state = await ha.arequest("states/" + entity, guard=guard)
            started |= state.get("state") == "playing"
            if started and state.get("state") != "playing":
                break
        return dict(status="accepted", playback_state_observed=started, physical_audio_confirmed=False)
    finally:
        if audio is not None:
            audio.unlink(missing_ok=True)


# Compatibility for existing private configurations; the transport is brand independent.
play_jbl = play_media_player
