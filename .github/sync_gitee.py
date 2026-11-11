# -*- coding: utf-8 -*-
"""把 GitHub 仓库 xp271/darkforge_update 的更新包和 latest.json 转发到 Gitee（用户 2026-11-11「GitHub 自动转发」）。
在 GitHub Actions 里跑（.github/workflows/sync-gitee.yml：latest.json 一改就跑，也可以在 Actions 页手动跑），
因为从开发者那边直接往 Gitee 传更新包太慢（几 KB/秒，传到一半就被断开），GitHub 的服务器连 Gitee 快得多。
本文件的原件在整合包开发目录 dev\\tools\\gitee_relay\\，用 publish_update.py --install-relay 装到 GitHub 仓库。

做法（只用 Python 标准库）：
  1. 读仓库里的 latest.json（GitHub 版，下载地址是 GitHub 的）；
  2. 每个更新包：Gitee 上同名发行版里有同名、同大小的附件就算有了；没有就从 GitHub 下载（核对 SHA-256）→ 传到 Gitee →
     不登录从 Gitee 下载核对；
  3. Gitee 的 latest.json = 同一份清单，下载地址换成 Gitee 的；最新版本的更新包有一个没传成就不改（玩家那边看不到半成品）。
令牌：环境变量 GITEE_TOKEN（GitHub 仓库 Settings → Secrets and variables → Actions 里存）。
  python sync_gitee.py [--dry]      --dry 只看要做什么"""
import base64
import hashlib
import io
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

OWNER, REPO = 'xp271', 'darkforge_update'
API = 'https://gitee.com/api/v5/repos/%s/%s' % (OWNER, REPO)
WEB = 'https://gitee.com/%s/%s' % (OWNER, REPO)
BRANCH = 'master'
MANIFEST = 'latest.json'
UA = 'Darkforge-Relay/1'
TOKEN = os.environ.get('GITEE_TOKEN', '').strip()


def say(*a):
    print(time.strftime('[%H:%M:%S]'), *a, flush=True)


def mask(s):
    return str(s).replace(TOKEN, '***') if TOKEN else str(s)


class HttpError(Exception):
    def __init__(self, code, text):
        super().__init__('HTTP %s：%s' % (code, mask(text)[:300]))
        self.code = code


def request(method, url, data=None, headers=None, timeout=120, tries=5):
    h = {'User-Agent': UA}
    h.update(headers or {})
    last = None
    for k in range(tries):
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method=method), timeout=timeout) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            if e.code < 500:
                raise HttpError(e.code, e.read().decode('utf-8', 'replace'))
            last = HttpError(e.code, e.read().decode('utf-8', 'replace'))
        except (urllib.error.URLError, OSError) as e:
            last = e
            if method == 'POST':                 # 建发行版、传附件：不盲目重试（免得重复），由上层核对后再传
                break
        time.sleep(3 + 3 * k)
    raise RuntimeError('连不上（%s %s）：%s' % (method, url.split('?')[0], mask(last)))


def multipart(fields, files):
    b = '----DarkforgeRelay' + os.urandom(12).hex()
    out = io.BytesIO()
    for k, v in fields.items():
        out.write(('--%s\r\nContent-Disposition: form-data; name="%s"\r\n\r\n' % (b, k)).encode('utf-8'))
        out.write(str(v).encode('utf-8') + b'\r\n')
    for k, (name, data) in files.items():
        out.write(('--%s\r\nContent-Disposition: form-data; name="%s"; filename="%s"\r\nContent-Type: application/zip\r\n\r\n'
                   % (b, k, name)).encode('utf-8'))
        out.write(data + b'\r\n')
    out.write(('--%s--\r\n' % b).encode('utf-8'))
    return out.getvalue(), 'multipart/form-data; boundary=' + b


def gitee(method, parts, fields=None, query=None, files=None, timeout=120):
    q, f = dict(query or {}), dict(fields or {})
    (q if method in ('GET', 'DELETE') else f)['access_token'] = TOKEN
    url = API + '/' + '/'.join(urllib.parse.quote(str(p), safe='') for p in parts) + '?' * bool(q) + urllib.parse.urlencode(q)
    h, data = {'Accept': 'application/json'}, None
    if files:
        data, h['Content-Type'] = multipart(f, files)
    elif method in ('POST', 'PUT'):
        data = urllib.parse.urlencode(f).encode('utf-8')
        h['Content-Type'] = 'application/x-www-form-urlencoded;charset=UTF-8'
    st, body = request(method, url, data, h, timeout)
    return json.loads(body.decode('utf-8')) if body.strip() else None


def gitee404(*a, **k):
    try:
        return gitee(*a, **k)
    except HttpError as e:
        if e.code == 404:
            return None
        raise


def get_release(tag):
    j = gitee404('GET', ['releases', 'tags', tag])
    return j if isinstance(j, dict) and j.get('id') else None


def attachments(rid):
    return gitee404('GET', ['releases', rid, 'attach_files'], query={'per_page': 100}) or []


def download(url, timeout=300):
    st, body = request('GET', url, None, {'Accept': '*/*'}, timeout)
    return body


def tag_of(u):
    """下载地址 …/releases/download/<标签>/<文件> 里的标签；没有就是 v<to>"""
    p = urllib.parse.urlsplit(str(u.get('url') or '')).path.split('/')
    if 'download' in p and p.index('download') + 1 < len(p):
        return urllib.parse.unquote(p[p.index('download') + 1])
    return 'v' + str(u['to'])


def sync_one(u, dry):
    """把一个更新包弄到 Gitee 上，返回 Gitee 的下载地址；不成功抛异常"""
    tag, name, size, sha = tag_of(u), u['file'], int(u['size']), u['sha256']
    gurl = '%s/releases/download/%s/%s' % (WEB, urllib.parse.quote(tag), urllib.parse.quote(name))
    rel = get_release(tag)
    if rel:
        have = {x.get('name'): x for x in attachments(rel['id'])}
        x = have.get(name)
        if x and int(x.get('size') or 0) == size:
            say('  %s / %s：Gitee 上已经有了' % (tag, name))
            return gurl
    say('  %s / %s：%s' % (tag, name, '要传（%.1f MB）' % (size / 1048576)))
    if dry:
        return gurl
    data = download(u['url'])
    if len(data) != size or hashlib.sha256(data).hexdigest() != sha:
        raise RuntimeError('从 GitHub 下到的 %s 和清单对不上（%d 字节）' % (name, len(data)))
    if not rel:
        rel = gitee('POST', ['releases'], {'tag_name': tag, 'name': '暗黑重铸 ' + tag, 'body': str(u.get('notes') or tag)[:6000] or tag,
                                           'prerelease': 'false', 'target_commitish': BRANCH})
        say('    建了发行版 %s' % tag)
    rid = rel['id']
    for k in range(4):
        for x in attachments(rid):                       # 以前传了一半的、大小不对的：删掉
            if x.get('name') == name and int(x.get('size') or 0) != size:
                gitee('DELETE', ['releases', rid, 'attach_files', x['id']])
        try:
            gitee('POST', ['releases', rid, 'attach_files'], files={'file': (name, data)}, timeout=900)
            break
        except RuntimeError as e:
            if any(x.get('name') == name and int(x.get('size') or 0) == size for x in attachments(rid)):
                break                                    # 断开了但服务器收全了
            if k == 3:
                raise
            say('    上传中断（%s），重传' % e)
    say('    传好了，不登录下载核对……')
    got = download(gurl)
    if hashlib.sha256(got).hexdigest() != sha:
        raise RuntimeError('从 Gitee 下到的 %s 不对（%d 字节）' % (name, len(got)))
    say('    核对一致')
    return gurl


def main():
    dry = '--dry' in sys.argv
    if not TOKEN:
        raise SystemExit('没有 GITEE_TOKEN：在 GitHub 仓库 Settings → Secrets and variables → Actions 里加一个（Gitee 私人令牌，勾 projects）')
    m = json.load(open(MANIFEST, encoding='utf-8-sig'))
    if m.get('name') != 'Darkforge':
        raise SystemExit('latest.json 不认识')
    latest = str(m.get('latest'))
    say('GitHub 清单：v%s，%d 个更新包' % (latest, len(m.get('updates') or [])))
    ups, failed = [], []
    for u in m.get('updates') or []:
        try:
            ups.append(dict(u, url=sync_one(u, dry)))
        except Exception as e:
            say('  [错误] %s → %s：%s' % (u.get('from'), u.get('to'), mask(e)))
            failed.append(u)
    if any(str(u.get('to')) == latest for u in failed):
        say('[错误] 最新版本 v%s 的更新包没传成，Gitee 的 latest.json 先不改' % latest)
        return 1
    new = dict(m, updates=ups)
    body = (json.dumps(new, ensure_ascii=False, indent=1) + '\n').encode('utf-8')
    j = gitee404('GET', ['contents', MANIFEST], query={'ref': BRANCH})
    old, osha = (base64.b64decode(j['content']), j['sha']) if isinstance(j, dict) and 'content' in j else (None, None)
    if old == body:
        say('Gitee 的 latest.json 已经是这一份')
    elif dry:
        say('（--dry）要把 Gitee 的 latest.json 改成 v%s' % latest)
    else:
        f = {'content': base64.b64encode(body).decode('ascii'), 'message': '同步 v%s（GitHub Actions）' % latest, 'branch': BRANCH}
        if osha:
            f['sha'] = osha
            gitee('PUT', ['contents', MANIFEST], f)
        else:
            gitee('POST', ['contents', MANIFEST], f)
        say('已更新 Gitee 的 latest.json → v%s' % latest)
    if failed:
        say('有 %d 个旧的更新包没传成（清单里先不放它们）' % len(failed))
    return 0


if __name__ == '__main__':
    sys.exit(main())
