import type { AnalysisResult } from "./types";

/**
 * 「生成 Excel」（纯浏览器版，不依赖 Kaggle 服务端；断网/演示模式也能导出）。
 * 与 kaggle/report.py 的版式、违规类别规则保持一致：每行一张图（缩略图嵌入单元格）+ 结论 + 违规类别（涉政/涉黄/暴力…）+ 原因，
 * 另有「汇总」页（结论统计 / 类别统计 / 来源类型 / 两张 GPU 的处理数）。
 * exceljs 体积较大，所以点击按钮时才动态加载。
 */

export type RowKind = "frame" | "cover" | "image" | "screenshot" | "upload";

export interface ExcelRow {
  idx: number;
  kind: RowKind | string;
  name: string;
  res?: AnalysisResult | null;
  err?: string;
  page?: string;
  src?: string;
  video?: string;
  ts?: number | null;
  title?: string;
  /** 原图或缩略图（会再缩到宽 300px 内嵌入） */
  image?: Blob | null;
}

export interface ExcelMeta {
  title: string;
  platform?: string;
  urls?: string[];
  keyword?: string;
  mode?: string;
  elapsed?: number;
  videos?: number;
  frames?: number;
  /** 被识别为登录墙而排除的张数 */
  walls?: number;
}

const KIND_LABEL: Record<string, string> = { frame: "视频截图(帧)", cover: "视频封面", image: "网络图片", screenshot: "页面截图", upload: "上传图片" };
const VERDICT_LABEL: Record<string, string> = { pass: "通过", review: "待复核", block: "拦截", error: "审核失败" };
const VERDICT_FILL: Record<string, string> = { block: "FFFFC7CE", review: "FFFFEB9C", pass: "FFC6EFCE", error: "FFD9D9D9" };
const RISK_RANK: Record<string, number> = { block: 0, review: 1, pass: 2, error: 3 };
const MODE_LABEL: Record<string, string> = { video: "视频抽帧(视频画面)", all: "视频+图片+截图", both: "图片+截图", images: "仅图片", screenshot: "仅截图" };
const CAT_NOTE: Record<string, string> = {
  "涉黄": "NSFW 分类器/NudeNet/CLIP/大模型综合判定为明确色情",
  "涉黄(疑似/低俗)": "性暗示/低俗，建议人工复核",
  "涉政": "疑似敏感政治内容或命中涉政关键词，已拦截",
  "涉政(疑似/需复核)": "含政治相关元素，建议人工复核",
  "暴力血腥": "疑似血腥/暴力，建议人工复核",
  "暴力(提示)": "出现武器等暴力倾向，仅提示",
  "疑似涉未成年人": "疑似未成年人 + 性相关内容，升级拦截",
  "二维码/联系方式引流": "图中有二维码或微信/QQ/手机号/网址",
  "未发现违规": "通过",
};

/** 违规类别（与 kaggle/report.py 的 categories_of 规则一致） */
export function categoriesOf(res?: AnalysisResult | null): string[] {
  if (!res) return [];
  const out: string[] = [];
  const porn = res.porn?.level;
  const pol = res.political?.level;
  const vio = res.violence?.level;
  if (porn === "explicit") out.push("涉黄");
  else if (porn === "suggestive") out.push("涉黄(疑似/低俗)");
  if (pol === "high") out.push("涉政");
  else if (pol === "low") out.push("涉政(疑似/需复核)");
  if (vio === "high") out.push("暴力血腥");
  else if (vio === "low") out.push("暴力(提示)");
  if (res.minors_suspected && (porn === "explicit" || porn === "suggestive")) out.push("疑似涉未成年人");
  if ((res.aux?.qr && res.aux.qr.length) || (res.aux?.contacts && res.aux.contacts.length)) out.push("二维码/联系方式引流");
  return out;
}

export function categoryText(res: AnalysisResult | null | undefined, verdict: string): string {
  const c = categoriesOf(res);
  if (c.length) return c.join("、");
  return verdict === "pass" ? "未发现违规" : verdict === "review" ? "需人工复核" : "";
}

// eslint-disable-next-line no-control-regex
const ILLEGAL = /[\u0000-\u0008\u000b\u000c\u000e-\u001f]/g;
const clean = (v: unknown, limit = 3000): string => (v == null ? "" : String(v)).replace(ILLEGAL, "").slice(0, limit);

interface Thumb {
  dataUrl: string;
  w: number;
  h: number;
}

/** 图片 Blob → 宽高 ≤300px 的 JPEG dataURL（xlsx 体积小、打开快） */
export async function blobToThumb(blob: Blob, maxSide = 300): Promise<Thumb | null> {
  try {
    const url = URL.createObjectURL(blob);
    try {
      const img = await new Promise<HTMLImageElement>((res, rej) => {
        const im = new Image();
        im.onload = () => res(im);
        im.onerror = () => rej(new Error("decode"));
        im.src = url;
      });
      const k = Math.min(1, maxSide / Math.max(img.naturalWidth, img.naturalHeight));
      const w = Math.max(1, Math.round(img.naturalWidth * k));
      const h = Math.max(1, Math.round(img.naturalHeight * k));
      const c = document.createElement("canvas");
      c.width = w;
      c.height = h;
      const ctx = c.getContext("2d");
      if (!ctx) return null;
      ctx.fillStyle = "#fff";
      ctx.fillRect(0, 0, w, h);
      ctx.drawImage(img, 0, 0, w, h);
      return { dataUrl: c.toDataURL("image/jpeg", 0.8), w, h };
    } finally {
      URL.revokeObjectURL(url);
    }
  } catch {
    return null;
  }
}

/** 缩略图在单元格里的显示尺寸：宽 150，高不超过 160 */
function displaySize(t: Thumb): { width: number; height: number } {
  let width = 150;
  let height = Math.max(40, Math.round((t.h * 150) / t.w));
  if (height > 160) {
    height = 160;
    width = Math.max(40, Math.round((t.w * 160) / t.h));
  }
  return { width, height };
}

const verdictOf = (r: ExcelRow): string => (r.res && VERDICT_LABEL[r.res.verdict] ? r.res.verdict : "error");

/** 生成 Excel 文件（Blob） */
export async function buildExcel(rows: ExcelRow[], meta: ExcelMeta): Promise<Blob> {
  const ExcelJS = (await import("exceljs")).default;
  const wb = new ExcelJS.Workbook();
  wb.creator = "图片智能审查系统";
  wb.created = new Date();
  const sm = wb.addWorksheet("汇总");
  const ws = wb.addWorksheet("审核明细", { views: [{ state: "frozen", xSplit: 2, ySplit: 1 }] });

  const sorted = rows.slice().sort((a, b) => {
    const ra = RISK_RANK[verdictOf(a)] ?? 3;
    const rb = RISK_RANK[verdictOf(b)] ?? 3;
    if (ra !== rb) return ra - rb;
    const sa = Math.max(a.res?.porn?.score ?? 0, a.res?.political?.score ?? 0);
    const sb = Math.max(b.res?.porn?.score ?? 0, b.res?.political?.score ?? 0);
    return sb - sa || a.idx - b.idx;
  });

  const heads = ["序号", "截图/照片", "来源类型", "名称", "视频时间点(秒)", "审核结论", "违规类别", "涉黄分", "涉政分", "暴力分", "判定原因", "AI 中文描述", "图中文字(OCR)", "命中关键词", "来源页面", "视频/图片地址", "处理 GPU"];
  const widths = [6, 24, 13, 26, 10, 10, 24, 8, 8, 8, 42, 42, 32, 20, 34, 34, 18];
  const thin = { style: "thin" as const, color: { argb: "FFBFBFBF" } };
  const border = { left: thin, right: thin, top: thin, bottom: thin };
  const hr = ws.getRow(1);
  hr.height = 26;
  heads.forEach((h, i) => {
    const c = hr.getCell(i + 1);
    c.value = h;
    c.font = { bold: true, color: { argb: "FFFFFFFF" } };
    c.fill = { type: "pattern", pattern: "solid", fgColor: { argb: "FF1F3864" } };
    c.alignment = { horizontal: "center", vertical: "middle", wrapText: true };
    c.border = border;
    ws.getColumn(i + 1).width = widths[i];
  });

  const cntV = new Map<string, number>();
  const cntC = new Map<string, number>();
  const cntK = new Map<string, number>();
  const cntG = new Map<string, number>();
  const inc = (m: Map<string, number>, k: string) => m.set(k, (m.get(k) ?? 0) + 1);

  for (let i = 0; i < sorted.length; i++) {
    const r = sorted[i];
    const rowNo = i + 2;
    const res = r.res ?? null;
    const v = verdictOf(r);
    const cats = categoriesOf(res);
    const kws = [...(res?.keywords?.political ?? []), ...(res?.keywords?.porn ?? [])];
    const gpu = clean(res?.gpu);
    const kind = KIND_LABEL[String(r.kind)] ?? String(r.kind ?? "");
    inc(cntV, v);
    inc(cntK, kind);
    if (gpu) inc(cntG, gpu);
    for (const c of cats.length ? cats : v === "pass" ? ["未发现违规"] : []) inc(cntC, c);
    const link = clean(r.page);
    const vals: (string | number | null)[] = [
      r.idx, null, kind, clean(r.title || r.name), r.ts ?? "", VERDICT_LABEL[v] ?? v,
      res ? categoryText(res, v) : clean(r.err || "审核失败"),
      res ? Math.round((res.porn?.score ?? 0) * 1000) / 1000 : "",
      res ? Math.round((res.political?.score ?? 0) * 1000) / 1000 : "",
      res ? Math.round((res.violence?.score ?? 0) * 1000) / 1000 : "",
      clean((res?.reasons ?? []).join("；")), clean(res?.description), clean(res?.ocr_text), clean(kws.join("、")),
      link, clean(r.video || r.src), gpu,
    ];
    const row = ws.getRow(rowNo);
    vals.forEach((val, ci) => {
      const c = row.getCell(ci + 1);
      c.value = val as string | number | null;
      c.alignment = { vertical: "top", wrapText: true };
      c.border = border;
      if (ci === 5 || ci === 6) {
        c.fill = { type: "pattern", pattern: "solid", fgColor: { argb: VERDICT_FILL[v] ?? "FFFFFFFF" } };
        c.font = { bold: true };
      }
    });
    if (link.startsWith("http")) {
      row.getCell(15).value = { text: link, hyperlink: link };
      row.getCell(15).font = { color: { argb: "FF0563C1" }, underline: true };
    }
    const th = r.image ? await blobToThumb(r.image) : null;
    if (th) {
      const id = wb.addImage({ base64: th.dataUrl, extension: "jpeg" });
      const d = displaySize(th);
      ws.addImage(id, { tl: { col: 1.05, row: rowNo - 1 + 0.05 }, ext: d });
      row.height = Math.max(60, d.height * 0.75 + 8);
    } else {
      row.getCell(2).value = "（无图）";
      row.height = 40;
    }
  }
  ws.autoFilter = { from: { row: 1, column: 1 }, to: { row: Math.max(1, sorted.length + 1), column: heads.length } };

  /* ---------------- 汇总 ---------------- */
  sm.getColumn(1).width = 28;
  sm.getColumn(2).width = 16;
  sm.getColumn(3).width = 70;
  let r0 = 1;
  const put = (a: string | number | null, b?: string | number | null, c?: string | number | null, o: { bold?: boolean; fill?: string } = {}) => {
    const row = sm.getRow(r0++);
    [a, b, c].forEach((val, i) => {
      if (val == null || val === undefined) return;
      const cell = row.getCell(i + 1);
      cell.value = typeof val === "string" ? clean(val, 1500) : val;
      cell.alignment = { vertical: "top", wrapText: true };
      if (o.bold) cell.font = { bold: true, size: r0 === 2 && i === 0 ? 13 : 11 };
      if (o.fill) cell.fill = { type: "pattern", pattern: "solid", fgColor: { argb: o.fill } };
    });
  };
  put(meta.title || "图片智能审查报告", null, null, { bold: true });
  put("生成时间", new Date().toLocaleString("zh-CN", { hour12: false }));
  if (meta.platform) put("平台", meta.platform);
  if (meta.urls?.length) put("入口网址", null, meta.urls.slice(0, 10).join("\n"));
  if (meta.keyword) put("关键词", meta.keyword);
  if (meta.mode) put("采集方式", MODE_LABEL[meta.mode] ?? meta.mode);
  if (meta.elapsed != null) put("用时(秒)", meta.elapsed);
  if (meta.videos != null || meta.frames != null) put("视频数 / 抽帧数", `${meta.videos ?? 0} / ${meta.frames ?? 0}`);
  if (meta.walls) put("登录墙截图(已排除，不在明细中)", meta.walls);
  r0++;
  put("审核结论", "数量", null, { bold: true, fill: "FFD9E1F2" });
  for (const k of ["block", "review", "pass", "error"]) if (cntV.get(k)) put(VERDICT_LABEL[k], cntV.get(k)!, null, { fill: VERDICT_FILL[k] });
  put("合计", sorted.length, null, { bold: true });
  r0++;
  put("违规类别", "数量", "说明", { bold: true, fill: "FFD9E1F2" });
  for (const [k, n] of [...cntC.entries()].sort((a, b) => b[1] - a[1])) put(k, n, CAT_NOTE[k] ?? "");
  r0++;
  put("来源类型", "数量", null, { bold: true, fill: "FFD9E1F2" });
  for (const [k, n] of [...cntK.entries()].sort((a, b) => b[1] - a[1])) put(k, n);
  r0++;
  put("GPU（T4 双卡）", "处理张数", null, { bold: true, fill: "FFD9E1F2" });
  for (const [k, n] of [...cntG.entries()].sort()) put(k, n);
  put("两张 GPU 是否都参与审核", cntG.size >= 2 ? "是" : cntG.size === 1 ? "否（只有一张卡处理过）" : "未记录（演示模式或未连接 GPU）");
  r0++;
  put("说明：本报告由 AI 自动初筛生成，涉政无统一视觉标准，边界样本请人工复核。");

  const buf = await wb.xlsx.writeBuffer();
  return new Blob([buf], { type: "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" });
}

export function downloadBlob(name: string, blob: Blob): void {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
}
