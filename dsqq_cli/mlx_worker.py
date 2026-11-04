"""Native macOS MLX worker. The shared queue avoids exposing a network service."""
import json
import os
from pathlib import Path
import re
import sys
import time

VOICES = {'vivian', 'serena', 'uncle_fu', 'dylan', 'eric', 'ryan', 'aiden', 'ono_anna', 'sohee'}


def atomic_json(path, value):
    temp = path.with_suffix('.tmp')
    with temp.open('w', encoding='utf-8') as out:
        os.chmod(temp, 0o600)
        json.dump(value, out, ensure_ascii=False)
    os.replace(temp, path)


def validate_job(job, voices=VOICES):
    text, voice = job.get('text'), job.get('voice')
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 600:
        raise ValueError('Invalid text')
    if not isinstance(voice, str) or voice.lower() not in voices:
        raise ValueError('Invalid voice')
    age = time.time() - float(job.get('created_at', 0))
    if not -5 <= age < 180:
        raise ValueError('Expired request')
    return text.strip(), voice


def generate_audio(model, config, text, voice):
    if config.get('mode') == 'clone':
        return model.generate(
            text=text, lang_code='Chinese',
            ref_audio=config['reference_audio'],
            ref_text=config['reference_text'], max_tokens=4096)
    return model.generate_custom_voice(
        text=text, speaker=voice, language='Chinese', max_tokens=4096)


def main():
    config = json.loads(Path(sys.argv[1]).read_text())
    voices = {config['voice'].lower()} if config.get('mode') == 'clone' else VOICES
    folder = Path(config['jobs_dir'])
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    health = folder / 'health.json'
    def state(status):
        atomic_json(health, {'status': status, 'updated_at': time.time(), 'pid': os.getpid(),
                            'voices': sorted(voices), 'mode': config.get('mode', 'preset')})
    state('loading')
    from mlx_audio.tts.utils import load_model
    import mlx.core as mx
    import numpy as np
    import soundfile as sf
    model = load_model(config['model_path'])
    if config.get('mode') == 'clone':
        if model.config.tts_model_type != 'base' or not model.speech_tokenizer.has_encoder:
            raise ValueError('Voice cloning requires a Base model with reference encoder')
        if not Path(config['reference_audio']).is_file() or not config['reference_text'].strip():
            raise ValueError('Missing voice reference')
    # A crashed worker never silently retries a claimed request.
    for old in folder.glob('*.working'):
        atomic_json(folder / (old.stem + '.result.json'), {'ok': False})
        old.unlink(missing_ok=True)
    last_cleanup = 0
    while True:
        state('ready')
        if time.time() - last_cleanup > 3600:
            for old in folder.iterdir():
                if re.fullmatch(r'[a-f0-9]{32}\.(wav|result\.json)', old.name) and time.time() - old.stat().st_mtime > 86400:
                    old.unlink(missing_ok=True)
            last_cleanup = time.time()
        for request in sorted(folder.glob('*.request.json')):
            job_id = request.name.split('.')[0]
            if not re.fullmatch('[a-f0-9]{32}', job_id):
                continue
            working = folder / (job_id + '.working')
            try:
                request.rename(working)
            except FileNotFoundError:
                continue
            wav = folder / (job_id + '.wav')
            started = time.monotonic()
            try:
                text, voice = validate_job(json.loads(working.read_text()), voices)
                state('busy')
                outputs = list(generate_audio(model, config, text, voice))
                audio = mx.concatenate([o.audio for o in outputs])
                mx.eval(audio)
                if not len(audio):
                    raise ValueError('Empty output')
                wav.touch(mode=0o600)
                sf.write(str(wav), np.asarray(audio), outputs[0].sample_rate)
                os.chmod(wav, 0o600)
                atomic_json(folder / (job_id + '.result.json'), {'ok': True})
                print(f'synthesis completed seconds={time.monotonic()-started:.2f}', flush=True)
            except Exception as exc:
                wav.unlink(missing_ok=True)
                atomic_json(folder / (job_id + '.result.json'), {'ok': False})
                print(f'synthesis failed type={type(exc).__name__}', flush=True)
            finally:
                working.unlink(missing_ok=True)
                mx.clear_cache()
        time.sleep(0.25)


if __name__ == '__main__':
    main()
