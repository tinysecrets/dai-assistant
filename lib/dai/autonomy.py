"""Bounded policy engine for DAI local autonomy. Fails closed."""
from __future__ import annotations
import json
import re
import subprocess
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
POLICY = ROOT / 'policy/sovereign.json'

READ_ONLY_PATTERNS=(
    r'^(?:/usr/bin/)?systemctl --user (?:is-active|is-enabled|status) [A-Za-z0-9_.@-]+(?: --no-pager)?$',
    r'^(?:/usr/bin/)?ss -[a-z]+(?: .*)?$',
    r'^ps (?:aux|-[A-Za-z]+)(?: .*)?$',
    r'^(?:/usr/bin/)?grep(?: -[A-Za-z0-9]+)* [^|;&`]+$',
    r'^(?:/usr/bin/)?cat [A-Za-z0-9_./:@+\-]+$',
    r'^(?:/usr/bin/)?head(?: -[A-Za-z0-9]+)* [A-Za-z0-9_./:@+\-]+$',
    r'^(?:/usr/bin/)?tail(?: -[A-Za-z0-9]+)* [A-Za-z0-9_./:@+\-]+$',
    r'^(?:/usr/bin/)?ls(?: -[A-Za-z0-9]+)*(?: [A-Za-z0-9_./:@+\-]+)?$',
    r'^(?:/usr/bin/)?find [A-Za-z0-9_./:@+\-]+ -maxdepth [0-9]+ -type [fd]$',
    r'^echo(?: .*)?$',
)


def policy() -> dict[str,Any]:
    try:
        return json.loads(POLICY.read_text(encoding='utf-8'))
    except Exception:
        return {'autonomy': {'enabled': False}}


def classify(command: str) -> str:
    c=' '.join(command.strip().split())
    if not c or any(x in c for x in (';', '&&', '||', '|', '`', '$(', '>', '<')):
        return 'unknown_command'
    if re.fullmatch(r'(?:/usr/bin/)?systemctl --user restart [A-Za-z0-9_.@-]+', c):
        svc=c.rsplit(' ',1)[-1]
        allow=policy().get('autonomy',{}).get('allowlisted_user_services',[])
        return 'restart_user_service' if svc in allow else 'unknown_command'
    if re.fullmatch(r'\./bin/dai-restart-service [A-Za-z0-9_.@-]+', c):
        svc=c.rsplit(' ',1)[-1]
        allow=policy().get('autonomy',{}).get('allowlisted_user_services',[])
        return 'restart_user_service' if svc in allow else 'unknown_command'
    for pat in READ_ONLY_PATTERNS:
        if re.fullmatch(pat,c):
            return 'read_only'
    if any(x in c for x in ('sudo','apt ','apt-get ','pip install','npm install','git push','git commit','curl ','wget ','scp ','rsync ')):
        return 'require_approval'
    return 'unknown_command'


def allowed(command: str) -> bool:
    cfg=policy().get('autonomy',{})
    if not cfg.get('enabled', False):
        return False
    return classify(command) in {'read_only','restart_user_service'}


def run(command: str, timeout: int|None=None) -> dict[str,Any]:
    kind=classify(command)
    if not allowed(command):
        return {'ok':False,'status':'approval_required','classification':kind,'command':command}
    lim=policy().get('autonomy',{}).get('limits',{})
    timeout=int(timeout or lim.get('command_timeout_seconds',20))
    started=time.time()
    try:
        p=subprocess.run(command,shell=True,cwd=ROOT,text=True,capture_output=True,timeout=timeout,check=False)
        return {'ok':p.returncode==0,'status':'executed','classification':kind,'command':command,'returncode':p.returncode,'stdout':p.stdout[-4000:],'stderr':p.stderr[-4000:],'elapsed_seconds':round(time.time()-started,3)}
    except subprocess.TimeoutExpired as e:
        return {'ok':False,'status':'timeout','classification':kind,'command':command,'stdout':str(e.stdout or '')[-2000:],'stderr':str(e.stderr or '')[-2000:]}
    except Exception as e:
        return {'ok':False,'status':'error','classification':kind,'command':command,'stderr':str(e)}
