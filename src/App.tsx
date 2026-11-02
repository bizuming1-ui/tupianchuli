import CrawlerPanel from "@/components/CrawlerPanel";
import ModerationApp from "@/components/ModerationApp";
import Simulator from "@/components/Simulator";
import { useRoute } from "@/lib/route";

/**
 * 路由（hash）：
 *   #/app        浏览器模拟器 + 图片审查网站（默认）
 *   #/crawl      浏览器模拟器 + 「自动爬取并审查网络照片」界面
 *   #/code       浏览器模拟器 + 复制代码界面
 *   #/site       纯网站（无浏览器外壳，手机/直接打开用）
 *   #/sitecrawl  纯网站版的爬取界面（无外壳）
 */
export default function App() {
  const [route, go] = useRoute();
  if (route === "site") {
    return (
      <div className="h-screen w-screen">
        <ModerationApp onOpenCrawl={() => go("sitecrawl")} />
      </div>
    );
  }
  if (route === "sitecrawl") {
    return (
      <div className="h-screen w-screen">
        <CrawlerPanel onBack={() => go("site")} />
      </div>
    );
  }
  return <Simulator route={route} go={go} />;
}
