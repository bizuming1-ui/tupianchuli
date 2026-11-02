import type { AnalysisResult, GpuStat } from "./types";

/** 平台预设：与 kaggle/crawler.py 的 PLATFORMS 保持一致 */
export const PLATFORMS: { id: string; name: string; icon: string; hint: string; placeholder: string }[] = [
  { id: "xiaohongshu", name: "小红书", icon: "📕", hint: "笔记/搜索页/主页链接", placeholder: "https://www.xiaohongshu.com/explore/…" },
  { id: "douyin", name: "抖音", icon: "🎵", hint: "作品/图文/搜索页链接，支持 v.douyin.com 短链", placeholder: "https://www.douyin.com/note/… 或 https://v.douyin.com/…" },
  { id: "weibo", name: "微博", icon: "🌐", hint: "微博详情页/搜索页", placeholder: "https://weibo.com/… 或 https://s.weibo.com/weibo?q=…" },
  { id: "bilibili", name: "哔哩哔哩", icon: "📺", hint: "视频封面/动态/专栏", placeholder: "https://www.bilibili.com/video/BV…" },
  { id: "baidu", name: "百度图片", icon: "🔍", hint: "关键词图片搜索", placeholder: "留空，仅填关键词即可" },
  { id: "bing", name: "必应图片", icon: "🅱️", hint: "关键词图片搜索", placeholder: "留空，仅填关键词即可" },
  { id: "custom", name: "任意网址", icon: "🌍", hint: "任何网页 / 直接图片链接", placeholder: "https://…" },
];

export interface CrawlForm {
  platform: string;
  urlsText: string;
  keyword: string;
  /** video=只要视频画面(默认) | all=视频+图片+截图 | both=图片+截图(旧) | images | screenshot */
  mode: "video" | "all" | "both" | "images" | "screenshot";
  max_images: number;
  /** 每个视频抽几帧 */
  frames_per_video: number;
  /** 最多处理几个视频 */
  video_limit: number;
  scrolls: number;
  min_size: number;
  delay: number;
  deep: boolean;
  deep_limit: number;
  mobile: boolean;
  cookie: string;
  analyze_mode: "full" | "fast";
}

export const DEFAULT_FORM: CrawlForm = {
  platform: "douyin", urlsText: "", keyword: "", mode: "video", max_images: 60, frames_per_video: 6, video_limit: 10, scrolls: 8, min_size: 200,
  delay: 0.8, deep: true, deep_limit: 12, mobile: false, cookie: "", analyze_mode: "full",
};

export type CrawlItemStatus = "queued" | "running" | "done" | "error" | "skipped";

export interface CrawlItem {
  idx: number;
  kind: "image" | "screenshot" | "frame" | "cover";
  src: string;
  page: string;
  w: number;
  h: number;
  bytes: number;
  name: string;
  status: CrawlItemStatus;
  res?: AnalysisResult;
  err?: string;
  rev: number;
  t: number;
  ms?: number;
  /** v2.2：被识别为登录/验证码墙截图（已排除出统计与 Excel） */
  wall?: boolean;
  /** v2.2：视频帧所属视频编号 / 时间点(秒) / 标题 / 视频地址 */
  vid?: number;
  ts?: number;
  title?: string;
  video?: string;
}

export type CrawlState = "queued" | "running" | "done" | "stopped" | "error";

export interface CrawlView {
  id: string;
  state: CrawlState;
  error: string | null;
  engine: string;
  rev: number;
  elapsed: number;
  stats: { pages: number; found: number; screenshots: number; analyzed: number; errors: number; dup: number; small: number; pass: number; review: number; block: number;
    frames?: number; videos?: number; video_fail?: number; walls?: number; wall_pages?: number; wall_skip?: number; ui?: number };
  pending: number;
  conc: number;
  gpu_count: Record<string, number>;
  cap_hit: boolean;
  video_cap_hit?: boolean;
  params?: { urls?: string[]; mode?: string; platform?: string };
  logs: string[];
  total: number;
  items: CrawlItem[];
  gpu_stats?: GpuStat[];
}

const H = { "ngrok-skip-browser-warning": "1" };

async function json<T>(res: Response): Promise<T> {
  const data = (await res.json().catch(() => ({}))) as T & { detail?: string };
  if (!res.ok) throw new Error(data.detail || `服务返回 ${res.status}`);
  return data;
}

export async function startCrawl(base: string, f: CrawlForm): Promise<{ id: string; urls: string[]; engine: string }> {
  const body = {
    urls: f.urlsText.split(/\n+/).map((s) => s.trim()).filter(Boolean),
    platform: f.platform, keyword: f.keyword.trim(), mode: f.mode, max_images: f.max_images, scrolls: f.scrolls,
    frames_per_video: f.frames_per_video, video_limit: f.video_limit,
    min_size: f.min_size, delay: f.delay, deep: f.deep, deep_limit: f.deep_limit, mobile: f.mobile, cookie: f.cookie.trim(),
    analyze_mode: f.analyze_mode,
  };
  const res = await fetch(`${base}/api/crawl/start`, { method: "POST", headers: { ...H, "content-type": "application/json" }, body: JSON.stringify(body) });
  return json(res);
}

export async function pollCrawl(base: string, id: string, since: number, signal?: AbortSignal): Promise<CrawlView> {
  return json(await fetch(`${base}/api/crawl/${id}?since=${since}`, { headers: H, signal }));
}

export async function stopCrawl(base: string, id: string): Promise<void> {
  await fetch(`${base}/api/crawl/${id}/stop`, { method: "POST", headers: H });
}

/** 图片带请求头获取（兼容 ngrok 的浏览器警告页），结果缓存为 blob URL */
const blobCache = new Map<string, Promise<string>>();
export function crawlImage(base: string, id: string, idx: number, thumb: boolean): Promise<string> {
  const url = `${base}/api/crawl/${id}/${thumb ? "thumb" : "img"}/${idx}`;
  let p = blobCache.get(url);
  if (!p) {
    p = fetch(url, { headers: H })
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error("HTTP " + r.status))))
      .then((b) => URL.createObjectURL(b));
    p.catch(() => blobCache.delete(url));
    blobCache.set(url, p);
  }
  return p;
}
export function clearCrawlImages(): void {
  for (const p of blobCache.values()) void p.then((u) => URL.revokeObjectURL(u)).catch(() => undefined);
  blobCache.clear();
}
