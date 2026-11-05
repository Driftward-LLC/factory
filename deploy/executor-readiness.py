#!/usr/bin/env python3
"""No-model native CLI/plugin readiness; private plugin code is operator supplied."""
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from urllib.parse import urlsplit

# Native registration imports private adapters; never write caches into the pin.
sys.dont_write_bytecode = True

SKILLS = ('implement-change', 'verify-app', 'prepare-pr')
PROFILE = 'factory-executor'
PROFILE_HOME = Path('/opt/data/profiles') / PROFILE


def manifest(value):
    if (not isinstance(value,dict) or set(value)!={'schema','namespace','version','source_commit','archive_sha256','tree_sha256','repository'}
            or value['schema']!='factory-executor-plugin-v1' or not re.fullmatch('[a-z][a-z0-9-]{0,99}',str(value['namespace']))
            or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9.+-]{0,99}',str(value['version']))
            or not re.fullmatch('[a-f0-9]{40}',str(value['source_commit']))
            or any(not re.fullmatch('[a-f0-9]{64}',str(value[key])) for key in ('archive_sha256','tree_sha256'))):
        raise ValueError('invalid reviewed executor plugin manifest')
    source=urlsplit(value['repository'])
    if source.scheme!='https' or source.username or source.password or source.query or source.fragment or not source.hostname:
        raise ValueError('invalid credential-free plugin source')
    return value


def package_digest(root):
    files={}
    if not root.is_dir(): raise ValueError('native plugin package missing')
    for path in root.rglob('*'):
        if path.is_symlink(): raise ValueError('native plugin package contains a symlink')
        if path.is_file():
            if path.stat().st_size>1024*1024 or len(files)>=128: raise ValueError('plugin package exceeds budget')
            if path.relative_to(root)!=Path('.factory-source.json'):
                files[str(path.relative_to(root))]=hashlib.sha256(path.read_bytes()).hexdigest()
        elif not path.is_dir(): raise ValueError('invalid native package file type')
    return hashlib.sha256(json.dumps(files,sort_keys=True,separators=(',',':')).encode()).hexdigest()


def check(arguments=None, reviewed=None):
    from hermes_constants import get_hermes_home
    from ruamel.yaml import YAML
    home=get_hermes_home()
    pin=manifest(reviewed if reviewed is not None else json.loads(Path(os.environ['FACTORY_HERMES_ENGINEERING_MANIFEST']).read_text()))
    namespace=pin['namespace']
    from pm.install import venv_is_current
    if os.environ.get('HERMES_RUNTIME_DIR')!='/opt/hermes/tools' or not venv_is_current():
        raise ValueError('native plugin dependency admission is not current')
    if home!=PROFILE_HOME or os.environ.get('FACTORY_HERMES_PROFILE')!=PROFILE or os.environ.get('FACTORY_HERMES_SKILL_NAMESPACE')!=namespace:
        raise ValueError('executor profile or namespace mismatch')
    expected=['--profile',PROFILE,'--skills',','.join(namespace+':'+name for name in SKILLS)]
    if arguments is None:
        module=Path(__file__).resolve().parents[1]/'hatchet/workers/bridge-executor-worker/dist/hermes/create-draft-pr.js'
        arguments=json.loads(subprocess.check_output(['node','-e','process.stdout.write(JSON.stringify(require(process.argv[1]).hermesProfileArguments()))',str(module)],stderr=subprocess.DEVNULL,timeout=10))
    if arguments!=expected: raise ValueError('actual executor arguments differ from native plugin selection')
    from hermes_cli._parser import build_top_level_parser, PRE_ARGPARSE_INHERITED_FLAGS
    from hermes_cli.profiles import resolve_profile_env
    if ('--profile',True) not in PRE_ARGPARSE_INHERITED_FLAGS or Path(resolve_profile_env(arguments[1]))!=home:
        raise ValueError('native CLI profile argument unsupported')
    # Native main consumes --profile first. Parse its real remaining arguments,
    # but never call main or an argument handler (no startup/model/tool action).
    parsed=build_top_level_parser()[0].parse_args(arguments[2:]+['--model','gpt-6.1-sol','--provider','openai-codex','--accept-hooks','--pass-session-id','-z','readiness-no-dispatch'])
    if [item for group in parsed.skills for item in group.split(',')]!=[namespace+':'+name for name in SKILLS] or parsed.oneshot!='readiness-no-dispatch':
        raise ValueError('native CLI skill arguments differ')
    root=home/'plugins'/namespace
    if any(path.is_symlink() for path in (root,*root.parents)): raise ValueError('native plugin path contains a symlink')
    if package_digest(root)!=pin['tree_sha256'] or json.loads((root/'.factory-source.json').read_text())!=pin:
        raise ValueError('native plugin differs from reviewed immutable source')
    metadata=YAML(typ='safe').load((root/'plugin.yaml').read_text())
    if metadata.get('name')!=namespace or metadata.get('version')!=pin['version']:
        raise ValueError('native plugin name or version mismatch')
    config=YAML(typ='safe').load((home/'config.yaml').read_text())
    enabled=config.get('plugins',{}).get('enabled')
    if not isinstance(enabled,list) or namespace not in enabled or len(set(enabled))!=len(enabled) or set(enabled)-{namespace,'observability/posthog'}:
        raise ValueError('executor readiness found an unreviewed enabled native plugin')
    if 'observability/posthog' in enabled:
        observer=home/'plugins/observability/posthog'
        expected=Path(__file__).resolve().parent/'factory-observers/posthog'
        if any(path.is_symlink() for path in (observer,*observer.parents)) or package_digest(observer)!=package_digest(expected):
            raise ValueError('executor observer differs from the image reviewed package')
    from tools.skills_tool import skill_view
    from agent.skill_commands import build_preloaded_skills_prompt
    identities={}
    for name in SKILLS:
        directory=root/'skills'/name
        viewed=json.loads(skill_view(namespace+':'+name,preprocess=False))
        if not viewed.get('success') or Path(viewed.get('skill_dir',''))!=directory:
            raise ValueError('native plugin skill is missing, disabled or shadowed')
        identities[name]=hashlib.sha256((directory/'SKILL.md').read_bytes()).hexdigest()
    template=json.loads(skill_view(namespace+':prepare-pr',file_path='assets/pr-description.md',preprocess=False))
    if not template.get('success'):
        raise ValueError('native plugin linked PR template unavailable')
    prompt,loaded,missing=build_preloaded_skills_prompt([namespace+':'+name for name in SKILLS])
    if missing or not prompt or set(loaded)!={namespace+':'+name for name in SKILLS} or len(loaded)!=len(SKILLS):
        raise ValueError('native CLI preload did not resolve all required skills')
    return {'profile':PROFILE,'namespace':namespace,'version':pin['version'],'source_commit':pin['source_commit'],
            'tree_sha256':pin['tree_sha256'],'archive_sha256':pin['archive_sha256'],
            'skills':identities,'model_called':False,'cli_arguments_checked':True,'native_dependencies_current':True}


if __name__=='__main__':
    try:
        # Candidate activation supplies only public pinned metadata before the
        # gateway's read-only manifest mount exists; actual startup reads the mount.
        candidate=len(sys.argv)==3 and sys.argv[1]=='--manifest-json'
        print(json.dumps(check(reviewed=json.loads(sys.argv[2])) if candidate else check(json.loads(sys.argv[1]) if len(sys.argv)==2 else None),sort_keys=True))
    except Exception:
        print('Factory executor native plugin readiness failed; private output withheld.',file=sys.stderr)
        raise SystemExit(1)
