import hashlib
import os
import ssl
import tempfile
from functools import lru_cache
from pathlib import Path
from urllib.request import urlopen

from editor import Speech, group_words


MODEL_SHA = '9ecf779972d90ba49c06d968637d720dd632c55bbf19d441fb42bf17a411e794'
MODEL_URL = f'https://openaipublic.azureedge.net/main/whisper/models/{MODEL_SHA}/small.pt'


def prepare_model():
    import truststore

    cache = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'ReelStudio' / 'models'
    cache.mkdir(parents=True, exist_ok=True)
    model = cache / 'small.pt'
    if model.exists():
        with model.open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() == MODEL_SHA:
                return cache

    context = truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    with tempfile.NamedTemporaryFile(dir=cache, suffix='.download', delete=False) as download:
        temporary = Path(download.name)
    try:
        digest = hashlib.sha256()
        with urlopen(MODEL_URL, context=context, timeout=45) as response, temporary.open('wb') as stream:
            for chunk in iter(lambda: response.read(1024 * 1024), b''):
                digest.update(chunk)
                stream.write(chunk)
        if digest.hexdigest() != MODEL_SHA:
            raise RuntimeError('다운로드한 Whisper 모델의 무결성을 확인할 수 없습니다.')
        temporary.replace(model)
    except OSError as error:
        raise RuntimeError('Whisper 모델을 내려받지 못했습니다. 인터넷 연결을 확인하거나 대본을 직접 입력하세요.') from error
    finally:
        temporary.unlink(missing_ok=True)
    return cache


@lru_cache(maxsize=1)
def load_whisper():
    import torch
    import whisper

    cache = prepare_model()
    torch.set_num_threads(min(6, os.cpu_count() or 1))
    return whisper.load_model('small', device='cpu', download_root=str(cache))


def transcribe(source, update):
    model = load_whisper()
    update(14, '원본의 한국어 발화 분석 중')
    # Decode via the wave module so renamed or non-ASCII temporary folders work in Windows.
    import wave
    import numpy as np

    with wave.open(str(source), 'rb') as stream:
        samples = np.frombuffer(stream.readframes(stream.getnframes()), dtype='<i2').astype(np.float32) / 32768
    result = model.transcribe(samples, language='ko', word_timestamps=True,
                              fp16=False, condition_on_previous_text=False, verbose=None)
    words = []
    for segment in result['segments']:
        if segment.get('words'):
            words.extend(Speech(word['word'].strip(), word['start'], word['end'])
                         for word in segment['words'] if word['word'].strip())
        elif segment['text'].strip():
            words.append(Speech(segment['text'].strip(), segment['start'], segment['end']))
    return group_words(words)
