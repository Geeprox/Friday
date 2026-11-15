"""面部网格的实验性朝向与双眼几何线索；不是身份或精确视线估计。"""

import math


def face_rows(result, width, height):
    rows = []
    matrices = result.facial_transformation_matrixes
    for points, matrix in zip(result.face_landmarks[:6], matrices[:6]):
        if len(points) != 478:
            continue
        coordinates = [(p.x, p.y) for p in points]
        if any(not math.isfinite(v) or not 0 <= v <= 1 for pair in coordinates for v in pair):
            continue
        xs, ys = zip(*coordinates)
        x, y, w, h = min(xs), min(ys), max(xs)-min(xs), max(ys)-min(ys)
        if w <= 0 or h <= 0:
            continue
        try:
            rotation = [[float(matrix[i][j]) for j in range(3)] for i in range(3)]
            if any(not math.isfinite(v) for row in rotation for v in row):
                continue
            yaw = math.atan2(rotation[0][2], rotation[2][2])
            pitch = math.atan2(-rotation[1][2], math.hypot(rotation[1][0], rotation[1][1]))
        except (IndexError, TypeError, ValueError):
            continue
        eyes = []
        for side, corners, iris in [('right', (33, 133, 159, 145), 468),
                                    ('left', (362, 263, 386, 374), 473)]:
            eye = [points[index] for index in corners]
            lo_x, hi_x = min(p.x for p in eye), max(p.x for p in eye)
            lo_y, hi_y = min(p.y for p in eye), max(p.y for p in eye)
            ew, eh = (hi_x-lo_x)*width, (hi_y-lo_y)*height
            usable = ew >= 12 and eh >= 3 and .14 <= eh/ew <= .8
            pupil = points[iris]
            offset_x = (pupil.x-lo_x)/(hi_x-lo_x)-.5 if ew else 9
            offset_y = (pupil.y-lo_y)/(hi_y-lo_y)-.5 if eh else 9
            eyes.append(dict(side=side, usable=usable, width_px=ew, height_px=eh,
                             offset_x=offset_x, offset_y=offset_y, pupil_present=True))
        clear = all(e['usable'] for e in eyes)
        centered = clear and all(abs(e['offset_x']) < .18 and abs(e['offset_y']) < .32 for e in eyes)
        consistent = abs(eyes[0]['offset_x']-eyes[1]['offset_x']) < .16
        facing = h*height >= 35 and abs(yaw) < .35 and abs(pitch) < .55
        rows.append(dict(box=dict(x=x, y=y, w=w, h=h), face_pixels=h*height,
                         yaw=yaw, pitch=pitch, facing_candidate=facing,
                         eyes=eyes, eyes_clear=clear, gaze_candidate=facing and centered and consistent,
                         gaze_method='head_and_iris_geometry_experimental',
                         gaze_source_size=[width, height], sample_age_ms=0,
                         confidence_source='model_presence_threshold_no_per_point_score'))
    return rows
