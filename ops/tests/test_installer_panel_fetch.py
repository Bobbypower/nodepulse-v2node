"""The panel token stays out of argv and redirect/error output."""
import io
import os
from pathlib import Path
import sys
from urllib.error import HTTPError
import urllib.request

import pytest

SCRIPT = Path(__file__).resolve().parents[1]/'install-nodepulse-v2node.sh'


def code():
    return SCRIPT.read_text().split('fetch_panel_json() {', 1)[1].split("<<'PY'\n",1)[1].split('\nPY\n',1)[0]


def test_fetch_does_not_put_panel_token_in_subprocess_arguments():
    function = SCRIPT.read_text().split('fetch_panel_json() {',1)[1].split('\nruntime_check()',1)[0]
    invocation = function.split("<<'PY'",1)[0]
    assert 'NODEPULSE_FETCH_TOKEN="${NODEPULSE_TOKEN}" python3' in invocation
    assert invocation.count('${NODEPULSE_TOKEN}') == 1
    assert '--data-urlencode' not in function and 'curl ' not in function and 'wget ' not in function


def test_panel_fetch_has_no_redirects_or_environment_proxy_and_writes_only_private_response(tmp_path, monkeypatch):
    seen = []
    class Response(io.BytesIO): status = 200
    class Opener:
        def open(self, request, timeout):
            seen.append(request.full_url)
            assert timeout == 30
            return Response(b'{"Nodes":[]}')
    def build(*handlers):
        assert handlers[0].proxies == {}
        assert handlers[1].redirect_request(None,None,None,None,None,None) is None
        return Opener()
    monkeypatch.setattr(urllib.request,'build_opener',build)
    monkeypatch.setenv('NODEPULSE_FETCH_TOKEN','synthetic-private-token')
    target=tmp_path/'config.json'
    monkeypatch.setattr(sys,'argv',['-', '/api/v2/server/local_config',str(target),'900','https://panel.example.invalid','1'])
    if os.name=='nt':monkeypatch.setattr(os,'fchmod',lambda fd,mode: None,raising=False)
    exec(compile(code(),'<panel-fetch>','exec'),{})
    assert target.read_bytes()==b'{"Nodes":[]}'
    assert len(seen)==1 and 'token=synthetic-private-token' in seen[0]
    if os.name=='posix':assert target.stat().st_mode&0o777==0o600


def test_fetch_error_does_not_emit_the_query_token_or_follow_redirect(tmp_path,monkeypatch,capsys):
    class Opener:
        def open(self,request,timeout):raise HTTPError(request.full_url,302,'url with synthetic-private-token',{},None)
    monkeypatch.setattr(urllib.request,'build_opener',lambda *a:Opener())
    monkeypatch.setenv('NODEPULSE_FETCH_TOKEN','synthetic-private-token')
    target=tmp_path/'config.json'
    monkeypatch.setattr(sys,'argv',['-','/api/v2/server/config',str(target),'900','https://panel.example.invalid','1'])
    with pytest.raises(SystemExit) as error:exec(compile(code(),'<panel-fetch>','exec'),{})
    captured=capsys.readouterr()
    assert error.value.code==1 and captured.err=='node_config_fetch_failed\n' and 'synthetic-private-token' not in captured.out+captured.err
    assert not target.exists()
