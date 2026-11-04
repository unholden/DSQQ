"""Provision the per-installation native MLX launch agent using CLI only."""
import os
from pathlib import Path
import platform
import plistlib
import re
import shutil
import time

from .runtime import run
from .state import UserError, read_json, write_json, private_write

MODEL = 'mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit'
CLONE_MODEL = 'mlx-community/Qwen3-TTS-12Hz-0.6B-Base-8bit'


def install_local_voice(inst, args, repo):
    if platform.system() != 'Darwin' or platform.machine() != 'arm64':
        raise UserError('本地 MLX 语音需要 Apple Silicon Mac。')
    uv = shutil.which('uv')
    if not uv:
        raise UserError('请先安装 uv：brew install uv')
    root = inst.root / 'mlx-voice'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    python = root / 'venv/bin/python'
    def checked(argv, timeout=600):
        result = run([str(a) for a in argv], timeout=timeout)
        if result.returncode:
            raise UserError(inst.redact((result.stderr or result.stdout)[-1500:]))
        return result
    if not python.exists():
        checked([uv, 'venv', '--python', '3.12', root / 'venv'])
    print('安装原生 MLX 运行环境并下载模型…', flush=True)
    checked([uv, 'pip', 'install', '--python', python, 'mlx-audio==0.6.4', 'soundfile==0.14.0'])
    checked([python, '-c', 'from huggingface_hub import snapshot_download;import sys;snapshot_download(sys.argv[1],local_dir=sys.argv[2])', MODEL, root / 'model'], 1200)
    queue = inst.root / 'astrbot/data/plugin_data/astrbot_plugin_dsqq_voice/mlx_jobs'
    queue.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Reinstalling the runtime must retain a configured reference voice.
    if not (root / 'worker.json').exists():
        write_json(root / 'worker.json', {'model_path': str(root / 'model'), 'jobs_dir': str(queue)})
    start_local_voice(inst, repo)


def configure_local_clone(inst, args, repo):
    root = inst.root / 'mlx-voice'
    python = root / 'venv/bin/python'
    if not python.is_file() or not (root / 'worker.json').is_file():
        raise UserError('请先运行 dsqq voice local-install。')
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{0,63}', args.name):
        raise UserError('音色名称须为字母开头的英文、数字、下划线或连字符。')
    if not args.audio.is_file() or not args.text_file.is_file():
        raise UserError('参考音频或转写文本文件不存在。')
    text = args.text_file.read_text(encoding='utf-8-sig').strip()
    if not text:
        raise UserError('参考文本不能为空。')
    model = root / 'clone-base-0.6b-8bit'
    result = run([str(python), '-c',
        'from huggingface_hub import snapshot_download;import sys;snapshot_download(sys.argv[1],local_dir=sys.argv[2])',
        CLONE_MODEL, str(model)], timeout=1200)
    if result.returncode:
        raise UserError('克隆模型下载失败：' + inst.redact(result.stderr[-1000:]))
    # Validate/decode before changing a running service. Store a private copy so
    # removing the original download does not break future speech generation.
    import tempfile
    references = root / 'references'
    references.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, target = tempfile.mkstemp(prefix=args.name + '-', suffix='.wav', dir=references)
    os.close(fd)
    audio = Path(target)
    result = run([str(python), '-c',
        'import soundfile as sf,sys; a,s=sf.read(sys.argv[1]);'
        'assert a.size>0; sf.write(sys.argv[2],a,s,subtype="PCM_16")',
        str(args.audio.resolve()), str(audio)])
    if result.returncode:
        audio.unlink(missing_ok=True)
        raise UserError('参考音频无法读取；请先提取为 WAV。')
    previous = read_json(root / 'worker.json')
    config = {**previous, 'mode': 'clone', 'model_path': str(model),
              'voice': args.name, 'reference_audio': str(audio), 'reference_text': text}
    write_json(root / 'worker.json', config)
    try:
        start_local_voice(inst, repo)
    except Exception:
        write_json(root / 'worker.json', previous)
        start_local_voice(inst, repo)
        audio.unlink(missing_ok=True)
        raise


def start_local_voice(inst, repo):
    root = inst.root / 'mlx-voice'
    python = root / 'venv/bin/python'
    queue = Path(read_json(root / 'worker.json')['jobs_dir'])
    def checked(argv):
        result = run([str(a) for a in argv])
        if result.returncode:
            raise UserError(inst.redact((result.stderr or result.stdout)[-1500:]))
    label = 'net.machinepulse.dsqq.voice.' + inst.config['project']
    plist = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    plist.parent.mkdir(parents=True, exist_ok=True)
    value = {'Label': label, 'ProgramArguments': [str(python), str(repo / 'dsqq_cli/mlx_worker.py'), str(root / 'worker.json')],
             'RunAtLoad': True, 'KeepAlive': True, 'ThrottleInterval': 10,
             'WorkingDirectory': str(root), 'StandardOutPath': str(root / 'worker.log'),
             'StandardErrorPath': str(root / 'worker-error.log'),
             'EnvironmentVariables': {'HF_HUB_OFFLINE': '1', 'TOKENIZERS_PARALLELISM': 'false'}}
    private_write(plist, plistlib.dumps(value).decode())
    domain = 'gui/' + str(os.getuid())
    if run(['launchctl', 'print', domain + '/' + label]).returncode == 0:
        checked(['launchctl', 'bootout', domain + '/' + label])
    (queue / 'health.json').unlink(missing_ok=True)
    # bootout may return before launchd releases the old service registration.
    # A short bounded retry handles its transient EIO without changing domains.
    for attempt in range(5):
        result = run(['launchctl', 'bootstrap', domain, str(plist)])
        if result.returncode == 0:
            break
        if attempt == 4 or 'Bootstrap failed: 5:' not in result.stderr:
            raise UserError(inst.redact((result.stderr or result.stdout)[-1500:]))
        time.sleep(1)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        h = read_json(queue / 'health.json')
        if h.get('status') == 'ready' and time.time() - h.get('updated_at', 0) < 10:
            print('本地 Qwen3-TTS 已就绪，登录后自动启动。')
            return
        time.sleep(1)
    raise UserError('本地语音尚未就绪，请查看 mlx-voice/worker-error.log。')


def stop_local_voice(inst):
    label = 'net.machinepulse.dsqq.voice.' + inst.config['project']
    plist = Path.home() / 'Library/LaunchAgents' / (label + '.plist')
    if not plist.exists():
        return
    target = 'gui/' + str(os.getuid()) + '/' + label
    if run(['launchctl', 'print', target]).returncode == 0:
        result = run(['launchctl', 'bootout', target])
        if result.returncode:
            raise UserError('无法停止本安装的本地语音服务。')
    plist.unlink(missing_ok=True)
    (inst.root / 'astrbot/data/plugin_data/astrbot_plugin_dsqq_voice/mlx_jobs/health.json').unlink(missing_ok=True)
