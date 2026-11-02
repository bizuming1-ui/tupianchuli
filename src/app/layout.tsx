import type { Metadata, Viewport } from "next";
import type { ReactNode } from "react";
import "./globals.css";

export const metadata: Metadata = {
  title: "图片审查系统",
  description: "批量图片审查：涉黄 / 涉政识别 + 中文描述，Kaggle T4x2 GPU 加速。",
};

export const viewport: Viewport = { themeColor: "#0a0f1f" };

export default function RootLayout({ children }: { children: ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="bg-[#0a0f1f] text-slate-100 antialiased">{children}</body>
    </html>
  );
}
