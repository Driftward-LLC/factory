#!/usr/bin/env python3
"""Credential-free, offline native acceptance against the deployed Hermes image."""
import argparse
import base64
import json
from pathlib import Path
import subprocess

IMAGE = 'sha256:6b0171bdfd5a0c03489ced7d0c527c6c305cb3660ac83ed6ac468805289ad9c3'
ROOT = Path(__file__).resolve().parents[1]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plugin-source', required=True, type=Path)
    args = parser.parse_args()
    archive = subprocess.check_output(['git', '-C', str(args.plugin_source), 'archive', '--format=tar',
                                       'db14524356cfebd132833ed06a7c8a097ef5c11c'])
    payload = {'engineering': base64.b64encode(archive).decode(), 'observer': {
        name: base64.b64encode((ROOT / 'deploy/factory-observers/posthog' / name).read_bytes()).decode()
        for name in ('__init__.py', 'plugin.yaml')}}
    script = (ROOT / 'deploy/factory-profile-runtime.py').read_text()
    reviewer = (ROOT / 'deploy/independent-reviewer/native_agent.py').read_text()
    review_observer = (ROOT / 'deploy/independent-reviewer/review_observer.py').read_bytes()
    probe = r'''
import contextlib, io, sys
sys.path.insert(0, '/opt/hermes')
root = ROOT
root.chmod(0o755)
os.chown(root, 10000, 987)
(root / 'config.yaml').write_text(json.dumps({'model': {'default':'gpt-6.1-sol','provider':'openai-codex'},
    'mcp_servers': {'fixture': {'command': 'true', 'env': {'API_KEY':'${FIXTURE_KEY}'}}}}))
os.chown(root / 'config.yaml',10000,987)
from hermes_cli.profiles import create_profile
with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
    for name in ROLES.values():
        create_profile(name,no_alias=True,no_skills=True)
        os.chown(root / 'profiles' / name,10000,987)
        for directory in ('audio_cache','cron','hooks','image_cache','logs','memories','pairing','sessions','skills'):
            target=root/'profiles'/name/directory
            if target.exists():
                os.chown(target,10000,987)
os.chown(root / 'profiles',10000,987)
legacy = root / 'factory-reviewer-auth'
legacy.mkdir(mode=0o700)
(legacy / 'auth.json').write_text('SYNTHETIC_AUTH_ONLY')
(legacy / 'auth.json').chmod(0o600)
def snapshot(path):
    st=path.stat()
    return (path.read_bytes(), st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode), st.st_ino)
protected = [root/'config.yaml', legacy/'auth.json']
(root/'.env').write_text('SYNTHETIC_DEFAULT_ENV=never-adopt\n')
(root/'.env').chmod(0o600)
protected.append(root/'.env')
for name in ROLES.values():
    env=root/'profiles'/name/'.env'
    env.write_text('SYNTHETIC_ENV_SENTINEL=never-copy\n')
    env.chmod(0o600)
    protected.append(env)
before={p:snapshot(p) for p in protected}
fixture_bin=Path('/tmp/fixture-bin')
fixture_bin.mkdir(exist_ok=True)
fixture_gh=fixture_bin/'gh'
fixture_gh.write_text('#!/opt/hermes/.venv/bin/python\nimport os,sys\n'
    'if sys.argv[1:]==["--version"]: print("gh version fixture"); sys.exit(0)\n'
    'assert "OPENAI_API_KEY" not in os.environ and "GH_TOKEN" not in os.environ\n'
    'assert sys.argv[1:]==["pr","create","--body","PRIVATE_SENTINEL"]\n'
    'assert sys.stdin.read()=="PRIVATE_STDIN"\n'
    'sys.stdout.write("PRIVATE_OUTPUT\\n");sys.stderr.write("PRIVATE_STDERR\\n");sys.exit(7)\n')
fixture_gh.chmod(0o755)
os.environ['PATH']=str(fixture_bin)+':'+os.environ['PATH']
inventory_before={p:(p.stat().st_uid,p.stat().st_gid,stat.S_IMODE(p.stat().st_mode),p.stat().st_ino)
                  for p in root.rglob('*')}
assert not inventory()['profiles']['executor']['engineering_enabled']
assert inventory_before=={p:(p.stat().st_uid,p.stat().st_gid,stat.S_IMODE(p.stat().st_mode),p.stat().st_ino)
                         for p in root.rglob('*')}
configured=configure(PAYLOAD)
assert configure(PAYLOAD)==configured
assert all(snapshot(p)==value for p,value in before.items())
assert inventory()['profiles']['executor']['capabilities_match_default']
assert (root/'profiles'/ROLES['executor']/'config.yaml').stat().st_uid==0
assert (root/'profiles'/ROLES['reviewer']).stat().st_mode & 0o077 == 0
observer_link=root/'profiles'/ROLES['executor']/'plugins'/'observability'/'posthog'
old_observer={'__init__.py':b'# previous observer fixture\n','plugin.yaml':base64.b64decode(PAYLOAD['observer']['plugin.yaml'])}
old_release=immutable_release(root/'profiles'/ROLES['executor']/'factory-managed'/'releases'/'factory-posthog',tree_digest(old_observer),old_observer)
observer_link.unlink()
observer_link.symlink_to(old_release,target_is_directory=True)
upgrade_before=observer_snapshot()
upgrade_result=upgrade_observer({**PAYLOAD,'snapshot_digest':recovery_digest(upgrade_before)})
assert upgrade_result['protected']==upgrade_before['protected']
assert upgrade_result['links']['posthog']['digest']==tree_digest(observer_files(PAYLOAD))
try:
    upgrade_observer({**PAYLOAD,'snapshot_digest':recovery_digest(upgrade_before)})
    raise AssertionError('stale observer upgrade accepted')
except ValueError:
    pass
move_auth()
named=root/'profiles'/ROLES['reviewer']/'auth.json'
assert snapshot(named)==before[legacy/'auth.json']
review_home=named.parent/'profiles'/'fixture'
review_home.mkdir(parents=True,mode=0o700)
reviewer_source=REVIEWER_SOURCE
auth_probe="""import sys,os
sys.path.insert(0,'/opt/hermes')
namespace={'__name__':'native_reviewer_fixture'}
""" + 'exec(' + repr(reviewer_source) + ',namespace)\n' + """
from pathlib import Path
import json
home=Path(os.environ['HERMES_HOME'])/'profiles'/'fixture'
os.environ['HERMES_HOME']=str(home)
(home/'config.yaml').write_text(json.dumps(namespace['CONFIG']))
from hermes_constants import set_hermes_home_override
set_hermes_home_override(home)
from agent.secret_scope import set_multiplex_context,set_secret_scope
set_multiplex_context(True)
set_secret_scope({},profile_home=str(home))
import hermes_cli.auth
hermes_cli.auth.get_codex_auth_status=lambda:{'logged_in':True,'auth_mode':'chatgpt','base_url':'https://chatgpt.com/backend-api/codex'}
namespace['dedicated_auth']()
from hermes_cli.plugins_discovery import suppress_plugin_discovery
with suppress_plugin_discovery():
    from run_agent import AIAgent
    agent=AIAgent(model='fixture',provider='openai',api_mode='chat_completions',api_key='SYNTHETIC_ONLY',
        base_url='https://example.invalid/v1',enabled_toolsets=[],fallback_model=[],max_iterations=1,
        quiet_mode=True,session_db=None,skip_context_files=True,load_soul_identity=False,skip_memory=True,
        skip_background_review=True,checkpoints_enabled=False,save_trajectories=False,verbose_logging=False)
    assert not agent.tools and not agent.valid_tool_names and not agent._fallback_chain
    assert not os.environ.get('SYNTHETIC_DEFAULT_ENV') and not os.environ.get('SYNTHETIC_ENV_SENTINEL')
    agent.close()
"""
auth_result=subprocess.run([sys.executable,'-c',auth_probe],capture_output=True,text=True,
               env={**os.environ,'HERMES_HOME':str(named.parent)})
assert auth_result.returncode==0, auth_result.stderr[-1500:]
move_auth(True)
assert snapshot(legacy/'auth.json')==before[legacy/'auth.json']
def executor_probe():
    assert os.geteuid()==0 and os.getegid()==0
    assert os.environ['FIXTURE_KEY']=='SYNTHETIC_REFERENCE_ONLY'
    import run_agent
    RealAgent=run_agent.AIAgent
    class ConstructorProbe(RealAgent):
        def __init__(self,**kwargs):
            kwargs.update(provider='openai',api_mode='chat_completions',api_key='SYNTHETIC_ONLY',base_url='https://example.invalid/v1')
            super().__init__(**kwargs)
        def run_conversation(self,*args,**kwargs):
            return {'final_response':'fixture constructor; no request'}
    run_agent.AIAgent=ConstructorProbe
    result=canary({'role':'executor','id':str(uuid.uuid4())})
    tool_result=github_canary({'id':str(uuid.uuid4()),'observer_digest':tree_digest(observer_files(PAYLOAD))})
    assert tool_result['exit_code']==0 and tool_result['parent_span_id']!=tool_result['child_span_id']
    from hermes_cli.plugins import get_plugin_manager
    observer=get_plugin_manager()._plugins['observability/posthog'].module
    from hermes_constants import get_hermes_home
    from gateway.session_context import get_session_env
    assert observer._github_available(), 'synthetic gh unavailable in isolated local executor'
    assert 'factory_github' in get_plugin_manager()._plugins['observability/posthog'].tools_registered, 'tool registration unavailable'
    from hermes_cli.plugins import render_system_prompt_sections
    assert 'factory_github' in str(render_system_prompt_sections({'session_id':'fixture-session'})), 'GitHub usage prompt unavailable'
    events=[]
    observer._enqueue=events.append
    ids={'session_id':'fixture-session','task_id':'fixture-task','turn_id':'fixture-turn',
         'api_request_id':'fixture-request','tool_call_id':'fixture-call'}
    observer.on_post_api_request(**ids)
    from model_tools import handle_function_call
    from model_tools import get_tool_definitions
    catalog=get_tool_definitions(enabled_toolsets=['terminal'],skip_tool_search_assembly=True)
    assert any(tool.get('function',tool).get('name')=='factory_github' for tool in catalog), 'factory_github absent from native catalog'
    assert 'factory_github' in handle_function_call('tool_search',{'queries':['github']},enabled_toolsets=['terminal'],**ids)
    assert 'factory_github' in handle_function_call('tool_describe',{'names':['factory_github']},enabled_toolsets=['terminal'],**ids)
    raw=handle_function_call('tool_call',{'calls':[{'name':'factory_github','arguments':{
        'args':['pr','create','--body','PRIVATE_SENTINEL'],'stdin':'PRIVATE_STDIN','workdir':'/tmp'}}]},
        enabled_toolsets=['terminal'],**ids)
    operation=json.loads(raw)
    assert operation['exit_code']==7 and operation['stdout']=='PRIVATE_OUTPUT\n' and operation['stderr']=='PRIVATE_STDERR\n', raw
    generation=next(e['properties'] for e in events if e['event']=='$ai_generation')
    tool=next(e['properties'] for e in events if e['properties'].get('$ai_span_name')=='factory_github')
    child=next(e['properties'] for e in events if e['properties'].get('github.observation')=='cli_invocation')
    assert child['$ai_parent_id']==tool['$ai_span_id'] and tool['$ai_parent_id']==generation['$ai_span_id']
    assert child['$ai_trace_id']==generation['$ai_trace_id'] and child['github.cli_status']=='nonzero_exit'
    assert child['github.effect_status']=='unverified' and child['github.cli_operation']=='pr.create'
    assert tool['hermes.tool_status']=='error' and tool['factory.external_operation_coverage']=='controlled_github_cli'
    assert 'PRIVATE_' not in json.dumps(events), events
    import threading, time
    from tools.interrupt import set_interrupt
    set_interrupt(True)
    try:
        response,status=observer._run_github([sys.executable,'-c','raise RuntimeError("must not run")'],'','/tmp',30,{})
        assert status=='interrupted_before_spawn' and response['exit_code'] is None
        before=observer._github_event(ids,'pr.create',status,None,time.monotonic())['properties']
        assert before['github.process_started'] is False
    finally:
        set_interrupt(False)
    for close_pipes in (False,True):
        ready=Path('/tmp/github-interrupt-ready-'+str(close_pipes))
        action=Path('/tmp/github-interrupt-action-'+str(close_pipes))
        delayed='import time; from pathlib import Path; time.sleep(1); Path('+repr(str(action))+').touch()'
        code=('import os,sys,time,subprocess; from pathlib import Path; '
            'subprocess.Popen([sys.executable,"-c",'+repr(delayed)+'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
            'Path('+repr(str(ready))+').touch(); '+('os.close(1);os.close(2); ' if close_pipes else '')+'time.sleep(2)')
        outcome=[]
        worker=threading.Thread(target=lambda:outcome.append(observer._run_github([sys.executable,'-c',code],'','/tmp',30,{})))
        worker.start()
        try:
            deadline=time.monotonic()+2
            while not ready.exists() and time.monotonic()<deadline:
                time.sleep(0.01)
            assert ready.exists(), 'synthetic interrupt child did not start'
            stopped_at=time.monotonic()
            set_interrupt(True,worker.ident)
            worker.join(1)
            assert not worker.is_alive() and time.monotonic()-stopped_at<1, 'native stop did not promptly finish'
            response,status=outcome[0]
            assert status=='interrupted' and response['exit_code']==-9, outcome
            after=observer._github_event(ids,'pr.create',status,response['exit_code'],stopped_at)['properties']
            assert after['github.process_started'] is True and after['github.effect_status']=='unverified'
            time.sleep(1.1)
            assert not action.exists(), 'interrupted subprocess group performed delayed action'
        finally:
            set_interrupt(True,worker.ident)
            worker.join(2)
            set_interrupt(False,worker.ident)
    from types import SimpleNamespace
    from hermes_constants import set_hermes_home_override
    for unrelated in (root, root/'profiles'/ROLES['reviewer']):
        set_hermes_home_override(unrelated)
        registered=[]
        observer.register(SimpleNamespace(register_hook=lambda *a:None,
            register_tool=lambda **kw:registered.append(kw),register_middleware=lambda *a:None,
            register_system_prompt_section=lambda *a,**kw:None))
        assert not registered, 'GitHub tool leaked to another profile'
    set_hermes_home_override(root/'profiles'/ROLES['executor'])
    from agent.skill_commands import build_preloaded_skills_prompt
    names=['driftward-engineering:'+name for name in ('implement-change','verify-app','prepare-pr')]
    prompt,loaded,missing=build_preloaded_skills_prompt(names)
    assert not missing and len(loaded)==3 and len(prompt)>1000, missing
    return {'uid':os.geteuid(),'native_constructor':True,'preloaded_skills':loaded,
            'github_catalog_dispatch_parent_tree':'passed','github_subprocess_fixture':'passed',
            'github_native_thread_interrupt_and_group_cleanup':'passed'}
def debug_probe():
    try:
        return executor_probe()
    except BaseException:
        import traceback
        traceback.print_exc()
        raise
result=debug_probe()
move_auth()
review_result=canary({'role':'reviewer','id':str(uuid.uuid4()),'review_observer':REVIEW_OBSERVER})
assert review_result['role']=='reviewer'
move_auth(True)
assert all(snapshot(p)==value for p,value in before.items())
print(json.dumps({'mixed_uid_configure':'passed','configure_idempotent':True,'auth_inode_owner_mode_preserved':True,
    'secrets_unchanged':True,'executor':result,'reviewer_dedicated_auth':'passed','reviewer_native_constructor':True,
    'provider_calls':0,'posthog_delivery':'not_tested_offline'}))
'''.replace('PAYLOAD', repr(payload)).replace('REVIEWER_SOURCE', repr(reviewer)).replace('REVIEW_OBSERVER',repr(base64.b64encode(review_observer).decode()))
    result = subprocess.run(['docker', 'run', '--rm', '-i', '--user', '0:0', '--network', 'none', '--read-only',
        '--cap-drop', 'ALL', '--cap-add', 'CHOWN', '--cap-add', 'SETUID', '--cap-add', 'SETGID', '--cap-add', 'DAC_OVERRIDE',
        '--security-opt', 'no-new-privileges', '--tmpfs', '/tmp:rw,exec', '--tmpfs', '/opt/data:rw',
        '--env', 'HERMES_HOME=/opt/data', '--env', 'FIXTURE_KEY=SYNTHETIC_REFERENCE_ONLY',
        '--env', 'POSTHOG_API_KEY=SYNTHETIC_ONLY', '--env', 'HERMES_BUNDLED_PLUGINS=/tmp/no-plugins',
        '--entrypoint', '/opt/hermes/.venv/bin/python', IMAGE, '-'], input=script+probe,
        text=True, capture_output=True, timeout=180)
    print(result.stdout[-4000:])
    print(result.stderr[-4000:])
    raise SystemExit(result.returncode)


if __name__ == '__main__':
    main()
