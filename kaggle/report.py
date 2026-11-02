"""
Excel 报告生成（v2.2）：把审核结果导出成 .xlsx —— 每行一张图（缩略图直接嵌在单元格里）+ 结论 + 违规类别（涉政/涉黄/暴力…）+ 原因。

用在两处（爬取页 / 图片审核页各有一个「生成 Excel」按钮）：
  GET  /api/crawl/{job_id}/xlsx   爬取任务的结果（缩略图读服务端落盘文件）
  POST /api/export/xlsx           图片审核页 / 任意前端：把 {rows:[{name,image(dataURL),res}]} 交给服务端生成
前端（React 网站）另有一份纯浏览器版（src/lib/excel.ts，用 exceljs），连不上 Kaggle 时也能导出；两边的「违规类别」规则保持一致。

只依赖 openpyxl + Pillow（Kaggle 自带 openpyxl；没有时 Cell 1 会安装）。
"""
from __future__ import annotations

import base64
import io
import re
import time
from collections import Counter
from typing import Any, Dict, List, Optional

from PIL import Image

KIND_LABEL = {"frame": "视频截图(帧)", "cover": "视频封面", "image": "网络图片", "screenshot": "页面截图", "upload": "上传图片"}
VERDICT_LABEL = {"pass": "通过", "review": "待复核", "block": "拦截", "error": "审核失败"}
VERDICT_FILL = {"block": "FFC7CE", "review": "FFEB9C", "pass": "C6EFCE", "error": "D9D9D9"}
RISK_RANK = {"block": 0, "review": 1, "pass": 2, "error": 3}
ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
IMG_W = 150  # 单元格里缩略图的显示宽度（像素）


def categories_of(res: Optional[Dict[str, Any]]) -> List[str]:
    """违规类别（与前端 src/lib/excel.ts 的 categoriesOf 规则一致）"""
    if not res:
        return []
    out: List[str] = []
    porn = (res.get("porn") or {}).get("level")
    pol = (res.get("political") or {}).get("level")
    vio = (res.get("violence") or {}).get("level")
    if porn == "explicit":
        out.append("涉黄")
    elif porn == "suggestive":
        out.append("涉黄(疑似/低俗)")
    if pol == "high":
        out.append("涉政")
    elif pol == "low":
        out.append("涉政(疑似/需复核)")
    if vio == "high":
        out.append("暴力血腥")
    elif vio == "low":
        out.append("暴力(提示)")
    if res.get("minors_suspected") and porn in ("explicit", "suggestive"):
        out.append("疑似涉未成年人")
    aux = res.get("aux") or {}
    if aux.get("qr") or aux.get("contacts"):
        out.append("二维码/联系方式引流")
    return out


def category_text(res: Optional[Dict[str, Any]], verdict: str) -> str:
    c = categories_of(res)
    if c:
        return "、".join(c)
    return "未发现违规" if verdict == "pass" else ("需人工复核" if verdict == "review" else "")


def _clean(v: Any, limit: int = 3000) -> str:
    s = "" if v is None else str(v)
    return ILLEGAL.sub("", s)[:limit]


def _thumb(raw: Optional[bytes]) -> Optional[tuple]:
    """原图字节 → (JPEG 字节, 显示宽, 显示高)。统一缩到宽 ≤300px，xlsx 体积小、打开快"""
    if not raw:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
        im = im.convert("RGB")
        im.thumbnail((300, 300))
        b = io.BytesIO()
        im.save(b, "JPEG", quality=80)
        w, h = im.size
        k = IMG_W / max(1, w)
        dw, dh = IMG_W, max(40, min(160, int(h * k)))
        if h * k > 160:  # 太高的竖图按高度限制
            dh = 160
            dw = max(40, int(w * 160 / max(1, h)))
        return b.getvalue(), dw, dh
    except Exception:  # noqa: BLE001
        return None


def rows_from_payload(payload: Dict[str, Any]) -> List[Dict[str, Any]]:
    """POST /api/export/xlsx 的请求体 → 统一行格式。image 为 dataURL（data:image/jpeg;base64,...）"""
    rows: List[Dict[str, Any]] = []
    for i, r in enumerate(payload.get("rows") or []):
        img = None
        data = r.get("image")
        if isinstance(data, str) and "," in data:
            try:
                img = base64.b64decode(data.split(",", 1)[1])
            except Exception:  # noqa: BLE001
                img = None
        rows.append({"idx": r.get("idx", i + 1), "kind": r.get("kind") or "upload", "name": r.get("name"), "res": r.get("res"),
                     "page": r.get("page"), "src": r.get("src"), "video": r.get("video"), "ts": r.get("ts"), "title": r.get("title"),
                     "err": r.get("err"), "image": img})
    return rows


def build_xlsx(rows: List[Dict[str, Any]], meta: Optional[Dict[str, Any]] = None) -> bytes:
    try:
        from openpyxl import Workbook
        from openpyxl.drawing.image import Image as XlImage
        from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
        from openpyxl.utils import get_column_letter
    except Exception as e:  # noqa: BLE001
        raise RuntimeError("缺少 openpyxl，请先运行 Cell 1（pip install openpyxl）：%s" % e)

    meta = meta or {}
    wb = Workbook()
    ws = wb.active
    ws.title = "审核明细"
    sm = wb.create_sheet("汇总", 0)

    def verdict_of(r: Dict[str, Any]) -> str:
        res = r.get("res")
        return str(res.get("verdict")) if res and res.get("verdict") in VERDICT_LABEL else "error"

    rows = sorted(rows, key=lambda r: (RISK_RANK.get(verdict_of(r), 3), -max(((r.get("res") or {}).get("porn") or {}).get("score", 0),
                                                                              ((r.get("res") or {}).get("political") or {}).get("score", 0)),
                                       r.get("idx") or 0))

    heads = ["序号", "截图/照片", "来源类型", "名称", "视频时间点(秒)", "审核结论", "违规类别", "涉黄分", "涉政分", "暴力分",
             "判定原因", "AI 中文描述", "图中文字(OCR)", "命中关键词", "来源页面", "视频/图片地址", "处理 GPU"]
    widths = [6, 24, 13, 26, 10, 10, 24, 8, 8, 8, 42, 42, 32, 20, 34, 34, 18]
    thin = Side(style="thin", color="BFBFBF")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    head_fill = PatternFill("solid", fgColor="1F3864")
    for c, (h, w) in enumerate(zip(heads, widths), start=1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center", wrap_text=True)
        cell.border = border
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.row_dimensions[1].height = 26
    ws.freeze_panes = "C2"

    cnt_v: Counter = Counter()
    cnt_c: Counter = Counter()
    cnt_k: Counter = Counter()
    cnt_g: Counter = Counter()
    top = Alignment(vertical="top", wrap_text=True)
    for i, r in enumerate(rows, start=2):
        res = r.get("res") or {}
        v = verdict_of(r)
        cats = categories_of(res)
        kw = res.get("keywords") or {}
        kws = list(kw.get("political") or []) + list(kw.get("porn") or [])
        gpu = str(res.get("gpu") or "")
        cnt_v[v] += 1
        cnt_k[KIND_LABEL.get(str(r.get("kind")), str(r.get("kind") or ""))] += 1
        if gpu:
            cnt_g[gpu] += 1
        for c in (cats or (["未发现违规"] if v == "pass" else [])):
            cnt_c[c] += 1
        link = _clean(r.get("page"))
        vals = [
            r.get("idx"), None, KIND_LABEL.get(str(r.get("kind")), str(r.get("kind") or "")),
            _clean(r.get("title") or r.get("name")), r.get("ts") if r.get("ts") is not None else "",
            VERDICT_LABEL.get(v, v), category_text(res, v) if res else _clean(r.get("err") or "审核失败"),
            round(float((res.get("porn") or {}).get("score", 0)), 3) if res else "",
            round(float((res.get("political") or {}).get("score", 0)), 3) if res else "",
            round(float((res.get("violence") or {}).get("score", 0)), 3) if res else "",
            _clean("；".join(res.get("reasons") or [])), _clean(res.get("description")), _clean(res.get("ocr_text")),
            _clean("、".join(kws)), link, _clean(r.get("video") or r.get("src")), gpu,
        ]
        fill = PatternFill("solid", fgColor=VERDICT_FILL.get(v, "FFFFFF"))
        for c, val in enumerate(vals, start=1):
            cell = ws.cell(row=i, column=c, value=val)
            cell.alignment = top
            cell.border = border
            if c in (6, 7):
                cell.fill = fill
                cell.font = Font(bold=True)
        if link.startswith("http"):
            ws.cell(row=i, column=15).hyperlink = link
            ws.cell(row=i, column=15).font = Font(color="0563C1", underline="single")
        th = _thumb(r.get("image"))
        if th:
            data, dw, dh = th
            img = XlImage(io.BytesIO(data))
            img.width, img.height = dw, dh
            ws.add_image(img, "B%d" % i)
            ws.row_dimensions[i].height = max(60, dh * 0.75 + 8)
        else:
            ws.cell(row=i, column=2, value="（无图）")
            ws.row_dimensions[i].height = 40
    ws.auto_filter.ref = "A1:%s%d" % (get_column_letter(len(heads)), max(1, len(rows) + 1))

    # ---------------- 汇总 ----------------
    sm.column_dimensions["A"].width = 26
    sm.column_dimensions["B"].width = 16
    sm.column_dimensions["C"].width = 70
    r0 = 1

    def put(a: Any, b: Any = None, c: Any = None, bold: bool = False, fill: Optional[str] = None) -> None:
        nonlocal r0
        for col, val in enumerate((a, b, c), start=1):
            if val is None:
                continue
            cell = sm.cell(row=r0, column=col, value=val if not isinstance(val, str) else _clean(val, 1500))
            cell.alignment = Alignment(vertical="top", wrap_text=True)
            if bold:
                cell.font = Font(bold=True, size=13 if col == 1 and r0 == 1 else 11)
            if fill:
                cell.fill = PatternFill("solid", fgColor=fill)
        r0 += 1

    put(meta.get("title") or "图片智能审查报告", bold=True)
    put("生成时间", time.strftime("%Y-%m-%d %H:%M:%S"))
    if meta.get("platform"):
        put("平台", meta["platform"])
    if meta.get("urls"):
        put("入口网址", None, "\n".join(meta["urls"][:10]))
    if meta.get("keyword"):
        put("关键词", meta["keyword"])
    if meta.get("mode"):
        put("采集方式", {"video": "视频抽帧(视频画面)", "all": "视频+图片+截图", "both": "图片+截图", "images": "仅图片",
                       "screenshot": "仅截图"}.get(meta["mode"], meta["mode"]))
    if meta.get("elapsed") is not None:
        put("用时(秒)", meta["elapsed"])
    st = meta.get("stats") or {}
    if st:
        put("视频数 / 抽帧数", "%s / %s" % (st.get("videos", 0), st.get("frames", 0)))
    if meta.get("walls"):
        put("登录墙截图(已排除，不在明细中)", meta["walls"])
    r0 += 1
    put("审核结论", "数量", None, bold=True, fill="D9E1F2")
    for k in ("block", "review", "pass", "error"):
        if cnt_v.get(k):
            put(VERDICT_LABEL[k], cnt_v[k], None, fill=VERDICT_FILL[k])
    put("合计", len(rows), None, bold=True)
    r0 += 1
    put("违规类别", "数量", "说明", bold=True, fill="D9E1F2")
    notes = {"涉黄": "NSFW 分类器/NudeNet/CLIP/大模型综合判定为明确色情", "涉黄(疑似/低俗)": "性暗示/低俗，建议人工复核",
             "涉政": "疑似敏感政治内容或命中涉政关键词，已拦截", "涉政(疑似/需复核)": "含政治相关元素，建议人工复核",
             "暴力血腥": "疑似血腥/暴力，建议人工复核", "暴力(提示)": "出现武器等暴力倾向，仅提示", "疑似涉未成年人": "疑似未成年人 + 性相关内容，升级拦截",
             "二维码/联系方式引流": "图中有二维码或微信/QQ/手机号/网址", "未发现违规": "通过"}
    for k, n in cnt_c.most_common():
        put(k, n, notes.get(k, ""))
    r0 += 1
    put("来源类型", "数量", None, bold=True, fill="D9E1F2")
    for k, n in cnt_k.most_common():
        put(k, n)
    r0 += 1
    put("GPU（T4 双卡）", "处理张数", None, bold=True, fill="D9E1F2")
    for k, n in sorted(cnt_g.items()):
        put(k, n)
    both = len([k for k in cnt_g if k]) >= 2
    put("两张 GPU 是否都参与审核", "是" if both else ("否（只有一张卡处理过）" if cnt_g else "未记录"))
    r0 += 1
    put("说明：本报告由 AI 自动初筛生成，涉政无统一视觉标准，边界样本请人工复核。", None, None)

    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()
