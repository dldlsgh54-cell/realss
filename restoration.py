import mmap
import subprocess
import tempfile
from collections import OrderedDict
from dataclasses import dataclass
from hashlib import blake2b

import cv2
import numpy as np
from rapidocr import RapidOCR


CONTEXT_FRAMES = 20
SAMPLE_STEP = 4


@dataclass(frozen=True)
class TextRegion:
    box: tuple[int, int, int, int]
    panel: bool = False


def display_size(stream):
    width, height = stream['width'], stream['height']
    ratio = stream.get('sample_aspect_ratio', '1:1').split(':')
    if len(ratio) == 2 and all(value.isdigit() and int(value) for value in ratio):
        width *= int(ratio[0]) / int(ratio[1])
    rotation = next((side['rotation'] for side in stream.get('side_data_list', ()) if 'rotation' in side), 0)
    if round(rotation) % 180 == 90:
        width, height = height, width
    return width, height


class SubtitleRestorer:
    def __init__(self, ff, source, video, region, fps, work, detector=None):
        self.ff = ff
        self.source = source
        self.region = region
        self.fps = fps
        self.work = work
        self.cache = {}
        self.frame_cache = OrderedDict()
        width, height = display_size(video)
        scale = min(1, 1920 / max(width, height))
        self.width = max(2, round(width * scale / 2) * 2)
        self.height = max(2, round(height * scale / 2) * 2)
        self.detector = detector if detector is not None else RapidOCR(params={
            'EngineConfig.onnxruntime.intra_op_num_threads': 3,
            'EngineConfig.onnxruntime.inter_op_num_threads': 1,
            'Global.log_level': 'error',
        })

    def boxes(self, frame):
        height, width = frame.shape[:2]
        x, y, right, bottom = 0, 0, width, height
        if self.region:
            left, top, end_x, end_y = self.region
            x, y = round(left * width), round(top * height)
            right, bottom = round(end_x * width), round(end_y * height)
        crop = frame[y:bottom, x:right]
        if min(crop.shape[:2]) < 10:
            return ()
        scale = min(1.55 if self.region else 1.22, 672 / max(crop.shape[:2]))
        image = cv2.resize(crop, None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
        # Reuse detections when the same image is used in more than one cut or output.
        signature = blake2b(image, digest_size=16).digest()
        if signature in self.frame_cache:
            return self.frame_cache[signature]
        detected = self.detect_boxes(frame, image, scale, x, y)
        self.frame_cache[signature] = detected
        if len(self.frame_cache) > 1800:
            self.frame_cache.popitem(last=False)
        return detected

    def detect_boxes(self, frame, image, scale, x, y):
        height, width = frame.shape[:2]
        result = self.detector(image, use_det=True, use_cls=False, use_rec=False)
        if result.boxes is None:
            return ()
        found = []
        for polygon in result.boxes:
            points = np.rint(np.asarray(polygon) / scale).astype(np.int32)
            left, top, w, h = cv2.boundingRect(points)
            center = (y + top + h / 2) / height
            if w < 11 or h < 7 or w < .85 * h or h > .24 * height:
                continue
            if not self.region and (w < .035 * width or (.28 < center < .54 and w < .22 * width)):
                continue
            pad = max(2, round(h * .12))
            box = (max(0, x + left - pad), max(0, y + top - pad),
                   min(width, x + left + w + pad), min(height, y + top + h + pad))
            panel = caption_panel(frame, box)
            candidate = TextRegion(panel or box, panel is not None)
            if candidate not in found:
                found.append(candidate)
        return tuple(sorted(found, key=lambda region: region.box))

    def prepare(self, start, window, duration):
        key = (round(start, 5), round(window, 5))
        if key in self.cache:
            return self.cache[key]
        raw = self.work / 'frames.bgr'
        output = self.work / f'restored-{len(self.cache):03}.mp4'
        lead = min(CONTEXT_FRAMES, int(start * self.fps))
        seek = max(0, start - lead / self.fps)
        count = max(1, round(window * self.fps))
        length = min(duration - seek, (lead + count + CONTEXT_FRAMES + 1) / self.fps)
        try:
            self.ff('-ss', f'{seek:.8f}', '-i', self.source, '-map', '0:V:0', '-an', '-sn', '-dn',
                    '-t', f'{length:.8f}', '-filter_threads', 2, '-vf',
                    f'fps={self.fps}:start_time=0,scale={self.width}:{self.height},setsar=1',
                    '-pix_fmt', 'bgr24', '-c:v', 'rawvideo', '-threads', 1, '-f', 'rawvideo', raw)
            removed = self.restore_segment(raw, output, lead, count)
        finally:
            raw.unlink(missing_ok=True)
        self.cache[key] = output if removed else None
        return self.cache[key]

    def restore_segment(self, raw, output, lead, count):
        frame_size = self.width * self.height * 3
        nframes = raw.stat().st_size // frame_size
        if not nframes:
            return 0
        with raw.open('rb') as reader, mmap.mmap(reader.fileno(), 0, access=mmap.ACCESS_READ) as contents:
            frames = np.ndarray((nframes, self.height, self.width, 3), dtype=np.uint8, buffer=contents)
            return self.restore_frames(frames, output, lead, count)

    def restore_frames(self, frames, output, lead, count):
        nframes = len(frames)
        samples = list(range(0, nframes, SAMPLE_STEP))
        if samples[-1] != nframes - 1:
            samples.append(nframes - 1)
        detected = {index: self.boxes(frames[index]) for index in samples}
        begin = min(lead, nframes - 1)
        end = min(begin + count, nframes)
        targets = {}
        for first, last in zip(samples, samples[1:]):
            left, right = detected[first], detected[last]
            for index in range(max(first, begin), min(last, end)):
                if index in detected:
                    targets[index] = detected[index]
                elif not left and not right:
                    continue
                elif comparable(left, right):
                    targets[index] = left if index - first <= last - index else right
                else:
                    targets[index] = self.boxes(frames[index])
        if end - 1 in detected:
            targets[end - 1] = detected[end - 1]
        total = sum(bool(boxes) for boxes in targets.values())
        if total:
            self.encode(output, (restore_frame(frames, index, targets.get(index, ()), detected)
                                 for index in range(begin, end)))
        return total

    def encode(self, output, frames):
        command = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y', '-f', 'rawvideo',
                   '-pix_fmt', 'bgr24', '-s', f'{self.width}x{self.height}', '-r', str(self.fps),
                   '-i', 'pipe:0', '-an', '-c:v', 'libx264', '-pix_fmt', 'yuv420p',
                   '-preset', 'ultrafast', '-crf', '14', '-threads', '4', str(output)]
        with tempfile.TemporaryFile() as errors:
            with subprocess.Popen(command, stdin=subprocess.PIPE, stderr=errors,
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0)) as encoder:
                try:
                    with encoder.stdin:
                        for frame in frames:
                            encoder.stdin.write(frame.tobytes())
                except BrokenPipeError:
                    pass
                if encoder.wait():
                    errors.seek(0)
                    detail = errors.read().decode('utf-8', errors='replace')
                    raise RuntimeError(f'자막을 제거한 영상 저장 오류\n{detail[-1500:]}')


def intersection(first, second):
    return max(0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0, min(first[3], second[3]) - max(first[1], second[1]))


def area(box):
    return (box[2] - box[0]) * (box[3] - box[1])


def comparable(first, second):
    return len(first) == len(second) and all(
        a.panel == b.panel and intersection(a.box, b.box) > .74 * max(area(a.box), area(b.box))
        for a, b in zip(first, second))


def caption_panel(frame, box):
    left, top, right, bottom = box
    reach = max(7, round((bottom - top) * .6))
    x, y = max(0, left - reach - 3), max(0, top - reach - 3)
    crop = frame[y:min(frame.shape[0], bottom + reach + 3), x:min(frame.shape[1], right + reach + 3)]
    left, right, top, bottom = left - x, right - x, top - y, bottom - y
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY).astype(np.int16)

    def straight_edge(image, expected, first, last, polarity=0):
        strongest = None
        for line in range(max(2, expected - reach), min(image.shape[0] - 2, expected + reach + 1)):
            delta = image[line + 1, first:last] - image[line - 2, first:last]
            contrast = np.median(delta)
            direction = 1 if contrast > 0 else -1
            if abs(contrast) < 14 or (polarity and direction != polarity):
                continue
            support = np.mean(direction * delta > 10)
            if support > .72:
                score = abs(contrast) * support - .18 * abs(line - expected)
                if strongest is None or score > strongest[0]:
                    strongest = score, line, direction
        return strongest

    inset = max(3, (right - left) // 9)
    upper = straight_edge(gray, top, left + inset, right - inset)
    if upper is None:
        return None
    direction = upper[2]
    lower = straight_edge(gray, bottom, left + inset, right - inset, -direction)
    if lower is None or lower[1] - upper[1] < .65 * (bottom - top):
        return None
    span = lower[1] - upper[1]
    first, last = upper[1] + span // 5, lower[1] - span // 5
    start = straight_edge(gray.T, left, first, last, direction)
    end = straight_edge(gray.T, right, first, last, -direction)
    if start is None or end is None or end[1] - start[1] < .78 * (right - left):
        return None
    return (max(0, min(left, start[1]) + x - 3), max(0, min(top, upper[1]) + y - 3),
            min(frame.shape[1], max(right, end[1]) + x + 3),
            min(frame.shape[0], max(bottom, lower[1]) + y + 3))


def rectangle_mask(shape, boxes, x, y):
    mask = np.zeros(shape[:2], dtype=np.uint8)
    for left, top, right, bottom in boxes:
        left, top = max(0, left - x), max(0, top - y)
        right, bottom = min(shape[1], right - x), min(shape[0], bottom - y)
        if left < right and top < bottom:
            mask[top:bottom, left:right] = 255
    return mask


def subtitle_ink(patch, box_mask, text_height):
    # Local bright/dark contrast finds glyph fill, outlines, and shadows inside the detector boxes.
    size = max(9, min(33, (int(text_height * .48) | 1)))
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (size, size))
    contrast = np.zeros(box_mask.shape, dtype=np.uint8)
    for channel in cv2.split(patch):
        bright = cv2.morphologyEx(channel, cv2.MORPH_TOPHAT, kernel)
        dark = cv2.morphologyEx(channel, cv2.MORPH_BLACKHAT, kernel)
        contrast = cv2.max(contrast, cv2.max(bright, dark))
    bright = (cv2.cvtColor(patch, cv2.COLOR_BGR2GRAY) >= 195) & (contrast >= 15) & (box_mask > 0)
    columns = np.flatnonzero(np.any(bright, axis=0))
    width = np.count_nonzero(np.any(box_mask, axis=0))
    if (len(columns) and columns[-1] - columns[0] >= .55 * width
            and np.count_nonzero(bright) >= .035 * np.count_nonzero(box_mask)):
        radius = max(3, min(10, round(text_height * .075)))
        kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius * 2 + 1,) * 2)
        return cv2.dilate(bright.astype(np.uint8) * 255, kernel)
    selected = np.where((contrast >= 13) & (box_mask > 0), 255, 0).astype(np.uint8)
    grow = max(3, min(11, int(text_height * .14) | 1))
    selected = cv2.dilate(selected, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow, grow)))
    return selected if np.count_nonzero(selected) >= .05 * np.count_nonzero(box_mask) else box_mask


def background_patch(target, donor, target_mask, donor_mask, required):
    shape = target.shape[:2]
    target_gray = cv2.cvtColor(target, cv2.COLOR_BGR2GRAY)
    donor_gray = cv2.cvtColor(donor, cv2.COLOR_BGR2GRAY)
    safe = cv2.bitwise_not(cv2.bitwise_or(target_mask, donor_mask))
    matrix = np.array([[1, 0, 0], [0, 1, 0]], dtype=np.float32)
    corners = cv2.goodFeaturesToTrack(donor_gray, 90, .015, 5, mask=safe)
    if corners is not None and len(corners) >= 5:
        tracked, valid, _ = cv2.calcOpticalFlowPyrLK(donor_gray, target_gray, corners, None)
        if tracked is not None:
            keep = valid.ravel() == 1
            if np.count_nonzero(keep) >= 5:
                transform, inliers = cv2.estimateAffinePartial2D(corners[keep], tracked[keep],
                                                                ransacReprojThreshold=1.8)
                if transform is not None and np.count_nonzero(inliers) / np.count_nonzero(keep) >= .60:
                    scale = np.hypot(transform[0, 0], transform[0, 1])
                    if .90 <= scale <= 1.10 and np.linalg.norm(transform[:, 2]) <= 38:
                        matrix = transform
    size = (shape[1], shape[0])
    aligned = cv2.warpAffine(donor, matrix, size, flags=cv2.INTER_LINEAR)
    forbidden = cv2.warpAffine(donor_mask, matrix, size, flags=cv2.INTER_NEAREST, borderValue=255)
    forbidden = cv2.dilate(forbidden, np.ones((3, 3), np.uint8))
    check = (target_mask == 0) & (forbidden == 0)
    usable = (required > 0) & (forbidden == 0)
    if np.count_nonzero(check) < .16 * check.size or not np.any(usable):
        return None
    error = cv2.absdiff(target, aligned)[check][::4].mean(axis=1)
    if not len(error) or np.median(error) > 12 or np.mean(error > 26) > .22:
        return None
    return aligned, usable


def restore_frame(frames, index, regions, detected):
    frame = frames[index]
    if not regions:
        return frame
    output = frame.copy()
    height, width = frame.shape[:2]
    neighbors = sorted((i for i in detected if 0 < abs(i - index) <= CONTEXT_FRAMES), key=lambda i: abs(i - index))
    boxes = tuple(region.box for region in regions)
    for region in regions:
        box = region.box
        left, top, right, bottom = box
        h = bottom - top
        margin = max(18, min(45, h))
        x, y = max(0, left - margin), max(0, top - margin)
        slice_y = slice(y, min(height, bottom + margin))
        slice_x = slice(x, min(width, right + margin))
        patch = output[slice_y, slice_x].copy()
        area_mask = rectangle_mask(patch.shape, (box,), x, y)
        border_mask = rectangle_mask(patch.shape, boxes, x, y)
        ink = area_mask if region.panel else subtitle_ink(patch, area_mask, h)
        pending = ink.copy()
        possible = [i for i in neighbors if sum(intersection(box, other.box) for other in detected[i]) < .8 * area(box)]
        for donor in possible[:3]:
            donor_mask = rectangle_mask(patch.shape, (other.box for other in detected[donor]), x, y)
            repair = background_patch(frame[slice_y, slice_x], frames[donor][slice_y, slice_x],
                                      border_mask, donor_mask, pending)
            if repair is None:
                continue
            pixels, valid = repair
            patch[valid] = pixels[valid]
            pending[valid] = 0
            if not np.any(pending):
                break
        if np.any(pending):
            patch = cv2.inpaint(patch, pending, max(2, min(5, h / 9)), cv2.INPAINT_NS)
        # Blend only just outside repaired text edges to avoid a visible hard outline.
        edge = cv2.GaussianBlur(ink, (5, 5), 0).astype(np.float32) / 255
        edge[ink > 0] = 1
        original = output[slice_y, slice_x]
        original[:] = np.rint(patch * edge[..., None] + original * (1 - edge[..., None])).astype(np.uint8)
    return output
