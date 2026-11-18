import asyncio
import json
import math
import shutil
import subprocess
import tempfile
import wave
from array import array
from dataclasses import dataclass, replace
from pathlib import Path

from editor import Speech, caption_lines, choose_highlight, ends_sentence, fit_caption_cues, highlight_word
from planning import custom_story, keywords, multi_source_story, polish_stories, sentences
from recognition import transcribe


FPS = 30
RATE = 48000
FRAME_SAMPLES = RATE // FPS
BGM_GAIN = 0.15
ROOT = Path(__file__).resolve().parent
VOICE_OPTIONS = {'한국어 여성 · 신경망': 'ko-KR-SunHiNeural',
                 '한국어 남성 · 신경망': 'ko-KR-InJoonNeural', '한국어 · Windows 오프라인': 'local'}


@dataclass(frozen=True)
class Job:
    source: Path
    output_dir: Path
    target: int = 30
    script: str = ''
    music: Path | None = None
    voice: str = 'ko-KR-SunHiNeural'
    layout: str = 'auto'
    subtitle_region: tuple[float, float, float, float] | None = None
    extra_sources: tuple[Path, ...] = ()
    subtitle_regions: tuple[tuple[float, float, float, float] | None, ...] = ()
    mirrored: frozenset[int] = frozenset()
    output_profiles: frozenset[str] | None = None


@dataclass(frozen=True)
class Source:
    path: Path
    video: dict
    duration: float
    layout: str
    has_audio: bool
    region: tuple[float, float, float, float] | None
    mirrored: bool


@dataclass(frozen=True)
class Beat:
    speech: Speech
    pcm: array
    offset: int
    frames: int
    lead: int

    @property
    def start(self):
        return (self.offset * FRAME_SAMPLES + self.lead) / RATE

    @property
    def end(self):
        return self.start + len(self.pcm) / RATE


@dataclass(frozen=True)
class Platform:
    profile: str
    name: str
    height: int
    caption_limit: int
    caption_size: int
    bottom_margin: int
    right_margin: int
    lead_frames: int
    tail_frames: int
    zooms: tuple[float, ...]
    music_bpm: int
    music_transpose: int
    music_pattern: tuple[int, ...]


PLATFORMS = (
    Platform('reels', '인스타 릴스', 1920, 22, 72, 700, 170, 2, 4, (1.02, 1.11, 1.03, 1.13),
             105, 0, (0, 2, 1, 3, 0, 1, 2, 1)),
    Platform('tiktok', '틱톡', 1920, 18, 78, 615, 240, 1, 3, (1.15, 1.03, 1.12, 1.05),
             124, -2, (0, 0, 2, 1, 0, 3, 1, 2)),
    Platform('naver', '네이버 클립', 1920, 24, 62, 555, 192, 2, 7, (1.0, 1.06, 1.02, 1.05),
             88, -5, (0, 1, 2, 1, 0, 2, 3, 2)),
)


def run(command, cwd=None):
    result = subprocess.run(command, cwd=cwd, stdin=subprocess.DEVNULL,
                            capture_output=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    if result.returncode:
        detail = result.stderr.decode('utf-8', errors='replace').strip()
        raise RuntimeError(f'{Path(command[0]).name} 실행 오류\n{detail[-3200:]}')
    return result.stdout


def ff(*args, cwd=None):
    return run(['ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-y', *map(str, args)], cwd)


def media_info(path):
    return json.loads(run(['ffprobe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]))


async def neural_voice(cues, voice, folder):
    import edge_tts

    limit = asyncio.Semaphore(3)

    async def speak(index, cue):
        async with limit:
            track = folder / f'voice-{index}.mp3'
            for attempt in range(2):
                try:
                    request = edge_tts.Communicate(cue.text, voice, rate='+5%', receive_timeout=18)
                    await request.save(str(track))
                    return track
                except Exception:
                    if attempt == 1:
                        raise
                    await asyncio.sleep(.65)

    async with asyncio.TaskGroup() as tasks:
        tracks = [tasks.create_task(speak(index, cue)) for index, cue in enumerate(cues)]
    return [track.result() for track in tracks]


def synthesize(cues, voice, folder):
    if voice != 'local':
        try:
            return asyncio.run(neural_voice(cues, voice, folder))
        except Exception as error:
            raise RuntimeError('온라인 음성 합성에 실패했습니다. 연결을 확인하거나 Windows 오프라인 음성을 선택하세요.') from error

    request = folder / 'voice-request.json'
    request.write_text(json.dumps([cue.text for cue in cues], ensure_ascii=False), encoding='utf-8')
    run(['powershell.exe', '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass',
         '-File', str(ROOT / 'voice.ps1'), '-Request', str(request), '-OutputFolder', str(folder)])
    return [folder / f'voice-{index}.wav' for index in range(len(cues))]


def pcm_from(path, channels=1, duration=None, loop=False):
    options = ['-stream_loop', '-1'] if loop else []
    args = ['-t', duration] if duration else []
    raw = ff(*options, '-i', path, *args, '-vn', '-ac', channels, '-ar', RATE, '-f', 's16le', '-')
    return array('h', raw)


def trim_voice(pcm):
    peak = max(map(abs, pcm), default=0)
    if peak < 100:
        raise ValueError('나레이션 음성이 비어 있습니다. 대본에 읽을 수 있는 문장을 입력하세요.')
    block = RATE // 100
    threshold = max(65, peak * .012)
    active = [index for index in range(0, len(pcm), block)
              if sum(sample * sample for sample in pcm[index:index + block]) > block * threshold ** 2]
    if not active:
        raise ValueError('나레이션에서 음성 구간을 확인할 수 없습니다.')
    return pcm[max(0, active[0] - 2 * block):active[-1] + 3 * block]


def build_timeline(cues, narration, target, custom, platform):
    beats = []
    offset = 0
    lead = platform.lead_frames * FRAME_SAMPLES
    for cue in cues:
        pcm = narration[cue.text]
        frames = max(28, math.ceil((lead + len(pcm)) / FRAME_SAMPLES) + platform.tail_frames)
        if offset + frames > target * FPS:
            if custom:
                raise ValueError('나레이션이 목표 길이를 넘습니다. 대본을 줄이거나 목표 길이를 늘리세요.')
            last_complete = next((index for index in range(len(beats) - 1, -1, -1)
                                  if ends_sentence(beats[index].speech.text)), None)
            if last_complete is not None:
                beats = beats[:last_complete + 1]
                offset = beats[-1].offset + beats[-1].frames
            break
        beats.append(Beat(cue, pcm, offset, frames, lead))
        offset += frames
    if not beats:
        raise ValueError('목표 길이에 들어가는 나레이션이 없습니다.')
    return beats, max(3 * FPS, offset)


def filter_for(layout, index, platform):
    zoom = platform.zooms[index % len(platform.zooms)]
    width = round(1080 * zoom / 2) * 2
    height = round(platform.height * zoom / 2) * 2
    fit = ':force_divisible_by=2:reset_sar=1'
    if layout == 'crop':
        return f'scale={width}:{height}:force_original_aspect_ratio=increase{fit},crop=1080:{platform.height}'
    short_height = round(platform.height / 8) * 2
    return (f'split[back][front];[back]scale=270:{short_height}:force_original_aspect_ratio=increase{fit},'
            f'crop=270:{short_height},boxblur=14:2,scale=1080:{platform.height},eq=brightness=-0.15[background];'
            f'[front]scale={round(min(1080, width * .88) / 2) * 2}:{round(platform.height * .78 / 2) * 2}:'
            f'force_original_aspect_ratio=decrease{fit}[foreground];'
            '[background][foreground]overlay=(W-w)/2:(H-h)/2')


def render_cut(source, speech, index, frames, platform, folder, restorer):
    start = min(max(0, speech.start), max(0, source.duration - .12))
    end = min(source.duration, max(start + .12, speech.end))
    output_seconds = frames / FPS
    window = min(end - start, output_seconds * 1.6)
    speed = max(.72, min(1.6, window / output_seconds))
    timing = (f'trim=duration={window:.6f},setpts=(PTS-STARTPTS)/{speed:.6f},'
              f'fps={FPS},tpad=stop_mode=clone:stop_duration={output_seconds + .2:.6f}')
    out = folder / f'cut-{index:03}.mp4'
    restored = restorer.prepare(start, window, source.duration)
    cut_input = ['-i', restored] if restored else ['-ss', f'{start:.6f}', '-i', source.path]
    mirror = 'hflip,' if source.mirrored else ''
    ff(*cut_input, '-map', '0:V:0', '-an', '-sn', '-dn',
       '-filter_threads', 2, '-vf', f'{mirror}{filter_for(source.layout, index, platform)},{timing},setsar=1,format=yuv420p',
       '-frames:v', frames, '-c:v', 'libx264', '-preset', 'veryfast', '-threads', 4, '-crf', 18, out)
    return out


def timecode(seconds, precision):
    unit = 10 ** precision
    ticks = int(seconds * unit + 1e-7) if precision == 2 else round(seconds * unit)
    whole, fraction = divmod(ticks, unit)
    minutes, second = divmod(whole, 60)
    hours, minute = divmod(minutes, 60)
    if precision == 2:
        return f'{hours}:{minute:02}:{second:02}.{fraction:02}'
    return f'{hours:02}:{minute:02}:{second:02},{fraction:03}'


def write_subtitles(beats, folder, platform):
    header = f'''[Script Info]
ScriptType: v4.00+
PlayResX: 1080
PlayResY: {platform.height}
ScaledBorderAndShadow: yes
WrapStyle: 2

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Reels,Malgun Gothic,{platform.caption_size},&H00FFFFFF,&H0000E4FF,&H00111620,&H80000000,-1,0,0,0,100,100,0,0,1,5,2,2,85,{platform.right_margin},{platform.bottom_margin},1
Style: Topic,Malgun Gothic,62,&H00FFFFFF,&H00FFFFFF,&H25111018,&H25111018,-1,0,0,0,100,100,0,0,3,12,0,8,120,160,440,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text
'''
    safe = str.maketrans({'{': '｛', '}': '｝', '\\': '＼'})
    events = []
    srt = []
    if platform.profile == 'naver':
        words = beats[0].speech.text.strip(' .?!,').split()
        if len(words) >= 2:
            topic = ' '.join(words[:min(3, len(words))]).translate(safe)
            lines = caption_lines(topic, 24)
            if len(lines) == 1:
                topic_end = min(2.6, (beats[-1].offset + beats[-1].frames) / FPS)
                events.append(f'Dialogue: 1,0:00:00.00,{timecode(topic_end, 2)},Topic,,0,0,0,,{topic}')
    for index, beat in enumerate(beats, 1):
        lines = caption_lines(beat.speech.text, platform.caption_limit)
        end = min((beat.offset + beat.frames) / FPS, max(beat.end, beat.start + .76))
        escaped = [line.translate(safe) for line in lines]
        if platform.profile == 'tiktok':
            emphasized = False
            for line_index, line in enumerate(escaped):
                span = highlight_word(line)
                if span:
                    first, last = span
                    escaped[line_index] = line[:first] + r'{\c&H00EDFF&}' + line[first:last] + r'{\c}' + line[last:]
                    emphasized = True
                    break
            if not emphasized and len(beat.speech.text.split()) >= 2:
                line = escaped[-1]
                first = line.rfind(' ') + 1
                escaped[-1] = line[:first] + r'{\c&H00EDFF&}' + line[first:] + r'{\c}'
        ass_text = r'\N'.join(escaped)
        events.append(f'Dialogue: 0,{timecode(beat.start, 2)},{timecode(end, 2)},Reels,,0,0,0,,{ass_text}')
        times = f'{timecode(beat.start, 3)} --> {timecode(end, 3)}'
        srt.append(f'{index}\n{times}\n' + '\n'.join(lines))
    (folder / 'captions.ass').write_text(header + '\n'.join(events) + '\n', encoding='utf-8')
    (folder / 'captions.srt').write_text('\n\n'.join(srt) + '\n', encoding='utf-8-sig')


def write_wave(path, pcm, channels=1, rate=RATE):
    with wave.open(str(path), 'wb') as stream:
        stream.setparams((channels, 2, rate, 0, 'NONE', 'not compressed'))
        stream.writeframes(pcm.tobytes())


def built_in_music(path, platform):
    rate = 24000
    step = round(rate * 30 / platform.music_bpm)
    bars = [(48, 60, 64, 67), (45, 60, 64, 69), (41, 60, 65, 69), (43, 59, 62, 67)]
    track = array('h')
    for chord in bars:
        for index, note in enumerate(platform.music_pattern):
            frequency = 440 * 2 ** ((chord[note] + platform.music_transpose - 69) / 12)
            accent = .28 if platform.profile == 'tiktok' and index in (0, 4) else 0
            overtone = .09 if platform.profile == 'naver' else .20
            for sample in range(step):
                time = sample / rate
                envelope = min(1, time / .012, (step - sample) / (rate * .065)) * math.exp(-5 * time)
                phase = math.tau * frequency * time
                percussion = accent * math.exp(-28 * time) * math.sin(math.tau * 67 * time)
                value = math.sin(phase) + overtone * math.sin(phase * 2) + percussion
                track.append(round(11000 * value * envelope))
    write_wave(path, track, rate=rate)


def mix_audio(beats, total_frames, music, cwd):
    total_samples = total_frames * FRAME_SAMPLES
    narration = array('h', [0]) * total_samples
    for beat in beats:
        start = beat.offset * FRAME_SAMPLES + beat.lead
        narration[start:start + len(beat.pcm)] = beat.pcm
    narration_peak = max(map(abs, narration))
    narration_gain = 23000 / narration_peak
    write_wave(cwd / 'narration.wav', array('h', (round(sample * narration_gain) for sample in narration)))
    stereo = pcm_from(music, channels=2, duration=f'{total_frames / FPS:.6f}', loop=True)
    fade_in, fade_out = round(RATE * .12) * 2, round(RATE * .24) * 2
    faded = array('h', (round(sample * min(1, index / fade_in, (len(stereo) - 1 - index) / fade_out))
                        for index, sample in enumerate(stereo)))
    music_peak = max(map(abs, faded), default=0)
    if not music_peak:
        raise ValueError('배경음 파일에서 재생 가능한 소리를 찾지 못했습니다.')
    gain = 23000 * BGM_GAIN / music_peak
    write_wave(cwd / 'bgm.wav', array('h', (round(sample * gain) for sample in faded)), channels=2)


def platform_highlights(cues, target, platforms=PLATFORMS):
    if len(cues) < 3:
        return [cues] * len(platforms)
    total_speech = sum(max(.85, cue.end - cue.start) for cue in cues)
    if total_speech < 13:
        return [choose_highlight(cues, target, platform.profile) for platform in platforms]

    # Budget each selection against the speech left for the other edits.
    remaining = [cues]
    selections = []
    for index, platform in enumerate(platforms):
        count = len(platforms) - index
        budget = min(target, max(4, total_speech / count))
        if platform.profile == 'tiktok':
            budget = max(4, budget * .82)
        if not remaining:
            selections.append(choose_highlight(cues, budget, platform.profile))
            continue
        pool = max(remaining, key=lambda section: sum(max(.85, cue.end - cue.start) for cue in section))
        remaining.remove(pool)
        highlight = choose_highlight(pool, budget, platform.profile)
        # Save complete cues for each remaining platform when source length permits.
        max_count = max(1, len(pool) + sum(map(len, remaining)) - count + 1)
        if len(highlight) > max_count:
            end = next((index for index in range(max_count, 0, -1)
                        if ends_sentence(highlight[index - 1].text)), max_count)
            highlight = highlight[:end]
        first = pool.index(highlight[0])
        total_speech -= sum(max(.85, cue.end - cue.start) for cue in highlight)
        remaining.extend(section for section in (pool[:first], pool[first + len(highlight):]) if section)
        selections.append(highlight)
    return selections


def plan_shots(beats, total_frames, sources, alternatives):
    shots = []
    broll = iter(alternatives)
    for index, beat in enumerate(beats):
        frames = beat.frames if index + 1 < len(beats) else total_frames - beat.offset
        speech = beat.speech
        source = next(broll, None) if frames >= 51 and (index > 0 or len(beats) == 1) else None
        if source is None:
            shots.append((speech, frames))
            continue
        insert_frames = min(33, max(18, round(frames * .34)))
        main_frames = frames - insert_frames
        split = speech.start + (speech.end - speech.start) * main_frames / frames
        shots.append((replace(speech, end=split), main_frames))
        duration = sources[source].duration
        start = min(duration * .24, max(0, duration - insert_frames / FPS))
        shots.append((Speech('', start, duration, source), insert_frames))
    return shots


def render_platform(job, sources, platform, cues, narration, music, work, staged, update, restorers, alternatives):
    work.mkdir()
    beats, total_frames = build_timeline(cues, narration, job.target, bool(job.script.strip()), platform)
    write_subtitles(beats, work, platform)
    update(0, '나레이션과 BGM 믹싱 중')
    if music is None:
        music = work / 'default.wav'
        built_in_music(music, platform)
    mix_audio(beats, total_frames, music, work)
    clips = []
    shots = plan_shots(beats, total_frames, sources, alternatives)
    for index, (speech, frames) in enumerate(shots):
        update(15 + int(62 * index / len(shots)), f'대본에 맞춰 컷 조합 중 · {index + 1}/{len(shots)}')
        clips.append(render_cut(sources[speech.source], speech, index, frames, platform, work, restorers[speech.source]))
    (work / 'cuts.txt').write_text(''.join(f"file '{clip.name}'\n" for clip in clips), encoding='utf-8')

    update(80, '자막을 입히고 MP4 내보내는 중')
    ff('-f', 'concat', '-safe', 1, '-i', 'cuts.txt', '-i', 'narration.wav', '-i', 'bgm.wav',
       '-filter_complex_threads', 2, '-filter_complex',
       '[0:v]setpts=N/(30*TB),ass=captions.ass[v];[1:a]pan=stereo|c0=c0|c1=c0[n];'
       '[n][2:a]amix=inputs=2:duration=first:normalize=0[a]',
       '-map', '[v]', '-map', '[a]', '-c:v', 'libx264', '-preset', 'veryfast', '-threads', 4,
       '-pix_fmt', 'yuv420p', '-crf', 19, '-maxrate', '16M', '-bufsize', '24M', '-g', '60',
       '-c:a', 'aac', '-b:a', '128k', '-ar', RATE, '-movflags', '+faststart',
       '-bf', '0', 'reel.mp4', cwd=work)
    update(96, '영상 비율과 오디오 확인 중')
    check_export(work / 'reel.mp4', platform, total_frames)
    (work / 'reel.mp4').replace(staged / f'{platform.name}.mp4')
    (work / 'captions.srt').replace(staged / f'{platform.name}.srt')


def check_export(path, platform, frames):
    streams = media_info(path)['streams']
    if len(streams) == 2:
        video, audio = streams
        if (video.get('codec_name') == 'h264' and video.get('width') == 1080
                and video.get('height') == platform.height and video.get('r_frame_rate') == f'{FPS}/1'
                and video.get('nb_frames') == str(frames) and audio.get('codec_name') == 'aac'
                and audio.get('sample_rate') == str(RATE) and audio.get('channels') == 2
                and abs(float(video['duration']) - float(audio['duration'])) <= 1 / FPS):
            return
    raise RuntimeError(f'{platform.name} 결과물의 화면비, 프레임 또는 오디오 규격을 확인할 수 없습니다.')


def read_sources(job, display_size):
    sources = []
    for index, path in enumerate((job.source, *job.extra_sources)):
        path = path.resolve(strict=True)
        info = media_info(path)
        video = next((stream for stream in info['streams'] if stream['codec_type'] == 'video'
                      and not stream.get('disposition', {}).get('attached_pic')), None)
        if not video:
            raise ValueError(f'{path.name} 파일에 재생 가능한 영상이 없습니다.')
        width, height = display_size(video)
        layout = ('crop' if width / height < .85 else 'blur') if job.layout == 'auto' else job.layout
        duration = float(video.get('duration') or info['format'].get('duration') or 0)
        if not math.isfinite(duration) or duration < .12:
            raise ValueError(f'{path.name} 파일의 영상 길이를 확인할 수 없습니다.')
        region = (job.subtitle_regions[index] if index < len(job.subtitle_regions)
                  else job.subtitle_region if index == 0 else None)
        sources.append(Source(path, video, duration, layout,
                              any(track['codec_type'] == 'audio' for track in info['streams']), region,
                              index in job.mirrored))
    return sources


def render(job, update=lambda *_: None):
    requested = job.output_profiles
    platforms = tuple(platform for platform in PLATFORMS if requested is None or platform.profile in requested)
    if not platforms:
        raise ValueError('내보낼 플랫폼을 한 개 이상 선택하세요.')
    if requested is not None and len(platforms) != len(requested):
        raise ValueError('지원하지 않는 플랫폼이 선택되었습니다.')
    try:
        from restoration import SubtitleRestorer, display_size
    except ImportError as error:
        raise RuntimeError('자막 복원 구성 요소를 설치하려면 python -m pip install -r requirements.txt 를 실행하세요.') from error
    if not shutil.which('ffmpeg') or not shutil.which('ffprobe'):
        raise RuntimeError('FFmpeg와 ffprobe를 설치하고 Windows PATH에 추가하세요.')
    update(4, '원본 영상 확인 중')
    sources = read_sources(job, display_size)
    music = job.music.resolve(strict=True) if job.music else None
    if music and any(music.samefile(source.path) for source in sources):
        raise ValueError('원본 소리는 편집본에서 제외합니다. BGM으로 별도의 오디오 파일을 선택하세요.')
    profiles = tuple(platform.profile for platform in platforms)
    if job.script.strip():
        cues = custom_story(job.script, [source.duration for source in sources])
        if len(cues) * 28 > FPS * job.target:
            raise ValueError('대본이 목표 길이에 비해 깁니다. 대본을 줄이거나 목표 길이를 늘리세요.')
        selections = [cues] * len(platforms)
        groups = [[] for source in sources]
    else:
        groups = []
        with tempfile.TemporaryDirectory(prefix='reels-asr-') as temp:
            for index, source in enumerate(sources):
                cues = []
                if source.has_audio:
                    progress = 8 + int(11 * index / len(sources))
                    update(progress, f'원본 발화 인식 중 · {index + 1}/{len(sources)}')
                    audio = Path(temp) / 'source.wav'
                    ff('-i', source.path, '-map', '0:a:0', '-vn', '-sn', '-dn', '-af', 'aresample=async=1:first_pts=0',
                       '-ac', 1, '-ar', 16000, audio)
                    cues = sentences([replace(cue, source=index) for cue in transcribe(audio, lambda *_: None)])
                groups.append(cues)
        speaking = [group for group in groups if group]
        if not speaking:
            raise ValueError('읽을 수 있는 음성을 찾지 못했습니다. 나레이션 대본을 직접 입력하세요.')
        update(21, '플랫폼별 대본과 컷 흐름 구성 중')
        selections = (platform_highlights(speaking[0], job.target, platforms) if len(speaking) == 1 else
                      [multi_source_story(groups, job.target, profile) for profile in profiles])
        selections = polish_stories(selections, profiles)
    if not all(selections):
        raise ValueError('읽을 수 있는 문장이 없습니다. 나레이션 대본을 확인하세요.')
    selections = [fit_caption_cues(selection, platform.caption_limit)
                  for selection, platform in zip(selections, platforms, strict=True)]
    unique_cues = {cue.text: cue for selection in selections for cue in selection}
    output_dir = job.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='.reels-', dir=output_dir) as temporary:
        work = Path(temporary)
        staged = work / 'finished'
        staged.mkdir()
        update(27, '나레이션 생성 중')
        tracks = synthesize(unique_cues.values(), job.voice, work)
        narration = {text: trim_voice(pcm_from(path)) for text, path in zip(unique_cues, tracks, strict=True)}
        restorers = []
        detector = None
        for index, source in enumerate(sources):
            cache = work / f'source-{index:03}'
            cache.mkdir()
            restorer = SubtitleRestorer(ff, source.path, source.video, source.region, FPS, cache, detector)
            detector = restorer.detector
            restorers.append(restorer)
        for index, (platform, selection) in enumerate(zip(platforms, selections, strict=True)):
            def advance(percent, stage):
                update(33 + int(66 * (index + percent / 100) / len(platforms)), f'{platform.name} · {stage}')

            used = {cue.source for cue in selection}
            topic = set().union(*(keywords(cue.text) for cue in selection))
            alternatives = [candidate for candidate, group in enumerate(groups)
                            if candidate not in used and sources[candidate].duration >= .65
                            and (not group or any(len(keywords(cue.text) & topic) >= 2 for cue in group))]
            render_platform(job, sources, platform, selection, narration, music,
                            work / f'platform-{index}', staged, advance, restorers, alternatives)

        # Publish the selected edits together once every export has completed.
        stem = f'{sources[0].path.stem[:70]}_릴스'
        for index in range(1, 10000):
            destination = output_dir / (stem if index == 1 else f'{stem}_{index}')
            try:
                staged.rename(destination)
                break
            except FileExistsError:
                continue
        else:
            raise FileExistsError('결과 폴더를 만들 수 없습니다. 다른 저장 위치를 선택하세요.')
    update(100, f'영상 {len(platforms)}개 제작 완료')
    return destination
