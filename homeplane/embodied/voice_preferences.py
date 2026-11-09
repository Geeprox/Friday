"""Shared voice preferences. Reading or saving never starts media or models."""
import fcntl
import json
import os
from pathlib import Path
import tempfile
import unicodedata

from pydantic import Field, field_validator

from homeplane.contracts import Contract
from .paths import PRIVATE


def wake_word(value):
    if not isinstance(value,str) or any(unicodedata.category(c).startswith('C') for c in value):
        raise ValueError('invalid_wake_word')
    value=' '.join(unicodedata.normalize('NFKC',value).split())
    if not 2<=len(value)<=24 or any(c!=' ' and unicodedata.category(c)[0] not in {'L','N','M'} for c in value):
        raise ValueError('invalid_wake_word')
    return value


class VoicePreferences(Contract):
    revision:int=Field(default=0,ge=0,strict=True)
    wake_word:str=Field(default='Friday',min_length=2,max_length=24,strict=True)

    @field_validator('wake_word')
    @classmethod
    def valid_word(cls,value):return wake_word(value)


class VoiceEdit(Contract):
    expected_revision:int=Field(ge=0,strict=True)
    wake_word:str=Field(min_length=2,max_length=24,strict=True)

    @field_validator('wake_word')
    @classmethod
    def valid_word(cls,value):return wake_word(value)


class VoicePreferencesStore:
    def __init__(self,private=None):
        self.private=Path(private if private is not None else PRIVATE)
        self.path=self.private/'voice-preferences.json'

    def read(self):
        if self.private.is_symlink() or self.path.is_symlink():raise ValueError('voice_preferences_unavailable')
        try:fd=os.open(self.path,os.O_RDONLY|os.O_NOFOLLOW)
        except FileNotFoundError:return VoicePreferences()
        with os.fdopen(fd,'rb') as stream:
            if os.fstat(stream.fileno()).st_mode&0o077:raise ValueError('voice_preferences_unavailable')
            body=stream.read(4097)
        if len(body)>4096:raise ValueError('voice_preferences_unavailable')
        return VoicePreferences.model_validate_json(body)

    def save(self,edit):
        if self.private.is_symlink():raise ValueError('voice_preferences_unavailable')
        self.private.mkdir(parents=True,exist_ok=True,mode=0o700)
        fd=os.open(self.private/'voice-preferences.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
        with os.fdopen(fd,'a+') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            current=self.read()
            if current.revision!=edit.expected_revision:raise ValueError('voice_preferences_changed')
            updated=VoicePreferences(revision=current.revision+1,wake_word=edit.wake_word)
            descriptor,name=tempfile.mkstemp(prefix='.voice-preferences-',dir=self.private)
            try:
                with os.fdopen(descriptor,'w') as stream:
                    json.dump(updated.model_dump(),stream,ensure_ascii=False)
                    stream.flush();os.fsync(stream.fileno())
                os.replace(name,self.path)
            finally:Path(name).unlink(missing_ok=True)
            return updated


def read_preferences():return VoicePreferencesStore().read()


def install(router,*,private=None):
    from fastapi import HTTPException
    owner=VoicePreferencesStore(private)
    @router.get('/api/embodied/voice-preferences')
    async def status():
        try:return owner.read()
        except (ValueError,OSError):raise HTTPException(409,detail='voice_preferences_unavailable')from None
    @router.post('/api/embodied/voice-preferences')
    async def save(edit:VoiceEdit):
        try:return owner.save(edit)
        except (ValueError,OSError):raise HTTPException(409,detail='voice_preferences_not_saved')from None
    return owner
