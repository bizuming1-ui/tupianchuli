"""
网络照片「自动爬取 + 截图 + 精准审核」模块 v2.1（与 server.py 配合，不改动原有审核逻辑）

第一性原则：吞吐 = 抓取速度 × GPU 利用率。所以抓取与审核做成【流水线】：
  抓到一张 → 立刻去重 → 立刻丢给 GPU 池审核（两张 T4 同时抢活）→ 前端实时看到结果。

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
    host = urllib.parse.urlparse(url).hostname or ""
    dom = "." + _registrable(host) if "." in host else host
    out = []
    for part in (raw or "").split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out.append({"name": k, "value": v, "domain": dom, "path": "/"})
    return out


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
    return {"playwright": pw, "chromium": bool(chromium), "engine": "playwright" if chromium else "http",
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
        if kind == "image":
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
                                      "dup": 0, "small": 0, "pass": 0, "review": 0, "block": 0}
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


async def ingest(job: Job, raw: bytes, kind: str, src: str, page_url: str) -> bool:
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
        # 截图只做 SHA1 精确去重（文字密集页面滚动前后的低分辨率哈希很接近，用近似去重会漏检）；图片用感知哈希去近似重复
        if kind == "image":
            if any(_popcount(dh ^ o) <= 2 for o in job.dhashes):
                job.stats["dup"] += 1
                return False
            job.dhashes.append(dh)
        idx = len(job.items)
        item: Dict[str, Any] = {
            "idx": idx, "kind": kind, "src": src[:600], "page": page_url[:600], "w": w, "h": h, "bytes": len(raw),
            "name": ("截图-%03d.jpg" % idx) if kind == "screenshot" else (_name_from_url(src) or "img-%03d.jpg" % idx),
            "status": "queued", "t": round(time.time() - job.created, 1),
        }
        # 先落盘再公开：前端一旦看到这一项，缩略图一定已经可以读取
        await asyncio.to_thread((job.dir / ("%d.jpg" % idx)).write_bytes, full)
        await asyncio.to_thread((job.dir / ("%d_t.jpg" % idx)).write_bytes, thumb)
        job.items.append(item)
        job.stats["found" if kind == "image" else "screenshots"] += 1
        job.touch(item)
    t = asyncio.create_task(_analyze_item(job, item))
    job.tasks.add(t)
    t.add_done_callback(job.tasks.discard)
    return True


# --------------------------------------------------------------------------
# 抓取：Playwright 模式
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


async def _download(job: Job, ctx: Any, url: str, referer: str) -> None:
    if url in job.seen_url or job.stop.is_set() or len(job.items) >= job.p["max_images"]:
        return
    job.seen_url.add(url)
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
        png = await page.screenshot(type="jpeg", quality=88, full_page=False)
        await ingest(job, png, "screenshot", page.url, page.url)
    except Exception as e:  # noqa: BLE001
        job.say("截图失败：%s" % str(e)[:80])


def _attach(job: Job, page: Any, bg: "set[asyncio.Task]") -> None:
    async def on_response(resp: Any) -> None:
        try:
            if job.stop.is_set() or resp.status != 200:
                return
            ct = (resp.headers.get("content-type") or "").lower()
            if not ct.startswith("image/") or "svg" in ct or "icon" in ct:
                return
            u = resp.url
            if u in job.seen_url or len(job.items) >= job.p["max_images"]:
                return
            job.seen_url.add(u)
            body = await resp.body()
            await ingest(job, body, "image", u, page.url)
        except Exception:  # noqa: BLE001
            pass

    def handler(resp: Any) -> None:
        t = asyncio.create_task(on_response(resp))
        bg.add(t)
        t.add_done_callback(bg.discard)

    page.on("response", handler)


async def _scroll_capture(job: Job, page: Any, ctx: Any, rounds: int, shots: bool, grab: bool) -> None:
    idle = 0
    delay = job.p["delay"]
    for r in range(rounds + 1):
        if job.stop.is_set() or len(job.items) >= job.p["max_images"]:
            break
        before = len(job.items) + len(job.seen_url)
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
        after = len(job.items) + len(job.seen_url)
        idle = idle + 1 if after == before else 0
        if idle >= 3:
            job.say("页面已滚动到底/没有新内容，停止滚动")
            break


async def _open(job: Job, ctx: Any, url: str, bg: "set[asyncio.Task]") -> Any:
    await check_url(url)
    page = await ctx.new_page()
    _attach(job, page, bg)
    await page.goto(url, wait_until="domcontentloaded", timeout=30000)
    try:
        await page.wait_for_load_state("networkidle", timeout=8000)
    except Exception:  # noqa: BLE001
        pass
    return page


async def crawl_playwright(job: Job, urls: List[str]) -> None:
    from playwright.async_api import async_playwright  # 延迟导入：没装也不影响服务启动

    job.engine = "playwright"
    mode = job.p["mode"]
    shots = mode in ("screenshot", "both")
    grab = mode in ("images", "both")
    mobile = job.p["mobile"]
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
        try:
            for ui, url in enumerate(urls):
                if job.stop.is_set() or len(job.items) >= job.p["max_images"]:
                    break
                job.say("(%d/%d) 打开 %s" % (ui + 1, len(urls), url[:100]))
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
                    page = await _open(job, ctx, url, bg)
                    job.stats["pages"] += 1
                    try:
                        head = await page.evaluate("document.body ? document.body.innerText.slice(0, 600) : ''")
                        if WALL_RE.search(head or ""):
                            job.say("⚠️ 页面出现登录/验证码提示（本系统不会绕过）。可用自己账号的 Cookie 或换公开链接。")
                    except Exception:  # noqa: BLE001
                        pass
                    await _scroll_capture(job, page, ctx, job.p["scrolls"], shots, grab)
                    if job.p["deep"] and not job.stop.is_set():
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
                        links = links[: job.p["deep_limit"]]
                        job.say("深入作品详情：发现 %d 个作品链接" % len(links))
                        await page.close()
                        for li, link in enumerate(links):
                            if job.stop.is_set() or len(job.items) >= job.p["max_images"]:
                                break
                            await asyncio.sleep(job.p["delay"])  # 礼貌间隔，降低对目标站的压力
                            try:
                                p2 = await _open(job, ctx, link, bg)
                                job.stats["pages"] += 1
                                await _scroll_capture(job, p2, ctx, min(2, job.p["scrolls"]), shots, grab)
                                await p2.close()
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
    job.say("未检测到 Playwright/Chromium：使用 HTTP 降级模式（只能抓静态页面里的图片；动态页面请先运行 Cell 1 安装 Chromium）")
    if job.p["mode"] == "screenshot":
        raise RuntimeError("截图模式需要 Playwright + Chromium（运行 Kaggle Cell 1 会自动安装）")
    cookie = job.p["cookie"]
    for ui, url in enumerate(urls):
        if job.stop.is_set():
            break
        job.say("(%d/%d) HTTP 抓取 %s" % (ui + 1, len(urls), url[:100]))
        try:
            await check_url(url)
            body, ct, final = await asyncio.to_thread(_http_get, url, url, cookie)
            job.stats["pages"] += 1
            if ct.startswith("image/"):
                await ingest(job, body, "image", final, final)
                continue
            html = body.decode("utf-8", "ignore")
            cands: List[str] = []
            for m in ATTR_RE.findall(html) + IMG_URL_RE.findall(html):
                u = m.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
                try:
                    u = urllib.parse.urljoin(final, u)
                except Exception:  # noqa: BLE001
                    continue
                if u.startswith("http") and re.search(r"\.(jpe?g|png|webp|gif|bmp|avif)(\?|$)|image|img|photo|pic", u, re.I) and u not in cands:
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
    mode = str(body.get("mode") or "both")
    return {
        "urls": urls[:30], "platform": platform, "keyword": kw,
        "mode": mode if mode in ("images", "screenshot", "both") else "both",
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
        job.say("任务%s：抓到 %d 张图片 + %d 张截图，已审核 %d 张（拦截 %d / 待复核 %d / 通过 %d）" % (
            "已停止" if job.stop.is_set() else "完成", job.stats["found"], job.stats["screenshots"],
            job.stats["analyzed"], job.stats["block"], job.stats["review"], job.stats["pass"]))
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
        "logs": job.logs[-60:], "total": len(job.items),
        "params": {k: v for k, v in job.p.items() if k != "cookie"},
        "items": [i for i in job.items if i.get("rev", 0) > since],
    }


def image_path(job: Job, idx: int, thumb: bool) -> Optional[Path]:
    p = job.dir / (("%d_t.jpg" if thumb else "%d.jpg") % idx)
    return p if p.exists() else None


def export_json(job: Job) -> str:
    return json.dumps({"job": view(job, 0)}, ensure_ascii=False, indent=2)
