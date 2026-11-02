import type { Metadata } from "next";
import CodeViewer from "@/components/CodeViewer";

export const metadata: Metadata = { title: "复制代码" };

export default function CodePage() {
  return <CodeViewer />;
}
