"""Conservative filename presentation; never changes audio or source identity."""
import re


def song_title(value: str) -> str:
    title = value.strip()
    if not re.search(r"\.(flac|mp3|m4a|wav|aac|ogg|ape|alac)$", title, re.I):
        return title
    title = re.sub(r"\.(flac|mp3|m4a|wav|aac|ogg|ape|alac)$", "", title, flags=re.I)
    title = re.sub(r"^\d{1,3}[.、 _-]+", "", title)
    if " - " in title:
        artist, candidate = title.split(" - ", 1)
        if artist and candidate:
            title = candidate
    return title.strip()
