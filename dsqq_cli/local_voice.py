"""Provision the per-installation native MLX launch agent using CLI only."""
import os
from pathlib import Path
import platform
import plistlib
import shutil
import time

from .runtime import run
from .state import UserError, read_json, write_json, private_write

MODEL = 'mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit'


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
    write_json(root / 'worker.json', {'model_path': str(root / 'model'), 'jobs_dir': str(queue)})
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
    checked(['launchctl', 'bootstrap', domain, plist])
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
