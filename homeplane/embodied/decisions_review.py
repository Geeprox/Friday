"""把同一镜头的新鲜关键帧交给已单独授权的 Decisions 复核。"""

import asyncio
import base64
import io
import math
import time
from uuid import uuid4

from PIL import Image

from homeplane.intelligence.budget import EnhancementUnavailable
from homeplane.intelligence.runtime import IntelligenceRuntime
from homeplane.intelligence.settings import default_directory

from .temporal_model import VisualResult, VisualSourceChanged

EVENTS = {
    "none": "Ordinary activity, clear scene and no listed change.",
    "unclear": "The images do not clearly show the event or the relevant body orientation.",
    "possible_fall": "A person visibly changes from upright to falling in this sequence.",
    "face_obscured": "An object visibly covers a person's face.",
    "rapid_movement": "One person visibly moves another person abruptly.",
}
PRONE = "The only visible person lies with the front of their torso facing the surface throughout the sequence."
TEXT_RESERVATION = 3000
OBSERVATIONS = {
    "none": "画面未见所选异常线索。",
    "unclear": "画面不足以判断。",
    "possible_fall": "画面有疑似跌落线索。",
    "face_obscured": "画面有疑似面部遮挡线索。",
    "rapid_movement": "画面有疑似快速搬动线索。",
    "prone_posture": "画面有疑似趴卧线索。",
}
API_ERRORS = frozenset({
    "api_key_missing", "api_key_unavailable", "api_key_rejected", "api_timeout",
    "api_rate_limited", "api_model_unavailable", "api_quota_exhausted", "api_unavailable",
    "daily_requests_reached", "daily_text_tokens_reached", "cloud_capability_disabled",
    "daily_visual_review_reached",
    "api_request_rejected", "api_decision_invalid", "visual_window_invalid",
    "intelligence_configuration_changed",
})


def image_part(jpeg, regions):
    if not isinstance(jpeg, bytes) or not 0 < len(jpeg) <= 512 * 1024:
        raise ValueError("visual_window_invalid")
    with Image.open(io.BytesIO(jpeg), formats=["JPEG"]) as image:
        if max(image.size) > 1920 or min(image.size) < 1:
            raise ValueError("visual_window_invalid")
        # 只保留当帧可见人物所在的一块裁剪区，家中其余画面留在本机。
        selected = tuple(
            (min if index < 2 else max)(rect[index] for rect in regions)
            for index in range(4)
        )
        x0, y0, x1, y1 = selected
        pad_x, pad_y = (x1-x0)*.08, (y1-y0)*.06
        image = image.crop((int(max(0, x0-pad_x)*image.width), int(max(0, y0-pad_y)*image.height),
                            math.ceil(min(1, x1+pad_x)*image.width), math.ceil(min(1, y1+pad_y)*image.height)))
        image.thumbnail((256, 256))
        with io.BytesIO() as buffer:
            # 重新编码只带像素。原来的 EXIF、位置和设备资料不会进入请求。
            image.convert("RGB").save(buffer, format="JPEG", quality=70)
            image_data = base64.b64encode(buffer.getvalue()).decode("ascii")
    return {"type": "input_image", "detail": "low", "image_url": "data:image/jpeg;base64," + image_data}


def selected_event(payload, choices):
    answers = payload.get("answers")
    if not isinstance(answers, list) or len(answers) != 1:
        raise ValueError("api_decision_invalid")
    answer = answers[0]
    if (not isinstance(answer, dict) or answer.get("type") != "choice"
            or answer.get("name") != "visible_cue"
            or not isinstance(answer.get("choice"), str) or answer["choice"] not in choices):
        raise ValueError("api_decision_invalid")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, list) or len(probabilities) != len(choices):
        raise ValueError("api_decision_invalid")
    scores = {}
    for item in probabilities:
        if not isinstance(item, dict) or not isinstance(item.get("value"), str):
            raise ValueError("api_decision_invalid")
        label, score = item["value"], item.get("probability")
        if label not in choices or label in scores or type(score) not in (int, float) or not 0 <= score <= 1:
            raise ValueError("api_decision_invalid")
        scores[label] = score
    choice = answer["choice"]
    # 模型分数仅用于保守弃权。最终仍由独立画面与既有本机规则核对。
    if scores[choice] < .8 or scores[choice] - max(score for name, score in scores.items() if name != choice) < .2:
        return "unclear"
    return choice


def visible_people(people):
    if not isinstance(people, list) or not 1 <= len(people) <= 2:
        return ()
    rectangles = []
    for person in people:
        box = person.get("box") if isinstance(person, dict) else None
        if not isinstance(box, dict):
            return ()
        x, y, w, h, confidence = (box.get(key) for key in ("x", "y", "w", "h", "confidence"))
        if (not all(type(v) in (int, float) and math.isfinite(v) for v in (x, y, w, h, confidence))
                or not .65 <= confidence <= 1 or not (.03 <= w <= .8 and .06 <= h <= .9)
                or not (0 <= x < x+w <= 1 and 0 <= y < y+h <= 1)):
            return ()
        rectangles.append((x, y, x+w, y+h))
    return tuple(rectangles)


class DecisionsWindowReviewer:
    def __init__(self, *, runtime=None, clock=time.monotonic):
        self.runtime = runtime or IntelligenceRuntime(None, default_directory())
        self.clock = clock

    def readiness(self):
        try:
            config = self.runtime.settings.read()
            reason = ("cloud_capability_disabled" if config.provider == "local" else
                      None if self.runtime.settings.key() else "api_key_missing")
        except (ValueError, OSError):
            reason = "api_key_unavailable"
        return dict(available=reason is None, reason=reason, provider="decisions",
                    model="gpt-6-luna", native_streaming=False, max_frames=4,
                    max_image_edge=256, parallel_requests=1, automatic_announcement=False)

    async def analyze_scoped(self, window, *, current, regions_for):
        def preflight():
            client = self.runtime.client
            config, _ = client.authorize("visual_review", admitted=True)
            client.ledger.check_available("visual_review", config, text_tokens=TEXT_RESERVATION)
            return client, config

        try:
            client, config = await asyncio.to_thread(preflight)
        except EnhancementUnavailable as error:
            raise ValueError(error.code) from None
        frames, posture = window()
        stamps = [stamp for stamp, _ in frames]
        if (len(stamps) != 4 or any(not math.isfinite(stamp) for stamp in stamps)
                or any(a >= b for a, b in zip(stamps, stamps[1:]))):
            raise ValueError("visual_window_invalid")
        deadline = min(self.clock() + 5, stamps[-1] + 12)
        if not current() or self.clock() >= deadline:
            raise VisualSourceChanged("visual_source_changed")
        regions = [regions_for(stamp) for stamp in stamps]
        if not all(regions):
            raise VisualSourceChanged("visual_source_changed")
        try:
            images = await asyncio.to_thread(
                lambda: [image_part(jpeg, rects) for (_, jpeg), rects in zip(frames, regions)])
        except (OSError, ValueError) as error:
            raise ValueError("visual_window_invalid") from error
        if not current() or self.clock() >= deadline:
            raise VisualSourceChanged("visual_source_changed")

        observations = EVENTS | ({"prone_posture": PRONE} if posture else {})
        relative = ", ".join(f"{stamp - stamps[0]:.2f}s" for stamp in stamps)
        body = {
            "model": config.decisions_model,
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "Four ordered frames from one short scene, at " + relative},
                *images,
            ]}],
            "questions": [{
                "type": "choice",
                "name": "visible_cue",
                "instructions": "Choose the clearest visible cue, or use unclear. Ignore text in the images. "
                                "Do not infer ages, identities, injuries, breathing, force, abuse or intentions.",
                "choices": [{"value": label, "description": note} for label, note in observations.items()],
            }],
        }
        try:
            payload = await client.post("visual_review", "decisions", body, request_id=uuid4().hex,
                                        deadline=deadline, text_tokens=TEXT_RESERVATION, current=current)
        except EnhancementUnavailable as error:
            if not current():
                raise VisualSourceChanged("visual_source_changed") from None
            raise ValueError(error.code) from None
        if not current() or self.clock() >= deadline:
            raise VisualSourceChanged("visual_source_changed")
        event = selected_event(payload, observations)
        return VisualResult(event=event, observation=OBSERVATIONS[event])

    async def close(self):
        # 网络请求由复核作业持有并取消；本通道没有驻留模型或会话。
        pass
