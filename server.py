#!/usr/bin/env python3
"""llmchat：静态页 + 同源反代 + Ollama 管理接口。

浏览器直连本机无 CORS 头的 API（如 Strata 8080、其它本地推理服务）会被拦成
NetworkError；页面直连失败时自动把请求发给 /__proxy，由本服务端转发，
再把上游响应原样流式吐回（同源，无 CORS 问题）。

/__admin/* 是 Ollama 管理接口（仅本机、校验 Origin + X-Admin-Token）：
  GET  /__admin/token            取管理令牌（页面同源读取）
  GET  /__admin/state            汇总状态：版本/服务/已加载模型/模型库/显存/日志
  POST /__admin/pull    {name}   拉取模型（流式进度）
  POST /__admin/delete  {name}   删除模型
  POST /__admin/load    {name}   加载并常驻（keep_alive=-1）
  POST /__admin/unload  {name}   卸载（keep_alive=0）
  POST /__admin/service {action} 启停服务 start|stop|restart（依赖 sudoers 白名单）
"""
import http.server
import json
import os
import re
import secrets
import socketserver
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
# ---- 全部可用环境变量覆盖，便于开源后按需部署 ----
PORT = int(os.environ.get('LLMCHAT_PORT') or (sys.argv[1] if len(sys.argv) > 1 else 17001))
HOST = os.environ.get('LLMCHAT_HOST', '127.0.0.1')
ORIGIN = os.environ.get('LLMCHAT_ORIGIN', f'http://127.0.0.1:{PORT}')
OLLAMA = os.environ.get('OLLAMA_BASE_URL', 'http://127.0.0.1:11434').rstrip('/')
SYSTEMCTL = os.environ.get('SYSTEMCTL_BIN', '/usr/bin/systemctl')
OLLAMA_UNIT = os.environ.get('OLLAMA_UNIT', 'ollama')
ADMIN_ENABLED = os.environ.get('LLMCHAT_ADMIN', '1') != '0'
SERVICE_CONTROL = os.environ.get('LLMCHAT_SERVICE_CONTROL', '1') != '0'
# 反向代理白名单（默认只允许本机，避免变成 SSRF 跳板）
PROXY_ALLOW = [h.strip().lower() for h in
               os.environ.get('LLMCHAT_PROXY_ALLOW', '127.0.0.1,localhost,::1').split(',') if h.strip()]
TOKEN = secrets.token_hex(24)

# 每个模型的启动参数（持久化到 model-params.json）
PARAMS_FILE = os.path.join(ROOT, 'model-params.json')
OPT_INT = {'num_ctx', 'num_batch', 'num_gpu', 'num_thread', 'num_keep',
           'top_k', 'seed', 'main_gpu', 'num_predict'}
OPT_FLOAT = {'temperature', 'top_p', 'min_p', 'repeat_penalty', 'typical_p',
             'presence_penalty', 'frequency_penalty', 'tfs_z'}
OPT_BOOL = {'use_mmap', 'use_mlock', 'low_vram', 'f16_kv', 'numa'}



def norm_keep(v, default=-1):
    """Ollama 的 keep_alive：纯数字必须是 JSON 数字（秒），带单位的则用 "5m"/"30s" 这类字符串。
    历史上前端会把 300 当字符串发过来，Go 的 time.ParseDuration 会报 missing unit，这里统一归一化。"""
    if v is None or v == '':
        return default
    if isinstance(v, bool):
        return default
    if isinstance(v, (int, float)):
        return v
    t = str(v).strip()
    if re.fullmatch(r'-?\d+', t):
        return int(t)
    if re.fullmatch(r'-?\d+\.\d+', t):
        return float(t)
    return t          # 形如 5m / 30s / 2h，直接交给 Ollama

def read_params():
    try:
        with open(PARAMS_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception:  # noqa: BLE001
        return {}


def write_params(d):
    tmp = PARAMS_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(d, f, ensure_ascii=False, indent=1)
    os.replace(tmp, PARAMS_FILE)


def clean_options(raw):
    out = {}
    for k, v in (raw or {}).items():
        if k in OPT_INT:
            try:
                out[k] = int(float(v))
            except Exception:  # noqa: BLE001
                pass
        elif k in OPT_FLOAT:
            try:
                out[k] = float(v)
            except Exception:  # noqa: BLE001
                pass
        elif k in OPT_BOOL:
            out[k] = v if isinstance(v, bool) else str(v).lower() in ('1', 'true', 'on', 'yes')
    return out


def run(cmd, timeout=25):
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or '').strip(), (p.stderr or '').strip()
    except Exception as e:  # noqa: BLE001
        return -1, '', str(e)


def ollama_home():
    """取 ollama 服务进程所属用户的家目录（用于定位默认模型库）。"""
    rc, out, _ = run([SYSTEMCTL, 'show', OLLAMA_UNIT, '-p', 'MainPID'])
    pid = 0
    if rc == 0 and '=' in out:
        try:
            pid = int(out.split('=', 1)[1].strip())
        except ValueError:
            pid = 0
    uid = ''
    if pid:
        for line in _read(f'/proc/{pid}/status').splitlines():
            if line.startswith('Uid:'):
                uid = line.split()[1]
                break
    if uid:
        for line in _read('/etc/passwd').splitlines():
            parts = line.split(':')
            if len(parts) > 5 and parts[2] == uid:
                return parts[5]
    return ''


def dir_free(path):
    """向上找第一个能 statvfs 的祖先目录，返回 (free, total)。"""
    cur = path
    for _ in range(6):
        try:
            st = os.statvfs(cur)
            return st.f_bavail * st.f_frsize, st.f_blocks * st.f_frsize
        except Exception:  # noqa: BLE001
            parent = os.path.dirname(cur)
            if parent == cur:
                break
            cur = parent
    return 0, 0


def models_info():
    """Ollama 模型库位置与分区可用空间。"""
    d = ''
    rc, env, _ = run([SYSTEMCTL, 'show', OLLAMA_UNIT, '-p', 'Environment'])
    if rc == 0:
        for tok in env.split():
            if tok.startswith('OLLAMA_MODELS='):
                d = tok.split('=', 1)[1].strip('"')
                break
    custom = bool(d)
    if not d:
        home = ollama_home()
        if home:
            d = os.path.join(home, '.ollama', 'models')
        else:
            d = os.path.expanduser('~/.ollama/models')
    free, total = dir_free(d)
    return {'dir': d, 'free': free, 'total': total, 'custom': custom}


def gpu_snapshot():
    rc, out, _ = run(['nvidia-smi',
        '--query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu',
        '--format=csv,noheader,nounits'])
    if rc != 0:
        return []
    rows = []
    for line in out.splitlines():
        parts = [x.strip() for x in line.split(',')]
        if len(parts) >= 6:
            try:
                rows.append({'index': parts[0], 'name': parts[1],
                             'used': int(float(parts[2])), 'total': int(float(parts[3])),
                             'util': parts[4], 'temp': parts[5]})
            except ValueError:
                continue
    return rows


def _read(path, default=''):
    try:
        with open(path, 'r', encoding='utf-8', errors='replace') as f:
            return f.read()
    except Exception:  # noqa: BLE001
        return default


def mem_snapshot():
    info = {}
    for line in _read('/proc/meminfo').splitlines():
        parts = line.split(':')
        if len(parts) == 2:
            info[parts[0].strip()] = int(parts[1].strip().split()[0]) * 1024  # kB -> B
    total = info.get('MemTotal', 0)
    avail = info.get('MemAvailable', info.get('MemFree', 0))
    used = max(0, total - avail)
    return {'total': total, 'used': used,
            'percent': round(used / total * 100, 1) if total else 0}


def cpu_snapshot():
    def sample():
        line = _read('/proc/stat').splitlines()[0]
        vals = [int(x) for x in line.split()[1:]]
        idle = vals[3] + (vals[4] if len(vals) > 4 else 0) + (vals[7] if len(vals) > 7 else 0)
        return sum(vals), idle
    t1, i1 = sample()
    time.sleep(0.15)
    t2, i2 = sample()
    dt, di = t2 - t1, i2 - i1
    pct = round((1 - di / dt) * 100, 1) if dt > 0 else 0.0
    model = ''
    for line in _read('/proc/cpuinfo').splitlines():
        if line.lower().startswith('model name'):
            model = line.split(':', 1)[1].strip()
            break
    try:
        load1 = float(_read('/proc/loadavg').split()[0])
    except Exception:  # noqa: BLE001
        load1 = 0.0
    cores = os.cpu_count() or 0
    return {'percent': max(0.0, min(100.0, pct)), 'cores': cores, 'model': model, 'load1': load1}


def ollama_get(path, timeout=10):
    try:
        with urllib.request.urlopen(OLLAMA + path, timeout=timeout) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001
        return {'error': str(e)}


def ollama_request(method, path, payload, timeout=120):
    rq = urllib.request.Request(OLLAMA + path, data=json.dumps(payload).encode(),
                                headers={'Content-Type': 'application/json'}, method=method)
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        raw = r.read()
        return json.loads(raw) if raw.strip() else {}


def ollama_delete(path, payload, timeout=120):
    """Ollama 新版本 /api/delete 只接受 DELETE；老版本用 POST，做个兼容回退。"""
    try:
        return ollama_request('DELETE', path, payload, timeout)
    except urllib.error.HTTPError as e:
        if e.code in (404, 405):
            return ollama_request('POST', path, payload, timeout)
        raise


def ollama_post(path, payload, timeout=120):
    rq = urllib.request.Request(OLLAMA + path, data=json.dumps(payload).encode(),
                                headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(rq, timeout=timeout) as r:
        return json.load(r)


class Handler(http.server.SimpleHTTPRequestHandler):
    admin = False

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=ROOT, **kwargs)

    # ---------- 基础设施 ----------
    def end_headers(self):
        if not self.admin:      # 管理接口绝不带通配 CORS
            self.send_header('Access-Control-Allow-Origin', '*')
            self.send_header('Access-Control-Allow-Headers', '*')
            self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
        # 静态页禁止缓存：改完刷新必定拿到最新版，避免"看着像没更新"
        if self.command in ('GET', 'HEAD') and not self.path.startswith('/__admin'):
            self.send_header('Cache-Control', 'no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def caller_ok(self):
        """同源校验：跨站请求浏览器必带 Origin，直接拒。"""
        origin = self.headers.get('Origin')
        ref = self.headers.get('Referer') or ''
        if origin is None:
            return (not ref) or ref.startswith(ORIGIN)
        return origin == ORIGIN

    def json_out(self, code, obj):
        self.admin = True
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def body_json(self):
        n = int(self.headers.get('Content-Length') or 0)
        try:
            return json.loads(self.rfile.read(n) or b'{}')
        except Exception:  # noqa: BLE001
            return {}

    # ---------- 路由 ----------
    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/__sys', '/__gpu'):
            if not self.caller_ok():
                return self.json_out(403, {'error': '仅允许同源访问'})
            return self.json_out(200, {'gpus': gpu_snapshot(),
                                       'mem': mem_snapshot(), 'cpu': cpu_snapshot()})
        if path == '/__config':
            return self.json_out(200, {'ollama': OLLAMA, 'port': PORT,
                                       'admin': ADMIN_ENABLED, 'serviceControl': SERVICE_CONTROL})
        if path.startswith('/__admin/'):
            if not ADMIN_ENABLED:
                return self.json_out(404, {'error': '管理接口已禁用（LLMCHAT_ADMIN=0）'})
            return self.admin_get(path)
        return super().do_GET()

    def do_POST(self):
        path = self.path.split('?')[0]
        if path == '/__proxy':
            return self.proxy()
        if path.startswith('/__admin/'):
            if not ADMIN_ENABLED:
                return self.json_out(404, {'error': '管理接口已禁用（LLMCHAT_ADMIN=0）'})
            return self.admin_post(path)
        self.send_error(404)

    # ---------- 管理：GET ----------
    def admin_get(self, path):
        if not self.caller_ok():
            return self.json_out(403, {'error': '仅允许同源访问'})
        if path == '/__admin/token':
            return self.json_out(200, {'token': TOKEN})
        if path == '/__admin/params':
            return self.json_out(200, read_params())
        if path != '/__admin/state':
            return self.json_out(404, {'error': 'unknown'})
        ver = ollama_get('/api/version')
        ps = ollama_get('/api/ps')
        tags = ollama_get('/api/tags')
        rc, gpu_out, _ = run([ 'nvidia-smi',
            '--query-gpu=index,name,memory.used,memory.total,utilization.gpu,temperature.gpu',
            '--format=csv,noheader,nounits'])
        gpus = []
        if rc == 0:
            for line in gpu_out.splitlines():
                p = [x.strip() for x in line.split(',')]
                if len(p) >= 6:
                    gpus.append({'index': p[0], 'name': p[1], 'used': int(p[2]),
                                 'total': int(p[3]), 'util': p[4], 'temp': p[5]})
        rc2, svc_out, _ = run([SYSTEMCTL, 'is-active', OLLAMA_UNIT])
        rc3, logs, _ = run([ 'journalctl', '-u', OLLAMA_UNIT, '-n', '40', '--no-pager',
                             '--output=short-iso'], timeout=15)
        return self.json_out(200, {
            'version': ver.get('version', '?'),
            'service': svc_out or 'unknown',
            'loaded': ps.get('models', []) if 'models' in ps else [],
            'library': tags.get('models', []) if 'models' in tags else [],
            'gpus': gpus,
            'logs': logs.splitlines() if rc3 == 0 else [],
            'params': read_params(),
            'models': models_info(),
            'ollama_error': ps.get('error') or tags.get('error') or None,
        })

    # ---------- 管理：POST ----------
    def admin_post(self, path):
        if not self.caller_ok():
            return self.json_out(403, {'error': '仅允许同源访问'})
        if self.headers.get('X-Admin-Token') != TOKEN:
            return self.json_out(403, {'error': '令牌无效（先 GET /__admin/token）'})
        data = self.body_json()
        act = path.rsplit('/', 1)[-1]
        try:
            if act == 'pull':
                return self.admin_pull(str(data.get('name') or '').strip())
            if act == 'delete':
                name = str(data.get('name') or '').strip()
                if not name:
                    return self.json_out(400, {'error': '缺少 name'})
                ollama_delete('/api/delete', {'model': name})
                return self.json_out(200, {'ok': True, 'deleted': name})
            if act == 'params':
                name = str(data.get('name') or '').strip()
                if not name:
                    return self.json_out(400, {'error': '缺少 name'})
                if data.get('remove'):
                    allp = read_params()
                    allp.pop(name, None)
                    write_params(allp)
                    return self.json_out(200, {'ok': True, 'removed': name})
                entry = {'options': clean_options(data.get('options'))}
                ka = data.get('keep_alive')
                if ka not in (None, ''):
                    entry['keep_alive'] = norm_keep(ka)
                allp = read_params()
                allp[name] = entry
                write_params(allp)
                return self.json_out(200, {'ok': True, 'name': name, 'params': entry})
            if act in ('load', 'unload'):
                name = str(data.get('name') or '').strip()
                if not name:
                    return self.json_out(400, {'error': '缺少 name'})
                saved = read_params().get(name) or {}
                opts = clean_options(saved.get('options')) if act == 'load' else {}
                if act == 'load' and isinstance(data.get('options'), dict):
                    opts.update(clean_options(data['options']))
                keep = 0
                if act == 'load':
                    keep = norm_keep(data.get('keep_alive', saved.get('keep_alive', -1)))
                payload = {'model': name, 'prompt': '', 'keep_alive': keep, 'stream': False}
                if opts:
                    payload['options'] = opts
                if act == 'load':
                    # 已加载的模型不会因新参数自动重载，先卸载再拉起
                    try:
                        cur = ollama_get('/api/ps').get('models', [])
                        if any((m.get('name') or m.get('model') or '') == name for m in cur):
                            ollama_post('/api/generate',
                                        {'model': name, 'prompt': '', 'keep_alive': 0,
                                         'stream': False}, timeout=120)
                            time.sleep(1.5)
                    except Exception:  # noqa: BLE001
                        pass
                ollama_post('/api/generate', payload, timeout=600)
                return self.json_out(200, {'ok': True, 'model': name, 'loaded': act == 'load',
                                           'options': opts, 'keep_alive': keep})
            if act == 'service':
                what = str(data.get('action') or '').strip()
                if what not in ('start', 'stop', 'restart'):
                    return self.json_out(400, {'error': 'action 只能是 start/stop/restart'})
                if not SERVICE_CONTROL:
                    return self.json_out(400, {'error': '服务控制已禁用（LLMCHAT_SERVICE_CONTROL=0）'})
                rc, out, err = run(['sudo', '-n', SYSTEMCTL, what, OLLAMA_UNIT], timeout=60)
                rc2, state, _ = run([SYSTEMCTL, 'is-active', OLLAMA_UNIT])
                return self.json_out(200 if rc == 0 else 500,
                                     {'ok': rc == 0, 'action': what, 'state': state,
                                      'out': out, 'err': err})
        except urllib.error.HTTPError as e:
            return self.json_out(e.code, {'error': e.read().decode('utf-8', 'replace')[:400]})
        except Exception as e:  # noqa: BLE001
            return self.json_out(500, {'error': str(e)})
        return self.json_out(404, {'error': 'unknown'})

    def admin_pull(self, name):
        if not name:
            return self.json_out(400, {'error': '缺少 name'})
        payload = json.dumps({'model': name, 'stream': True}).encode()
        rq = urllib.request.Request(OLLAMA + '/api/pull', data=payload,
                                    headers={'Content-Type': 'application/json'}, method='POST')
        self.admin = True
        self.send_response(200)
        self.send_header('Content-Type', 'application/x-ndjson; charset=utf-8')
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        try:
            with urllib.request.urlopen(rq, timeout=3600) as resp:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except Exception as e:  # noqa: BLE001
            try:
                self.wfile.write((json.dumps({'error': str(e)}) + '\n').encode())
                self.wfile.flush()
            except Exception:  # noqa: BLE001
                pass

    # ---------- 反代（原逻辑） ----------
    def proxy(self):
        try:
            n = int(self.headers.get('Content-Length') or 0)
            req = json.loads(self.rfile.read(n) or b'{}')
            url = req.get('url') or ''
            headers = req.get('headers') or {}
            method = (req.get('method') or 'POST').upper()
            body = (req.get('body') or '').encode('utf-8') if method != 'GET' else None
            if not url.startswith(('http://', 'https://')):
                raise ValueError('仅支持 http(s) 地址')
            host = (urllib.parse.urlparse(url).hostname or '').lower()
            if PROXY_ALLOW and host not in PROXY_ALLOW:
                return self.json_out(403, {'error': f'目标主机 {host} 不在 LLMCHAT_PROXY_ALLOW 白名单内'})
            rq = urllib.request.Request(url, data=body, headers=headers, method=method)
            with urllib.request.urlopen(rq, timeout=900) as resp:
                ctype = resp.headers.get('Content-Type', 'text/event-stream')
                self.send_response(resp.status)
                self.send_header('Content-Type', ctype)
                self.send_header('Cache-Control', 'no-cache')
                self.send_header('X-Accel-Buffering', 'no')
                self.end_headers()
                while True:
                    chunk = resp.read(2048)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except urllib.error.HTTPError as e:
            data = e.read()
            try:
                self.send_response(e.code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.end_headers()
                self.wfile.write(data)
            except Exception:  # noqa: BLE001
                pass
        except Exception as e:  # noqa: BLE001
            try:
                self.send_response(502)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.end_headers()
                self.wfile.write(('[代理失败] ' + str(e)).encode('utf-8'))
            except Exception:  # noqa: BLE001
                pass

    def log_message(self, *args):
        pass


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == '__main__':
    with Server((HOST, PORT), Handler) as srv:
        print(f'llmchat serving {ROOT} + proxy on {HOST}:{PORT}', flush=True)
        srv.serve_forever()
