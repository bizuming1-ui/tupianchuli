import type { Metadata } from "next";
import ModerationApp from "@/components/ModerationApp";

export const metadata: Metadata = { title: "图片审查系统" };

export default function ToolPage() {
  return <ModerationApp />;
}
