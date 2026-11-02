"""
图片审查系统 v2.1 - Kaggle GPU 服务端 (FastAPI)

设计要点（第一性原则：吞吐 = GPU 利用率 × 并行度；准确率 = 多信号交叉验证 × 区域级覆盖）
1. T4 x2：每张 GPU 各加载一份完整模型副本（数据并行），请求由空闲副本抢占处理。
   每个副本内部再分 3 条并行通道：GPU 主通道(分类器/CLIP/检测器) | VLM 通道(大模型) | CPU 通道(NudeNet/OCR/二维码)。
2. 七路信号融合（任何一路失败都只会降级，不会拖垮整体）：
   - NSFW 分类器 x2 (Falconsai + AdamCodd ViT)          -> 整图 + 分块涉黄概率
   - CLIP 零样本 (clip-vit-large-patch14)                -> 涉黄 / 涉政 / 暴力 / 未成年 / 场景
   - NudeNet 身体部位检测 (ONNX, 带框)                    -> 裸露部位精确定位
   - OWLv2 开放词汇检测 (带框)                            -> 旗帜/横幅/极端标志/枪械/刀具/血迹/军事装备
   - RapidOCR 文字识别 (带框) + 二维码 + 联系方式         -> 敏感词 / 引流线索
   - 视觉大模型 VLM (Qwen2.5-VL-3B，带自检，失败自动回退 Qwen2-VL-2B) -> 中文描述 + 综合判断
   - 区域分块分析（滑窗/九宫格）                          -> 避免 CLIP 中心裁剪丢内容、发现小面积违规区域
3. 结论 verdict: pass(通过) / review(需人工复核) / block(拦截)。宁可多复核，不轻易放过。
   v2.0 的所有新信号只会"追加"证据，不会让 v1.1 已拦截/复核的图片变成放行。
4. 同一张图片(按 SHA1)结果缓存，批量重复图片秒回。
5. 环境变量 MOCK=1 时不加载任何模型，仅用于接口联调。
6. v2.1 新增「自动爬取并审核网络照片」（crawler.py）：Playwright 无头浏览器抓图/截图（抖音、小红书、微博、B站、百度/必应图片、任意网址），
   抓到即审，流水线并发数 = 副本数x3，两张 T4 同时满载。接口：POST /api/crawl/start、GET /api/crawl/{id}、POST /api/crawl/{id}/stop。

环境变量:
  PORT(8000)  PROFILE=fast|balanced|max   (balanced 默认；max=7B 两卡切分)
  VLM_MODELS=a,b,c  (按顺序尝试，自检失败自动换下一个)   VLM_MODEL(兼容旧版，单个)
  NSFW_MODELS=a,b   CLIP_MODEL  OWL_MODEL  VLM_MAX_PIXELS  VLM_SHARD=1
  DISABLE_VLM=1 DISABLE_OWL=1 DISABLE_NUDENET=1 DISABLE_OCR=1 DISABLE_TILES=1  MOCK=1
  POLITICAL_KEYWORDS=词1,词2   PORN_KEYWORDS=词1,词2   (追加自定义关键词)
"""
import asyncio
import hashlib
import io
import json
import logging
import math
import os
import re
import threading
import time
import urllib.request
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageDraw, ImageOps

try:  # v2.1 爬取模块：缺失或异常只会让爬取功能不可用，不影响审核主流程
    import crawler
except Exception as _e:  # noqa: BLE001
    crawler = None  # type: ignore
    print("crawler 模块加载失败（爬取功能不可用）:", _e)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("moderation")

BASE_DIR = Path(__file__).resolve().parent
VERSION = "2.1.0"
MOCK = os.getenv("MOCK") == "1"
PROFILE = os.getenv("PROFILE", "balanced").lower()
MAX_BYTES = 40 * 1024 * 1024


def _csv(name: str, default: str) -> List[str]:
    return [x.strip() for x in os.getenv(name, default).split(",") if x.strip()]


# ---- 视觉大模型候选链：按顺序尝试，自检(Self-test)失败/加载失败自动换下一个 ----
_FALLBACK_VLM = "Qwen/Qwen2-VL-2B-Instruct"  # v1.1 已验证可用，作为最后兜底
if os.getenv("VLM_MODELS"):
    VLM_MODELS = _csv("VLM_MODELS", "")
elif os.getenv("VLM_MODEL"):  # 兼容旧版环境变量
    VLM_MODELS = [os.environ["VLM_MODEL"]]
    if _FALLBACK_VLM not in VLM_MODELS:
        VLM_MODELS.append(_FALLBACK_VLM)
elif PROFILE == "fast":
    VLM_MODELS = [_FALLBACK_VLM]
elif PROFILE == "max":
    VLM_MODELS = ["Qwen/Qwen2.5-VL-7B-Instruct", "Qwen/Qwen2.5-VL-3B-Instruct", _FALLBACK_VLM]
else:
    VLM_MODELS = ["Qwen/Qwen2.5-VL-3B-Instruct", _FALLBACK_VLM]
VLM_SHARD = os.getenv("VLM_SHARD") == "1" or PROFILE == "max"  # 7B 跨两张卡切分
NSFW_MODELS = _csv("NSFW_MODELS", "Falconsai/nsfw_image_detection,AdamCodd/vit-base-nsfw-detector")
CLIP_MODEL = os.getenv("CLIP_MODEL", "openai/clip-vit-large-patch14")
OWL_MODEL = os.getenv("OWL_MODEL", "google/owlv2-base-patch16-ensemble")
OWL_THRESHOLD = float(os.getenv("OWL_THRESHOLD", "0.2"))
VLM_MAX_PIXELS = int(os.getenv("VLM_MAX_PIXELS", str(640 * 28 * 28)))
DISABLE_VLM = os.getenv("DISABLE_VLM") == "1"
DISABLE_OWL = os.getenv("DISABLE_OWL") == "1"
DISABLE_NUDENET = os.getenv("DISABLE_NUDENET") == "1"
DISABLE_OCR = os.getenv("DISABLE_OCR") == "1"
DISABLE_TILES = os.getenv("DISABLE_TILES") == "1"
NUDENET_640M_URL = "https://github.com/notAI-tech/NudeNet/releases/download/v3.4-weights/640m.onnx"

# 【关键修复(v1.1)】transformers/accelerate 的"meta 设备初始化"是进程级全局状态，
# 两个线程同时 from_pretrained 会互相污染 -> "Cannot copy out of meta tensor"。
# 所以所有 from_pretrained 必须串行（全局锁），并且加载后校验不能残留 meta 张量。
LOAD_LOCK = threading.RLock()
_VLM_START = 0  # 第一张卡确定可用的 VLM 下标，第二张卡直接从这里开始，避免重复下载/重复失败


def _dtype_key() -> str:
    """transformers>=4.56 用 dtype，旧版本用 torch_dtype（旧版传 dtype 会被静默当成 config 参数，导致仍是 fp32）"""
    try:
        import transformers
        nums = re.findall(r"\d+", transformers.__version__)[:2]
        return "dtype" if (int(nums[0]), int(nums[1])) >= (4, 56) else "torch_dtype"
    except Exception:
        return "torch_dtype"


def _has_meta(model) -> bool:
    try:
        return any(getattr(p, "is_meta", False) or p.device.type == "meta" for p in model.parameters()) or any(
            b.device.type == "meta" for b in model.buffers())
    except Exception:
        return False


# --------------------------------------------------------------------------
# 关键词 / 联系方式
# --------------------------------------------------------------------------
DEFAULT_KEYWORDS = [
    "法轮功", "法轮大法", "六四", "天安门事件", "藏独", "疆独", "台独", "港独",
    "反共", "打倒共产党", "东突", "伊斯兰国", "纳粹", "卐",
    "全能神", "东方闪电", "李洪志", "光复香港", "时代革命", "天安门屠杀", "八九六四", "新疆独立", "西藏独立",
]
POLITICAL_KEYWORDS = DEFAULT_KEYWORDS + _csv("POLITICAL_KEYWORDS", "")
DEFAULT_PORN_KEYWORDS = [
    "约炮", "裸聊", "援交", "一夜情", "色情", "成人视频", "包夜", "上门服务", "楼凤",
    "性服务", "嫖娼", "黄色网站", "成人网站", "无码", "私密直播", "福利姬", "裸照",
]
PORN_KEYWORDS = DEFAULT_PORN_KEYWORDS + _csv("PORN_KEYWORDS", "")

CONTACT_RES = [
    (re.compile(r"(?:微信|薇信|威信|vx|v信|wx|weixin|wechat)[\s:：=+＋]*([A-Za-z][-_A-Za-z0-9]{5,19})", re.I), "微信"),
    (re.compile(r"(?<![A-Za-z])(?:qq|扣扣|企鹅)[\s:：=]*([1-9]\d{4,10})", re.I), "QQ"),
    (re.compile(r"(?<!\d)(1[3-9]\d{9})(?!\d)"), "手机号"),
    (re.compile(r"(?:telegram|tg|飞机|纸飞机)[\s:：=@]*([A-Za-z][A-Za-z0-9_]{4,31})", re.I), "Telegram"),
    (re.compile(r"(https?://[^\s]+|(?:[a-z0-9-]+\.)+(?:com|cn|net|xyz|top|cc|vip|me|co)\b[^\s]*)", re.I), "网址"),
]


def find_contacts(text: str) -> List[str]:
    out: List[str] = []
    for rx, kind in CONTACT_RES:
        for m in rx.finditer(text or ""):
            item = "%s:%s" % (kind, m.group(1)[:40])
            if item not in out:
                out.append(item)
    return out[:8]


def find_keywords(text: str, words: List[str]) -> List[str]:
    t = (text or "").lower()
    return [k for k in dict.fromkeys(words) if k and k.lower() in t]


# --------------------------------------------------------------------------
# CLIP 零样本提示词（英文效果最好）。 neutral 用来"吸收"正常图片的概率质量，
# 这样 porn / political 的概率才有意义，而不是在互相比较中被放大。
# 分组 softmax：A组(中性+涉黄+性暗示+涉政) 与 v1.1 完全一致，保证原有阈值校准不变；
#              B组(中性+暴力+未成年+成年) 为 v2.0 新增，单独归一化。
# --------------------------------------------------------------------------
NEUTRAL = [
    ("a photo of a landscape or nature scenery", "风景/自然"),
    ("a photo of a person wearing normal clothes", "穿着正常的人物"),
    ("a photo of food or drinks", "食物/饮品"),
    ("a photo of an animal or pet", "动物/宠物"),
    ("a photo of a building or city street", "建筑/街景"),
    ("a screenshot of a website, app or chat", "网页/应用截图"),
    ("a document, poster or page full of text", "文档/文字"),
    ("a cartoon, anime or illustration", "卡通/插画"),
    ("a photo of a product or object", "物品/商品"),
    ("a photo of a vehicle", "交通工具"),
    ("a photo of a child or a family", "儿童/家庭"),
    ("a photo of a sports activity", "体育运动"),
]
PORN = [
    "explicit pornography",
    "a completely naked person",
    "exposed female breasts or nipples",
    "sexual intercourse",
    "exposed genitals",
]
SUGGESTIVE = [
    "a woman in lingerie or bikini in a sexy pose",
    "a sexually suggestive photo",
]
POLITICAL = [
    ("a portrait of a political leader", "政治人物肖像"),
    ("a political protest with crowds and banners", "游行示威"),
    ("a political propaganda poster", "政治宣传画"),
    ("a national flag", "旗帜"),
    ("a military parade", "阅兵/军事场景"),
    ("a government meeting with officials", "官方会议"),
    ("an extremist or terrorist flag or symbol", "极端/恐怖组织标志"),
    ("a political slogan banner", "政治标语"),
    ("Tiananmen Square", "天安门广场"),
]
VIOLENCE = [
    "a person holding a gun or weapon",
    "a bloody violent scene with injuries or a dead body",
    "a terrorist attack or explosion",
    "a physical fight or assault",
]
MINOR = ["a photo of a young child", "a photo of a baby or toddler"]
ADULT = ["a photo of an adult person"]

CLIP_PROMPTS = ([p for p, _ in NEUTRAL] + PORN + SUGGESTIVE + [p for p, _ in POLITICAL]
                + VIOLENCE + MINOR + ADULT)
N_NEU, N_PORN, N_SUG, N_POL = len(NEUTRAL), len(PORN), len(SUGGESTIVE), len(POLITICAL)
N_A = N_NEU + N_PORN + N_SUG + N_POL
IDX_A = list(range(N_A))
IDX_B = list(range(N_NEU)) + list(range(N_A, len(CLIP_PROMPTS)))

# OWLv2 开放词汇检测：(英文查询, 中文名, 类别)
OWL_QUERIES: List[Tuple[str, str, str]] = [
    ("a photo of a national flag", "旗帜", "flag"),
    ("a photo of a protest banner with slogan", "标语横幅", "banner"),
    ("a photo of a political propaganda poster", "宣传海报", "banner"),
    ("a photo of a swastika or extremist symbol", "极端标志", "extremist"),
    ("a photo of a gun", "枪械", "weapon"),
    ("a photo of a knife", "刀具", "weapon"),
    ("a photo of a pool of blood", "血迹", "gore"),
    ("a photo of a military tank or armored vehicle", "军事装备", "military"),
    ("a photo of a person in military uniform", "军人", "military"),
]

NUDE_CN = {
    "FEMALE_GENITALIA_EXPOSED": "女性生殖器裸露", "MALE_GENITALIA_EXPOSED": "男性生殖器裸露",
    "ANUS_EXPOSED": "肛门裸露", "FEMALE_BREAST_EXPOSED": "女性乳房裸露", "BUTTOCKS_EXPOSED": "臀部裸露",
    "FEMALE_BREAST_COVERED": "女性胸部(遮挡)", "FEMALE_GENITALIA_COVERED": "女性下体(遮挡)",
    "MALE_GENITALIA_COVERED": "男性下体(遮挡)", "BUTTOCKS_COVERED": "臀部(遮挡)",
}
# 触发涉黄证据的类别: (阈值, 分组)
NUDE_KEY = {
    "FEMALE_GENITALIA_EXPOSED": (0.35, "genital"), "MALE_GENITALIA_EXPOSED": (0.35, "genital"),
    "ANUS_EXPOSED": (0.35, "genital"), "FEMALE_BREAST_EXPOSED": (0.5, "body"), "BUTTOCKS_EXPOSED": (0.5, "body"),
}

VLM_PROMPT = (
    "你是一名严谨的内容安全审核员。请仔细观察这张图片，只输出一个 JSON 对象，不要输出任何其他文字。\n"
    '格式: {"description":"用一句中文简要描述画面(不超过50字)",'
    '"porn":"none|suggestive|explicit","political":"none|low|high","violence":"none|low|high",'
    '"minors":false,"political_reasons":["涉政原因，没有则为空数组"],"text":"图中清晰可见的主要文字，没有则为空字符串"}\n'
    "porn 判定: explicit=裸露生殖器/乳头或性行为; suggestive=内衣泳装性暗示、挑逗姿势、大面积裸露; none=正常。\n"
    "political 判定: high=敏感政治人物或事件、游行示威、分裂/邪教/极端组织标志、攻击性政治标语; "
    "low=国旗、官方会议、一般政治相关内容; none=与政治无关。\n"
    "violence 判定: high=血腥、尸体、持枪/持刀行凶、恐怖袭击; low=出现武器但无暴力行为; none=无。\n"
    "minors: 画面中是否出现疑似未成年人，true 或 false。"
)


# --------------------------------------------------------------------------
# 图像工具
# --------------------------------------------------------------------------
def load_image(raw: bytes) -> Image.Image:
    img = Image.open(io.BytesIO(raw))
    img.load()
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, (255, 255, 255))
        bg.paste(img, mask=img.split()[-1])
        img = bg
    else:
        img = img.convert("RGB")
    img.thumbnail((1280, 1280))
    return img


def tile_boxes(w: int, h: int) -> List[Tuple[float, float, float, float]]:
    """区域分块（归一化坐标）。
    - 宽/高图 (长宽比>=1.4)：用 短边x短边 的方形窗口沿长边滑动，保证整幅图都被 CLIP 的方形输入覆盖（CLIP 会中心裁剪，
      不分块的话 16:9 的图会丢掉两侧约 44% 的内容）。
    - 近似方形：四角 + 中心，各占 62%，用来发现小面积违规区域。
    - 小图 (<480px)：不分块。
    """
    if DISABLE_TILES or max(w, h) < 480:
        return []
    short, long_ = min(w, h), max(w, h)
    ar = long_ / float(short)
    boxes: List[Tuple[float, float, float, float]] = []
    if ar >= 1.4:
        n = min(4, max(2, int(math.ceil(ar))))
        for i in range(n):
            off = (long_ - short) * i / float(n - 1)
            if w >= h:
                boxes.append((off / w, 0.0, (off + short) / w, 1.0))
            else:
                boxes.append((0.0, off / h, 1.0, (off + short) / h))
    else:
        s = 0.62
        o = 1.0 - s
        for (x, y) in ((0.0, 0.0), (o, 0.0), (0.0, o), (o, o), (o / 2, o / 2)):
            boxes.append((x, y, x + s, y + s))
    return boxes


def make_crops(img: Image.Image) -> Tuple[List[Image.Image], List[Tuple[float, float, float, float]]]:
    w, h = img.size
    boxes = tile_boxes(w, h)
    crops = [img]
    for (x0, y0, x1, y1) in boxes:
        crops.append(img.crop((int(x0 * w), int(y0 * h), max(int(x0 * w) + 8, int(x1 * w)), max(int(y0 * h) + 8, int(y1 * h)))))
    return crops, boxes


def _iou(a: List[float], b: List[float]) -> float:
    ix0, iy0, ix1, iy1 = max(a[0], b[0]), max(a[1], b[1]), min(a[2], b[2]), min(a[3], b[3])
    inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    ua = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / ua if ua > 0 else 0.0


def nms(dets: List[Dict[str, Any]], thr: float = 0.5, per_class: int = 4) -> List[Dict[str, Any]]:
    dets = sorted(dets, key=lambda d: -d["score"])
    keep: List[Dict[str, Any]] = []
    for d in dets:
        if sum(1 for k in keep if k["label"] == d["label"]) >= per_class:
            continue
        if any(k["label"] == d["label"] and _iou(k["box"], d["box"]) > thr for k in keep):
            continue
        keep.append(d)
    return keep


def is_degenerate(text: str) -> bool:
    """VLM 在 T4(fp16) 上数值溢出时常见症状：输出为空 或 全是 '!!!!'。"""
    t = (text or "").strip()
    if not t:
        return True
    return re.search(r"[A-Za-z0-9\u4e00-\u9fff]", t) is None


def parse_vlm_json(text: str) -> Dict[str, Any]:
    text = (text or "").strip()
    data: Dict[str, Any] = {}
    m = re.search(r"\{.*\}", text, re.S)
    if m:
        try:
            data = json.loads(m.group(0))
        except Exception:
            data = {}
    if not data:
        # 容错：用正则抽取字段
        def pick(key: str) -> str:
            mm = re.search(r'"%s"\s*:\s*"([^"]*)"' % key, text)
            return mm.group(1) if mm else ""

        data = {
            "description": pick("description") or text[:60],
            "porn": pick("porn"),
            "political": pick("political"),
            "violence": pick("violence"),
            "text": pick("text"),
            "minors": bool(re.search(r'"minors"\s*:\s*true', text, re.I)),
            "political_reasons": [],
        }
    porn = str(data.get("porn", "")).lower()
    pol = str(data.get("political", "")).lower()
    vio = str(data.get("violence", "")).lower()
    data["porn"] = "explicit" if "explicit" in porn else "suggestive" if "sugg" in porn else "none"
    data["political"] = "high" if "high" in pol else "low" if "low" in pol else "none"
    data["violence"] = "high" if "high" in vio else "low" if "low" in vio else "none"
    mn = data.get("minors", False)
    data["minors"] = (mn is True) or (isinstance(mn, str) and mn.strip().lower() in ("true", "yes", "是"))
    reasons = data.get("political_reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    data["political_reasons"] = [str(r) for r in reasons if str(r).strip()][:5]
    data["description"] = str(data.get("description", "")).strip()
    data["text"] = str(data.get("text", "")).strip()
    return data


# --------------------------------------------------------------------------
# 多信号融合
# --------------------------------------------------------------------------
def fuse(nsfw_p: float, clip: Dict[str, Any], vlm: Optional[Dict[str, Any]],
         aux: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """多信号融合 -> porn / political / violence / verdict / reasons。
    规则分两层：①v1.1 原规则（原样保留，保证已有成功行为不变）②v2.0 新增证据（只追加，不会让原本拦截的变放行）。"""
    aux = aux or {}
    reasons: List[str] = []
    clip_porn = clip["porn"]
    clip_sugg = clip["suggestive"]
    clip_pol = clip["political"]
    clip_vio = clip.get("violence", 0.0)
    vporn = vlm["porn"] if vlm else None
    vpol = vlm["political"] if vlm else None
    vvio = vlm.get("violence") if vlm else None
    ocr_text = aux.get("ocr_text", "")
    text = " ".join([(vlm or {}).get("text", ""), (vlm or {}).get("description", ""), ocr_text,
                     " ".join(aux.get("qr", []))])
    kw_hits = find_keywords(text, POLITICAL_KEYWORDS)
    porn_kw = find_keywords(text, PORN_KEYWORDS)
    contacts: List[str] = aux.get("contacts", [])
    nude: List[Dict[str, Any]] = aux.get("nude", [])
    objs: List[Dict[str, Any]] = aux.get("objects", [])

    # ---- NudeNet 身体部位证据 ----
    genital = [d["score"] for d in nude if NUDE_KEY.get(d["class"], (9, ""))[1] == "genital"
               and d["score"] >= NUDE_KEY[d["class"]][0]]
    body = [d["score"] for d in nude if NUDE_KEY.get(d["class"], (9, ""))[1] == "body"
            and d["score"] >= NUDE_KEY[d["class"]][0]]
    nude_genital = max(genital, default=0.0)
    nude_body = max(body, default=0.0)
    nude_any = max(nude_genital, nude_body)

    # ---- 涉黄 ----
    vmap = {"none": 0.02, "suggestive": 0.6, "explicit": 0.97}
    porn_score = max(nsfw_p, clip_porn, clip_sugg * 0.8, vmap.get(vporn or "none", 0.0),
                     nude_any * 0.95, 0.6 if porn_kw else 0.0)
    explicit = (
        nsfw_p >= 0.9
        or clip_porn >= 0.8
        or (vporn == "explicit" and (nsfw_p >= 0.4 or clip_porn >= 0.3))
        # v2.0：NudeNet 精确定位到裸露部位，并有任一其他信号佐证
        or (nude_genital >= 0.5 and (nsfw_p >= 0.25 or clip_porn >= 0.15 or vporn in ("suggestive", "explicit")))
        or nude_genital >= 0.75
        or (nude_body >= 0.5 and nsfw_p >= 0.5)
        or (nude_body >= 0.7 and vporn in ("suggestive", "explicit"))
    )
    suggestive = (
        nsfw_p >= 0.5 or (clip_porn + clip_sugg) >= 0.5 or vporn in ("suggestive", "explicit")
        or nude_any > 0 or bool(porn_kw)
    )
    porn_level = "explicit" if explicit else "suggestive" if suggestive else "none"
    minors = bool(vlm and vlm.get("minors")) or clip.get("minor", 0.0) >= 0.6
    if minors and porn_level == "suggestive" and (nsfw_p >= 0.5 or nude_any > 0):
        porn_level = "explicit"
        reasons.append("涉黄：画面疑似涉及未成年人且含性相关内容，已升级拦截")
    if porn_level == "explicit":
        extra = []
        if vporn == "explicit":
            extra.append("大模型确认")
        if nude_any:
            extra.append("检测到裸露部位")
        reasons.append("涉黄：疑似色情/裸露内容（分类器 %.0f%%，CLIP %.0f%%%s）" % (
            nsfw_p * 100, clip_porn * 100, ("，" + "，".join(extra)) if extra else ""))
    elif porn_level == "suggestive":
        reasons.append("涉黄：疑似低俗/性暗示内容，建议人工复核（分类器 %.0f%%）" % (nsfw_p * 100))
    if porn_kw:
        reasons.append("涉黄：图中文字命中敏感词 " + "、".join(porn_kw[:5]))
    if contacts and (porn_level != "none" or porn_kw):
        reasons.append("涉黄：疑似引流（图中含联系方式 %s）" % "、".join(contacts[:3]))

    # ---- 涉政 ----
    pmap = {"none": 0.03, "low": 0.5, "high": 0.9}
    obj_pol = max([o["score"] for o in objs if o["cat"] in ("extremist", "banner")], default=0.0)
    obj_ext = max([o["score"] for o in objs if o["cat"] == "extremist"], default=0.0)
    pol_score = max(clip_pol, pmap.get(vpol or "none", 0.0), 0.95 if kw_hits else 0.0, obj_pol * 0.9)
    if kw_hits or (vpol == "high" and clip_pol >= 0.3):
        pol_level = "high"
    elif vpol in ("high", "low") or clip_pol >= 0.5 or obj_ext >= 0.3 or obj_pol >= 0.4:
        pol_level = "low"
    else:
        pol_level = "none"
    labels = list(clip.get("political_labels", []))
    labels += [o["cn"] for o in objs if o["cat"] in ("flag", "banner", "extremist", "military")]
    if vlm:
        labels = list(vlm.get("political_reasons", [])) + labels
    labels = list(dict.fromkeys(labels))
    if kw_hits:
        reasons.append("涉政：图中文字命中敏感词 " + "、".join(kw_hits))
    if pol_level == "high":
        reasons.append("涉政：疑似敏感政治内容（%s）" % ("、".join(labels[:3]) or "大模型判定"))
    elif pol_level == "low":
        reasons.append("涉政：含政治相关元素，建议人工复核（%s）" % ("、".join(labels[:3]) or "CLIP 倾向"))

    # ---- 暴力血腥（v2.0 新增，仅提示/复核，不会单独拦截） ----
    wea = max([o["score"] for o in objs if o["cat"] == "weapon"], default=0.0)
    gore = max([o["score"] for o in objs if o["cat"] == "gore"], default=0.0)
    vio_score = max(clip_vio, {"none": 0.02, "low": 0.5, "high": 0.9}.get(vvio or "none", 0.0), wea * 0.8, gore)
    if (vvio == "high" and (clip_vio >= 0.3 or wea >= 0.3 or gore >= 0.3)) or clip_vio >= 0.85 or gore >= 0.5:
        vio_level = "high"
    elif vvio in ("high", "low") or clip_vio >= 0.5 or wea >= 0.4:
        vio_level = "low"
    else:
        vio_level = "none"
    if vio_level == "high":
        reasons.append("暴力：疑似血腥/暴力内容，建议人工复核")
    elif vio_level == "low":
        reasons.append("暴力：出现武器或暴力倾向，仅提示")

    if porn_level == "explicit" or pol_level == "high":
        verdict = "block"
    elif porn_level == "suggestive" or pol_level == "low" or vio_level == "high":
        verdict = "review"
    else:
        verdict = "pass"
    return {
        "verdict": verdict,
        "porn": {"score": round(min(porn_score, 1.0), 4), "level": porn_level},
        "political": {"score": round(min(pol_score, 1.0), 4), "level": pol_level, "labels": labels[:6]},
        "violence": {"score": round(min(vio_score, 1.0), 4), "level": vio_level},
        "reasons": reasons,
        "keywords": {"political": kw_hits, "porn": porn_kw},
        "minors_suspected": bool(minors),
    }


# --------------------------------------------------------------------------
# NudeNet 模型文件（640m 更准，下载并校验失败则回退内置 320n）
# --------------------------------------------------------------------------
def ensure_nude_640m() -> Optional[str]:
    p = BASE_DIR / "models" / "640m.onnx"
    try:
        if p.exists() and p.stat().st_size > 50 * 1024 * 1024:
            return str(p)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = str(p) + ".part"
        urllib.request.urlretrieve(NUDENET_640M_URL, tmp)
        if os.path.getsize(tmp) < 50 * 1024 * 1024:  # 被代理/网络错误页替换了
            os.remove(tmp)
            return None
        os.replace(tmp, str(p))
        return str(p)
    except Exception as e:  # noqa: BLE001
        log.warning("NudeNet 640m 下载失败，将使用内置 320n: %s", str(e)[:120])
        return None


# --------------------------------------------------------------------------
# 模型副本（每张 GPU 一份）
# --------------------------------------------------------------------------
class Replica:
    def __init__(self, idx: int, device: str):
        self.idx = idx
        self.device = device
        self.executor = ThreadPoolExecutor(max_workers=1)      # 本副本的"主通道"：一次只处理一张图
        self.vlm_exec = ThreadPoolExecutor(max_workers=1)      # VLM 通道（Qwen-VL 内部有可变状态，单线程最安全）
        self.cpu_exec = ThreadPoolExecutor(max_workers=2)      # CPU 工具通道：NudeNet / OCR / 二维码
        self.torch = None
        self.clfs: List[Dict[str, Any]] = []
        self.clip = None
        self.clip_proc = None
        self.txt_feats = None
        self.owl = None
        self.owl_proc = None
        self.owl_ids = None
        self.nude = None
        self.nude_name: Optional[str] = None
        self.ocr = None
        self.vlm = None
        self.proc = None
        self.has_vlm = False
        self.vlm_name: Optional[str] = None
        self.vlm_idx = -1
        self.vlm_bad = 0
        self.ready = False
        self.busy = False
        self.done = 0
        self.total_ms = 0
        self.notes: List[str] = []
        self.modules: Dict[str, Any] = {}

    # ---- 加载 ----
    def _load_small(self, cls, name: str, dtype):
        """加载小模型：先 CPU 完整加载 -> 校验无 meta -> 再搬到本卡。失败则关闭 low_cpu_mem_usage 重试。"""
        import gc
        key = _dtype_key()
        last: Optional[Exception] = None
        for attempt, extra in enumerate(({}, {"low_cpu_mem_usage": False})):
            try:
                with LOAD_LOCK:
                    m = cls.from_pretrained(name, **{key: dtype}, **extra)
                    if _has_meta(m):
                        raise RuntimeError("模型含 meta 张量（第 %d 次尝试）" % (attempt + 1))
                    m = m.to(self.device).eval()
                return m
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("[%s] 加载 %s 失败(第 %d 次): %s", self.device, name, attempt + 1, str(e)[:160])
                gc.collect()
                if self.torch is not None and self.torch.cuda.is_available():
                    self.torch.cuda.empty_cache()
        raise RuntimeError("加载 %s 失败: %s" % (name, last))

    def _note(self, msg: str) -> None:
        log.warning("[%s] %s", self.device, msg)
        self.notes.append(msg)

    def load(self) -> None:
        t0 = time.time()
        if MOCK:
            time.sleep(0.5)
            self.modules = {"classifier": ["mock"], "clip": "mock", "vlm": "mock", "detector": "mock",
                            "nudenet": "mock", "ocr": True, "qr": True}
            self.has_vlm = True
            self.vlm_name = "mock"
            self.ready = True
            return
        with LOAD_LOCK:  # transformers 的懒加载 import 同样不是线程安全的
            import torch
            from transformers import (AutoImageProcessor, AutoModelForImageClassification,
                                      CLIPModel, CLIPProcessor)

        self.torch = torch
        use_cuda = self.device.startswith("cuda")
        dtype = torch.float16 if use_cuda else torch.float32

        # 1) NSFW 分类器（第一个是必需的；其余是增强，失败只降级）
        for i, name in enumerate(NSFW_MODELS):
            try:
                log.info("[%s] 加载 NSFW 分类器 %s", self.device, name)
                with LOAD_LOCK:
                    proc = AutoImageProcessor.from_pretrained(name)
                model = self._load_small(AutoModelForImageClassification, name, dtype)
                idx = 1
                for k, lab in model.config.id2label.items():
                    if "nsfw" in str(lab).lower() or "porn" in str(lab).lower():
                        idx = int(k)
                self.clfs.append({"name": name, "model": model, "proc": proc, "idx": idx,
                                  "dtype": next(model.parameters()).dtype})
            except Exception as e:  # noqa: BLE001
                if i == 0:
                    raise
                self._note("附加分类器 %s 加载失败，已跳过: %s" % (name, str(e)[:100]))
        self.modules["classifier"] = [c["name"] for c in self.clfs]

        # 2) CLIP
        log.info("[%s] 加载 CLIP %s", self.device, CLIP_MODEL)
        with LOAD_LOCK:
            self.clip_proc = CLIPProcessor.from_pretrained(CLIP_MODEL)
        self.clip = self._load_small(CLIPModel, CLIP_MODEL, dtype)
        with torch.inference_mode():
            tok = self.clip_proc(text=CLIP_PROMPTS, return_tensors="pt", padding=True).to(self.device)
            tf = self.clip.get_text_features(**tok)
            tf = tf if torch.is_tensor(tf) else getattr(tf, "pooler_output", tf)
            self.txt_feats = tf / tf.norm(dim=-1, keepdim=True)
        self.modules["clip"] = CLIP_MODEL

        # 3) CPU 工具：NudeNet / OCR / 二维码（失败只降级）
        if not DISABLE_NUDENET:
            try:
                self._load_nudenet()
            except Exception as e:  # noqa: BLE001
                self._note("NudeNet 不可用（pip install nudenet）: %s" % str(e)[:100])
        self.modules["nudenet"] = self.nude_name
        if not DISABLE_OCR:
            try:
                from rapidocr_onnxruntime import RapidOCR
                self.ocr = RapidOCR()
            except Exception as e:  # noqa: BLE001
                self._note("OCR 不可用（pip install rapidocr_onnxruntime）: %s" % str(e)[:100])
        self.modules["ocr"] = self.ocr is not None
        try:
            import cv2  # noqa: F401
            self.modules["qr"] = True
        except Exception:
            self.modules["qr"] = False

        # 4) OWLv2 开放词汇检测器
        if use_cuda and not DISABLE_OWL:
            try:
                self._load_owl(dtype)
            except Exception as e:  # noqa: BLE001
                self.owl = None
                self._note("OWL 检测器加载失败，已跳过: %s" % str(e)[:100])
        self.modules["detector"] = OWL_MODEL if self.owl is not None else None

        # 5) 视觉大模型（带自检与回退链）
        if use_cuda and not DISABLE_VLM:
            try:
                self._load_vlm(_VLM_START)
            except Exception as e:  # 大模型失败不影响整体可用性（降级为快速模式）
                log.exception("[%s] VLM 加载失败，将仅使用分类器+CLIP+工具: %s", self.device, e)
                self._note("VLM 加载失败: %s" % str(e)[:120])
        self.modules["vlm"] = self.vlm_name if self.has_vlm else None
        self.ready = True
        log.info("[%s] 副本就绪，用时 %.1fs，模块=%s", self.device, time.time() - t0, self.modules)

    def _load_nudenet(self) -> None:
        from nudenet import NudeDetector
        path = ensure_nude_640m()
        blank = np.full((256, 256, 3), 128, np.uint8)
        if path:
            try:
                det = NudeDetector(model_path=path, inference_resolution=640)
                det.detect(blank)
                self.nude, self.nude_name = det, "640m"
                return
            except Exception as e:  # noqa: BLE001
                log.warning("NudeNet 640m 加载失败，回退 320n: %s", str(e)[:100])
        det = NudeDetector()
        det.detect(blank)
        self.nude, self.nude_name = det, "320n"

    def _load_owl(self, dtype) -> None:
        from transformers import AutoModelForZeroShotObjectDetection, AutoProcessor
        log.info("[%s] 加载检测器 %s", self.device, OWL_MODEL)
        with LOAD_LOCK:
            self.owl_proc = AutoProcessor.from_pretrained(OWL_MODEL)
        self.owl = self._load_small(AutoModelForZeroShotObjectDetection, OWL_MODEL, dtype)
        # 自检：空白图前向一次，确认形状与数值正常（fp16 下出现 NaN 则关闭）
        self._owl_detect(Image.new("RGB", (640, 480), (128, 128, 128)), selftest=True)

    def _load_vlm(self, start: int = 0) -> None:
        """按候选链加载 VLM；每个候选加载后都做一次自检，输出异常(fp16 溢出)则释放并换下一个。"""
        global _VLM_START
        import gc
        import torch
        last: Optional[Exception] = None
        for i in range(start, len(VLM_MODELS)):
            name = VLM_MODELS[i]
            try:
                self._load_vlm_one(name)
                self.vlm_selftest()
                self.has_vlm = True
                self.vlm_name = name
                self.vlm_idx = i
                self.vlm_bad = 0
                with LOAD_LOCK:
                    _VLM_START = max(_VLM_START, i)
                log.info("[%s] 视觉大模型就绪: %s", self.device, name)
                return
            except Exception as e:  # noqa: BLE001
                last = e
                self._note("VLM %s 不可用，尝试下一个: %s" % (name, str(e)[:110]))
                self.vlm = None
                self.proc = None
                self.has_vlm = False
                gc.collect()
                torch.cuda.empty_cache()
        raise RuntimeError("所有候选 VLM 都失败: %s" % last)

    def _load_vlm_one(self, name: str) -> None:
        import gc
        import torch
        from transformers import AutoProcessor
        if "Qwen2.5-VL" in name:
            from transformers import Qwen2_5_VLForConditionalGeneration as Auto
        elif "Qwen2-VL" in name:
            from transformers import Qwen2VLForConditionalGeneration as Auto
        else:
            try:
                from transformers import AutoModelForImageTextToText as Auto
            except ImportError:  # 旧版本 transformers
                from transformers import AutoModelForVision2Seq as Auto

        key = _dtype_key()
        shard = VLM_SHARD and bool(re.search(r"(7|32|72)B", name, re.I))
        log.info("[%s] 加载视觉大模型 %s%s", self.device, name, "（两卡切分）" if shard else "")
        last: Optional[Exception] = None
        for attempt in range(2):
            try:
                with LOAD_LOCK:
                    self.proc = AutoProcessor.from_pretrained(name, min_pixels=128 * 28 * 28, max_pixels=VLM_MAX_PIXELS)
                    if shard:
                        m = Auto.from_pretrained(
                            name, **{key: torch.float16}, device_map="auto",
                            max_memory={i: "13GiB" for i in range(torch.cuda.device_count())})
                    else:
                        idx = int(self.device.split(":")[1]) if ":" in self.device else 0
                        if attempt == 0:  # 直接加载到本卡（最快、最省内存）
                            m = Auto.from_pretrained(name, **{key: torch.float16}, device_map={"": idx},
                                                     attn_implementation="sdpa")
                        else:  # 兜底：CPU 完整加载后再搬运
                            m = Auto.from_pretrained(name, **{key: torch.float16}, low_cpu_mem_usage=False,
                                                     attn_implementation="sdpa").to(self.device)
                    if _has_meta(m):
                        raise RuntimeError("VLM 含 meta 张量")
                    self.vlm = m.eval()
                return
            except Exception as e:  # noqa: BLE001
                last = e
                log.warning("[%s] VLM %s 加载失败(第 %d 次): %s", self.device, name, attempt + 1, str(e)[:160])
                self.vlm = None
                gc.collect()
                torch.cuda.empty_cache()
        raise RuntimeError("VLM 加载失败: %s" % last)

    def vlm_selftest(self) -> None:
        im = Image.new("RGB", (448, 448), (255, 255, 255))
        ImageDraw.Draw(im).rectangle((80, 80, 370, 370), fill=(220, 30, 30))
        out = self.vlm_run(im, "这张图片里有什么颜色？用一句中文回答。", max_new_tokens=30)
        if is_degenerate(out):
            raise RuntimeError("自检输出异常(疑似 fp16 数值溢出): %r" % out[:40])
        log.info("[%s] VLM 自检通过: %s", self.device, out.strip()[:40])

    def _vlm_drop_and_fallback(self) -> None:
        """运行中连续输出异常：释放当前 VLM，尝试候选链的下一个；全部失败则关闭 VLM（其余信号照常工作）。"""
        import gc
        import torch
        bad = self.vlm_name
        self._note("VLM %s 连续输出异常，尝试回退" % bad)
        self.vlm = None
        self.proc = None
        self.has_vlm = False
        gc.collect()
        torch.cuda.empty_cache()
        try:
            self._load_vlm(self.vlm_idx + 1)
        except Exception as e:  # noqa: BLE001
            self._note("VLM 已关闭（无可用回退）: %s" % str(e)[:100])
        self.modules["vlm"] = self.vlm_name if self.has_vlm else None

    # ---- 推理：GPU 主通道 ----
    def _classify(self, crops: List[Image.Image]) -> Dict[str, List[float]]:
        torch = self.torch
        res: Dict[str, List[float]] = {}
        with torch.inference_mode():
            for n, c in enumerate(self.clfs):
                try:
                    inp = c["proc"](images=crops, return_tensors="pt")
                    pv = inp["pixel_values"].to(self.device, dtype=c["dtype"])
                    logits = c["model"](pixel_values=pv).logits.float()
                    res[c["name"]] = torch.softmax(logits, dim=-1)[:, c["idx"]].tolist()
                except Exception as e:  # noqa: BLE001
                    if n == 0:
                        raise
                    log.warning("附加分类器 %s 推理失败: %s", c["name"], str(e)[:100])
        return res

    def _clip(self, crops: List[Image.Image]) -> List[Dict[str, Any]]:
        torch = self.torch
        with torch.inference_mode():
            inp = self.clip_proc(images=crops, return_tensors="pt")
            pv = inp["pixel_values"].to(self.device, dtype=next(self.clip.parameters()).dtype)
            f = self.clip.get_image_features(pixel_values=pv)
            f = f if torch.is_tensor(f) else getattr(f, "pooler_output", f)
            f = f / f.norm(dim=-1, keepdim=True)
            logits = 100.0 * f.float() @ self.txt_feats.float().T
            pa = torch.softmax(logits[:, IDX_A], dim=-1).tolist()
            pb = torch.softmax(logits[:, IDX_B], dim=-1).tolist()
        out = []
        for a, b in zip(pa, pb):
            neutral = a[:N_NEU]
            porn = a[N_NEU:N_NEU + N_PORN]
            sugg = a[N_NEU + N_PORN:N_NEU + N_PORN + N_SUG]
            pol = a[N_NEU + N_PORN + N_SUG:]
            vio = b[N_NEU:N_NEU + len(VIOLENCE)]
            minor = b[N_NEU + len(VIOLENCE):N_NEU + len(VIOLENCE) + len(MINOR)]
            top_n = max(range(N_NEU), key=lambda i: neutral[i])
            pol_sorted = sorted(range(N_POL), key=lambda i: -pol[i])
            out.append({
                "porn": float(sum(porn)), "suggestive": float(sum(sugg)), "political": float(sum(pol)),
                "violence": float(sum(vio)), "minor": float(sum(minor)),
                "scene": NEUTRAL[top_n][1],
                "political_labels": [POLITICAL[i][1] for i in pol_sorted[:2] if pol[i] >= 0.12],
            })
        return out

    def _owl_detect(self, img: Image.Image, selftest: bool = False) -> List[Dict[str, Any]]:
        torch = self.torch
        texts = [[q for q, _, _ in OWL_QUERIES]]
        with torch.inference_mode():
            inp = self.owl_proc(text=texts, images=img, return_tensors="pt")
            inp = {k: (v.to(self.device, dtype=next(self.owl.parameters()).dtype) if v.is_floating_point()
                       else v.to(self.device)) for k, v in inp.items()}
            out = self.owl(**inp)
            logits = out.logits[0].float()
            if not bool(torch.isfinite(logits).all()):
                raise RuntimeError("检测器输出含 NaN/Inf（fp16 溢出）")
            scores = torch.sigmoid(logits)
            best, qi = scores.max(dim=1)
            keep = (best >= (0.99 if selftest else OWL_THRESHOLD)).nonzero(as_tuple=True)[0]
            boxes = out.pred_boxes[0].float()[keep].tolist()
            sc = best[keep].tolist()
            qq = qi[keep].tolist()
        W, H = img.size
        S = float(max(W, H))
        padded = "owlv2" in OWL_MODEL.lower()  # Owlv2 把图片补成正方形(右/下补边)，框相对于方形；OWL-ViT 直接缩放
        dets: List[Dict[str, Any]] = []
        for (cx, cy, bw, bh), s, q in zip(boxes, sc, qq):
            x0, y0, x1, y1 = cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2
            if padded:
                x0, x1 = x0 * S / W, x1 * S / W
                y0, y1 = y0 * S / H, y1 * S / H
            box = [max(0.0, min(1.0, v)) for v in (x0, y0, x1, y1)]
            if box[2] - box[0] < 0.01 or box[3] - box[1] < 0.01:
                continue
            _, cn, cat = OWL_QUERIES[q]
            dets.append({"kind": "object", "label": cn, "cn": cn, "cat": cat, "score": round(float(s), 3),
                         "box": [round(v, 4) for v in box]})
        return nms(dets)

    def vlm_run(self, img: Image.Image, prompt: str, max_new_tokens: int = 200) -> str:
        torch = self.torch
        messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt}]}]
        text = self.proc.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.proc(text=[text], images=[img], padding=True, return_tensors="pt").to(self.vlm.device)
        with torch.inference_mode():
            out = self.vlm.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        out = out[:, inputs["input_ids"].shape[1]:]
        return self.proc.batch_decode(out, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]

    # ---- VLM 通道 ----
    def _vlm_lane(self, img: Image.Image) -> Tuple[Optional[Dict[str, Any]], str, Optional[str]]:
        t = time.time()
        try:
            raw = self.vlm_run(img, VLM_PROMPT)
        except Exception as e:  # noqa: BLE001
            log.exception("VLM 推理失败: %s", e)
            try:
                self.torch.cuda.empty_cache()
            except Exception:  # noqa: BLE001
                pass
            return None, "", "大模型推理失败，已降级: %s" % str(e)[:80]
        ms = int((time.time() - t) * 1000)
        if is_degenerate(raw):
            self.vlm_bad += 1
            if self.vlm_bad >= 3:
                self._vlm_drop_and_fallback()
            return None, raw, "大模型输出异常（疑似 fp16 数值溢出），已降级"
        self.vlm_bad = 0
        data = parse_vlm_json(raw)
        data["_ms"] = ms
        return data, raw, None

    # ---- CPU 工具通道 ----
    def _tool_nude(self, img: Image.Image) -> Dict[str, Any]:
        """NudeNet 返回像素级 [x, y, w, h]，这里统一转换成归一化 [x0, y0, x1, y1]。"""
        t = time.time()
        W, H = img.size
        arr = np.asarray(img)[:, :, ::-1].copy()
        res = self.nude.detect(arr)
        out = [d for d in res if d["class"] in NUDE_CN and d["score"] >= 0.3
               and (d["class"] in NUDE_KEY or d["score"] >= 0.5)]
        for d in out:
            x, y, w, h = d["box"]
            d["nbox"] = [round(max(0.0, x / W), 4), round(max(0.0, y / H), 4),
                         round(min(1.0, (x + w) / W), 4), round(min(1.0, (y + h) / H), 4)]
        faces = sum(1 for d in res if d["class"].startswith("FACE_") and d["score"] >= 0.5)
        return {"dets": out, "faces": faces, "ms": int((time.time() - t) * 1000)}

    def _tool_ocr_qr(self, img: Image.Image) -> Dict[str, Any]:
        t = time.time()
        W, H = img.size
        arr = np.asarray(img)[:, :, ::-1].copy()
        lines: List[Dict[str, Any]] = []
        if self.ocr is not None:
            try:
                res, _ = self.ocr(arr)
                for item in res or []:
                    try:
                        box, txt, sc = item[0], str(item[1]), float(item[2])
                    except Exception:  # noqa: BLE001
                        continue
                    if sc < 0.5 or not txt.strip():
                        continue
                    xs = [p[0] for p in box]
                    ys = [p[1] for p in box]
                    lines.append({"text": txt.strip(), "score": round(sc, 3),
                                  "box": [round(max(0.0, min(xs) / W), 4), round(max(0.0, min(ys) / H), 4),
                                          round(min(1.0, max(xs) / W), 4), round(min(1.0, max(ys) / H), 4)]})
            except Exception as e:  # noqa: BLE001
                log.warning("OCR 失败: %s", str(e)[:100])
        t_ocr = int((time.time() - t) * 1000)
        qrs: List[Dict[str, Any]] = []
        try:
            import cv2
            det = cv2.QRCodeDetector()
            ok, infos, pts, _ = det.detectAndDecodeMulti(arr)
            if ok and pts is not None:
                for s, p in zip(infos, pts):
                    xs, ys = p[:, 0], p[:, 1]
                    qrs.append({"text": s or "", "box": [round(float(max(0, xs.min()) / W), 4),
                                                         round(float(max(0, ys.min()) / H), 4),
                                                         round(float(min(W, xs.max()) / W), 4),
                                                         round(float(min(H, ys.max()) / H), 4)]})
        except Exception as e:  # noqa: BLE001
            log.debug("二维码检测跳过: %s", str(e)[:80])
        return {"lines": lines, "qr": qrs, "ocr_ms": t_ocr, "total_ms": int((time.time() - t) * 1000)}

    # ---- 主流程 ----
    def analyze(self, img: Image.Image, mode: str, seed: str) -> Dict[str, Any]:
        t0 = time.time()
        if MOCK:
            return self._analyze_mock(img, mode, seed, t0)
        warnings: List[str] = list(self.notes[-3:]) if self.notes else []
        full = mode != "fast"

        # 先把 VLM 与 CPU 工具丢到各自通道，GPU 主通道同时推进（三路并行）
        vlm_f = self.vlm_exec.submit(self._vlm_lane, img) if (full and self.has_vlm) else None
        nude_f = self.cpu_exec.submit(self._tool_nude, img) if self.nude is not None else None
        ocr_f = self.cpu_exec.submit(self._tool_ocr_qr, img) if (full and (self.ocr is not None or self.modules.get("qr"))) else None

        crops, boxes = make_crops(img)
        cls = self._classify(crops)
        t1 = time.time()
        clips = self._clip(crops)
        t2 = time.time()

        # 分类器集成：primary 永远不被削弱（ens >= primary），第二分类器只会追加证据
        names = list(cls.keys())
        n = len(crops)
        ens = [max(cls[names[0]][i], sum(cls[m][i] for m in names) / len(names)) for i in range(n)]
        tile_ens = ens[1:]
        nsfw_p = max(ens[0], 0.9 * max(tile_ens)) if tile_ens else ens[0]

        c0 = clips[0]
        tiles = clips[1:]

        def agg(key: str, damp: float) -> float:
            return max(c0[key], damp * max((t[key] for t in tiles), default=0.0))

        clip = {
            "porn": agg("porn", 0.85), "suggestive": agg("suggestive", 0.85), "political": agg("political", 0.85),
            "violence": agg("violence", 0.85), "minor": agg("minor", 0.9),
            "scene": c0["scene"], "political_labels": list(c0["political_labels"]),
        }
        if not clip["political_labels"]:
            for t in tiles:
                if t["political_labels"]:
                    clip["political_labels"] = list(t["political_labels"])
                    break
        regions = []
        for (box, tc, te) in zip(boxes, tiles, tile_ens):
            regions.append({"box": [round(v, 4) for v in box], "nsfw": round(te, 3),
                            "clip_porn": round(tc["porn"], 3), "clip_political": round(tc["political"], 3),
                            "clip_violence": round(tc["violence"], 3)})

        # OWL 开放词汇检测（GPU 主通道）
        objects: List[Dict[str, Any]] = []
        t_owl0 = time.time()
        if full and self.owl is not None:
            try:
                objects = self._owl_detect(img)
            except Exception as e:  # noqa: BLE001
                log.warning("检测器推理失败: %s", str(e)[:120])
                warnings.append("目标检测失败，已跳过: %s" % str(e)[:60])
        t_owl = int((time.time() - t_owl0) * 1000)

        # 收集 CPU 通道结果
        nude: List[Dict[str, Any]] = []
        nude_ms = 0
        faces = 0
        if nude_f is not None:
            try:
                nr = nude_f.result(timeout=60)
                nude, nude_ms, faces = nr["dets"], nr["ms"], nr["faces"]
            except Exception as e:  # noqa: BLE001
                warnings.append("NudeNet 失败，已跳过: %s" % str(e)[:60])
        ocr_lines: List[Dict[str, Any]] = []
        qrs: List[Dict[str, Any]] = []
        ocr_ms = 0
        if ocr_f is not None:
            try:
                r = ocr_f.result(timeout=90)
                ocr_lines, qrs, ocr_ms = r["lines"], r["qr"], r["total_ms"]
            except Exception as e:  # noqa: BLE001
                warnings.append("OCR 失败，已跳过: %s" % str(e)[:60])

        # 收集 VLM 通道结果
        t3 = time.time()
        vlm = None
        if vlm_f is not None:
            try:
                vlm, _raw, warn = vlm_f.result(timeout=240)
                if warn:
                    warnings.append(warn)
                elif vlm is not None and not vlm["description"]:
                    warnings.append("大模型未返回有效描述")
            except Exception as e:  # noqa: BLE001
                warnings.append("大模型通道失败，已降级: %s" % str(e)[:80])
        elif full and not self.has_vlm:
            warnings.append("大模型未加载，使用 分类器+CLIP+工具 结果")
        t4 = time.time()

        ocr_text = "\n".join(l["text"] for l in ocr_lines)[:800]
        qr_texts = [q["text"] for q in qrs if q["text"]]
        contacts = find_contacts(ocr_text + " " + " ".join(qr_texts) + " " + (vlm or {}).get("text", ""))
        aux = {"nude": nude, "objects": objects, "ocr_text": ocr_text, "qr": qr_texts, "contacts": contacts}
        res = fuse(nsfw_p, clip, vlm, aux)

        # 检测框清单（前端画框用，坐标均为归一化 [x0,y0,x1,y1]）
        dets: List[Dict[str, Any]] = []
        for d in nude:
            exposed = d["class"] in NUDE_KEY and d["score"] >= NUDE_KEY[d["class"]][0]
            dets.append({"kind": "nude", "label": d["class"], "cn": NUDE_CN.get(d["class"], d["class"]),
                         "score": round(d["score"], 3), "box": d["nbox"], "flag": bool(exposed)})
        for o in objects:
            dets.append({"kind": "object", "label": o["label"], "cn": o["cn"], "score": o["score"],
                         "box": o["box"], "flag": o["cat"] in ("extremist", "gore") or (o["cat"] == "weapon" and o["score"] >= 0.4)})
        kws = res["keywords"]["political"] + res["keywords"]["porn"]
        for l in ocr_lines[:40]:
            dets.append({"kind": "text", "label": l["text"][:24], "cn": l["text"][:24], "score": l["score"],
                         "box": l["box"], "flag": any(k in l["text"] for k in kws)})
        for q in qrs:
            dets.append({"kind": "qr", "label": "二维码", "cn": "二维码" + (":" + q["text"][:30] if q["text"] else ""),
                         "score": 1.0, "box": q["box"], "flag": bool(q["text"])})

        if vlm and vlm["description"]:
            description = vlm["description"]
        else:
            description = "快速模式：画面最接近「%s」" % clip["scene"]
        gpu_used = "+vlm" if vlm else ""
        res.update({
            "description": description,
            "ocr_text": (ocr_text or (vlm or {}).get("text", ""))[:600],
            "engine": "kaggle-gpu" + gpu_used,
            "gpu": self.device,
            "scene": clip["scene"],
            "regions": regions,
            "detections": dets,
            "aux": {"contacts": contacts, "qr": qr_texts, "ocr_lines": len(ocr_lines),
                    "faces": faces, "tiles": len(boxes)},
            "signals": {
                "nsfw_classifier": round(cls[names[0]][0], 4),
                "nsfw_ensemble": round(nsfw_p, 4),
                "nsfw_models": {m: round(cls[m][0], 4) for m in names},
                "clip_porn": round(clip["porn"], 4),
                "clip_suggestive": round(clip["suggestive"], 4),
                "clip_political": round(clip["political"], 4),
                "clip_violence": round(clip["violence"], 4),
                "vlm_porn": (vlm or {}).get("porn"),
                "vlm_political": (vlm or {}).get("political"),
                "vlm_violence": (vlm or {}).get("violence"),
                "vlm_model": self.vlm_name if vlm else None,
                "nudenet": self.nude_name,
            },
            "timings": {
                "classifier_ms": int((t1 - t0) * 1000),
                "clip_ms": int((t2 - t1) * 1000),
                "detector_ms": t_owl,
                "nudenet_ms": nude_ms,
                "ocr_ms": ocr_ms,
                "vlm_ms": int((vlm or {}).get("_ms", 0)),
                "wait_ms": int((t4 - t3) * 1000),
                "total_ms": int((time.time() - t0) * 1000),
            },
            "warnings": warnings,
        })
        return res

    def _analyze_mock(self, img: Image.Image, mode: str, seed: str, t0: float) -> Dict[str, Any]:
        h = int(seed[:6], 16)
        nsfw_p = (h % 100) / 100.0 * (1.0 if h % 7 == 0 else 0.2)
        clip = {"porn": nsfw_p * 0.5, "suggestive": 0.05, "political": 0.9 if h % 11 == 0 else 0.02,
                "violence": 0.6 if h % 13 == 0 else 0.02, "minor": 0.0,
                "scene": "风景/自然", "political_labels": ["旗帜"] if h % 11 == 0 else []}
        vlm = {"description": "（模拟）一张 %dx%d 的测试图片" % img.size, "porn": "none",
               "political": "low" if h % 11 == 0 else "none", "violence": "none", "minors": False,
               "political_reasons": [], "text": ""}
        objs = [{"kind": "object", "label": "旗帜", "cn": "旗帜", "cat": "flag", "score": 0.52,
                 "box": [0.1, 0.1, 0.4, 0.35]}] if h % 11 == 0 else []
        res = fuse(nsfw_p, clip, vlm if mode != "fast" else None, {"objects": objs})
        res.update({
            "description": vlm["description"], "ocr_text": "", "engine": "mock", "gpu": self.device,
            "scene": clip["scene"],
            "regions": [{"box": list(b), "nsfw": round(nsfw_p * (0.5 + 0.1 * i), 3), "clip_porn": 0.0,
                         "clip_political": 0.0, "clip_violence": 0.0}
                        for i, b in enumerate(tile_boxes(*img.size))],
            "detections": [{"kind": "object", "label": o["label"], "cn": o["cn"], "score": o["score"],
                            "box": o["box"], "flag": False} for o in objs],
            "aux": {"contacts": [], "qr": [], "ocr_lines": 0, "faces": 0, "tiles": len(tile_boxes(*img.size))},
            "signals": {"nsfw_classifier": round(nsfw_p, 4), "nsfw_ensemble": round(nsfw_p, 4),
                        "nsfw_models": {"mock": round(nsfw_p, 4)}, "clip_porn": round(clip["porn"], 4),
                        "clip_political": round(clip["political"], 4), "clip_violence": round(clip["violence"], 4)},
            "timings": {"total_ms": int((time.time() - t0) * 1000)},
            "warnings": ["MOCK 模式：结果仅用于联调"],
        })
        return res

    def ask(self, img: Image.Image, question: str) -> str:
        if MOCK:
            return "（模拟回答）你问的是：" + question
        if not self.has_vlm:
            raise RuntimeError("大模型未加载")
        return self.vlm_run(img, question, max_new_tokens=300).strip()


# --------------------------------------------------------------------------
# 调度池：空闲副本抢占式处理（两张 GPU 同时各处理一张图）
# --------------------------------------------------------------------------
class Pool:
    def __init__(self) -> None:
        self.replicas: List[Replica] = []
        self.free: Optional[asyncio.Queue] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.ready_count = 0
        self.load_errors: List[str] = []
        self.load_done = False
        self.inflight = 0
        self.done = 0
        self.total_ms = 0
        self.started = time.time()
        self.gpus: List[Dict[str, Any]] = []
        self.cache: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.free = asyncio.Queue()
        devices: List[str] = []
        if MOCK:
            devices = ["mock:0", "mock:1"]
        else:
            import torch
            n = torch.cuda.device_count()
            if n == 0:
                log.warning("未检测到 GPU，使用 CPU（很慢，仅快速模式有意义）")
                devices = ["cpu"]
            else:
                for i in range(n):
                    p = torch.cuda.get_device_properties(i)
                    self.gpus.append({"index": i, "name": p.name, "memory_gb": round(p.total_memory / 1024 ** 3, 1)})
                devices = ["cuda:%d" % i for i in range(n)]
                if VLM_SHARD:
                    devices = devices[:1]
        self.replicas = [Replica(i, d) for i, d in enumerate(devices)]
        threading.Thread(target=self._load_all, daemon=True).start()

    def _load_all(self) -> None:
        # 【串行】逐张 GPU 加载（并行会触发 meta tensor 竞态）。加载完一张立刻开始接活，第二张在后台继续。
        for r in self.replicas:
            try:
                r.load()
                self.ready_count += 1
                self.loop.call_soon_threadsafe(self.free.put_nowait, r)  # type: ignore
            except Exception as e:  # noqa: BLE001
                log.exception("副本 %s 加载失败", r.device)
                self.load_errors.append("%s: %s" % (r.device, str(e)[:300]))
        self.load_done = True

    @property
    def ready(self) -> bool:
        return self.ready_count > 0

    async def run(self, fn):
        assert self.free is not None
        self.inflight += 1
        try:
            r: Replica = await self.free.get()
            r.busy = True
            t = time.time()
            try:
                return await asyncio.get_running_loop().run_in_executor(r.executor, fn, r)
            finally:
                r.busy = False
                r.done += 1
                r.total_ms += int((time.time() - t) * 1000)
                self.free.put_nowait(r)
        finally:
            self.inflight -= 1

    def gpu_stats(self) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        for r in self.replicas:
            s: Dict[str, Any] = {
                "index": r.idx, "device": r.device, "ready": r.ready, "busy": r.busy, "processed": r.done,
                "avg_ms": int(r.total_ms / r.done) if r.done else 0, "vlm": r.vlm_name if r.has_vlm else None,
                "modules": r.modules, "notes": r.notes[-4:],
            }
            if not MOCK and r.device.startswith("cuda"):
                try:
                    import torch
                    free, total = torch.cuda.mem_get_info(int(r.device.split(":")[1]))
                    s["mem_used_gb"] = round((total - free) / 1024 ** 3, 1)
                    s["mem_total_gb"] = round(total / 1024 ** 3, 1)
                except Exception:  # noqa: BLE001
                    pass
            out.append(s)
        return out


pool = Pool()


@asynccontextmanager
async def lifespan(_app: FastAPI):
    pool.start(asyncio.get_running_loop())
    yield


app = FastAPI(title="Image Moderation GPU Server", version=VERSION, lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
                   expose_headers=["*"], max_age=3600)


@app.get("/api/health")
async def health() -> Dict[str, Any]:
    ready_reps = [r for r in pool.replicas if r.ready]
    r0 = ready_reps[0] if ready_reps else None
    mods = r0.modules if r0 else {}
    notes: List[str] = []
    for r in pool.replicas:
        notes += r.notes[-3:]
    return {
        "status": "ok" if pool.ready else ("error" if (pool.load_done and pool.load_errors) else "loading"),
        "ready": pool.ready,
        "version": VERSION,
        "profile": PROFILE,
        "mock": MOCK,
        "replicas": len(pool.replicas),
        "replicas_ready": pool.ready_count,
        "gpus": pool.gpus,
        "gpu_stats": pool.gpu_stats(),
        "models": {"nsfw": NSFW_MODELS[0], "nsfw_all": mods.get("classifier", NSFW_MODELS), "clip": CLIP_MODEL,
                   "vlm": VLM_MODELS[0] if MOCK else (r0.vlm_name if (r0 and r0.has_vlm) else None),
                   "detector": mods.get("detector"), "nudenet": mods.get("nudenet"),
                   "ocr": bool(mods.get("ocr")), "qr": bool(mods.get("qr"))},
        "vlm_enabled": bool(r0 and r0.has_vlm) or MOCK,
        "inflight": pool.inflight,
        "processed": pool.done,
        "avg_ms": int(pool.total_ms / pool.done) if pool.done else 0,
        "uptime_s": int(time.time() - pool.started),
        "error": ("; ".join(pool.load_errors) or "加载失败") if (pool.load_done and not pool.ready) else None,
        "warnings": (pool.load_errors + notes)[-8:] if pool.ready else [],
        "loading_done": pool.load_done,
        "crawler": crawler.capabilities() if crawler else {"playwright": False, "chromium": False, "engine": "none"},
    }


async def analyze_raw(raw: bytes, mode: str, filename: Optional[str]) -> Dict[str, Any]:
    """审核一张图片（字节）。/api/analyze 与 爬取流水线 共用同一条路径：缓存 → 解码 → GPU 池 → 融合。"""
    if len(raw) > MAX_BYTES:
        raise HTTPException(413, "图片过大（>40MB）")
    if not pool.ready:
        raise HTTPException(503, "模型加载中，请稍后重试")
    mode = "fast" if mode == "fast" else "full"
    key = hashlib.sha1(raw).hexdigest()
    ck = key + ":" + mode
    if ck in pool.cache:
        pool.cache.move_to_end(ck)
        res = dict(pool.cache[ck])
        res["cached"] = True
        res["filename"] = filename
        return res
    loop = asyncio.get_running_loop()
    try:
        img = await loop.run_in_executor(None, load_image, raw)
    except Exception:
        raise HTTPException(400, "无法解析该图片（格式不支持或文件损坏）")
    t0 = time.time()
    try:
        res = await pool.run(lambda r: r.analyze(img, mode, key))
    except Exception as e:
        log.exception("分析失败")
        raise HTTPException(500, "分析失败: %s" % str(e)[:200])
    pool.done += 1
    pool.total_ms += int((time.time() - t0) * 1000)
    res["sha1"] = key
    res["size"] = list(img.size)
    pool.cache[ck] = res
    if len(pool.cache) > 5000:
        pool.cache.popitem(last=False)
    res = dict(res)
    res["filename"] = filename
    res["cached"] = False
    return res


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...), mode: str = Form("full")) -> JSONResponse:
    raw = await file.read()
    return JSONResponse(await analyze_raw(raw, mode, file.filename))


@app.post("/api/ask")
async def ask(file: UploadFile = File(...), question: str = Form(...)) -> Dict[str, str]:
    if not pool.ready:
        raise HTTPException(503, "模型加载中")
    raw = await file.read()
    try:
        img = await asyncio.get_running_loop().run_in_executor(None, load_image, raw)
    except Exception:
        raise HTTPException(400, "无法解析该图片")
    try:
        ans = await pool.run(lambda r: r.ask(img, question[:500]))
    except Exception as e:
        raise HTTPException(500, "追问失败: %s" % str(e)[:200])
    return {"answer": ans}


# --------------------------------------------------------------------------
# v2.1 自动爬取并审核网络照片
# --------------------------------------------------------------------------
if crawler:
    crawler.configure(
        analyze_raw,
        # 审核并发 = 已就绪副本数 x 3（每张 GPU 同时有活在算 + 排队，两张 T4 持续满载）
        lambda: max(4, 3 * max(1, pool.ready_count)),
    )


def _need_crawler() -> Any:
    if not crawler:
        raise HTTPException(501, "爬取模块未加载（缺少 kaggle/crawler.py）")
    return crawler


def _job(job_id: str) -> Any:
    j = _need_crawler().JOBS.get(job_id)
    if not j:
        raise HTTPException(404, "任务不存在或已过期")
    return j


@app.get("/api/crawl/capabilities")
async def crawl_caps() -> Dict[str, Any]:
    return _need_crawler().capabilities()


@app.post("/api/crawl/start")
async def crawl_start(body: Dict[str, Any]) -> Dict[str, Any]:
    c = _need_crawler()
    if not pool.ready:
        raise HTTPException(503, "模型加载中，请稍后重试")
    p = c.normalize_params(body)
    if not p["urls"]:
        raise HTTPException(400, "没有有效网址：请粘贴网址/分享链接，或填写关键词并选择平台")
    try:
        for u in p["urls"][:30]:
            await c.check_url(u)
    except ValueError as e:
        raise HTTPException(400, str(e))
    job = c.start_job(body)
    return {"id": job.id, "urls": job.p["urls"], "engine": c.capabilities()["engine"]}


@app.get("/api/crawl/{job_id}")
async def crawl_status(job_id: str, since: int = 0) -> Dict[str, Any]:
    j = _job(job_id)
    out = crawler.view(j, since)  # type: ignore[union-attr]
    out["gpu_stats"] = pool.gpu_stats()
    return out


@app.post("/api/crawl/{job_id}/stop")
async def crawl_stop(job_id: str) -> Dict[str, Any]:
    j = _job(job_id)
    j.stop.set()
    j.say("收到停止指令：不再抓取新图片，未开始审核的会跳过")
    return {"ok": True}


@app.get("/api/crawl/{job_id}/{kind}/{idx}")
async def crawl_image(job_id: str, kind: str, idx: int):
    j = _job(job_id)
    p = crawler.image_path(j, idx, kind == "thumb")  # type: ignore[union-attr]
    if kind not in ("thumb", "img") or not p:
        raise HTTPException(404, "图片不存在")
    return FileResponse(str(p), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@app.get("/crawl")
async def crawl_page():
    p = BASE_DIR / "crawl.html"
    if p.exists():
        return FileResponse(str(p), media_type="text/html; charset=utf-8")
    raise HTTPException(404, "缺少 crawl.html")


@app.get("/")
async def index():
    p = BASE_DIR / "index.html"
    if p.exists():
        return FileResponse(str(p), media_type="text/html; charset=utf-8")
    return JSONResponse({"message": "GPU 服务运行中。缺少 index.html，仅提供 /api/* 接口。"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), log_level="warning")
