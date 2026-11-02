import type { AnalysisResult, HealthInfo } from "./types";

export function normalizeApiUrl(raw: string): string {
  let u = raw.trim();
  if (!u) return "";
  if (!/^https?:\/\//i.test(u)) u = "https://" + u;
  return u.replace(/\/+$/, "").replace(/\/api\/?$/, "");
}

export interface Prepared {
  blob: Blob;
  thumb: string;
  width: number;
  height: number;
  bitmap?: ImageBitmap;
}

async function decode(file: Blob): Promise<{ src: CanvasImageSource; w: number; h: number; close: () => void }> {
  if (typeof createImageBitmap === "function") {
    try {
      const bmp = await createImageBitmap(file);
      return { src: bmp, w: bmp.width, h: bmp.height, close: () => bmp.close() };
    } catch {
      /* 降级 */
    }
  }
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise<HTMLImageElement>((res, rej) => {
      const im = new Image();
      im.onload = () => res(im);
      im.onerror = () => rej(new Error("无法读取该图片"));
      im.src = url;
    });
    return { src: img, w: img.naturalWidth, h: img.naturalHeight, close: () => URL.revokeObjectURL(url) };
  } catch (e) {
    URL.revokeObjectURL(url);
    throw e;
  }
}

function toBlob(c: HTMLCanvasElement, quality: number): Promise<Blob> {
  return new Promise((res, rej) => c.toBlob((b) => (b ? res(b) : rej(new Error("编码失败"))), "image/jpeg", quality));
}

/**
 * 上传前在浏览器里压缩到最长边 1280px：
 * 体积通常缩小 5~20 倍，网络和 GPU 预处理都明显更快，对审查精度几乎无影响。
 */
export async function prepareImage(file: File, max = 1280): Promise<Prepared> {
  const d = await decode(file);
  try {
    const r = Math.min(1, max / Math.max(d.w, d.h));
    const c = document.createElement("canvas");
    c.width = Math.max(1, Math.round(d.w * r));
    c.height = Math.max(1, Math.round(d.h * r));
    const ctx = c.getContext("2d")!;
    ctx.fillStyle = "#fff";
    ctx.fillRect(0, 0, c.width, c.height);
    ctx.drawImage(d.src, 0, 0, c.width, c.height);
    const blob = r < 1 || file.size > 1.5 * 1024 * 1024 ? await toBlob(c, 0.86) : file;

    const tr = Math.min(1, 160 / Math.max(d.w, d.h));
    const t = document.createElement("canvas");
    t.width = Math.max(1, Math.round(d.w * tr));
    t.height = Math.max(1, Math.round(d.h * tr));
    t.getContext("2d")!.drawImage(d.src, 0, 0, t.width, t.height);
    return { blob, thumb: t.toDataURL("image/jpeg", 0.6), width: d.w, height: d.h };
  } finally {
    d.close();
  }
}

export async function fetchHealth(base: string, signal?: AbortSignal): Promise<HealthInfo> {
  const res = await fetch(`${base}/api/health`, { signal, headers: { "ngrok-skip-browser-warning": "1" } });
  if (!res.ok) throw new Error("HTTP " + res.status);
  return (await res.json()) as HealthInfo;
}

export async function analyzeRemote(base: string, blob: Blob, name: string, mode: "full" | "fast"): Promise<AnalysisResult> {
  let lastErr: unknown = null;
  for (let attempt = 0; attempt < 4; attempt++) {
    try {
      const fd = new FormData();
      fd.append("file", blob, name);
      fd.append("mode", mode);
      const res = await fetch(`${base}/api/analyze`, { method: "POST", body: fd, headers: { "ngrok-skip-browser-warning": "1" } });
      if (res.ok) return (await res.json()) as AnalysisResult;
      const detail = ((await res.json().catch(() => ({}))) as { detail?: string }).detail;
      if (res.status === 400 || res.status === 413) throw Object.assign(new Error(detail || "图片无法处理"), { fatal: true });
      lastErr = new Error(detail || "服务返回 " + res.status);
    } catch (e) {
      if ((e as { fatal?: boolean }).fatal) throw e;
      lastErr = e;
    }
    await new Promise((r) => setTimeout(r, 800 * (attempt + 1) ** 2));
  }
  throw lastErr instanceof Error ? lastErr : new Error("网络错误");
}

export async function askRemote(base: string, blob: Blob, question: string): Promise<string> {
  const fd = new FormData();
  fd.append("file", blob, "q.jpg");
  fd.append("question", question);
  const res = await fetch(`${base}/api/ask`, { method: "POST", body: fd, headers: { "ngrok-skip-browser-warning": "1" } });
  const data = (await res.json().catch(() => ({}))) as { answer?: string; detail?: string };
  if (!res.ok) throw new Error(data.detail || "追问失败");
  return data.answer || "";
}

const COLOR_NAMES: [string, (h: number, s: number, v: number) => boolean][] = [
  ["黑色", (_h, _s, v) => v < 0.18],
  ["白色", (_h, s, v) => s < 0.1 && v > 0.85],
  ["灰色", (_h, s) => s < 0.12],
  ["红色", (h) => h < 15 || h >= 345],
  ["橙色", (h) => h < 40],
  ["黄色", (h) => h < 70],
  ["绿色", (h) => h < 165],
  ["青色", (h) => h < 200],
  ["蓝色", (h) => h < 260],
  ["紫色", (h) => h < 300],
  ["粉色", (h) => h < 345],
];

/**
 * 演示模式（没有 GPU 时的本地兜底）：仅基于肤色占比 / 主色调 / 尺寸的粗略启发式。
 * 它无法理解语义，更无法判断涉政——结果会被明确标记为 demo，只有连接 Kaggle GPU 后才是 AI 审查。
 */
export async function analyzeDemo(file: Blob, width: number, height: number): Promise<AnalysisResult> {
  const d = await decode(file);
  try {
    const S = 64;
    const c = document.createElement("canvas");
    c.width = S;
    c.height = S;
    const ctx = c.getContext("2d", { willReadFrequently: true })!;
    ctx.drawImage(d.src, 0, 0, S, S);
    const px = ctx.getImageData(0, 0, S, S).data;
    let skin = 0;
    let bright = 0;
    const buckets: Record<string, number> = {};
    for (let i = 0; i < px.length; i += 4) {
      const r = px[i], g = px[i + 1], b = px[i + 2];
      const y = 0.299 * r + 0.587 * g + 0.114 * b;
      const cb = 128 - 0.168736 * r - 0.331264 * g + 0.5 * b;
      const cr = 128 + 0.5 * r - 0.418688 * g - 0.081312 * b;
      if (y > 40 && cb >= 77 && cb <= 127 && cr >= 133 && cr <= 173 && r > g && r > b) skin++;
      bright += y;
      const mx = Math.max(r, g, b) / 255, mn = Math.min(r, g, b) / 255;
      const df = mx - mn;
      let h = 0;
      if (df > 0) {
        if (mx === r / 255) h = ((g - b) / 255 / df + 6) % 6;
        else if (mx === g / 255) h = (b - r) / 255 / df + 2;
        else h = (r - g) / 255 / df + 4;
      }
      const name = COLOR_NAMES.find(([, f]) => f(h * 60, mx === 0 ? 0 : df / mx, mx))?.[0] ?? "混合色";
      buckets[name] = (buckets[name] || 0) + 1;
    }
    const n = S * S;
    const skinRatio = skin / n;
    const avg = bright / n;
    const main = Object.entries(buckets).sort((a, b) => b[1] - a[1]).slice(0, 2).map(([k]) => k);
    const orient = width > height * 1.15 ? "横向" : height > width * 1.15 ? "竖向" : "方形";
    const tone = avg < 70 ? "偏暗" : avg > 180 ? "明亮" : "亮度适中";
    const description = `${orient}图片（${width}×${height}），整体${tone}，主色调为${main.join("、")}${skinRatio > 0.25 ? "，画面含较多肤色区域" : ""}。`;

    const porn = Math.min(1, Math.max(0, (skinRatio - 0.3) / 0.4));
    const review = skinRatio > 0.45;
    return {
      verdict: review ? "review" : "pass",
      porn: { score: Number(porn.toFixed(3)), level: review ? "suggestive" : "none" },
      political: { score: 0, level: "none", labels: [] },
      reasons: review ? [`肤色占比 ${(skinRatio * 100).toFixed(0)}%，可能涉黄（演示模式仅为粗略估计，请连接 GPU 精确审查）`] : [],
      description,
      engine: "demo",
      scene: main[0],
      warnings: ["演示模式：无法判断涉政，也无法理解画面内容；请连接 Kaggle GPU"],
      signals: { clip_political: 0 },
    };
  } finally {
    d.close();
  }
}
