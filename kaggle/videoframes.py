"""
视频 → 审核用截图（帧）模块 v2.2（与 crawler.py 配合；不依赖 GPU 模块，缺依赖时只会降级）

为什么需要它（第一性原则）：
  旧版爬取只做「抓 <img> + 页面截图」。抖音/小红书/微博等平台的搜索页、推荐页没有登录就会被登录弹窗遮住，
  于是抓到的全是「二维码 / 登录框 / 头像 / 图标」截图。而你真正要审核的是【视频里的画面】。
  视频画面不在 <img> 里，它在视频流里 —— 所以正确做法是：找到视频流地址 → 下载/拉流 → 用 ffmpeg 抽帧 → 送 GPU 审核。

视频流地址的 4 个来源（从最稳到最广，任何一个失败都只会降级）：
  1) 平台公开解析器：抖音分享页(iesdouyin.com/share/video/<id>，公开页，不用登录)、B 站公开接口(playurl 的 mp4 低清晰度流)
  2) yt-dlp：覆盖 B 站/微博/YouTube/几百个站点（可选；装不上就跳过）
  3) 浏览器网络抓包：页面加载时出现的 video/*、m3u8、mp4 请求；JSON/HTML 响应里挖出来的 .mp4/.m3u8 地址
  4) DOM：<video src> / <source src> / og:video
抽帧：ffmpeg 快速定位（-ss 在 -i 前）均匀取 N 帧；没有 ffmpeg 时回退 OpenCV；丢弃纯黑/纯色帧，并按感知哈希去重。

合规边界（与 crawler.py 一致）：不破解验证码、不绕过登录、不做指纹伪装；只处理公开可访问的视频流，
或使用者提供的【自己账号】Cookie 能访问的视频流；不保存视频文件（抽完帧立即删除）。
"""
from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from PIL import Image, ImageStat

UA_DESKTOP = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")
UA_MOBILE = ("Mozilla/5.0 (iPhone; CPU iPhone OS 16_6 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
             "Version/16.6 Mobile/15E148 Safari/604.1")
MAX_DOWNLOAD = int(os.getenv("VIDEO_MAX_MB", "150")) * 1024 * 1024  # 单个视频最多下载多少（超出则只用已下载的前半部分）


# --------------------------------------------------------------------------
# 能力探测
# --------------------------------------------------------------------------
def ffmpeg_path() -> Optional[str]:
    p = shutil.which("ffmpeg")
    if p:
        return p
    try:  # pip install imageio-ffmpeg 自带静态 ffmpeg（Kaggle 一般已有系统 ffmpeg，这里只是兜底）
        import imageio_ffmpeg  # type: ignore

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:  # noqa: BLE001
        return None


def _has_mod(name: str) -> bool:
    try:
        import importlib.util

        return importlib.util.find_spec(name) is not None
    except Exception:  # noqa: BLE001
        return False


def has_ytdlp() -> bool:
    return _has_mod("yt_dlp")


def has_cv2() -> bool:
    return _has_mod("cv2")


def capabilities() -> Dict[str, Any]:
    return {"ffmpeg": bool(ffmpeg_path()), "ytdlp": has_ytdlp(), "cv2": has_cv2()}


# --------------------------------------------------------------------------
# 从任意文本（HTML / JSON / JS）里挖视频地址
# --------------------------------------------------------------------------
VIDEO_URL_RE = re.compile(r"https?://[^\s\"'<>\\)]+?\.(?:mp4|m3u8|webm|mov|m4v|flv)(?:\?[^\s\"'<>\\)]*)?", re.I)
# 没有扩展名但一定是视频流的 CDN / 接口
VIDEO_HOST_RE = re.compile(
    r"https?://[^\s\"'<>\\)]*(?:douyinvod\.com|aweme/v1/play|aweme/v1/playwm|bilivideo\.(?:com|cn)|/upgcxcode/|"
    r"xhscdn\.com/stream|video\.weibocdn\.com|ixigua\.com/video|zjcdn\.com|"
    r"mime_type=video)[^\s\"'<>\\)]*", re.I)
NOT_VIDEO_RE = re.compile(r"\.(?:jpe?g|png|gif|webp|svg|css|js|json|ts|m4s)(?:\?|$)|mime_type=audio|media-audio|\.mp3(?:\?|$)|/ads?/|doubleclick", re.I)


def _unescape(text: str) -> str:
    return (text.replace("\\u002F", "/").replace("\\u002f", "/").replace("\\/", "/")
            .replace("\\u0026", "&").replace("&amp;", "&"))


def mine_video_urls(text: str, base: str = "") -> List[str]:
    """从文本里挖出所有疑似视频流地址（已去转义、去重、保持出现顺序）"""
    if not text:
        return []
    t = _unescape(text)
    out: List[str] = []
    for rx in (VIDEO_URL_RE, VIDEO_HOST_RE):
        for m in rx.findall(t):
            u = m.rstrip(".,;")
            if u.startswith("//"):
                u = "https:" + u
            if base:
                u = urllib.parse.urljoin(base, u)
            if NOT_VIDEO_RE.search(u):
                continue
            if u not in out:
                out.append(u)
    return out


_TAG_VIDEO = re.compile(r"<(?:video|source)\b[^>]*>", re.I)
_ATTR_SRC = re.compile(r"""\b(?:src|data-src|data-video|data-url)\s*=\s*["']([^"']+)["']""", re.I)
_META_VIDEO = re.compile(r"""<meta[^>]+(?:property|name)=["'](?:og:video(?::url|:secure_url)?|twitter:player:stream)["'][^>]*content=["']([^"']+)["']""", re.I)


def mine_html_videos(html: str, base: str) -> List[str]:
    """HTML 里的 <video src> / <source src>（含相对路径）/ og:video，转成绝对地址；再加上正则从文本里挖到的绝对地址"""
    out: List[str] = []

    def add(u: str) -> None:
        u = _unescape(u.strip())
        if not u or u.startswith(("blob:", "data:", "javascript:")):
            return
        try:
            u = urllib.parse.urljoin(base, u)
        except Exception:  # noqa: BLE001
            return
        if u.startswith("http") and u not in out and not re.search(r"\.(?:jpe?g|png|gif|webp|svg)(?:\?|$)", u, re.I):
            out.append(u)

    for tag in _TAG_VIDEO.findall(html or ""):
        for m in _ATTR_SRC.findall(tag):
            add(m)
    for m in _META_VIDEO.findall(html or ""):
        add(m)
    for u in mine_video_urls(html, base):
        add(u)
    return out


def norm_key(u: str) -> str:
    """同一个视频的不同分片/不同签名参数归一成同一个 key，避免重复抽帧"""
    try:
        p = urllib.parse.urlparse(u)
        q = urllib.parse.parse_qs(p.query)
        keep = [(k, q[k][0]) for k in ("video_id", "vid", "file_id", "fid") if k in q]
        return p.path + ("?" + urllib.parse.urlencode(keep) if keep else "")
    except Exception:  # noqa: BLE001
        return u


def is_media_response(ct: str, url: str) -> bool:
    """网络响应是不是视频流（只看响应头，不读 body）"""
    ct = (ct or "").lower()
    if ct.startswith("audio/") or NOT_VIDEO_RE.search(url) and not ct.startswith("video/"):
        return False
    if ct.startswith("video/") or "mpegurl" in ct:
        return True
    if ct in ("application/octet-stream", "binary/octet-stream", "") and VIDEO_HOST_RE.search(url):
        return True
    return bool(re.search(r"\.(?:mp4|m3u8|webm|mov|m4v)(?:\?|$)", url, re.I)) and not ct.startswith("image/") and "json" not in ct


# --------------------------------------------------------------------------
# Cookie：支持 3 种常见格式（浏览器请求头 / Cookie-Editor JSON / Netscape cookies.txt）
# --------------------------------------------------------------------------
def parse_cookie_text(raw: str, default_domain: str = "") -> List[Dict[str, Any]]:
    raw = (raw or "").strip()
    out: List[Dict[str, Any]] = []
    if not raw:
        return out
    if raw[0] in "[{":  # EditThisCookie / Cookie-Editor 导出的 JSON
        try:
            data = json.loads(raw)
            arr = data if isinstance(data, list) else data.get("cookies", [])
            for c in arr:
                if isinstance(c, dict) and c.get("name"):
                    out.append({"name": str(c["name"]), "value": str(c.get("value", "")),
                                "domain": str(c.get("domain") or default_domain), "path": str(c.get("path") or "/")})
            return out
        except Exception:  # noqa: BLE001
            pass
    if "\t" in raw and "\n" in raw:  # Netscape cookies.txt：domain flag path secure expiry name value
        for line in raw.splitlines():
            line = line.strip()
            if not line or (line.startswith("#") and not line.startswith("#HttpOnly_")):
                continue
            line = line.replace("#HttpOnly_", "")
            f = line.split("\t")
            if len(f) >= 7:
                out.append({"name": f[5], "value": f[6], "domain": f[0], "path": f[2] or "/"})
        if out:
            return out
    for part in raw.split(";"):  # 最常见：a=1; b=2
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            out.append({"name": k, "value": v, "domain": default_domain, "path": "/"})
    return out


def cookie_header_for(cookies: List[Dict[str, Any]], url: str) -> str:
    host = (urllib.parse.urlparse(url).hostname or "").lower()
    parts = []
    for c in cookies:
        d = str(c.get("domain") or "").lstrip(".").lower()
        if not d or host == d or host.endswith("." + d):
            parts.append("%s=%s" % (c["name"], c["value"]))
    return "; ".join(parts)


def write_netscape(cookies: List[Dict[str, Any]], path: str) -> None:
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies:
        d = str(c.get("domain") or "")
        if not d:
            continue
        lines.append("\t".join([d, "TRUE" if d.startswith(".") else "FALSE", c.get("path") or "/", "FALSE", "2147483647",
                                c["name"], c["value"]]))
    Path(path).write_text("\n".join(lines) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------
# 小工具
# --------------------------------------------------------------------------
def _http(url: str, headers: Dict[str, str], timeout: int = 20, limit: int = 4 * 1024 * 1024) -> Tuple[bytes, str]:
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(limit), r.geturl()


def _json_after(text: str, marker: str) -> Optional[Any]:
    """在 text 中找到 marker 之后的第一个 JSON 对象并完整解析（比 .*? 正则可靠）"""
    i = text.find(marker)
    if i < 0:
        return None
    j = text.find("{", i + len(marker))
    if j < 0:
        return None
    try:
        obj, _ = json.JSONDecoder().raw_decode(text[j:])
        return obj
    except Exception:  # noqa: BLE001
        return None


def _dhash(img: Image.Image) -> int:
    g = img.convert("L").resize((9, 8), Image.BILINEAR)
    px = list(g.getdata())
    bits = 0
    for r in range(8):
        for c in range(8):
            bits = (bits << 1) | (1 if px[r * 9 + c] > px[r * 9 + c + 1] else 0)
    return bits


def _bad_frame(img: Image.Image) -> bool:
    """纯黑 / 纯色（还没解码出画面、转场黑屏）的帧没有审核价值"""
    g = img.convert("L").resize((64, 64))
    st = ImageStat.Stat(g)
    return st.stddev[0] < 5.0 or st.mean[0] < 6.0


# --------------------------------------------------------------------------
# 平台公开解析器：返回 {"url","headers","cover","title","images":[...],"via"} 或 None（同步函数，放线程里跑）
# --------------------------------------------------------------------------
_DY_ID = re.compile(r"/(?:video|note|slides)/(\d{8,})|modal_id=(\d{8,})")


def douyin_resolve(url: str) -> Optional[Dict[str, Any]]:
    """抖音：短链/长链 → 作品 id → 公开分享页 iesdouyin.com/share/video/<id>（手机 UA）→ 页面内 _ROUTER_DATA"""
    try:
        h = {"User-Agent": UA_MOBILE, "Accept-Language": "zh-CN,zh;q=0.9"}
        m = _DY_ID.search(url)
        if not m:
            _, final = _http(url, h, timeout=15, limit=2000)
            m = _DY_ID.search(final)
        if not m:
            return None
        vid = m.group(1) or m.group(2)
        html, _ = _http("https://www.iesdouyin.com/share/video/%s/" % vid, h, timeout=20, limit=3 * 1024 * 1024)
        data = _json_after(html.decode("utf-8", "ignore"), "window._ROUTER_DATA")
        if not data:
            return None
        info = None
        for v in (data.get("loaderData") or {}).values():
            if isinstance(v, dict) and isinstance(v.get("videoInfoRes"), dict):
                lst = v["videoInfoRes"].get("item_list") or []
                if lst:
                    info = lst[0]
                    break
        if not info:
            return None
        video = info.get("video") or {}
        urls = (video.get("play_addr") or {}).get("url_list") or []
        cover = ((video.get("cover") or {}).get("url_list") or [""])[0]
        images = []
        for im in info.get("images") or []:
            ul = (im or {}).get("url_list") or []
            if ul:
                images.append(ul[0])
        play = urls[0].replace("playwm", "play") if urls else ""
        if play and images and ("mp3" in play or not video.get("duration")):
            play = ""  # 图集的配乐，不是视频
        if not play and not images:
            return None
        return {"url": play, "headers": {"User-Agent": UA_MOBILE, "Referer": "https://www.douyin.com/"},
                "cover": cover, "title": info.get("desc") or "", "images": images, "via": "抖音公开分享页"}
    except Exception:  # noqa: BLE001
        return None


def bilibili_resolve(url: str) -> Optional[Dict[str, Any]]:
    """B 站：BV 号 → view 接口（cid/封面）→ playurl（html5 平台、mp4 单文件流，未登录可得低清晰度）"""
    try:
        h = {"User-Agent": UA_DESKTOP, "Referer": "https://www.bilibili.com/"}
        if "b23.tv" in url:
            _, url = _http(url, h, timeout=15, limit=2000)
        m = re.search(r"(BV[0-9A-Za-z]{10})", url)
        if not m:
            return None
        bvid = m.group(1)
        pm = re.search(r"[?&]p=(\d+)", url)
        page = int(pm.group(1)) if pm else 1
        raw, _ = _http("https://api.bilibili.com/x/web-interface/view?bvid=%s" % bvid, h)
        d = json.loads(raw.decode("utf-8", "ignore")).get("data") or {}
        pages = d.get("pages") or []
        cid = (pages[page - 1] if 0 < page <= len(pages) else {}).get("cid") or d.get("cid")
        if not cid:
            return None
        raw, _ = _http("https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&qn=32&fnval=1&fnver=0&platform=html5&high_quality=0"
                       % (bvid, cid), h)
        pu = json.loads(raw.decode("utf-8", "ignore")).get("data") or {}
        durl = pu.get("durl") or []
        if not durl:
            return None
        return {"url": durl[0]["url"], "headers": h, "cover": d.get("pic") or "", "title": d.get("title") or "",
                "images": [], "via": "B站公开接口"}
    except Exception:  # noqa: BLE001
        return None


def ytdlp_resolve(url: str, cookies: Optional[List[Dict[str, Any]]] = None, mobile: bool = False) -> Optional[Dict[str, Any]]:
    """yt-dlp 只解析不下载：返回最合适的（≤720p）视频流地址 + 需要的请求头"""
    try:
        import yt_dlp  # type: ignore
    except Exception:  # noqa: BLE001
        return None
    tmp = None
    try:
        opts: Dict[str, Any] = {
            "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True, "socket_timeout": 20,
            "format": "bv*[height<=720]/b[height<=720]/bv*/b",
            "http_headers": {"User-Agent": UA_MOBILE if mobile else UA_DESKTOP}, "playlist_items": "1",
        }
        if cookies:
            fd, tmp = tempfile.mkstemp(suffix=".txt")
            os.close(fd)
            host = urllib.parse.urlparse(url).hostname or ""
            dom = "." + ".".join(host.split(".")[-2:]) if "." in host else host
            write_netscape([dict(c, domain=c.get("domain") or dom) for c in cookies], tmp)
            opts["cookiefile"] = tmp
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
        if not info:
            return None
        if info.get("entries"):
            info = next((e for e in info["entries"] if e), None) or info
        fmt = (info.get("requested_formats") or [info])[0]
        u = fmt.get("url") or info.get("url")
        if not u:
            return None
        hh = dict(fmt.get("http_headers") or info.get("http_headers") or {})
        return {"url": u, "headers": hh, "cover": info.get("thumbnail") or "", "title": info.get("title") or "",
                "images": [], "via": "yt-dlp(%s)" % (info.get("extractor_key") or "?")}
    except Exception:  # noqa: BLE001
        return None
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# --------------------------------------------------------------------------
# 抽帧
# --------------------------------------------------------------------------
def _hdr_args(headers: Dict[str, str]) -> List[str]:
    if not headers:
        return []
    ua = headers.get("User-Agent") or headers.get("user-agent")
    rest = "".join("%s: %s\r\n" % (k, v) for k, v in headers.items() if k.lower() != "user-agent")
    args: List[str] = []
    if ua:
        args += ["-user_agent", ua]
    if rest:
        args += ["-headers", rest]
    return args


def _probe_duration(ff: str, src: str, hdr: List[str]) -> float:
    try:
        p = subprocess.run([ff, "-hide_banner", *hdr, "-i", src], capture_output=True, text=True, timeout=45)
        m = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", p.stderr or "")
        if m:
            return int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    except Exception:  # noqa: BLE001
        pass
    return 0.0


def _download(url: str, headers: Dict[str, str], dest: str, limit: int = MAX_DOWNLOAD) -> int:
    req = urllib.request.Request(url, headers=headers)
    n = 0
    with urllib.request.urlopen(req, timeout=25) as r, open(dest, "wb") as f:
        while n < limit:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            n += len(chunk)
    return n


def _jpeg(img: Image.Image, maxside: int = 1280) -> bytes:
    img = img.convert("RGB")
    img.thumbnail((maxside, maxside))
    b = io.BytesIO()
    img.save(b, "JPEG", quality=90)
    return b.getvalue()


def _frames_ffmpeg(ff: str, src: str, hdr: List[str], times: List[float], workdir: str) -> List[Tuple[float, Image.Image]]:
    out: List[Tuple[float, Image.Image]] = []
    for i, t in enumerate(times):
        dst = os.path.join(workdir, "f%02d.jpg" % i)
        try:
            subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", *hdr, "-ss", "%.2f" % t, "-i", src, "-frames:v", "1",
                            "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", dst],
                           capture_output=True, timeout=60)
            if os.path.exists(dst) and os.path.getsize(dst) > 1000:
                img = Image.open(dst)
                img.load()
                out.append((t, img))
        except Exception:  # noqa: BLE001
            continue
    return out


def _frames_cv2(path: str, times: List[float]) -> List[Tuple[float, Image.Image]]:
    out: List[Tuple[float, Image.Image]] = []
    try:
        import cv2  # type: ignore
    except Exception:  # noqa: BLE001
        return out
    cap = cv2.VideoCapture(path)
    try:
        for t in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, t * 1000.0)
            ok, fr = cap.read()
            if ok and fr is not None:
                out.append((t, Image.fromarray(cv2.cvtColor(fr, cv2.COLOR_BGR2RGB))))
    finally:
        cap.release()
    return out


def extract_frames(src_url: str, headers: Dict[str, str], n: int = 6) -> Tuple[List[Tuple[float, bytes]], str]:
    """
    视频地址（http(s) 或本地路径）→ [(时间点秒, JPEG 字节)]，以及一句说明（时长 / 失败原因）。同步函数，放线程里跑。
    mp4：先下载（≤150MB）再本地抽帧（快且稳）；m3u8：直接让 ffmpeg 拉流抽帧。
    """
    n = max(1, min(24, int(n)))
    ff = ffmpeg_path()
    work = tempfile.mkdtemp(prefix="vf_")
    try:
        is_http = src_url.lower().startswith(("http://", "https://"))
        is_hls = is_http and "m3u8" in src_url.lower()
        local: Optional[str] = None
        note = ""
        if not is_http:
            local = src_url
        elif not is_hls:
            local = os.path.join(work, "v.bin")
            try:
                size = _download(src_url, headers, local)
                if size < 3000:
                    local = None
                    note = "视频下载内容过小（可能被防盗链拦截）"
            except Exception as e:  # noqa: BLE001
                local = None
                note = "下载失败：%s" % str(e)[:80]
        src = local or src_url
        if not ff and not (local and has_cv2()):
            return [], note or "没有 ffmpeg / OpenCV，无法抽帧（运行 Cell 1 会自动安装）"
        hdr = [] if local else _hdr_args(headers)
        dur = _probe_duration(ff, src, hdr) if ff else 0.0
        if dur <= 0 and local and has_cv2():
            try:
                import cv2  # type: ignore

                cap = cv2.VideoCapture(local)
                fps = cap.get(cv2.CAP_PROP_FPS) or 0
                cnt = cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0
                cap.release()
                dur = cnt / fps if fps > 0 and cnt > 0 else 0.0
            except Exception:  # noqa: BLE001
                dur = 0.0
        if dur <= 0:
            times = [0.5, 2.0, 4.0, 8.0, 15.0, 30.0][:n]
        else:
            k = n if dur >= 3 else min(n, 2)
            times = [max(0.2, min(dur - 0.3, dur * (i + 0.5) / k)) for i in range(k)]
        raw: List[Tuple[float, Image.Image]] = []
        if ff:
            raw = _frames_ffmpeg(ff, src, hdr, times, work)
        if not raw and local and has_cv2():
            raw = _frames_cv2(local, times)
        if not raw:
            return [], note or "ffmpeg 无法解码该视频（可能是加密/DRM 流）"
        kept: List[Tuple[float, bytes]] = []
        hashes: List[int] = []
        for t, img in raw:
            if _bad_frame(img):
                continue
            h = _dhash(img)
            if any(bin(h ^ o).count("1") <= 2 for o in hashes):
                continue
            hashes.append(h)
            kept.append((round(t, 1), _jpeg(img)))
        return kept, ("时长 %.0fs" % dur if dur else "时长未知") + ("" if kept else "，但所有帧都是黑屏/纯色")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def fetch_bytes(url: str, headers: Dict[str, str], limit: int = 8 * 1024 * 1024) -> Optional[bytes]:
    """下载封面/图集图片（同步）"""
    try:
        data, _ = _http(url, headers, timeout=20, limit=limit)
        return data
    except Exception:  # noqa: BLE001
        return None


def selftest() -> str:
    """Cell 1 自检：用 ffmpeg 现场生成一个 2 秒测试视频，再抽帧，证明「视频→截图」链路可用"""
    ff = ffmpeg_path()
    if not ff:
        return "❌ 没有 ffmpeg"
    d = tempfile.mkdtemp(prefix="vf_t_")
    try:
        p = os.path.join(d, "t.mp4")
        r = subprocess.run([ff, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i",
                            "testsrc=size=320x240:rate=10:duration=2", "-pix_fmt", "yuv420p", p], capture_output=True, timeout=60)
        if not os.path.exists(p):
            return "❌ ffmpeg 不能编码测试视频：" + (r.stderr or b"").decode("utf-8", "ignore")[-120:]
        frames, note = extract_frames(p, {}, 2)
        return "✅ ffmpeg 抽帧自检通过（%d 帧）" % len(frames) if frames else "❌ 抽帧自检失败：" + note
    except Exception as e:  # noqa: BLE001
        return "❌ 抽帧自检异常：%s" % str(e)[:100]
    finally:
        shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    print(capabilities(), time.strftime("%H:%M:%S"))
    print(selftest())
