"""Execute early refusal paths of the real installer without system changes."""
import os
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT=Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("flags",[
    {"ISOLATED_RUNTIME":"0","DEFER_START":"1"},
    {"ISOLATED_RUNTIME":"2","DEFER_START":"0"},
    {"ISOLATED_RUNTIME":"1","DEFER_START":"0","CERTIFICATE_PROFILE_ID":"","CERTIFICATE_REQUEST_ID":""},
    {"ISOLATED_RUNTIME":"1","DEFER_START":"1","CERTIFICATE_PROFILE_ID":"","CERTIFICATE_REQUEST_ID":""},
    {"ISOLATED_RUNTIME":"1","DEFER_START":"0","CERTIFICATE_PROFILE_ID":"origin_900","CERTIFICATE_REQUEST_ID":"initial_job_900","BOOTSTRAP_BINDING_SHA256":""},
])
def test_invalid_bootstrap_request_stops_before_any_install_command(tmp_path,flags):
    bash=Path('C:/Program Files/Git/bin/bash.exe') if os.name=='nt' else Path(shutil.which('bash') or '')
    if not bash.is_file():pytest.skip('a local bash executable is required')
    fake=tmp_path/'fake';fake.mkdir();(fake/'id').write_text('#!/usr/bin/env bash\necho 0\n',encoding='utf-8');(fake/'id').chmod(0o755)
    # A regression must fail safely even on a root CI machine. Every external
    # operation after validation is a refusal stub, never a real system change.
    for name in ['install','systemctl','sha256sum','ss','flock','python3','curl','wget','docker','uname','mkdir','cp','mv','rm','chmod','date']:
        path=fake/name;path.write_text('#!/usr/bin/env bash\necho unexpected-install-command >&2\nexit 99\n',encoding='utf-8');path.chmod(0o755)
    fake_path=str(fake)
    if os.name=='nt':
        cygpath=bash.parents[1]/'usr/bin/cygpath.exe'
        fake_path=subprocess.check_output([str(cygpath),'-u',str(fake)],text=True).strip()
    environment={**os.environ,'NODE_ID':'900','NODE_PORT':'8443','NODEPULSE_TOKEN':'synthetic-node-token',**flags}
    result=subprocess.run([str(bash),'--noprofile','--norc','-c','export PATH="$1:/usr/bin:/bin"; exec bash "$2"','bootstrap-test',fake_path,str(ROOT/'ops/install-nodepulse-v2node.sh')],
                          env=environment,text=True,capture_output=True,timeout=15,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    assert result.returncode==2 and 'synthetic-node-token' not in result.stdout+result.stderr
    assert 'Deferred bootstrap' in result.stderr or 'requires' in result.stderr
