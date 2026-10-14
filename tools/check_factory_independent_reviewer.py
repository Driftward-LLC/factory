#!/usr/bin/env python3
"""Disposable Docker split-role probe, synthetic credentials only; no model call."""
import argparse
import json
from pathlib import Path
import subprocess
import tempfile
import uuid


def runtime_probe(image, common, source, agent, temporary, marker):
    root = Path(temporary)
    repo = root / "fixture"
    repo.mkdir()
    def git(*args):
        return command(["git", "-c", "core.hooksPath=/dev/null", "-C", str(repo), *args])
    git("init", "-q", "-b", "main")
    git("config", "user.name", "Synthetic")
    git("config", "user.email", "fixture@example.invalid")
    git("config", "commit.gpgsign", "false")
    (repo / "index.html").write_text("<button>Factory synthetic browser</button>")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-qb", "candidate")
    (repo / "counter.py").write_text("count = 1\n")
    git("add", ".")
    git("commit", "-qm", "head")
    head = git("rev-parse", "HEAD")
    bundle = root / "source.bundle"
    git("bundle", "create", str(bundle), "main", "candidate")
    snapshot = root / "source.git"
    command(["git", "clone", "--bare", str(bundle), str(snapshot)])
    command(["git", "--git-dir", str(snapshot), "config", "--remove-section", "remote.origin"])
    token = root / "mcp-token"
    token.write_text("synthetic-private-mcp-token")
    token.chmod(0o444)
    runtime_common = common + ["--memory", "1g", "--pids-limit", "128",
                               "--tmpfs", "/workspace:rw,nosuid,nodev,size=256m,mode=0777"]
    command(["docker", "run", "-d", "--name", source, *runtime_common,
             "--network-alias", "source", "--user", "10001:10001", "--env", "HOME=/workspace/home",
             "--mount", f"type=bind,src={bundle},dst=/run/source.bundle,readonly",
             "--mount", f"type=bind,src={snapshot},dst=/run/source.git,readonly",
             "--mount", f"type=bind,src={token},dst=/run/mcp-token,readonly",
             image, "/opt/factory-reviewer/source_server.py", head, base])
    probe = """import json,time,urllib.request,subprocess
def call(name,args):
 raw=json.dumps({'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':name,'arguments':args}}).encode()
 req=urllib.request.Request('http://source:8765/mcp',data=raw,headers={'Content-Type':'application/json','Authorization':'Bearer synthetic-private-mcp-token'})
 with urllib.request.urlopen(req,timeout=30) as response: result=json.loads(response.read())['result']
 assert not result.get('isError'),result
 return result['content']
for attempt in range(40):
 try: info=json.loads(call('inspect_source',{})[0]['text']);break
 except Exception:
  if attempt==39:raise
  time.sleep(0.5)
assert info['head_sha']==HEAD and info['base_sha']==BASE and info['browser_probe']=='passed'
assert 'counter.py' in json.loads(call('diff',{})[0]['text'])
assert json.loads(call('read_file',{'path':'counter.py'})[0]['text'])=='count = 1'
check=json.loads(call('run_check',{'argv':['python3','-c',"from pathlib import Path; import os; assert not Path('/run/reviewer-auth.json').exists(); assert not Path('/root/.codex/auth.json').exists(); assert not os.access('/run/source.git/config',os.W_OK); print('source_credentials_absent')"]})[0]['text'])
assert check['exit_code']==0,check
call('start_preview',{'argv':['python3','-m','http.server','3000','--bind','127.0.0.1']})
time.sleep(0.5)
browser=call('browser_check',{'url':'http://127.0.0.1:3000/index.html'})
assert 'Factory synthetic browser' in browser[0]['text'] and browser[1]['type']=='image'
version=subprocess.check_output(['codex','--version'],text=True).strip()
print(json.dumps({'native_source_mcp':'passed','immutable_source_read':'passed','test_execution':'passed','browser_execution':'passed','source_credentials':'absent','codex_binary':version,'model_called':False}))
""".replace("HEAD", repr(head)).replace("BASE", repr(base))
    return json.loads(command(["docker", "run", "--rm", "--name", agent, *runtime_common,
                              "--mount", f"type=bind,src={marker},dst=/run/reviewer-auth.json,readonly",
                              image, "-c", probe], timeout=90))


def command(args, timeout=30):
    return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL, timeout=timeout).strip()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", required=True)
    parser.add_argument("--evidence", required=True)
    parser.add_argument("--runtime", action="store_true")
    args = parser.parse_args()
    name = "factory-review-fixture-" + uuid.uuid4().hex[:16]
    agent, source, network = name + "-agent", name + "-source", name + "-net"
    evidence = {"schema": "factory-reviewer-synthetic-containment-v1", "model_called": False,
                "real_credentials_used": False, "production_canary": False}
    with tempfile.TemporaryDirectory(prefix="factory-review-fixture-") as temporary:
        marker = Path(temporary) / "auth.json"
        marker.write_text('{"synthetic":"reviewer-only-marker"}')
        marker.chmod(0o644)
        try:
            command(["docker", "network", "create", "--internal", "--opt",
                     "com.docker.network.bridge.gateway_mode_ipv4=isolated", network])
            common = ["--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                      "--pids-limit", "32", "--memory", "128m", "--network", network,
                      "--tmpfs", "/tmp:rw,nosuid,nodev,size=16m", "--entrypoint", "python3"]
            probe = """import json,os,socket,importlib.util
from pathlib import Path
paths=['/run/reviewer-auth.json','/run/docker.sock','/var/run/docker.sock','/root/.codex/auth.json','/docker/driftward/hermes/data']
result={'visible':{p:Path(p).exists() for p in paths},'credential_env_present':any(any(s in k for s in ['TOKEN','API_KEY','AUTHORIZATION']) for k in os.environ),'playwright_available':importlib.util.find_spec('playwright') is not None}
try:
 socket.create_connection(('1.1.1.1',443),timeout=1).close();result['external_network_reachable']=True
except OSError:result['external_network_reachable']=False
print(json.dumps(result))"""
            evidence["source"] = json.loads(command(["docker", "run", "--rm", "--name", source, *common,
                                                    "--user", "10001:10001", args.image, "-c", probe]))
            evidence["agent"] = json.loads(command(["docker", "run", "--rm", "--name", agent, *common,
                                                   "--mount", f"type=bind,src={marker},dst=/run/reviewer-auth.json,readonly",
                                                   args.image, "-c", probe]))
            metadata = json.loads(command(["docker", "network", "inspect", network]))[0]
            evidence["network"] = {"internal": metadata["Internal"], "ipv6": metadata["EnableIPv6"],
                                   "gateway_mode": metadata["Options"]["com.docker.network.bridge.gateway_mode_ipv4"]}
            assert not any(evidence["source"]["visible"].values())
            assert not evidence["source"]["credential_env_present"]
            assert not evidence["source"]["external_network_reachable"]
            assert evidence["agent"]["visible"]["/run/reviewer-auth.json"]
            assert not evidence["agent"]["external_network_reachable"]
            evidence["containment_probe"] = "passed"
            if args.runtime:
                evidence["runtime"] = runtime_probe(args.image, common, source, agent, temporary, marker)
            else:
                evidence["full_reviewer_capability"] = "unavailable_without_provisioned_codex_playwright_image"
            Path(args.evidence).write_text(json.dumps(evidence, indent=2) + "\n")
            print(json.dumps(evidence))
        finally:
            for container in [agent, source]:
                subprocess.run(["docker", "rm", "-f", container], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            subprocess.run(["docker", "network", "rm", network], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


if __name__ == "__main__":
    main()
