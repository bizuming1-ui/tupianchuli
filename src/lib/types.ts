export type Verdict = "pass" | "review" | "block";

/** 归一化坐标 [x0, y0, x1, y1]（0~1，相对整张图） */
export type Box = [number, number, number, number];

export interface Detection {
  /** nude=NudeNet 身体部位 | object=OWLv2 目标 | text=OCR 文字 | qr=二维码 */
  kind: "nude" | "object" | "text" | "qr" | string;
  label: string;
  cn: string;
  score: number;
  box: Box;
  /** 该检测框是否构成风险证据（红框） */
  flag?: boolean;
}

export interface Region {
  box: Box;
  nsfw: number;
  clip_porn: number;
  clip_political: number;
  clip_violence: number;
}

export interface AnalysisResult {
  verdict: Verdict;
  porn: { score: number; level: "none" | "suggestive" | "explicit" | string };
  political: { score: number; level: "none" | "low" | "high" | string; labels: string[] };
  /** v2.0 新增：暴力血腥（仅提示/复核） */
  violence?: { score: number; level: "none" | "low" | "high" | string };
  reasons: string[];
  description: string;
  /** v2.0 新增：区域分块得分、检测框、辅助线索 */
  regions?: Region[];
  detections?: Detection[];
  aux?: { contacts?: string[]; qr?: string[]; ocr_lines?: number; faces?: number; tiles?: number };
  keywords?: { political?: string[]; porn?: string[] };
  minors_suspected?: boolean;
  size?: [number, number];
  ocr_text?: string;
  /** kaggle-gpu+vlm | kaggle-gpu | demo | mock */
  engine: string;
  gpu?: string;
  scene?: string;
  cached?: boolean;
  signals?: {
    nsfw_classifier?: number;
    clip_porn?: number;
    clip_suggestive?: number;
    clip_political?: number;
    vlm_porn?: string | null;
    vlm_political?: string | null;
    vlm_violence?: string | null;
    vlm_model?: string | null;
    nsfw_ensemble?: number;
    nsfw_models?: Record<string, number>;
    clip_violence?: number;
    nudenet?: string | null;
  };
  timings?: {
    total_ms?: number; classifier_ms?: number; clip_ms?: number; vlm_ms?: number;
    detector_ms?: number; nudenet_ms?: number; ocr_ms?: number; wait_ms?: number;
  };
  warnings?: string[];
}

export interface GpuStat {
  index: number;
  device: string;
  ready: boolean;
  busy: boolean;
  processed: number;
  avg_ms: number;
  vlm: string | null;
  mem_used_gb?: number;
  mem_total_gb?: number;
  modules?: Record<string, unknown>;
  notes?: string[];
}

export interface HealthInfo {
  status: "ok" | "loading" | "error";
  ready: boolean;
  version: string;
  mock: boolean;
  replicas: number;
  replicas_ready: number;
  gpus: { index: number; name: string; memory_gb: number }[];
  models: {
    nsfw: string; clip: string; vlm: string | null;
    nsfw_all?: string[]; detector?: string | null; nudenet?: string | null; ocr?: boolean; qr?: boolean;
  };
  profile?: string;
  gpu_stats?: GpuStat[];
  vlm_enabled: boolean;
  inflight: number;
  processed: number;
  avg_ms: number;
  error: string | null;
  warnings?: string[];
  loading_done?: boolean;
  /** v2.1：爬取模块能力（playwright/chromium 是否可用） */
  crawler?: { playwright: boolean; chromium: boolean; engine: string };
}

export type ItemStatus = "queued" | "running" | "done" | "error";

export interface ScanItem {
  id: number;
  file: File;
  name: string;
  status: ItemStatus;
  thumb?: string;
  res?: AnalysisResult;
  err?: string;
  /** 每次变化 +1，用于 React.memo 精准刷新 */
  rev: number;
  saved?: boolean;
  elapsedMs?: number;
}
