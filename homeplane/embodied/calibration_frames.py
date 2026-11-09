"""Immutable editor frames and bounded private edit leases; no native work."""

import base64
import copy
import secrets
import time
from collections import OrderedDict


class CalibrationFrames:
    def __init__(self, *, clock=time.monotonic, wall=time.time, capacity=4, ttl=900):
        self.clock, self.wall, self.capacity, self.ttl = clock, wall, capacity, ttl
        self.current = None
        self.leases = OrderedDict()

    def clear(self):
        self.current = None
        self.leases.clear()

    def publish(self, draft, picture, meta, source, received_at):
        self.current = None
        if draft is None:
            return
        self.current = dict(
            draft=copy.deepcopy(draft),
            picture=picture,
            meta=copy.deepcopy(meta),
            source=dict(source),
            received_at=received_at,
        )

    def prune(self):
        for key, lease in tuple(self.leases.items()):
            if not 0 <= self.clock() - lease["opened"] <= self.ttl:
                self.leases.pop(key, None)

    def open(self, source):
        self.prune()
        value = self.current
        if value is None or value["source"] != source or not 0 <= self.wall() - value["received_at"] <= 1.5:
            raise ValueError("calibration_source_changed")
        key = secrets.token_hex(16)
        while len(self.leases) >= self.capacity:
            self.leases.popitem(last=False)
        self.leases[key] = dict(value, opened=self.clock())
        return dict(
            copy.deepcopy(value["draft"]),
            _source=dict(source, frame=key),
            _reference=dict(
                jpeg=base64.b64encode(value["picture"]).decode(), meta=copy.deepcopy(value["meta"])
            ),
        )

    def valid(self, source, current):
        self.prune()
        if (
            not isinstance(source, dict)
            or set(source) != {"worker", "generation", "motion_generation", "frame"}
            or not isinstance(source["frame"], str)
            or any(type(source[k]) is not int for k in ("generation", "motion_generation"))
        ):
            return False
        lease = self.leases.get(source["frame"])
        return lease is not None and lease["source"] == current == {
            k: v for k, v in source.items() if k != "frame"
        }

    def resolve(self, source, reference, current):
        if not self.valid(source, current):
            raise ValueError("calibration_source_changed")
        picture = self.leases[source["frame"]]["picture"]
        if not secrets.compare_digest(picture, reference):
            raise ValueError("calibration_source_changed")
        return picture
