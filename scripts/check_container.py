"""Run an isolated, network-disabled container smoke test with synthetic config."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import tempfile
import time
import uuid

from cryptography.fernet import Fernet


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--image", default="canvas-connector:verify")
    parser.add_argument("--context")
    parser.add_argument("--mode", choices=["institution", "personal"], default="institution")
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/:@-]*", args.image):
        parser.error("Expected a Docker image reference")
    docker = ["docker"] + (["--context", args.context] if args.context else [])
    name = "canvas-check-" + uuid.uuid4().hex[:10]
    test_image = name + ":synthetic"

    def run(*command, **kwargs):
        return subprocess.run([*docker, *command], check=True, capture_output=True, text=True, **kwargs)

    with tempfile.TemporaryDirectory(prefix="canvas-container-check-") as directory:
        folder = Path(directory)
        env = folder / "private.env"
        env.write_text("\n".join([
            "CONNECTOR_AUTH_MODE=" + args.mode,
            "CONNECTOR_PUBLIC_URL=https://connector.example",
            "CONNECTOR_DATABASE=/data/connector.db",
            "CONNECTOR_ENCRYPTION_KEY=" + Fernet.generate_key().decode(),
            "CONNECTOR_INSTITUTIONS_FILE=/app/synthetic-institutions.json",
        ]) + "\n")
        env.chmod(0o600)
        institutions = folder / "institutions.json"
        institutions.write_text(json.dumps([{"origin": "https://school.example",
            "client_id": "synthetic-client", "client_secret": "synthetic-secret"}]))
        # Bake only the synthetic institution fixture into a temporary image so
        # the runtime can remain read-only without mounting any host folders.
        (folder / "Dockerfile").write_text("FROM " + args.image + "\nCOPY institutions.json /app/synthetic-institutions.json\n")
        (folder / ".dockerignore").write_text("*\n!Dockerfile\n!institutions.json\n")
        try:
            run("build", "--network", "none", "-t", test_image, str(folder), timeout=90)
            command = ["canvas-connector", "serve", "--host", "127.0.0.1", "--port", "8000", "--workers", "2"]
            if args.mode == "personal":
                # Synthetic operator bootstrap only. Production initialization
                # is an explicit interactive command, never a startup action.
                bootstrap = '''import os
from canvas_connector.storage import open_store
from canvas_connector.personal_admin import initialize_owner
store=open_store(os.environ['CONNECTOR_DATABASE'],os.environ['CONNECTOR_ENCRYPTION_KEY'])
initialize_owner(store,'owner@example.test','Synthetic-container-password!')
store.close()
os.execvp('canvas-connector',['canvas-connector','serve','--host','127.0.0.1','--port','8000','--workers','1'])
'''
                command = ["python", "-c", bootstrap]
            run("create", "--name", name, "--network", "none", "--read-only",
                "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
                "--tmpfs", "/tmp", "--tmpfs", "/data:uid=10001,gid=10001,mode=700",
                "--env-file", str(env), test_image, *command)
            run("start", name)
            probe = '''import json, os, urllib.request, urllib.error
def fetch(path):
    request=urllib.request.Request('http://127.0.0.1:8000'+path,headers={'Host':'connector.example'})
    try:
        with urllib.request.urlopen(request,timeout=5) as r: return r.status,json.load(r)
    except urllib.error.HTTPError as e: return e.code,None
status,health=fetch('/health')
assert status==200 and health['canvas_authenticated'] is False
assert fetch('/mcp')[0]==401
assert fetch('/.well-known/oauth-protected-resource/mcp')[1]['scopes_supported']==['canvas:read','canvas:write']
assert os.getuid()==10001
workers=0
for pid in os.listdir('/proc'):
    if not pid.isdigit(): continue
    if int(pid)==os.getpid(): continue
    try:
        with open('/proc/'+pid+'/cmdline','rb') as handle: command=handle.read()
        if b'multiprocessing.spawn' in command and b'--multiprocessing-fork' in command: workers+=1
    except (OSError,PermissionError): pass
personal=os.environ.get('CONNECTOR_AUTH_MODE')=='personal'
assert workers==(0 if personal else 2)
if personal:
    assert fetch('/account/signup')[0]==404
    assert fetch('/account/reset')[0]==404
    assert fetch('/connection.json')[1]['auth_mode']=='personal'
print(json.dumps({'health':200,'unauthenticated_mcp':401,'oauth_discovery':True,'nonroot':True,'workers':1 if personal else 2,'external_network':'disabled','mode':'personal' if personal else 'institution'}))
'''
            deadline = time.monotonic() + 60
            while True:
                try:
                    result = run("exec", name, "python", "-c", probe, timeout=15)
                    print(result.stdout.strip())
                    break
                except subprocess.CalledProcessError as error:
                    if time.monotonic() >= deadline:
                        logs = run("logs", name)
                        print((logs.stdout + logs.stderr)[-4000:])
                        print(error.stderr[-1000:])
                        raise RuntimeError("Container did not pass the smoke test") from None
                    time.sleep(0.3)
        finally:
            subprocess.run([*docker, "rm", "-f", name], capture_output=True, check=False)
            subprocess.run([*docker, "image", "rm", test_image], capture_output=True, check=False)


if __name__ == "__main__":
    main()
