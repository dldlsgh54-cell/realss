import re
import unicodedata
from dataclasses import dataclass


@dataclass(frozen=True)
class Speech:
    text: str
    start: float
    end: float
    source: int = 0


FILLER = re.compile(r"^(?:(?:음+|어+|아+|그니까|뭐랄까|um+|uh+)[,.…!?]*(?:\s+|$))+", re.I)
OPENING = re.compile(r"^(?:안녕|반갑|구독|좋아요|hello|welcome|여러분 안녕)", re.I)
HOOK = re.compile(r"\?|방법|핵심|이유|실수|문제|결과|비결|처음|\d|how\b|why\b|tip\b", re.I)
SENTENCE_END = re.compile(r'[.!?。！？…][”’"]?$')
KOREAN_END = re.compile(r'(?:습니다|입니다|합니다|했어요|할게요|있어요|없어요|돼요|하세요|보세요|거예요)$')
DEPENDENT_START = re.compile(r'^(?:그래서|그런데|그러면|그러니까|그다음|다음으로|두 번째|세 번째|마지막으로|이것도|이거는|그게|그걸)\b')
OPENING_FEATURES = {
    'reels': re.compile(r'결과|전후|변화|바뀌|방법|핵심|[?？]'),
    'tiktok': re.compile(r'왜|실수|문제|비결|꿀팁|[?？]'),
    'naver': re.compile(r'가격|위치|가는 법|예약|메뉴|준비물|영업|사용법|비교|[0-9]\s*(?:원|분|개|층|번|단계)\b'),
}


def highlight_word(line):
    words = list(re.finditer(r'\S+', line))
    return next((word.span() for word in words if HOOK.search(word.group())), None)


def ends_sentence(text):
    return bool(SENTENCE_END.search(text) or KOREAN_END.search(text))


def text_width(text):
    return sum(0 if unicodedata.combining(char) else
               2 if unicodedata.east_asian_width(char) in 'WF' else 1 for char in text)


def caption_lines(text, limit=24):
    lines = []
    line = ''
    for word in text.split():
        if text_width(word) > limit:
            if line:
                lines.append(line)
                line = ''
            for char in word:
                if text_width(line + char) > limit:
                    lines.append(line)
                    line = ''
                line += char
            continue
        candidate = f'{line} {word}' if line else word
        if text_width(candidate) > limit:
            lines.append(line)
            line = word
        else:
            line = candidate
    if line:
        lines.append(line)
    if len(lines) == 2 and len(text.split()) >= 3:
        words = text.split()
        alternatives = []
        for split in range(1, len(words)):
            left, right = ' '.join(words[:split]), ' '.join(words[split:])
            widths = text_width(left), text_width(right)
            if max(widths) <= limit:
                punctuation = 5 if left.endswith((',', '，', ':', '、')) else 0
                alternatives.append((abs(widths[0] - widths[1]) - punctuation, left, right))
        if alternatives:
            _, left, right = min(alternatives)
            return [left, right]
    return lines


def fit_caption_cues(cues, limit):
    result = []
    for cue in cues:
        lines = caption_lines(cue.text, limit)
        if len(lines) <= 2:
            result.append(cue)
            continue
        blocks = [' '.join(lines[index:index + 2]) for index in range(0, len(lines), 2)]
        if len(blocks) == 2:
            words = cue.text.split()
            pairs = [(' '.join(words[:split]), ' '.join(words[split:])) for split in range(1, len(words))]
            balanced = [pair for pair in pairs if all(len(caption_lines(part, limit)) <= 2 for part in pair)]
            if balanced:
                blocks = min(balanced, key=lambda pair: abs(text_width(pair[0]) - text_width(pair[1])))
        total = sum(map(text_width, blocks))
        offset = 0
        for block in blocks:
            end = offset + text_width(block)
            duration = cue.end - cue.start
            result.append(Speech(block, cue.start + duration * offset / total,
                                 cue.start + duration * end / total, cue.source))
            offset = end
    return result


def group_words(words):
    cues = []
    group = []
    for word in words:
        candidate = ' '.join(item.text for item in [*group, word])
        if group and (len(caption_lines(candidate)) > 2 or
                      word.start - group[-1].end > 0.48 or
                      word.end - group[0].start > 3.25):
            cues.append(Speech(' '.join(item.text for item in group), group[0].start, group[-1].end))
            group = []
        group.append(word)
        if ends_sentence(word.text):
            cues.append(Speech(' '.join(item.text for item in group), group[0].start, group[-1].end))
            group = []
    if group:
        cues.append(Speech(' '.join(item.text for item in group), group[0].start, group[-1].end))

    cleaned = []
    for cue in cues:
        line = FILLER.sub('', cue.text).strip()
        if not any(char.isalnum() for char in line):
            continue
        lines = caption_lines(line)
        for index in range(0, len(lines), 2):
            start = cue.start + (cue.end - cue.start) * index / len(lines)
            end = cue.start + (cue.end - cue.start) * min(index + 2, len(lines)) / len(lines)
            cleaned.append(Speech(' '.join(lines[index:index + 2]), start, end))
    return cleaned


def choose_highlight(cues, target, profile):
    if not cues:
        return []
    opening_indices = [index for index, cue in enumerate(cues) if not OPENING.search(cue.text)]
    featured = OPENING_FEATURES[profile]
    best = []
    best_rank = None
    for start in range(len(cues)):
        if start and not (ends_sentence(cues[start - 1].text) or
                          cues[start].start - cues[start - 1].end > .48):
            continue
        estimate = 0.0
        score = 0.0
        for end in range(start, len(cues)):
            cue = cues[end]
            estimate += max(0.85, cue.end - cue.start) + .12
            score += (min(1.1, text_width(cue.text) / 35) + .20 * bool(HOOK.search(cue.text))
                      + (.40 if profile == 'naver' else .12) * bool(featured.search(cue.text)))
            if estimate > target + 4 and end > start:
                break
            ending = ends_sentence(cue.text)
            opening = cues[start].text
            candidate = (score + .8 * bool(HOOK.search(opening)) + 1.8 * bool(featured.search(opening))
                         - 3 * bool(OPENING.search(opening)) - 2.1 * bool(DEPENDENT_START.search(opening))
                         - .24 * abs(target - estimate))
            rank = (ending and estimate <= target + .75, candidate)
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best = cues[start:end + 1]
    if opening_indices and best and all(OPENING.search(cue.text) for cue in best):
        first = opening_indices[0]
        end = next((index + 1 for index in range(first, len(cues)) if ends_sentence(cues[index].text)), len(cues))
        return cues[first:end]
    return best


def script_cues(script, source_duration):
    parts = []
    for sentence in re.split(r'[\r\n]+|(?<=[.!?。！？])\s+', script.strip()):
        lines = caption_lines(sentence)
        parts.extend(' '.join(lines[index:index + 2]) for index in range(0, len(lines), 2))
    parts = [part for part in parts if any(char.isalnum() for char in part)]
    weights = [max(12, text_width(part)) for part in parts]
    total = sum(weights)
    cursor = 0
    cues = []
    for part, weight in zip(parts, weights):
        cues.append(Speech(part, source_duration * cursor / total,
                           source_duration * (cursor + weight) / total))
        cursor += weight
    return cues
