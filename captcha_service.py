"""
完全自动的公共图灵测试去分辨计算机和人类，所以我们简称它为 CAPTCHA

支持 Cloudflare Turnstile / hCaptcha / Google reCAPTCHA 
"""

import json
import urllib.parse
import urllib.request


PROVIDERS = {
    "turnstile": (
        "https://challenges.cloudflare.com/turnstile/v0/siteverify",
        "https://challenges.cloudflare.com/turnstile/v0/api.js?render=explicit",
    ),
    "hcaptcha": (
        "https://hcaptcha.com/siteverify",
        "https://js.hcaptcha.com/1/api.js?render=explicit",
    ),
    "recaptcha": (
        "https://www.google.com/recaptcha/siteverify",
        "https://www.google.com/recaptcha/api.js?render=explicit",
    ),
}

IMAGE_PROVIDER = "image"

REQUEST_TIMEOUT = 10


def is_third_party(provider):
    """第三方？"""
    return str(provider).lower() in PROVIDERS


def verify_token(provider, secret, token):
    """
    调用供应商 siteverify 校验
    """
    provider = str(provider).lower()
    if provider not in PROVIDERS:
        return False
    if not secret or not token:
        return False

    verify_url, _ = PROVIDERS[provider]
    data = urllib.parse.urlencode({
        "secret": secret,
        "response": token,
    }).encode("utf-8")

    req = urllib.request.Request(
        verify_url,
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
            body = resp.read().decode("utf-8")
    except Exception:
        return False

    try:
        result = json.loads(body)
    except (ValueError, TypeError):
        return False

    return bool(result.get("success", False))


def render_page(provider, site_key):
    """
    渲染验证码 HTML

    显然客户端需要 callHandler 'captchaDone' 然后拿到头肯即可（显然 UI Remake 不能直接拿）

    返回 (html, provider, script_url)
    """
    provider = str(provider).lower()
    if provider not in PROVIDERS or not site_key:
        return None, None, None

    _, script_url = PROVIDERS[provider]

    if provider == "turnstile":
        render_expr = "window.turnstile.render(el, {sitekey: SITE_KEY, callback: onDone})"
        init_guard = "window.turnstile"
    elif provider == "hcaptcha":
        render_expr = "window.hcaptcha.render(el, {sitekey: SITE_KEY, callback: onDone})"
        init_guard = "window.hcaptcha"
    elif provider == "recaptcha":
        render_expr = "window.grecaptcha.render(el, {sitekey: SITE_KEY, callback: onDone})"
        init_guard = "window.grecaptcha"

    html = """<!DOCTYPE html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>TouchFish AntiAI</title>
<style>
  body { margin:0; display:flex; align-items:center; justify-content:center;
         min-height:100vh; background:#1c1c1e; font-family:system-ui,sans-serif; }
  #box { text-align:center; }
  #err { color:#ff6b6b; font-size:14px; margin-top:12px; display:none; }
</style>
</head>
<body>
  <div id="box">
    <div id="captcha"></div>
    <div id="err">LOADING FAILED</div>
  </div>
<script>
  var SITE_KEY = %SITE_KEY%;
  function onDone(token) {
    if (!token) return;
    console.log('[tf-captcha] onDone called, token length=', token.length);
    // 是的，专门给 TFC 留的让它直接发给 flutter，TFUR 自己想办法（bushi
    try {
      if (window.flutter_inappwebview && window.flutter_inappwebview.callHandler) {
        window.flutter_inappwebview.callHandler('captchaDone', token);
      }
    } catch (e) { console.log('[tf-captcha] callHandler error', e); }
    // iframe？
    try {
      if (window.parent && window.parent !== window) {
        console.log('[tf-captcha] posting to parent');
        window.parent.postMessage('captcha_tk=' + token, '*');
      } else {
        console.log('[tf-captcha] no parent (top-level window)');
      }
    } catch (e) { console.log('[tf-captcha] postMessage error', e); }
  }
  function render() {
    var el = document.getElementById('captcha');
    if (%GUARD%) {
      %RENDER_EXPR%;
    } else {
      document.getElementById('err').style.display = 'block';
    }
  }
  var s = document.createElement('script');
  s.src = %SCRIPT_URL%;
  s.async = true;
  s.defer = true;
  s.onload = render;
  s.onerror = function () {
    document.getElementById('err').style.display = 'block';
  };
  document.head.appendChild(s);
</script>
</body>
</html>"""

    html = html.replace("%SITE_KEY%", json.dumps(site_key))
    html = html.replace("%GUARD%", init_guard)
    html = html.replace("%RENDER_EXPR%", render_expr)
    html = html.replace("%SCRIPT_URL%", json.dumps(script_url))

    return html, provider, script_url
