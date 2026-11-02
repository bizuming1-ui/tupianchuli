# ===== Kaggle Cell 4：启动 GPU 服务 + 生成公网网址（每次重启 Notebook 都要运行）=====
import os, re, subprocess, sys, time, threading, urllib.request, json

APP_DIR = "/kaggle/working/app"
PORT = 8000
os.chdir(APP_DIR)

# ---- 可选：运行档位 PROFILE（取消注释即可）----
# fast     : 只用 Qwen2-VL-2B（最快，吞吐最高）
# balanced : 默认。Qwen2.5-VL-3B（更强），自检失败自动回退到 Qwen2-VL-2B；两张卡各一份，同时处理两张图
# max      : Qwen2.5-VL-7B 两张卡切分（最准最慢；失败会依次回退 3B / 2B）
# os.environ["PROFILE"] = "max"
# 自定义涉政 / 涉黄关键词（逗号分隔）:
# os.environ["POLITICAL_KEYWORDS"] = "词1,词2"
# os.environ["PORN_KEYWORDS"] = "词1,词2"
# 关闭某个辅助工具（排查问题时用）: DISABLE_OWL / DISABLE_NUDENET / DISABLE_OCR / DISABLE_TILES / DISABLE_VLM = "1"

# 1) 先杀掉旧进程，避免端口占用
subprocess.run("pkill -f '[a]pp/server.py' ; pkill -f '[c]loudflared' ; sleep 1", shell=True)  # [a] 写法避免 pkill 误杀自己

# 2) 启动服务（后台），日志写入文件
log = open("/kaggle/working/server.log", "w")
# USE_TF/USE_FLAX=0：避免 transformers 去 import Kaggle 里版本冲突的 TensorFlow/Flax
env = dict(os.environ, PORT=str(PORT), HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false",
           USE_TF="0", USE_FLAX="0", PYTORCH_CUDA_ALLOC_CONF="expandable_segments:True")
server = subprocess.Popen([sys.executable, f"{APP_DIR}/server.py"], cwd=APP_DIR, stdout=log, stderr=subprocess.STDOUT, env=env)

def health():
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/health", timeout=3) as r:
            return json.loads(r.read())
    except Exception:
        return None

# 3) 建立公网隧道（先开隧道，模型加载期间你就可以先打开网页）
public_url = None
try:
    from kaggle_secrets import UserSecretsClient
    token = UserSecretsClient().get_secret("NGROK_AUTHTOKEN")
except Exception:
    token = None

if token:  # 配置了 ngrok 密钥则用 ngrok（更稳定）
    from pyngrok import ngrok
    ngrok.set_auth_token(token)
    public_url = ngrok.connect(PORT, bind_tls=True).public_url
else:
    cf = subprocess.Popen(["/kaggle/working/cloudflared", "tunnel", "--url", f"http://127.0.0.1:{PORT}", "--no-autoupdate"],
                          stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    t0 = time.time()
    while time.time() - t0 < 60 and not public_url:
        line = cf.stdout.readline()
        m = re.search(r"https://[-a-z0-9]+\.trycloudflare\.com", line or "")
        if m:
            public_url = m.group(0)
    threading.Thread(target=lambda: [None for _ in cf.stdout], daemon=True).start()  # 持续消费输出，防止管道堵塞

print("=" * 66)
print("🌐 公网网址（复制到浏览器模拟器 / 网站「设置」里的 Kaggle 地址）:")
print("   ", public_url)
print("   该网址本身就是一个完整的网页版审查系统，可直接打开使用")
print("=" * 66)

def report(h):
    m = h.get("models", {})
    print(f"   分类器: {m.get('nsfw_all')} | CLIP: {m.get('clip')}")
    print(f"   视觉大模型: {m.get('vlm')} | 目标检测: {m.get('detector')} | NudeNet: {m.get('nudenet')} | OCR: {m.get('ocr')}")
    for g in h.get("gpu_stats", []):
        mem = f"{g.get('mem_used_gb')}/{g.get('mem_total_gb')}GB" if "mem_used_gb" in g else "-"
        print(f"   GPU{g['index']} {g['device']}: 就绪={g['ready']} 显存={mem} 大模型={g.get('vlm')}")

# 4) 等待模型加载（首次需要下载约 8~12GB，约 4~10 分钟）。两张 GPU 是【串行】加载的：GPU0 好了就能用，GPU1 在后台继续。
t0 = time.time()
last = ""
announced = False
while time.time() - t0 < 2400:
    h = health()
    if h and h.get("ready"):
        if not announced:
            announced = True
            print(f"✅ 可以使用了！已就绪 GPU 副本 {h['replicas_ready']}/{h['replicas']}，GPU: {[g['name'] for g in h['gpus']]}")
        if h.get("loading_done"):
            print(f"✅ 全部加载完成：副本 {h['replicas_ready']}/{h['replicas']}（v{h.get('version')} · {h.get('profile')}）")
            report(h)
            for w in h.get("warnings") or []:
                print("⚠️ 降级/提示（系统已自动处理并继续运行）:", w)
            break
    elif h and h.get("error"):
        print("❌ 加载失败:", h["error"]); print(open('/kaggle/working/server.log').read()[-2500:]); break
    if server.poll() is not None:
        print("❌ 服务进程退出，日志如下："); print(open('/kaggle/working/server.log').read()[-3000:]); break
    tail = [l for l in open("/kaggle/working/server.log").read().strip().split("\n") if "INFO" in l or "WARNING" in l or "ERROR" in l]
    tail = (tail[-1] if tail else "")[-110:]
    if tail != last:
        print("…加载中:", tail); last = tail
    time.sleep(5)
print("提示：保持此 Cell 所在 Notebook 运行；日志: /kaggle/working/server.log；网址:", public_url)
