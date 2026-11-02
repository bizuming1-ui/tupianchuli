import { useCallback, useEffect, useState } from "react";

export type Route = "app" | "code" | "site";

function parse(): Route {
  const h = window.location.hash;
  if (h.startsWith("#/code")) return "code";
  if (h.startsWith("#/site")) return "site";
  return "app";
}

/** 极简 hash 路由（静态托管 / GitHub Pages / Kaggle 都不需要服务端配置） */
export function useRoute(): [Route, (r: Route) => void] {
  const [route, setRoute] = useState<Route>(parse);
  useEffect(() => {
    const on = () => setRoute(parse());
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const go = useCallback((r: Route) => {
    window.location.hash = "#/" + r;
  }, []);
  return [route, go];
}
