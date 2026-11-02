"""
网络照片「自动爬取 + 视频抽帧 + 精准审核」模块 v2.2（与 server.py 配合，不改动原有审核逻辑）

第一性原则：吞吐 = 抓取速度 × GPU 利用率。所以抓取与审核做成【流水线】：
  抓到一张 → 立刻去重 → 立刻丢给 GPU 池审核（两张 T4 同时抢活）→ 前端实时看到结果。

v2.2 重点修复：旧版抓到的几乎全是「需要登录」的图（登录二维码 / 登录弹窗截图 / 头像 / 图标）。现在：
  A) 【视频优先】默认模式 video：找到视频流（公开解析器 / yt-dlp / 网络抓包 / <video>）→ ffmpeg 抽帧 → 每帧送 GPU 审核（videoframes.py）
  B) 【登录墙过滤】三层：界面类图片地址不要 → 页面被登录/验证码墙遮住时不截图（先尝试像用户一样关掉弹窗）
     → 审核完成后再按「OCR 文字 + 大模型描述 + 二维码」识别登录墙截图，标记为 wall 并排除出统计
  C) 【Cookie 多格式】请求头 a=1;b=2 / Cookie-Editor JSON / Netscape cookies.txt，只用于使用者自己的账号，仅内存使用

抓取三条腿（互相补充，任何一条失败都只会降级）：
  1) 网络响应拦截：浏览器加载页面时，直接截获所有 image/* 响应的字节（不用二次下载，天然绕开防盗链/签名过期）
  2) DOM 扫描：img / srcset / data-src / 背景图 / video poster / og:image，补抓拦截不到的懒加载图
  3) 页面截图：按滚动步长逐屏截图（图片 + 文字 + 评论一起进入审核，OCR 能读出截图里的敏感词）
可选「深入作品详情」：从列表页收集作品链接，逐个打开再抓（小红书/抖音/微博/B站 有链接规则预设）。
没有安装 Playwright/Chromium 时自动降级为 HTTP 模式（仅能抓静态页面里的图片地址）。

合规边界（刻意不做的事）：不破解验证码、不绕过登录、不做指纹伪装。
遇到登录/验证码墙只会提示；需要登录态时由使用者提供【自己账号】的 Cookie（仅内存使用，不落盘）。
请求之间有间隔（默认 0.8 秒），并带 SSRF 防护（默认禁止访问内网/本机地址）。
请仅用于内容审核/自有或已获授权的内容，并遵守目标平台的服务条款与当地法律。
"""
from __future__ import annotations

import asyncio
import hashlib
import importlib.util
import io
import ipaddress
import json
import logging
import os
import re
import shutil
import socket
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple

from PIL import Image, ImageOps

try:  # v2.2 视频抽帧模块：缺失只会让「视频抽帧」不可用，图片/截图爬取照常
    import videoframes
except Exception as _ve:  # noqa: BLE001
    videoframes = None  # type: ignore
    print("videoframes 模块加载失败（视频抽帧不可用）:", _ve)

log = logging.getLogger("crawler")

CRAWL_DIR = Path(os.getenv("CRAWL_DIR", "/tmp/crawl_jobs"))
ALLOW_PRIVATE = os.getenv("CRAWL_ALLOW_PRIVATE") == "1"  # 仅本机测试用
MAX_JOBS_KEPT = 6
HARD_MAX_IMAGES = 600
MIN_BYTES = 2500  # 小于 2.5KB 的基本是图标/像素点
UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/16.6 Mobile/15E148 Safari/604.1")

# 平台预设：搜索地址 + 域名 + 作品链接规则（用于「深入作品详情」）
PLATFORMS: Dict[str, Dict[str, Any]] = {
    "douyin": {"name": "抖音", "search": "https://www.douyin.com/search/{kw}", "hosts": ["douyin.com", "iesdouyin.com"],
               "links": r"/(video|note)/\d+"},
    "xiaohongshu": {"name": "小红书", "search": "https://www.xiaohongshu.com/search_result?keyword={kw}&source=web_search_result_notes",
                    "hosts": ["xiaohongshu.com", "xhslink.com"], "links": r"/(explore|discovery/item|search_result|user/profile/[0-9a-f]+)/[0-9a-f]{16,}"},
    "weibo": {"name": "微博", "search": "https://s.weibo.com/weibo?q={kw}", "hosts": ["weibo.com", "weibo.cn"],
              "links": r"/(detail|status)/\w+|weibo\.com/\d+/\w{6,}"},
    "bilibili": {"name": "哔哩哔哩", "search": "https://search.bilibili.com/all?keyword={kw}", "hosts": ["bilibili.com", "b23.tv"],
                 "links": r"/(video/BV\w+|opus/\d+|read/cv\d+)"},
    "baidu": {"name": "百度图片", "search": "https://image.baidu.com/search/index?tn=baiduimage&word={kw}", "hosts": ["baidu.com"], "links": ""},
    "bing": {"name": "必应图片", "search": "https://www.bing.com/images/search?q={kw}", "hosts": ["bing.com"], "links": ""},
    "custom": {"name": "自定义网址", "search": "", "hosts": [], "links": r"."},
}

AnalyzeCb = Callable[[bytes, str, Optional[str]], Awaitable[Dict[str, Any]]]
_analyze_cb: Optional[AnalyzeCb] = None
_conc_fn: Callable[[], int] = lambda: 4
JOBS: "Dict[str, Job]" = {}


def configure(analyze_cb: AnalyzeCb, conc_fn: Callable[[], int]) -> None:
    """由 server.py 注入：审核函数（走 GPU 池）和并发数函数（按已就绪 GPU 副本数动态计算）"""
    global _analyze_cb, _conc_fn
    _analyze_cb = analyze_cb
    _conc_fn = conc_fn


# --------------------------------------------------------------------------
# 工具函数
# --------------------------------------------------------------------------
URL_RE = re.compile(r"https?://[^\s<>\"'，。；、）)】》]+", re.I)


def extract_urls(text: str) -> List[str]:
    """从任意文本里提取网址（可直接粘贴「复制打开抖音…」「小红书分享文案」等整段分享文字）"""
    out: List[str] = []
    for m in URL_RE.findall(text or ""):
        u = m.rstrip(".,;!?")
        if u not in out:
            out.append(u)
    return out


def detect_platform(url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    for key, p in PLATFORMS.items():
        if any(host == h or host.endswith("." + h) for h in p["hosts"]):
            return key
    return "custom"


def _registrable(host: str) -> str:
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in {"com", "net", "org", "gov", "edu", "co"} and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def parse_cookies(raw: str, url: str) -> List[Dict[str, Any]]:
    """支持：请求头 a=1; b=2 / Cookie-Editor JSON / Netscape cookies.txt（v2.2）。没写域名的 Cookie 默认挂到该网址的主域名上"""
    host = urllib.parse.urlparse(url).hostname or ""
    dom = "." + _registrable(host) if "." in host else host
    if videoframes:
        out = videoframes.parse_cookie_text(raw, dom)
    else:
        out = []
        for part in (raw or "").split(";"):
            if "=" in part:
                k, v = part.split("=", 1)
                if k.strip():
                    out.append({"name": k.strip(), "value": v.strip(), "domain": dom, "path": "/"})
    return [c for c in out if c.get("domain")]


def _host_is_public(host: str) -> bool:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_multicast or ip.is_unspecified:
            return False
    return True


async def check_url(url: str) -> None:
    """SSRF 防护：只允许 http/https，且默认不允许解析到内网/本机地址"""
    p = urllib.parse.urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ValueError("只支持 http/https 网址：%s" % url[:80])
    if ALLOW_PRIVATE:
        return
    ok = await asyncio.to_thread(_host_is_public, p.hostname)
    if not ok:
        raise ValueError("出于安全考虑，禁止访问内网/本机地址：%s" % p.hostname)


def capabilities() -> Dict[str, Any]:
    pw = importlib.util.find_spec("playwright") is not None
    base = Path(os.getenv("PLAYWRIGHT_BROWSERS_PATH") or (Path.home() / ".cache" / "ms-playwright"))
    chromium = pw and base.exists() and any(base.glob("chromium*"))
    vf = videoframes.capabilities() if videoframes else {"ffmpeg": False, "ytdlp": False, "cv2": False}
    chrome = bool(shutil.which("google-chrome") or shutil.which("google-chrome-stable") or Path("/opt/google/chrome/chrome").exists())
    return {"playwright": pw, "chromium": bool(chromium), "engine": "playwright" if chromium else "http",
            "chrome": chrome, "video": bool(videoframes) and (vf["ffmpeg"] or vf["cv2"]), "ffmpeg": vf["ffmpeg"], "ytdlp": vf["ytdlp"],
            "platforms": {k: v["name"] for k, v in PLATFORMS.items()}}


def _popcount(x: int) -> int:
    return bin(x).count("1")


def _dhash(img: Image.Image) -> int:
    g = img.convert("L").resize((9, 8), Image.BILINEAR)
    px = list(g.getdata())
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (1 if px[r * 9 + c] > px[r * 9 + c + 1] else 0)
    return bits


def _prepare(raw: bytes, kind: str, min_size: int) -> Optional[Tuple[int, int, bytes, bytes, int]]:
    """解码 → 过滤小图/极端长宽比 → 统一成 ≤1280px 的 JPEG（审核用）+ 320px 缩略图 + 感知哈希"""
    try:
        img = Image.open(io.BytesIO(raw))
        w, h = img.size
        if kind in ("image", "cover"):
            if min(w, h) < min_size or w * h < min_size * min_size:
                return None
            if max(w, h) / max(1, min(w, h)) > 8:
                return None
        img.load()
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            img = img.convert("RGBA")
            bg = Image.new("RGB", img.size, (255, 255, 255))
            bg.paste(img, mask=img.split()[-1])
            img = bg
        else:
            img = img.convert("RGB")
        dh = _dhash(img)
        full = img.copy()
        full.thumbnail((1280, 1280))
        b1 = io.BytesIO()
        full.save(b1, "JPEG", quality=92)
        th = img.copy()
        th.thumbnail((320, 320))
        b2 = io.BytesIO()
        th.save(b2, "JPEG", quality=78)
        return w, h, b1.getvalue(), b2.getvalue(), dh
    except Exception:  # noqa: BLE001
        return None


# --------------------------------------------------------------------------
# Job
# --------------------------------------------------------------------------
class Job:
    def __init__(self, params: Dict[str, Any]) -> None:
        self.id = uuid.uuid4().hex[:10]
        self.p = params
        self.state = "queued"  # queued | running | done | stopped | error
        self.error: Optional[str] = None
        self.engine = ""
        self.items: List[Dict[str, Any]] = []
        self.logs: List[str] = []
        self.rev = 0
        self.created = time.time()
        self.finished: Optional[float] = None
        self.stop = asyncio.Event()
        self.conc = max(2, int(_conc_fn()))
        self.sem = asyncio.Semaphore(self.conc)
        self.tasks: "set[asyncio.Task]" = set()
        self.seen_url: "set[str]" = set()
        self.seen_hash: "set[str]" = set()
        self.dhashes: List[int] = []
        self.lock = asyncio.Lock()
        self.dir = CRAWL_DIR / self.id
        self.dir.mkdir(parents=True, exist_ok=True)
        self.stats: Dict[str, int] = {"pages": 0, "found": 0, "screenshots": 0, "analyzed": 0, "errors": 0,
                                      "dup": 0, "small": 0, "pass": 0, "review": 0, "block": 0,
                                      # v2.2：视频帧 / 视频数 / 抽帧失败 / 被识别为登录墙而排除 / 遮住的页面 / 被跳过的截图 / 界面图
                                      "frames": 0, "videos": 0, "video_fail": 0, "walls": 0, "wall_pages": 0, "wall_skip": 0, "ui": 0}
        self.cookies: List[Dict[str, Any]] = videoframes.parse_cookie_text(params.get("cookie", ""), "") if (videoframes and params.get("cookie")) else []
        self.vsem = asyncio.Semaphore(2)  # 同时抽帧的视频数（ffmpeg 吃 CPU，2 个刚好不拖慢 GPU 喂图）
        self.seen_video: "set[str]" = set()
        self.ytdlp_tried: "set[str]" = set()
        self.cand_total = 0
        self.video_cap_hit = False
        self.gpu_count: Dict[str, int] = {}
        self.cap_hit = False

    def say(self, msg: str) -> None:
        self.logs.append("%s %s" % (time.strftime("%H:%M:%S"), msg))
        if len(self.logs) > 300:
            del self.logs[:100]
        self.rev += 1

    def touch(self, item: Dict[str, Any]) -> None:
        self.rev += 1
        item["rev"] = self.rev

    def cookie_header(self, url: str) -> str:
        if not self.cookies or not videoframes:
            return ""
        return videoframes.cookie_header_for(self.cookies, url)

    @property
    def pending(self) -> int:
        return sum(1 for t in self.tasks if not t.done())


async def _analyze_item(job: Job, item: Dict[str, Any]) -> None:
    assert _analyze_cb is not None
    async with job.sem:
        if job.stop.is_set() and job.p.get("cancel_on_stop", True):
            item["status"] = "skipped"
            job.touch(item)
            return
        item["status"] = "running"
        job.touch(item)
        t0 = time.time()
        try:
            raw = await asyncio.to_thread((job.dir / ("%d.jpg" % item["idx"])).read_bytes)
            res = await _analyze_cb(raw, job.p["analyze_mode"], item["name"])
            item["res"] = res
            item["status"] = "done"
            item["ms"] = int((time.time() - t0) * 1000)
            job.stats["analyzed"] += 1
            if looks_like_wall(res, str(item.get("kind"))):
                # 登录/验证码墙截图：不是内容，不计入 拦截/复核/通过（前端单独放在「登录墙」页签，Excel 不导出）
                item["wall"] = True
                job.stats["walls"] += 1
            else:
                v = res.get("verdict")
                if v in ("pass", "review", "block"):
                    job.stats[v] += 1
            g = str(res.get("gpu") or "?")
            job.gpu_count[g] = job.gpu_count.get(g, 0) + 1
        except Exception as e:  # noqa: BLE001
            item["status"] = "error"
            item["err"] = str(getattr(e, "detail", None) or e)[:200]
            job.stats["errors"] += 1
        job.touch(item)


def _name_from_url(u: str) -> str:
    try:
        n = Path(urllib.parse.unquote(urllib.parse.urlparse(u).path)).name
        n = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", n)[:60]
        return n if "." in n else (n + ".jpg" if n else "")
    except Exception:  # noqa: BLE001
        return ""


async def ingest(job: Job, raw: bytes, kind: str, src: str, page_url: str, extra: Optional[Dict[str, Any]] = None) -> bool:
    """一张候选图片进入流水线：过滤 → 去重 → 落盘 → 异步审核。返回是否被接收"""
    if job.stop.is_set() or len(job.items) >= job.p["max_images"]:
        job.cap_hit = len(job.items) >= job.p["max_images"]
        return False
    if len(raw) < (MIN_BYTES if kind == "image" else 1000):
        return False
    sha = hashlib.sha1(raw).hexdigest()
    if sha in job.seen_hash:
        job.stats["dup"] += 1
        return False
    job.seen_hash.add(sha)
    # 背压：待审核太多时让抓取等一等，保证两张 GPU 持续有活、内存不爆
    while job.pending >= job.conc * 3 and not job.stop.is_set():
        await asyncio.sleep(0.15)
    prep = await asyncio.to_thread(_prepare, raw, kind, job.p["min_size"])
    if prep is None:
        job.stats["small"] += 1
        return False
    w, h, full, thumb, dh = prep
    async with job.lock:
        if len(job.items) >= job.p["max_images"]:
            job.cap_hit = True
            return False
        # 截图只做 SHA1 精确去重（文字密集页面滚动前后的低分辨率哈希很接近，用近似去重会漏检）；图片/封面/视频帧用感知哈希去近似重复
        if kind in ("image", "cover", "frame"):
            if any(_popcount(dh ^ o) <= 2 for o in job.dhashes):
                job.stats["dup"] += 1
                return False
            job.dhashes.append(dh)
        idx = len(job.items)
        item: Dict[str, Any] = {
            "idx": idx, "kind": kind, "src": src[:600], "page": page_url[:600], "w": w, "h": h, "bytes": len(raw),
            "name": (extra or {}).get("name") or (("截图-%03d.jpg" % idx) if kind == "screenshot" else (_name_from_url(src) or "img-%03d.jpg" % idx)),
            "status": "queued", "t": round(time.time() - job.created, 1),
        }
        for k2, v2 in (extra or {}).items():
            if k2 != "name":
                item[k2] = v2
        # 先落盘再公开：前端一旦看到这一项，缩略图一定已经可以读取
        await asyncio.to_thread((job.dir / ("%d.jpg" % idx)).write_bytes, full)
        await asyncio.to_thread((job.dir / ("%d_t.jpg" % idx)).write_bytes, thumb)
        job.items.append(item)
        job.stats["frames" if kind == "frame" else "screenshots" if kind == "screenshot" else "found"] += 1
        job.touch(item)
    t = asyncio.create_task(_analyze_item(job, item))
    job.tasks.add(t)
    t.add_done_callback(job.tasks.discard)
    return True


# --------------------------------------------------------------------------
# 抓取公共脚本（JS）
# --------------------------------------------------------------------------
WALL_RE = re.compile(r"验证码|安全验证|滑动验证|人机验证|captcha|are you a robot|verify you are human|请先登录|扫码登录", re.I)

JS_COLLECT = """() => {
  const out = new Set();
  const add = (u) => { if (!u || /^(data|blob|javascript):/i.test(u)) return; try { out.add(new URL(u, location.href).href); } catch (e) {} };
  document.querySelectorAll('img').forEach(i => {
    add(i.currentSrc || i.src); add(i.getAttribute('data-src')); add(i.getAttribute('data-original')); add(i.getAttribute('data-lazy-src'));
    (i.srcset || '').split(',').forEach(s => add(s.trim().split(/\\s+/)[0]));
  });
  document.querySelectorAll('source[srcset]').forEach(s => (s.srcset || '').split(',').forEach(x => add(x.trim().split(/\\s+/)[0])));
  document.querySelectorAll('video[poster]').forEach(v => add(v.poster));
  document.querySelectorAll('[style*="background-image"]').forEach(e => { const m = /url\\(["']?(.*?)["']?\\)/.exec(e.style.backgroundImage || ''); if (m) add(m[1]); });
  document.querySelectorAll('meta[property="og:image"],meta[name="twitter:image"]').forEach(m => add(m.content));
  return Array.from(out);
}"""

JS_LINKS = "() => Array.from(document.querySelectorAll('a[href]')).map(a => a.href)"


# --------------------------------------------------------------------------
# v2.2：登录墙 / 界面图过滤 + 视频流 → 帧
# --------------------------------------------------------------------------
# 强信号：出现这些词基本可以确定是登录/验证码墙（单独一个「登录」按钮不算，导航栏里到处都有）
STRONG_WALL_RE = re.compile(r"验证码|安全验证|滑动验证|人机验证|captcha|are you a robot|verify you are human|请先登录|扫码登录|登录后(?:查看|可见|继续|才能)|log ?in to continue", re.I)
# 界面类图片地址（二维码 / 登录 / 头像 / 图标 / 表情 …）：不是内容，直接不要
UI_URL_RE = re.compile(r"qrcode|qr_code|/qr/|login|passport|captcha|avatar|favicon|/logo|sprite|emoji|emoticon|/icons?/|badge|placeholder|default[-_]?(?:avatar|head)", re.I)
# 审核完成后的「登录墙截图」识别：OCR 文字 + 大模型描述 + 二维码，多信号累计
WALL_WORDS = ("登录", "扫码", "验证码", "手机号", "密码登录", "获取验证码", "用户协议", "隐私政策", "未注册", "自动注册", "立即登录",
              "请先登录", "安全验证", "人机验证", "滑动验证", "打开抖音", "打开小红书", "打开微博", "下载app", "下载APP")
WALL_DESC_RE = re.compile(r"登录(?:界面|页面|窗口|弹窗|框)|扫码登录|二维码登录|(?:登录|验证)(?:页|界面)|安全验证|login (?:page|screen|prompt|window)|sign[- ]?in (?:page|screen)|captcha", re.I)


def looks_like_wall(res: Dict[str, Any], kind: str) -> bool:
    """审核结果是不是「登录/验证码墙」截图。视频帧门槛更高（视频里偶尔出现的「登录」字样不能误杀）"""
    text = str(res.get("ocr_text") or "")
    score = sum(1 for w in WALL_WORDS if w in text)
    if WALL_DESC_RE.search(str(res.get("description") or "")):
        score += 2
    if (res.get("aux") or {}).get("qr"):
        score += 1
    return score >= (3 if kind == "frame" else 2)


JS_WALL = r"""() => {
  const vw = innerWidth, vh = innerHeight;
  const text = ((document.body && document.body.innerText) || '').slice(0, 1500);
  let modal = false;
  const sel = '[role="dialog"],[class*="login" i],[id*="login" i],[class*="passport" i],[class*="captcha" i],[class*="verify" i],[class*="modal" i],[class*="mask" i],[class*="popup" i]';
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width * r.height < vw * vh * 0.1) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden' || parseFloat(cs.opacity) === 0) continue;
    const t = (el.innerText || '').slice(0, 800);
    if (/登录|扫码|验证码|验证|captcha|sign in|log in/i.test(t)) { modal = true; break; }
  }
  return { text, modal };
}"""

JS_CLOSE_MODAL = r"""() => {
  const vh = innerHeight; let n = 0;
  for (const el of document.querySelectorAll('[class*="close" i],[aria-label*="关闭"],[aria-label*="close" i]')) {
    const r = el.getBoundingClientRect();
    if (r.width < 8 || r.height < 8 || r.width > 120 || r.height > 120 || r.bottom < 0 || r.top > vh) continue;
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const box = el.closest('[role="dialog"],[class*="login" i],[class*="modal" i],[class*="mask" i],[class*="popup" i],[id*="login" i]');
    if (!box || !/登录|扫码|验证码|login/i.test(box.innerText || '')) continue;
    try { el.click(); n++; } catch (e) {}
    if (n >= 2) break;
  }
  return n;
}"""

JS_VIDEOS = r"""() => {
  const out = [];
  const add = (u, p) => { if (!u || /^(blob|data|javascript):/i.test(u)) return; try { out.push({ url: new URL(u, location.href).href, poster: p || '' }); } catch (e) {} };
  document.querySelectorAll('video').forEach(v => {
    const p = v.poster || '';
    add(v.currentSrc, p); add(v.src, p);
    v.querySelectorAll('source').forEach(s => add(s.src, p));
  });
  document.querySelectorAll('meta[property="og:video"],meta[property="og:video:url"],meta[property="og:video:secure_url"]').forEach(m => add(m.content, ''));
  return out;
}"""

JS_AUTOPLAY = "() => { document.querySelectorAll('video').forEach(v => { try { v.muted = true; const p = v.play(); if (p && p.catch) p.catch(() => {}); } catch (e) {} }); }"

SHORT_HOSTS = ("v.douyin.com", "xhslink.com", "b23.tv", "m.weibo.cn", "www.iesdouyin.com", "iesdouyin.com")


def is_content_url(url: str) -> bool:
    """这个网址本身是不是「一个具体作品」（而不是搜索页/主页/列表）。作品页不需要再「深入」"""
    p = urllib.parse.urlparse(url)
    host = (p.hostname or "").lower()
    if host in SHORT_HOSTS:
        return True
    plat = detect_platform(url)
    rule = PLATFORMS[plat]["links"]
    if plat == "custom" or not rule:
        return False
    if re.search(r"search|keyword|/s\?|/user/|/u/|space\.bilibili", url, re.I) and not re.search(r"/(video|note|detail|status)/|BV\w{10}", url, re.I):
        return False
    return bool(re.search(rule, url))


def _bad_jpeg(raw: bytes) -> bool:
    try:
        img = Image.open(io.BytesIO(raw))
        img.load()
        return bool(videoframes and videoframes._bad_frame(img))  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001
        return True


async def _page_wall(page: Any) -> bool:
    """当前页面是否被登录/验证码墙遮住（只检测，不绕过）"""
    try:
        if re.search(r"/(?:login|passport|signin|sso)(?:[/?#.]|$)", urllib.parse.urlparse(page.url).path, re.I):
            return True
        r = await page.evaluate(JS_WALL)
    except Exception:  # noqa: BLE001
        return False
    return bool(r.get("modal")) or bool(STRONG_WALL_RE.search(r.get("text") or ""))


async def _clear_wall(job: Job, page: Any) -> bool:
    """被遮住时先尝试关闭弹窗（Esc / 点关闭按钮，等同于用户手动点 ✕）；仍然遮住返回 True。不会填账号、不会破解验证码"""
    if not await _page_wall(page):
        return False
    try:
        await page.keyboard.press("Escape")
        await page.wait_for_timeout(500)
        await page.evaluate(JS_CLOSE_MODAL)
        await page.wait_for_timeout(700)
    except Exception:  # noqa: BLE001
        pass
    if not await _page_wall(page):
        job.say("已关闭页面弹窗，继续抓取")
        return False
    return True


class VideoCandidates:
    """一个页面里发现的视频流地址（网络抓包 + JSON 挖掘 + DOM）"""

    def __init__(self, job: Job) -> None:
        self.job = job
        self.items: Dict[str, Dict[str, str]] = {}
        self.done: "set[str]" = set()

    def add(self, url: str, referer: str, cover: str = "") -> None:
        if not url or not videoframes or url.startswith(("blob:", "data:")) or len(url) > 2000:
            return
        k = videoframes.norm_key(url)
        if k in self.items:
            if cover and not self.items[k].get("cover"):
                self.items[k]["cover"] = cover
            return
        self.items[k] = {"url": url, "referer": referer, "cover": cover}
        self.job.cand_total += 1

    async def add_dom(self, page: Any) -> None:
        try:
            arr = await page.evaluate(JS_VIDEOS)
        except Exception:  # noqa: BLE001
            return
        for v in arr or []:
            self.add(v.get("url", ""), page.url, v.get("poster", ""))


async def ingest_video(job: Job, info: Dict[str, Any], page_url: str) -> int:
    """
    一个视频（已解析出流地址）→ 抽帧 → 每一帧立刻进入审核流水线（两张 T4 同时抢活）。返回入审的帧数/图数。
    info: url(视频流，可为空：图集) · headers · cover · title · images[] · via
    """
    if not videoframes or job.stop.is_set() or len(job.items) >= job.p["max_images"]:
        return 0
    got = 0
    title = str(info.get("title") or "")[:80]
    headers = dict(info.get("headers") or {})
    url = str(info.get("url") or "")
    vno = 0
    if url:
        key = videoframes.norm_key(url)
        if key in job.seen_video:
            return 0
        if job.stats["videos"] >= job.p["video_limit"]:
            job.video_cap_hit = True
            return 0
        job.seen_video.add(key)
        try:
            await check_url(url)
        except ValueError as e:
            job.say("跳过视频：%s" % str(e)[:100])
            return 0
        job.stats["videos"] += 1
        vno = job.stats["videos"]
        if "Cookie" not in headers and job.cookies:
            ck = videoframes.cookie_header_for(job.cookies, url)
            if ck:
                headers["Cookie"] = ck
        job.say("🎬 视频 #%d（%s）%s：开始抽取 %d 帧" % (vno, info.get("via") or "页面抓包", title or url[:70], job.p["frames_per_video"]))
        try:
            async with job.vsem:
                frames, note = await asyncio.wait_for(
                    asyncio.to_thread(videoframes.extract_frames, url, headers, job.p["frames_per_video"]), timeout=300)
        except Exception as e:  # noqa: BLE001
            frames, note = [], "抽帧超时/异常：%s" % str(e)[:80]
        if not frames:
            job.stats["video_fail"] += 1
            job.say("⚠️ 视频 #%d 没有取到画面：%s" % (vno, note))
        for ts, jpg in frames:
            if await ingest(job, jpg, "frame", url, page_url, {"vid": vno, "ts": ts, "title": title, "video": url[:600],
                                                                "name": "视频%02d-%05.1fs.jpg" % (vno, ts)}):
                got += 1
        if frames:
            job.say("✅ 视频 #%d：%s，送审 %d 帧" % (vno, note, got))
    cover = str(info.get("cover") or "")
    if cover.startswith("//"):
        cover = "https:" + cover
    if cover.startswith("http") and not job.stop.is_set():
        try:
            await check_url(cover)
            raw = await asyncio.to_thread(videoframes.fetch_bytes, cover, {"User-Agent": UA_DESKTOP, "Referer": page_url})
            if raw:
                await ingest(job, raw, "cover", cover, page_url, {"vid": vno, "title": title, "name": "视频%02d-封面.jpg" % vno if vno else "封面.jpg"})
        except Exception:  # noqa: BLE001
            pass
    for i, iu in enumerate((info.get("images") or [])[:9]):  # 图集作品：每张图就是内容
        if job.stop.is_set():
            break
        try:
            await check_url(iu)
            raw = await asyncio.to_thread(videoframes.fetch_bytes, iu, {"User-Agent": UA_DESKTOP, "Referer": page_url})
            if raw and await ingest(job, raw, "image", iu, page_url, {"title": title}):
                got += 1
        except Exception:  # noqa: BLE001
            pass
    return got


async def video_direct(job: Job, url: str) -> int:
    """作品链接 → 公开解析器（抖音/B站）→ yt-dlp → 抽帧入审。不需要打开浏览器页面，速度最快。失败返回 0"""
    if not videoframes or job.stop.is_set():
        return 0
    plat = detect_platform(url)
    info: Optional[Dict[str, Any]] = None
    try:
        if plat == "douyin" or urllib.parse.urlparse(url).hostname in ("v.douyin.com", "www.iesdouyin.com", "iesdouyin.com"):
            info = await asyncio.to_thread(videoframes.douyin_resolve, url)
        elif plat == "bilibili":
            info = await asyncio.to_thread(videoframes.bilibili_resolve, url)
        if not info and job.p["use_ytdlp"] and videoframes.has_ytdlp() and plat != "custom":
            job.ytdlp_tried.add(url)
            info = await asyncio.wait_for(asyncio.to_thread(videoframes.ytdlp_resolve, url, job.cookies, job.p["mobile"]), timeout=90)
    except Exception as e:  # noqa: BLE001
        job.say("解析作品失败：%s" % str(e)[:80])
        info = None
    if not info:
        return 0
    return await ingest_video(job, info, url)


async def process_candidates(job: Job, vc: VideoCandidates, page_url: str) -> int:
    """把页面里发现的视频流逐个抽帧入审（受 video_limit 限制）"""
    got = 0
    ua = UA_MOBILE if job.p["mobile"] else UA_DESKTOP
    for k, c in list(vc.items.items()):
        if k in vc.done:
            continue
        if job.stop.is_set() or len(job.items) >= job.p["max_images"] or job.stats["videos"] >= job.p["video_limit"]:
            break
        vc.done.add(k)
        ref = c.get("referer") or page_url
        pu = urllib.parse.urlparse(ref)
        headers = {"User-Agent": ua, "Referer": "%s://%s/" % (pu.scheme or "https", pu.netloc)}
        got += await ingest_video(job, {"url": c["url"], "headers": headers, "cover": c.get("cover", ""), "via": "页面抓包"}, page_url)
    return got


async def element_shots(job: Job, page: Any, n: int) -> int:
    """兜底：浏览器能解码时，直接给页面里正在播放的 <video> 截图（Chrome 通道支持 H.264；纯 Chromium 可能是黑屏，会被自动丢弃）"""
    got = 0
    try:
        handles = await page.query_selector_all("video")
    except Exception:  # noqa: BLE001
        return 0
    for h in handles[:3]:
        if job.stop.is_set() or len(job.items) >= job.p["max_images"]:
            break
        try:
            box = await h.bounding_box()
            if not box or box["width"] < 160 or box["height"] < 90:
                continue
            await h.scroll_into_view_if_needed(timeout=2500)
            await page.evaluate("(v) => { v.muted = true; if (v.play) v.play().catch(() => {}); }", h)
            if job.stats["videos"] >= job.p["video_limit"]:
                break
            job.stats["videos"] += 1
            vno = job.stats["videos"]
            job.say("🎬 视频 #%d（浏览器播放截图）：每 1.2 秒截取一帧" % vno)
            ok = 0
            for k in range(n):
                await page.wait_for_timeout(1200)
                if await _page_wall(page):
                    break
                png = await h.screenshot(type="jpeg", quality=88, timeout=6000)
                if await asyncio.to_thread(_bad_jpeg, png):
                    continue
                if await ingest(job, png, "frame", page.url, page.url, {"vid": vno, "ts": round((k + 1) * 1.2, 1), "video": page.url[:600],
                                                                         "name": "视频%02d-播放截图%d.jpg" % (vno, k + 1)}):
                    ok += 1
            got += ok
            if ok == 0:
                job.stats["video_fail"] += 1
                job.say("⚠️ 视频 #%d 播放截图是黑屏（浏览器不支持该视频编码）。运行 Cell 1 安装 Chrome 可解决" % vno)
        except Exception as e:  # noqa: BLE001
            job.say("视频截图失败：%s" % str(e)[:80])
    return got


# --------------------------------------------------------------------------
# 抓取：Playwright 模式
# --------------------------------------------------------------------------
async def _download(job: Job, ctx: Any, url: str, referer: str) -> None:
    if url in job.seen_url or job.stop.is_set() or len(job.items) >= job.p["max_images"]:
        return
    job.seen_url.add(url)
    if UI_URL_RE.search(url):
        job.stats["ui"] += 1
        return
    try:
        await check_url(url)
        r = await ctx.request.get(url, headers={"Referer": referer}, timeout=15000)
        ct = (r.headers.get("content-type") or "").lower()
        if r.status == 200 and (ct.startswith("image/") or not ct) and "svg" not in ct and "icon" not in ct:
            await ingest(job, await r.body(), "image", url, referer)
    except Exception:  # noqa: BLE001
        pass


async def _collect_dom(job: Job, page: Any, ctx: Any) -> int:
    try:
        urls: List[str] = await page.evaluate(JS_COLLECT)
    except Exception:  # noqa: BLE001
        return 0
    fresh = [u for u in urls if u not in job.seen_url]
    sem = asyncio.Semaphore(6)

    async def one(u: str) -> None:
        async with sem:
            await _download(job, ctx, u, page.url)

    await asyncio.gather(*[one(u) for u in fresh[:200]])
    return len(fresh)


async def _shot(job: Job, page: Any) -> None:
    try:
        if await _clear_wall(job, page):  # 被登录/验证码墙遮住：不截（截出来只会是登录框）
            job.stats["wall_skip"] += 1
            return
        png = await page.screenshot(type="jpeg", quality=88, full_page=False)
        await ingest(job, png, "screenshot", page.url, page.url)
    except Exception as e:  # noqa: BLE001
        job.say("截图失败：%s" % str(e)[:80])


def _attach(job: Job, page: Any, bg: "set[asyncio.Task]", vc: Optional[VideoCandidates] = None) -> None:
    async def on_response(resp: Any) -> None:
        try:
            if job.stop.is_set() or resp.status not in (200, 206):
                return
            ct = (resp.headers.get("content-type") or "").lower()
            u = resp.url
            if vc is not None and videoframes:
                if videoframes.is_media_response(ct, u):  # 视频流：只记地址，不读内容
                    vc.add(u, page.url)
                    return
                if resp.status == 200 and ("json" in ct or "html" in ct or "javascript" in ct):  # 接口/页面里常埋着视频地址
                    body = await resp.body()
                    if len(body) <= 3_000_000:
                        for mu in videoframes.mine_video_urls(body.decode("utf-8", "ignore"), u):
                            vc.add(mu, page.url)
                    return
            if resp.status != 200 or not job.p.get("grab_images", True):
                return
            if not ct.startswith("image/") or "svg" in ct or "icon" in ct:
                return
            if u in job.seen_url or len(job.items) >= job.p["max_images"]:
                return
            job.seen_url.add(u)
            if UI_URL_RE.search(u):
                job.stats["ui"] += 1
                return
            body = await resp.body()
            await ingest(job, body, "image", u, page.url)
        except Exception:  # noqa: BLE001
            pass

    def handler(resp: Any) -> None:
        t = asyncio.create_task(on_response(resp))
        bg.add(t)
        t.add_done_callback(bg.discard)

    page.on("response", handler)


async def _scroll_capture(job: Job, page: Any, ctx: Any, rounds: int, shots: bool, grab: bool, vc: Optional[VideoCandidates] = None) -> None:
    idle = 0
    delay = job.p["delay"]
    for r in range(rounds + 1):
        if job.stop.is_set() or len(job.items) >= job.p["max_images"]:
            break
        if vc is not None:
            await vc.add_dom(page)
        before = len(job.items) + len(job.seen_url) + job.cand_total
        if shots:
            await _shot(job, page)
        if grab:
            await _collect_dom(job, page, ctx)
        if r == rounds:
            break
        try:
            vh = await page.evaluate("window.innerHeight")
            await page.mouse.move(200, 300)
            await page.mouse.wheel(0, int(vh * 0.85))
            await page.evaluate("(d) => window.scrollBy(0, d)", int(vh * 0.05))
        except Exception:  # noqa: BLE001
            pass
        await page.wait_for_timeout(int(delay * 1000))
        try:
            await page.wait_for_load_state("networkidle", timeout=2500)
        except Exception:  # noqa: BLE001
            pass
        after = len(job.items) + len(job.seen_url) + job.cand_total
        idle = idle + 1 if after == before else 0
        if idle >= 3:
            job.say("页面已滚动到底/没有新内容，停止滚动")
            break


async def _open(job: Job, ctx: Any, url: str, bg: "set[asyncio.Task]", vc: Optional[VideoCandidates] = None) -> Any:
    await check_url(url)
    page = await ctx.new_page()
    _attach(job, page, bg, vc)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:  # noqa: BLE001
        pass
    return page


async def _collect_links(job: Job, page: Any, url: str) -> List[str]:
    plat = detect_platform(url)
    rule = PLATFORMS[plat]["links"] or PLATFORMS["custom"]["links"]
    try:
        allv: List[str] = await page.evaluate(JS_LINKS)
    except Exception:  # noqa: BLE001
        allv = []
    base_host = urllib.parse.urlparse(page.url).hostname or ""
    here = page.url.split("#")[0]
    links: List[str] = []
    for l in allv:
        lh = urllib.parse.urlparse(l).hostname or ""
        if not l.startswith("http") or l.split("#")[0] == here or not re.search(rule, l):
            continue
        if plat == "custom" and _registrable(lh) != _registrable(base_host):
            continue
        l = l.split("#")[0]
        if l not in links:
            links.append(l)
    return links[: job.p["deep_limit"]]


async def _page_pass(job: Job, ctx: Any, url: str, bg: "set[asyncio.Task]", rounds: int, shots: bool, grab: bool,
                     vid_mode: bool, want_links: bool) -> List[str]:
    """打开一个页面：滚动抓取 →（视频模式）收集视频流 → 抽帧入审 → 兜底（yt-dlp / 播放截图）。返回页面里的作品链接"""
    vc = VideoCandidates(job) if vid_mode else None
    page = await _open(job, ctx, url, bg, vc)
    links: List[str] = []
    try:
        job.stats["pages"] += 1
        walled = await _clear_wall(job, page)
        if walled:
            job.stats["wall_pages"] += 1
            job.say("🔒 页面被登录/验证码墙遮住：%s。本系统不会绕过登录，也不会截登录框当内容。"
                    "请粘贴【具体视频链接】（抖音/B站可直接解析），或在高级选项填入你自己账号的 Cookie" % url[:70])
        if vid_mode:
            try:
                await page.evaluate(JS_AUTOPLAY)
            except Exception:  # noqa: BLE001
                pass
        await _scroll_capture(job, page, ctx, rounds, shots, grab, vc)
        if want_links:
            links = await _collect_links(job, page, url)
        if vc is not None:
            await vc.add_dom(page)
            got = await process_candidates(job, vc, page.url)
            if got == 0 and not links and job.p["use_ytdlp"] and videoframes and videoframes.has_ytdlp() and page.url not in job.ytdlp_tried:
                job.ytdlp_tried.add(page.url)
                try:
                    info = await asyncio.wait_for(asyncio.to_thread(videoframes.ytdlp_resolve, page.url, job.cookies, job.p["mobile"]), timeout=90)
                except Exception:  # noqa: BLE001
                    info = None
                if info:
                    got += await ingest_video(job, info, page.url)
            if got == 0 and not links and not walled:
                got += await element_shots(job, page, min(4, job.p["frames_per_video"]))
            if got == 0 and not links:
                job.say("本页没有发现可抽帧的视频%s" % ("（被登录墙遮住）" if walled else ""))
    finally:
        try:
            await page.close()
        except Exception:  # noqa: BLE001
            pass
    return links


def _chrome_installed() -> bool:
    return bool(shutil.which("google-chrome") or shutil.which("google-chrome-stable") or Path("/opt/google/chrome/chrome").exists())


async def crawl_playwright(job: Job, urls: List[str]) -> None:
    from playwright.async_api import async_playwright  # 延迟导入：没装也不影响服务启动

    job.engine = "playwright"
    mode = job.p["mode"]
    vid_mode = mode in ("video", "all")
    shots = mode in ("screenshot", "both", "all")
    grab = mode in ("images", "both", "all")
    job.p["grab_images"] = grab
    mobile = job.p["mobile"]
    async with async_playwright() as pw:
        browser = None
        if _chrome_installed():  # Google Chrome 带 H.264 解码器，视频截图兜底才有画面；没有就用 Playwright 自带 Chromium
            try:
                browser = await pw.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                job.say("浏览器：Google Chrome（支持 H.264）")
            except Exception:  # noqa: BLE001
                browser = None
        if browser is None:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            for ui, url in enumerate(urls):
                if job.stop.is_set() or len(job.items) >= job.p["max_images"]:
                    break
                job.say("(%d/%d) 打开 %s" % (ui + 1, len(urls), url[:100]))
                content = is_content_url(url)
                if vid_mode and content:
                    if await video_direct(job, url) > 0:  # 公开解析器已拿到视频帧：不用再开浏览器
                        continue
                kw: Dict[str, Any] = dict(locale="zh-CN", timezone_id="Asia/Shanghai",
                                          extra_http_headers={"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6"})
                if mobile:
                    kw.update(user_agent=UA_MOBILE, viewport={"width": 390, "height": 844}, device_scale_factor=2,
                              is_mobile=True, has_touch=True)
                else:
                    kw.update(user_agent=UA_DESKTOP, viewport={"width": 1366, "height": 900})
                ctx = await browser.new_context(**kw)
                bg: "set[asyncio.Task]" = set()
                try:
                    if job.p["cookie"]:
                        try:
                            await ctx.add_cookies(parse_cookies(job.p["cookie"], url))
                        except Exception as e:  # noqa: BLE001
                            job.say("Cookie 格式无法识别，已忽略：%s" % str(e)[:60])
                    want_links = (job.p["deep"] or vid_mode) and not content
                    rounds = min(2, job.p["scrolls"]) if content else job.p["scrolls"]
                    links = await _page_pass(job, ctx, url, bg, rounds, shots, grab, vid_mode, want_links)
                    if links:
                        job.say("深入作品详情：发现 %d 个作品链接" % len(links))
                        for li, link in enumerate(links):
                            if job.stop.is_set() or len(job.items) >= job.p["max_images"] or (vid_mode and job.stats["videos"] >= job.p["video_limit"]):
                                break
                            await asyncio.sleep(job.p["delay"])  # 礼貌间隔，降低对目标站的压力
                            try:
                                if vid_mode and await video_direct(job, link) > 0:
                                    continue
                                await _page_pass(job, ctx, link, bg, min(2, job.p["scrolls"]), shots, grab, vid_mode, False)
                            except Exception as e:  # noqa: BLE001
                                job.say("作品页失败 %d/%d：%s" % (li + 1, len(links), str(e)[:80]))
                    if bg:
                        await asyncio.gather(*list(bg), return_exceptions=True)
                except Exception as e:  # noqa: BLE001
                    job.say("❌ 打开失败：%s" % str(e).split("\n")[0][:140])
                finally:
                    await ctx.close()
        finally:
            await browser.close()


# --------------------------------------------------------------------------
# 抓取：HTTP 降级模式（没有 Chromium 时）
# --------------------------------------------------------------------------
IMG_URL_RE = re.compile(r"https?:(?:\\?/){2}[^\s\"'<>()\\]+?\.(?:jpe?g|png|webp|gif|bmp|avif)(?:\?[^\s\"'<>()\\]*)?", re.I)
ATTR_RE = re.compile(r"""(?:src|data-src|data-original|data-lazy-src|content|poster)\s*=\s*["']([^"']+)["']""", re.I)


def _http_get(url: str, referer: str, cookie: str, limit: int = 20 * 1024 * 1024) -> Tuple[bytes, str, str]:
    req = urllib.request.Request(url, headers={"User-Agent": UA_DESKTOP, "Referer": referer, "Accept-Language": "zh-CN,zh;q=0.9",
                                               **({"Cookie": cookie} if cookie else {})})
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read(limit), (r.headers.get("content-type") or "").lower(), r.geturl()


async def crawl_http(job: Job, urls: List[str]) -> None:
    job.engine = "http"
    mode = job.p["mode"]
    vid_mode = mode in ("video", "all")
    grab = mode in ("images", "both", "all")
    job.say("未检测到 Playwright/Chromium：使用 HTTP 降级模式（视频直链/公开解析可用；动态页面请先运行 Cell 1 安装 Chromium）")
    if mode == "screenshot":
        raise RuntimeError("截图模式需要 Playwright + Chromium（运行 Kaggle Cell 1 会自动安装）")
    for ui, url in enumerate(urls):
        if job.stop.is_set():
            break
        job.say("(%d/%d) HTTP 抓取 %s" % (ui + 1, len(urls), url[:100]))
        cookie = job.cookie_header(url)
        try:
            await check_url(url)
            if vid_mode and is_content_url(url) and await video_direct(job, url) > 0:
                continue
            body, ct, final = await asyncio.to_thread(_http_get, url, url, cookie)
            job.stats["pages"] += 1
            if ct.startswith("image/"):
                if grab:
                    await ingest(job, body, "image", final, final)
                continue
            if ct.startswith("video/") and vid_mode and videoframes:
                await ingest_video(job, {"url": final, "headers": {"User-Agent": UA_DESKTOP, "Referer": url}, "via": "视频直链"}, final)
                continue
            html = body.decode("utf-8", "ignore")
            if vid_mode and videoframes:
                vurls = videoframes.mine_html_videos(html, final)
                job.say("页面中发现 %d 个视频地址" % len(vurls))
                got = 0
                for vu in vurls:
                    if job.stats["videos"] >= job.p["video_limit"] or job.stop.is_set():
                        break
                    got += await ingest_video(job, {"url": vu, "headers": {"User-Agent": UA_DESKTOP, "Referer": final}, "via": "页面源码"}, final)
                if got == 0 and job.p["use_ytdlp"] and videoframes.has_ytdlp() and url not in job.ytdlp_tried:
                    job.ytdlp_tried.add(url)
                    info = await asyncio.wait_for(asyncio.to_thread(videoframes.ytdlp_resolve, url, job.cookies, False), timeout=90)
                    if info:
                        await ingest_video(job, info, final)
            if not grab:
                continue
            cands: List[str] = []
            for m in ATTR_RE.findall(html) + IMG_URL_RE.findall(html):
                u = m.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
                try:
                    u = urllib.parse.urljoin(final, u)
                except Exception:  # noqa: BLE001
                    continue
                if u.startswith("http") and re.search(r"\.(jpe?g|png|webp|gif|bmp|avif)(\?|$)|image|img|photo|pic", u, re.I) and u not in cands \
                        and not UI_URL_RE.search(u):
                    cands.append(u)
            job.say("页面中发现 %d 个候选图片地址" % len(cands))
            sem = asyncio.Semaphore(6)

            async def one(u: str) -> None:
                async with sem:
                    if u in job.seen_url or job.stop.is_set():
                        return
                    job.seen_url.add(u)
                    try:
                        await check_url(u)
                        b, c, _ = await asyncio.to_thread(_http_get, u, final, cookie)
                        if c.startswith("image/") and "svg" not in c:
                            await ingest(job, b, "image", u, final)
                    except Exception:  # noqa: BLE001
                        pass

            await asyncio.gather(*[one(u) for u in cands[:400]])
        except Exception as e:  # noqa: BLE001
            job.say("❌ 抓取失败：%s" % str(e)[:140])


# --------------------------------------------------------------------------
# 任务生命周期
# --------------------------------------------------------------------------
def normalize_params(body: Dict[str, Any]) -> Dict[str, Any]:
    def num(k: str, d: float, lo: float, hi: float) -> float:
        try:
            return max(lo, min(hi, float(body.get(k, d))))
        except (TypeError, ValueError):
            return d

    urls: List[str] = []
    for raw in body.get("urls") or []:
        s = str(raw).strip()
        for u in extract_urls(s) or ([s] if s.startswith("http") else []):
            if u not in urls:
                urls.append(u)
    platform = str(body.get("platform") or "custom")
    if platform not in PLATFORMS:
        platform = "custom"
    kw = str(body.get("keyword") or "").strip()
    if kw and PLATFORMS[platform]["search"]:
        urls.append(PLATFORMS[platform]["search"].format(kw=urllib.parse.quote(kw)))
    mode = str(body.get("mode") or "video")
    return {
        "urls": urls[:30], "platform": platform, "keyword": kw,
        # video=只要视频画面（默认）| all=视频+图片+截图 | both=图片+截图（v2.1 旧行为）| images | screenshot
        "mode": mode if mode in ("video", "all", "images", "screenshot", "both") else "video",
        "frames_per_video": int(num("frames_per_video", 6, 1, 24)),
        "video_limit": int(num("video_limit", 10, 1, 60)),
        "use_ytdlp": bool(body.get("use_ytdlp", True)),
        "max_images": int(num("max_images", 60, 1, HARD_MAX_IMAGES)),
        "scrolls": int(num("scrolls", 8, 0, 60)),
        "min_size": int(num("min_size", 200, 32, 2000)),
        "delay": num("delay", 0.8, 0.2, 5),
        "deep": bool(body.get("deep")), "deep_limit": int(num("deep_limit", 12, 1, 60)),
        "mobile": bool(body.get("mobile", False)),
        "cookie": str(body.get("cookie") or "")[:8000],
        "analyze_mode": "fast" if body.get("analyze_mode") == "fast" else "full",
        "max_seconds": int(num("max_seconds", 420, 30, 1800)),
        "cancel_on_stop": bool(body.get("cancel_on_stop", True)),
    }


async def _run(job: Job) -> None:
    job.state = "running"
    try:
        urls = job.p["urls"]
        if not urls:
            raise RuntimeError("没有有效网址：请粘贴网址/分享链接，或填写关键词并选择平台")
        job.say("任务开始：%d 个入口 · 模式=%s · 最多 %d 张 · 审核并发=%d（T4×2 同时处理）" %
                (len(urls), job.p["mode"], job.p["max_images"], job.conc))
        cap = capabilities()
        runner = crawl_playwright if cap["chromium"] else crawl_http
        try:
            await asyncio.wait_for(runner(job, urls), timeout=job.p["max_seconds"])
        except asyncio.TimeoutError:
            job.say("达到最长运行时间 %ds，已停止抓取，等待已抓到的图片审核完成" % job.p["max_seconds"])
        except Exception as e:  # noqa: BLE001
            if runner is crawl_playwright and not job.items:
                job.say("Playwright 启动/运行失败（%s），自动改用 HTTP 降级模式" % str(e).split("\n")[0][:100])
                await crawl_http(job, urls)
            else:
                raise
        while job.tasks:  # 等待流水线里剩余的审核完成
            await asyncio.gather(*list(job.tasks), return_exceptions=True)
        job.state = "stopped" if job.stop.is_set() else "done"
        st = job.stats
        job.say("任务%s：视频 %d 个 → 抽帧 %d 张 · 图片 %d 张 · 截图 %d 张，已审核 %d 张（拦截 %d / 待复核 %d / 通过 %d），登录墙排除 %d 张" % (
            "已停止" if job.stop.is_set() else "完成", st["videos"], st["frames"], st["found"], st["screenshots"],
            st["analyzed"], st["block"], st["review"], st["pass"], st["walls"]))
        if job.p["mode"] in ("video", "all") and st["frames"] == 0 and not job.stop.is_set():
            job.say("⚠️ 没有拿到任何视频画面。常见原因：① 该平台的搜索页/推荐页必须登录才出内容；② 视频流需要登录态。"
                    "办法：直接粘贴【具体的视频链接】（抖音/B站会走公开解析，不用登录），或在高级选项里填入你自己账号的 Cookie。")
    except Exception as e:  # noqa: BLE001
        log.exception("爬取任务失败")
        job.state = "error"
        job.error = str(e)[:300]
        job.say("❌ " + job.error)
    finally:
        job.finished = time.time()
        job.rev += 1


def start_job(body: Dict[str, Any]) -> Job:
    if _analyze_cb is None:
        raise RuntimeError("爬取模块尚未初始化")
    p = normalize_params(body)
    job = Job(p)
    JOBS[job.id] = job
    for old in sorted(JOBS.values(), key=lambda j: j.created)[:-MAX_JOBS_KEPT]:
        if old.state not in ("queued", "running"):
            JOBS.pop(old.id, None)
            shutil.rmtree(old.dir, ignore_errors=True)
    asyncio.create_task(_run(job))
    return job


def view(job: Job, since: int = 0) -> Dict[str, Any]:
    return {
        "id": job.id, "state": job.state, "error": job.error, "engine": job.engine, "rev": job.rev,
        "elapsed": round((job.finished or time.time()) - job.created, 1), "stats": job.stats,
        "pending": job.pending, "conc": job.conc, "gpu_count": job.gpu_count, "cap_hit": job.cap_hit,
        "video_cap_hit": job.video_cap_hit,
        "logs": job.logs[-60:], "total": len(job.items),
        "params": {k: v for k, v in job.p.items() if k != "cookie"},
        "items": [i for i in job.items if i.get("rev", 0) > since],
    }


def image_path(job: Job, idx: int, thumb: bool) -> Optional[Path]:
    p = job.dir / (("%d_t.jpg" if thumb else "%d.jpg") % idx)
    return p if p.exists() else None


def export_json(job: Job) -> str:
    return json.dumps({"job": view(job, 0)}, ensure_ascii=False, indent=2)


def export_xlsx(job: Job, include_wall: bool = False) -> bytes:
    """把本次爬取结果导出为 Excel（含缩略图 + 违规类别 + 汇总）。登录墙截图默认不导出"""
    import report  # 延迟导入：缺 openpyxl 时只影响导出

    rows: List[Dict[str, Any]] = []
    for it in job.items:
        res = it.get("res")
        if not res or (it.get("wall") and not include_wall):
            continue
        tp = job.dir / ("%d_t.jpg" % it["idx"])
        rows.append({
            "idx": it["idx"], "kind": it.get("kind"), "name": it.get("name"), "res": res, "page": it.get("page"),
            "src": it.get("src"), "video": it.get("video"), "ts": it.get("ts"), "title": it.get("title"),
            "image": tp.read_bytes() if tp.exists() else None,
        })
    meta = {"title": "网络照片自动爬取审核报告", "platform": PLATFORMS.get(job.p["platform"], {}).get("name", ""),
            "urls": job.p["urls"], "keyword": job.p["keyword"], "mode": job.p["mode"], "elapsed": round((job.finished or time.time()) - job.created, 1),
            "stats": job.stats, "gpu_count": job.gpu_count, "walls": job.stats.get("walls", 0)}
    return report.build_xlsx(rows, meta)
