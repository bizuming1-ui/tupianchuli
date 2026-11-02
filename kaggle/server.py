"""
图片审查系统 - Kaggle GPU 服务端 (FastAPI)

设计要点（第一性原则：吞吐 = GPU 利用率 × 并行度，准确率 = 多信号交叉验证）
1. T4 x2：每张 GPU 各加载一份完整模型副本（数据并行），请求由空闲副本抢占处理。
2. 三路信号融合：
   - NSFW 分类器 (Falconsai/nsfw_image_detection)      -> 涉黄概率，速度极快
   - CLIP 零样本 (openai/clip-vit-large-patch14)        -> 涉黄 / 涉政 / 场景概率
   - 视觉大模型 VLM (Qwen2-VL-2B-Instruct，可换 7B)      -> 中文描述 + 涉黄/涉政判断 + 图中文字(OCR)
3. 结论 verdict: pass(通过) / review(需人工复核) / block(拦截)。宁可多复核，不轻易放过。
4. 同一张图片(按 SHA1)结果缓存，批量重复图片秒回。
5. 环境变量 MOCK=1 时不加载任何模型，仅用于接口联调。

环境变量:
  PORT(8000) VLM_MODEL NSFW_MODEL CLIP_MODEL VLM_MAX_PIXELS DISABLE_VLM=1 VLM_SHARD=1 MOCK=1
  POLITICAL_KEYWORDS=词1,词2   (追加自定义涉政关键词)
"""
import asyncio
import hashlib
import io
import json
import logging
import os
import re
import threading
import time
from collections import OrderedDict
from contextlib import asynccontextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from PIL import Image, ImageOps

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("moderation")

BASE_DIR = Path(__file__).resolve().parent
MOCK = os.getenv("MOCK") == "1"
VLM_MODEL = os.getenv("VLM_MODEL", "Qwen/Qwen2-VL-2B-Instruct")
NSFW_MODEL = os.getenv("NSFW_MODEL", "Falconsai/nsfw_image_detection")
CLIP_MODEL = os.getenv("CLIP_MODEL", "openai/clip-vit-large-patch14")
VLM_MAX_PIXELS = int(os.getenv("VLM_MAX_PIXELS", str(640 * 28 * 28)))
DISABLE_VLM = os.getenv("DISABLE_VLM") == "1"
VLM_SHARD = os.getenv("VLM_SHARD") == "1"  # 大模型(7B)跨两张卡切分，只有 1 个副本
MAX_BYTES = 40 * 1024 * 1024
VERSION = "1.0.0"

DEFAULT_KEYWORDS = [
    "法轮功", "法轮大法", "六四", "天安门事件", "藏独", "疆独", "台独", "港独",
    "反共", "打倒共产党", "东突", "伊斯兰国", "纳粹", "卐",
]
POLITICAL_KEYWORDS = DEFAULT_KEYWORDS + [
    k.strip() for k in os.getenv("POLITICAL_KEYWORDS", "").split(",") if k.strip()
]

# --------------------------------------------------------------------------
# CLIP 零样本提示词（英文效果最好）。 neutral 用来"吸收"正常图片的概率质量，
# 这样 porn / political 的概率才有意义，而不是在互相比较中被放大。
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

VLM_PROMPT = (
    "你是一名严谨的内容安全审核员。请仔细观察这张图片，只输出一个 JSON 对象，不要输出任何其他文字。\n"
    '格式: {"description":"用一句中文简要描述画面(不超过40字)",'
    '"porn":"none|suggestive|explicit","political":"none|low|high",'
    '"political_reasons":["涉政原因，没有则为空数组"],"text":"图中清晰可见的主要文字，没有则为空字符串"}\n'
    "porn 判定: explicit=裸露生殖器/乳头或性行为; suggestive=内衣泳装性暗示、挑逗姿势、大面积裸露; none=正常。\n"
    "political 判定: high=敏感政治人物或事件、游行示威、分裂/邪教/极端组织标志、攻击性政治标语; "
    "low=国旗、官方会议、一般政治相关内容; none=与政治无关。"
)


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
            "text": pick("text"),
            "political_reasons": [],
        }
    porn = str(data.get("porn", "")).lower()
    pol = str(data.get("political", "")).lower()
    data["porn"] = "explicit" if "explicit" in porn else "suggestive" if "sugg" in porn else "none"
    data["political"] = "high" if "high" in pol else "low" if "low" in pol else "none"
    reasons = data.get("political_reasons") or []
    if isinstance(reasons, str):
        reasons = [reasons]
    data["political_reasons"] = [str(r) for r in reasons if str(r).strip()][:5]
    data["description"] = str(data.get("description", "")).strip()
    data["text"] = str(data.get("text", "")).strip()
    return data


def fuse(nsfw_p: float, clip: Dict[str, Any], vlm: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """多信号融合 -> porn / political / verdict / reasons"""
    reasons: List[str] = []
    clip_porn = clip["porn"]
    clip_sugg = clip["suggestive"]
    clip_pol = clip["political"]
    vporn = vlm["porn"] if vlm else None
    vpol = vlm["political"] if vlm else None
    text = ((vlm or {}).get("text", "") + " " + (vlm or {}).get("description", ""))
    kw_hits = [k for k in POLITICAL_KEYWORDS if k and k in text]

    # ---- 涉黄 ----
    vmap = {"none": 0.02, "suggestive": 0.6, "explicit": 0.97}
    porn_score = max(nsfw_p, clip_porn, clip_sugg * 0.8, vmap.get(vporn or "none", 0.0))
    explicit = (
        nsfw_p >= 0.9
        or clip_porn >= 0.8
        or (vporn == "explicit" and (nsfw_p >= 0.4 or clip_porn >= 0.3))
    )
    suggestive = (
        nsfw_p >= 0.5 or (clip_porn + clip_sugg) >= 0.5 or vporn in ("suggestive", "explicit")
    )
    porn_level = "explicit" if explicit else "suggestive" if suggestive else "none"
    if explicit:
        reasons.append("涉黄：疑似色情/裸露内容（分类器 %.0f%%，CLIP %.0f%%%s）" % (
            nsfw_p * 100, clip_porn * 100, "，大模型确认" if vporn == "explicit" else ""))
    elif suggestive:
        reasons.append("涉黄：疑似低俗/性暗示内容，建议人工复核（分类器 %.0f%%）" % (nsfw_p * 100))

    # ---- 涉政 ----
    pmap = {"none": 0.03, "low": 0.5, "high": 0.9}
    pol_score = max(clip_pol, pmap.get(vpol or "none", 0.0), 0.95 if kw_hits else 0.0)
    if kw_hits or (vpol == "high" and clip_pol >= 0.3):
        pol_level = "high"
    elif vpol in ("high", "low") or clip_pol >= 0.5:
        pol_level = "low"
    else:
        pol_level = "none"
    labels = list(clip.get("political_labels", []))
    if vlm:
        labels = list(dict.fromkeys(vlm.get("political_reasons", []) + labels))
    if kw_hits:
        reasons.append("涉政：图中文字命中敏感词 " + "、".join(kw_hits))
    if pol_level == "high":
        reasons.append("涉政：疑似敏感政治内容（%s）" % ("、".join(labels[:3]) or "大模型判定"))
    elif pol_level == "low":
        reasons.append("涉政：含政治相关元素，建议人工复核（%s）" % ("、".join(labels[:3]) or "CLIP 倾向"))

    if porn_level == "explicit" or pol_level == "high":
        verdict = "block"
    elif porn_level == "suggestive" or pol_level == "low":
        verdict = "review"
    else:
        verdict = "pass"
    return {
        "verdict": verdict,
        "porn": {"score": round(min(porn_score, 1.0), 4), "level": porn_level},
        "political": {"score": round(min(pol_score, 1.0), 4), "level": pol_level,
                      "labels": labels[:5]},
        "reasons": reasons,
    }


# --------------------------------------------------------------------------
# 模型副本（每张 GPU 一份）
# --------------------------------------------------------------------------
class Replica:
    def __init__(self, idx: int, device: str):
        self.idx = idx
        self.device = device
        self.executor = ThreadPoolExecutor(max_workers=1)  # Qwen2-VL 内部有可变状态，单线程最安全
        self.vlm = None
        self.proc = None
        self.has_vlm = False
        self.nsfw = None
        self.nsfw_proc = None
        self.nsfw_idx = 1
        self.clip = None
        self.clip_proc = None
        self.txt_feats = None
        self.torch = None

    # ---- 加载 ----
    def load(self) -> None:
        t0 = time.time()
        if MOCK:
            time.sleep(0.5)
            return
        import torch
        from transformers import (AutoImageProcessor, AutoModelForImageClassification, AutoProcessor,
                                  CLIPModel, CLIPProcessor)

        self.torch = torch
        use_cuda = self.device.startswith("cuda")
        dtype = torch.float16 if use_cuda else torch.float32

        log.info("[%s] 加载 NSFW 分类器 %s", self.device, NSFW_MODEL)
        self.nsfw_proc = AutoImageProcessor.from_pretrained(NSFW_MODEL)
        self.nsfw = AutoModelForImageClassification.from_pretrained(NSFW_MODEL, torch_dtype=dtype).to(self.device).eval()
        for i, name in self.nsfw.config.id2label.items():
            if "nsfw" in str(name).lower() or "porn" in str(name).lower():
                self.nsfw_idx = int(i)

        log.info("[%s] 加载 CLIP %s", self.device, CLIP_MODEL)
        self.clip_proc = CLIPProcessor.from_pretrained(CLIP_MODEL)
        self.clip = CLIPModel.from_pretrained(CLIP_MODEL, torch_dtype=dtype).to(self.device).eval()
        prompts = [p for p, _ in NEUTRAL] + PORN + SUGGESTIVE + [p for p, _ in POLITICAL]
        with torch.inference_mode():
            tok = self.clip_proc(text=prompts, return_tensors="pt", padding=True).to(self.device)
            tf = self.clip.get_text_features(**tok)
            tf = tf if torch.is_tensor(tf) else getattr(tf, "pooler_output", tf)
            self.txt_feats = tf / tf.norm(dim=-1, keepdim=True)

        if use_cuda and not DISABLE_VLM:
            try:
                self._load_vlm()
            except Exception as e:  # 大模型失败不影响整体可用性（降级为快速模式）
                log.exception("[%s] VLM 加载失败，将仅使用分类器+CLIP: %s", self.device, e)
        log.info("[%s] 副本就绪，用时 %.1fs，VLM=%s", self.device, time.time() - t0, self.has_vlm)

    def _load_vlm(self) -> None:
        import torch
        from transformers import AutoProcessor
        try:
            from transformers import AutoModelForImageTextToText as Auto
        except ImportError:  # 旧版本 transformers
            from transformers import AutoModelForVision2Seq as Auto

        log.info("[%s] 加载视觉大模型 %s", self.device, VLM_MODEL)
        self.proc = AutoProcessor.from_pretrained(VLM_MODEL, min_pixels=128 * 28 * 28, max_pixels=VLM_MAX_PIXELS)
        if VLM_SHARD:
            self.vlm = Auto.from_pretrained(
                VLM_MODEL, torch_dtype=torch.float16, device_map="auto",
                max_memory={i: "13GiB" for i in range(torch.cuda.device_count())},
            ).eval()
        else:
            idx = int(self.device.split(":")[1]) if ":" in self.device else 0
            self.vlm = Auto.from_pretrained(
                VLM_MODEL, torch_dtype=torch.float16, device_map={"": idx}, attn_implementation="sdpa",
            ).eval()
        self.has_vlm = True

    # ---- 推理 ----
    def _classify(self, img: Image.Image) -> float:
        torch = self.torch
        with torch.inference_mode():
            inp = self.nsfw_proc(images=img, return_tensors="pt")
            pv = inp["pixel_values"].to(self.device, dtype=next(self.nsfw.parameters()).dtype)
            logits = self.nsfw(pixel_values=pv).logits.float()
            return float(torch.softmax(logits, dim=-1)[0, self.nsfw_idx].item())

    def _clip(self, img: Image.Image) -> Dict[str, Any]:
        torch = self.torch
        with torch.inference_mode():
            inp = self.clip_proc(images=img, return_tensors="pt")
            pv = inp["pixel_values"].to(self.device, dtype=next(self.clip.parameters()).dtype)
            f = self.clip.get_image_features(pixel_values=pv)
            f = f if torch.is_tensor(f) else getattr(f, "pooler_output", f)
            f = f / f.norm(dim=-1, keepdim=True)
            logits = 100.0 * f.float() @ self.txt_feats.float().T
            probs = torch.softmax(logits, dim=-1)[0].tolist()
        n, p, s = len(NEUTRAL), len(PORN), len(SUGGESTIVE)
        neutral = probs[:n]
        porn = probs[n:n + p]
        sugg = probs[n + p:n + p + s]
        pol = probs[n + p + s:]
        top_n = max(range(n), key=lambda i: neutral[i])
        pol_sorted = sorted(range(len(POLITICAL)), key=lambda i: -pol[i])
        return {
            "porn": float(sum(porn)),
            "suggestive": float(sum(sugg)),
            "political": float(sum(pol)),
            "scene": NEUTRAL[top_n][1],
            "political_labels": [POLITICAL[i][1] for i in pol_sorted[:2] if pol[i] >= 0.12],
        }

    def vlm_run(self, img: Image.Image, prompt: str, max_new_tokens: int = 200) -> str:
        torch = self.torch
        messages = [{"role": "user", "content": [{"type": "image"}, {"type": "text", "text": prompt}]}]
        text = self.proc.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = self.proc(text=[text], images=[img], padding=True, return_tensors="pt").to(self.vlm.device)
        with torch.inference_mode():
            out = self.vlm.generate(**inputs, max_new_tokens=max_new_tokens, do_sample=False)
        out = out[:, inputs["input_ids"].shape[1]:]
        return self.proc.batch_decode(out, skip_special_tokens=True, clean_up_tokenization_spaces=False)[0]

    def analyze(self, img: Image.Image, mode: str, seed: str) -> Dict[str, Any]:
        t0 = time.time()
        warnings: List[str] = []
        if MOCK:
            h = int(seed[:6], 16)
            nsfw_p = (h % 100) / 100.0 * (1.0 if h % 7 == 0 else 0.2)
            clip = {"porn": nsfw_p * 0.5, "suggestive": 0.05, "political": 0.9 if h % 11 == 0 else 0.02,
                    "scene": "风景/自然", "political_labels": ["旗帜"] if h % 11 == 0 else []}
            vlm = {"description": "（模拟）一张 %dx%d 的测试图片" % img.size, "porn": "none",
                   "political": "low" if h % 11 == 0 else "none", "political_reasons": [], "text": ""}
            res = fuse(nsfw_p, clip, vlm if mode != "fast" else None)
            res.update({"description": vlm["description"], "ocr_text": "", "engine": "mock", "gpu": self.device})
            res["timings"] = {"total_ms": int((time.time() - t0) * 1000)}
            res["warnings"] = ["MOCK 模式：结果仅用于联调"]
            return res

        nsfw_p = self._classify(img)
        t1 = time.time()
        clip = self._clip(img)
        t2 = time.time()
        vlm = None
        raw = ""
        if mode != "fast" and self.has_vlm:
            try:
                raw = self.vlm_run(img, VLM_PROMPT)
                vlm = parse_vlm_json(raw)
                if not vlm["description"]:
                    warnings.append("大模型未返回有效描述")
            except Exception as e:
                log.exception("VLM 推理失败: %s", e)
                warnings.append("大模型推理失败，已降级: %s" % str(e)[:80])
        elif mode != "fast" and not self.has_vlm:
            warnings.append("大模型未加载，使用 分类器+CLIP 结果")
        t3 = time.time()

        res = fuse(nsfw_p, clip, vlm)
        if vlm and vlm["description"]:
            description = vlm["description"]
        else:
            description = "快速模式：画面最接近「%s」" % clip["scene"]
        res.update({
            "description": description,
            "ocr_text": (vlm or {}).get("text", ""),
            "engine": "kaggle-gpu" + ("+vlm" if vlm else ""),
            "gpu": self.device,
            "scene": clip["scene"],
            "signals": {
                "nsfw_classifier": round(nsfw_p, 4),
                "clip_porn": round(clip["porn"], 4),
                "clip_suggestive": round(clip["suggestive"], 4),
                "clip_political": round(clip["political"], 4),
                "vlm_porn": (vlm or {}).get("porn"),
                "vlm_political": (vlm or {}).get("political"),
            },
            "timings": {
                "classifier_ms": int((t1 - t0) * 1000),
                "clip_ms": int((t2 - t1) * 1000),
                "vlm_ms": int((t3 - t2) * 1000),
                "total_ms": int((time.time() - t0) * 1000),
            },
            "warnings": warnings,
        })
        return res

    def ask(self, img: Image.Image, question: str) -> str:
        if MOCK:
            return "（模拟回答）你问的是：" + question
        if not self.has_vlm:
            raise RuntimeError("大模型未加载")
        return self.vlm_run(img, question, max_new_tokens=300).strip()


# --------------------------------------------------------------------------
# 调度池：空闲副本抢占式处理
# --------------------------------------------------------------------------
class Pool:
    def __init__(self) -> None:
        self.replicas: List[Replica] = []
        self.free: Optional[asyncio.Queue] = None
        self.loop: Optional[asyncio.AbstractEventLoop] = None
        self.ready_count = 0
        self.loading_error: Optional[str] = None
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
        # 各 GPU 并行加载，加载完一个就立刻开始接活
        def one(r: Replica) -> None:
            try:
                r.load()
                self.ready_count += 1
                self.loop.call_soon_threadsafe(self.free.put_nowait, r)  # type: ignore
            except Exception as e:
                log.exception("副本 %s 加载失败", r.device)
                self.loading_error = "%s: %s" % (r.device, e)

        threads = [threading.Thread(target=one, args=(r,)) for r in self.replicas]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    @property
    def ready(self) -> bool:
        return self.ready_count > 0

    async def run(self, fn):
        assert self.free is not None
        self.inflight += 1
        try:
            r: Replica = await self.free.get()
            try:
                return await asyncio.get_running_loop().run_in_executor(r.executor, fn, r)
            finally:
                self.free.put_nowait(r)
        finally:
            self.inflight -= 1


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
    r0 = pool.replicas[0] if pool.replicas else None
    return {
        "status": "ok" if pool.ready else ("error" if pool.loading_error else "loading"),
        "ready": pool.ready,
        "version": VERSION,
        "mock": MOCK,
        "replicas": len(pool.replicas),
        "replicas_ready": pool.ready_count,
        "gpus": pool.gpus,
        "models": {"nsfw": NSFW_MODEL, "clip": CLIP_MODEL, "vlm": VLM_MODEL if (r0 and r0.has_vlm) else None},
        "vlm_enabled": bool(r0 and r0.has_vlm) or MOCK,
        "inflight": pool.inflight,
        "processed": pool.done,
        "avg_ms": int(pool.total_ms / pool.done) if pool.done else 0,
        "uptime_s": int(time.time() - pool.started),
        "error": pool.loading_error,
    }


@app.post("/api/analyze")
async def analyze(file: UploadFile = File(...), mode: str = Form("full")) -> JSONResponse:
    raw = await file.read()
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
        res["filename"] = file.filename
        return JSONResponse(res)
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
    pool.cache[ck] = res
    if len(pool.cache) > 5000:
        pool.cache.popitem(last=False)
    res = dict(res)
    res["filename"] = file.filename
    res["cached"] = False
    return JSONResponse(res)


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


@app.get("/")
async def index():
    p = BASE_DIR / "index.html"
    if p.exists():
        return FileResponse(str(p), media_type="text/html; charset=utf-8")
    return JSONResponse({"message": "GPU 服务运行中。缺少 index.html，仅提供 /api/* 接口。"})


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.getenv("PORT", "8000")), log_level="warning")
