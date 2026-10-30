"""Small same-origin browser terminal for the public human security capability."""

from __future__ import annotations

import json
import secrets
from http.cookies import SimpleCookie
from urllib.parse import urljoin, urlsplit

from jiuwen_memory.api import AgentMemoryError, RateLimitedError

_BASE = "/security/privacy"
_COOKIE = "jiuwen_human_browser"
_HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Jiuwen 敏感记忆查看</title><h1>敏感记忆查看</h1>
<p>通过 Passkey 验证本人身份，仅在此页面显示当前记忆。刷新页面会清除显示内容。</p>
<p><label>归属 Scope（JSON） <input id="scope" size="65" value='{}'></label></p>
<p><label>记忆 ID <input id="unit" size="50"></label></p>
<p><label>记忆版本 privacy_revision <input id="revision" size="40"></label></p>
<p><button id="view">验证并查看</button> <button id="clear">清除显示</button></p>
<details><summary>首次登记 Passkey</summary><p>使用管理员通过独立渠道交付的一次性登记码。</p>
<input id="invite" type="password" autocomplete="off" aria-label="一次性登记码">
<button id="enroll">登记</button></details>
<p id="status" role="status"></p><pre id="content"></pre>
<script src="/security/privacy/viewer.js" defer></script></html>"""
_JS = """'use strict';
const el = id => document.getElementById(id);
const decode = s => Uint8Array.from(atob(s.replace(/-/g,'+').replace(/_/g,'/')),c=>c.charCodeAt(0));
const encode = b => btoa(String.fromCharCode(...new Uint8Array(b)))
  .replace(/\\+/g,'-').replace(/\\//g,'_').replace(/=+$/,'');
async function post(action, payload) {
  const r = await fetch('/security/privacy/'+action, {method:'POST',credentials:'same-origin',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),cache:'no-store'});
  const b = await r.json();
  if(!r.ok) throw new Error('操作未完成，请重新验证并检查权限或记忆版本。'); return b;
}
async function run(enroll) {
  el('content').textContent=''; el('status').textContent='等待本人验证…';
  el('view').disabled=el('enroll').disabled=true;
  try {
    const target={scope:JSON.parse(el('scope').value),unit_id:el('unit').value,
      revision:el('revision').value};
    const invite=enroll?el('invite').value:''; el('invite').value='';
    const begin=await post('begin',{...target,invite});
    const p=begin.publicKey; p.challenge=decode(p.challenge);
    if(enroll) p.user.id=decode(p.user.id);
    for(const key of ['allowCredentials','excludeCredentials']) if(p[key])
      p[key]=p[key].map(x=>({...x,id:decode(x.id)}));
    const credential=enroll?await navigator.credentials.create({publicKey:p})
      :await navigator.credentials.get({publicKey:p});
    const response={clientDataJSON:encode(credential.response.clientDataJSON)};
    if(enroll) response.attestationObject=encode(credential.response.attestationObject);
    else { response.authenticatorData=encode(credential.response.authenticatorData);
      response.signature=encode(credential.response.signature);
      response.userHandle=credential.response.userHandle
        ?encode(credential.response.userHandle):null; }
    const result=await post('finish',{...target,challenge_id:begin.challenge_id,
      credential:{id:credential.id,rawId:encode(credential.rawId),type:credential.type,response,
      clientExtensionResults:credential.getClientExtensionResults()}});
    el('content').textContent=result.content||'';
    el('status').textContent=result.enrolled?'登记成功，请再点击验证并查看。':'已通过验证。';
  } catch(e) { el('status').textContent='操作未完成，请重新验证并检查权限或记忆版本。'; }
  finally {el('view').disabled=el('enroll').disabled=false;}
}
el('view').addEventListener('click',()=>run(false));
el('enroll').addEventListener('click',()=>run(true));
el('clear').addEventListener('click',()=>{el('content').textContent='';el('status').textContent='';});
window.addEventListener('pagehide',()=>{el('content').textContent='';});
try {const t=JSON.parse(decodeURIComponent(location.hash.slice(1))); location.hash='';
  el('scope').value=JSON.stringify(t.scope||{});el('unit').value=t.unit_id||'';el('revision').value=t.revision||'';
} catch(e) {}"""


def handle_human_request(handler, capability, *, post: bool) -> bool:
    """Return False for other routes; no access to kernel private state."""
    if not handler.path.startswith(_BASE):
        return False

    def send(status, body, mime="application/json; charset=utf-8", cookie=""):
        data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
        handler.send_response(status)
        for name, value in {
            "Content-Type": mime,
            "Content-Length": str(len(data)),
            "Cache-Control": "no-store",
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "default-src 'none'; script-src 'self'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'; form-action 'none'",
        }.items():
            handler.send_header(name, value)
        if cookie:
            handler.send_header("Set-Cookie", cookie)
        handler.end_headers()
        handler.wfile.write(data)

    if capability is None:
        send(404, {"error": "human view unavailable"})
        return True
    origin = urlsplit(capability.origin)
    if handler.headers.get("Host") != origin.netloc:
        send(403, {"error": "origin rejected"})
        return True
    if not post:
        if handler.path in {_BASE, _BASE + "/"}:
            secure = "; Secure" if origin.scheme == "https" else ""
            cookie = (
                f"{_COOKIE}={secrets.token_urlsafe(32)}; Path={_BASE}; "
                f"HttpOnly; SameSite=Strict{secure}"
            )
            send(200, _HTML, "text/html; charset=utf-8", cookie)
        elif handler.path == urljoin(f"{_BASE}/", "viewer.js"):
            send(200, _JS, "text/javascript; charset=utf-8")
        else:
            send(404, {"error": "not found"})
        return True
    if handler.headers.get("Origin") != capability.origin:
        send(403, {"error": "origin rejected"})
        return True
    if handler.headers.get("Content-Type", "").split(";")[0] != "application/json":
        send(415, {"error": "JSON required"})
        return True
    operation = handler.path.removeprefix(_BASE + "/")
    if operation not in {"begin", "finish"}:
        send(404, {"error": "not found"})
        return True
    try:
        length = int(handler.headers.get("Content-Length", "0"))
        if not 0 < length <= 65536:
            send(413, {"error": "invalid body length"})
            return True
        with capability.admission(handler.client_address[0]):
            cookies = SimpleCookie()
            cookies.load(handler.headers.get("Cookie", ""))
            browser = cookies[_COOKIE].value if _COOKIE in cookies else ""
            payload = json.loads(handler.rfile.read(length))
            if not isinstance(payload, dict):
                raise ValueError
            result = (
                capability.finish(payload, browser, peer=handler.client_address[0])
                if operation == "finish"
                else capability.begin(payload, browser)
            )
    except RateLimitedError:
        send(429, {"error": "human authentication budget exhausted"})
    except (AgentMemoryError, ValueError, TypeError, KeyError):
        send(403, {"error": "human verification or resource authorization failed"})
    except Exception:
        send(503, {"error": "human view unavailable"})
    else:
        send(200, result)
    return True
