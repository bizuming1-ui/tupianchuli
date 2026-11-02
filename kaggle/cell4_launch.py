# ===== Kaggle Cell 4：启动 GPU 服务 + 生成公网网址（每次重启 Notebook 都要运行）=====
import os, re, subprocess, sys, time, threading, urllib.request, json

APP_DIR = "/kaggle/working/app"
PORT = 8000
os.chdir(APP_DIR)

# ---- 可选：换更强的大模型（取消注释即可）----
# 7B 在 T4 上需要两张卡切分(fp16 约 16GB)，吞吐更低但描述/判断更准:
# os.environ["VLM_MODEL"] = "Qwen/Qwen2-VL-7B-Instruct"; os.environ["VLM_SHARD"] = "1"
# 自定义涉政关键词（逗号分隔）:
# os.environ["POLITICAL_KEYWORDS"] = "词1,词2"

# 1) 先杀掉旧进程，避免端口占用
subprocess.run("pkill -f '[a]pp/server.py' ; pkill -f '[c]loudflared' ; sleep 1", shell=True)  # [a] 写法避免 pkill 误杀自己

# 2) 启动服务（后台），日志写入文件
log = open("/kaggle/working/server.log", "w")
env = dict(os.environ, PORT=str(PORT), HF_HUB_DISABLE_TELEMETRY="1", TOKENIZERS_PARALLELISM="false")
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

# 4) 等待模型加载（首次需要下载约 6~8GB，约 3~8 分钟）。两张 GPU 是【串行】加载的：GPU0 好了就能用，GPU1 在后台继续。
t0 = time.time()
last = ""
announced = False
while time.time() - t0 < 1800:
    h = health()
    if h and h.get("ready"):
        if not announced:
            announced = True
            print(f"✅ 可以使用了！已就绪 GPU 副本 {h['replicas_ready']}/{h['replicas']}，GPU: {[g['name'] for g in h['gpus']]}，大模型: {h['models']['vlm']}")
        if h.get("loading_done"):
            print(f"✅ 全部加载完成：副本 {h['replicas_ready']}/{h['replicas']}，大模型: {h['models']['vlm']}")
            for w in h.get("warnings") or []:
                print("⚠️ 有副本加载失败（系统已自动降级继续运行）:", w)
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
