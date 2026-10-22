#!/usr/bin/env python3
"""部署体检：配置、凭证、供应商、目录可写性、常驻进程，一条命令自查。

    python tools/doctor.py           # 静态检查（不打网络，几秒出结果）
    python tools/doctor.py --net     # 附加：对已配的每把 Key 探活一次供应商
    python tools/doctor.py --json    # 机器可读输出（每个检查一行）

退出码：0 = 全绿；1 = 有必填项缺失或文件系统不可写；2 = 只有警告。
所有检查都是**只读**或写一个 .probe-* 临时文件，不会动你的任何数据。

常用场景：
  · 第一次部署：填完 .env 后先跑一次，把 ✖ 都清掉再启动 bot.py；
  · 机器人「哑了/瞎了」：跑一次，重点看触发词、守护、常驻进程三段；
  · 换供应商：--net 一把 Key 一条延迟，不用挂日志猜。
"""

import json
import os
import platform
import subprocess
import sys
import time

TOOLS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TOOLS_DIR)

_results = []   # (level, name, detail)
_json_mode = False
# 内部等级：ok=0 < warn=1 < fail=2（退出码由 finish() 换算：绿=0、黄=2、红=1）
EXIT = {"ok": 0, "warn": 2, "fail": 1}


def rec(level, name, detail=""):
    _results.append((level, name, detail))
    if not _json_mode:
        icon = {"ok": "✔", "warn": "⚠️ ", "fail": "✖"}[level]
        print(f"{icon} {name}" + (f" —— {detail}" if detail else ""))
    return {"ok": 0, "warn": 1, "fail": 2}[level]



def mask(s):
    if not s:
        return "(空)"
    s = str(s)
    return f"****{s[-4:]}" if len(s) > 8 else "****"


def main():
    global _json_mode
    do_net = "--net" in sys.argv
    is_json = "--json" in sys.argv
    _json_mode = is_json
    if any(not a.startswith("--") for a in sys.argv[1:]):
        print(__doc__)
        return 0

    worst = 0
    # ── 1. Python 版本 ──
    py = sys.version_info
    if py >= (3, 10):
        worst = max(worst, rec("ok", f"Python {py.major}.{py.minor}.{py.micro}（需 3.10+）"))
    else:
        worst = max(worst, rec("fail", f"Python {py.major}.{py.minor}", "需要 3.10+，先升级 Python"))

    # ── 2. 五个依赖都装上了吗 ──
    for modname in ("botpy", "certifi", "httpx", "aiohttp", "openai"):
        try:
            mod = __import__(modname)
            ver = getattr(mod, "__version__", "?")
            worst = max(worst, rec("ok", f"依赖 {modname}=={ver}"))
        except Exception as e:  # noqa: BLE001
            worst = max(worst, rec("fail", f"依赖 {modname} 缺失", str(e)[:120]))

    # ── 3. .env 与 .env.example ──
    env_path = os.path.join(REPO, ".env")
    has_env = os.path.isfile(env_path)
    if has_env:
        worst = max(worst, rec("ok", ".env 存在", env_path))
    else:
        worst = max(worst, rec(
            "fail" if not os.environ.get("QQ_APP_ID") else "warn",
            "没有 .env",
            "cp .env.example .env 再填；或当前 shell 只依赖环境变量",
        ))

    # ── 4. config（会把 .env 读进环境；出错直接终止） ──
    sys.path.insert(0, REPO)
    try:
        import config  # noqa: E402
    except Exception as e:  # noqa: BLE001
        worst = max(worst, rec("fail", "config 不可导入", str(e)[:200]))
        return finish(worst, is_json)

    # ── 5. QQ 凭证 ──
    if config.QQ_APP_ID and config.QQ_APP_SECRET:
        worst = max(worst, rec("ok", "QQ_APP_ID / QQ_APP_SECRET 已填",
                               f"AppID {mask(config.QQ_APP_ID)}"))
    else:
        worst = max(worst, rec("fail", "QQ 凭证缺失",
                               "去 QQ 开放平台→开发设置，把 AppID 与 AppSecret 填进 .env"))

    # ── 6. 触发词（最大静默失效源） ──
    if config.BOT_NAMES:
        worst = max(worst, rec("ok", "触发词 BOT_NAMES", "、".join(config.BOT_NAMES)))
    else:
        worst = max(worst, rec("warn", "BOT_NAMES 为空",
                               "只认平台昵称/「机器人」——群里喊别的名字它不应。"
                               "部署前最该改的一项！"))

    # ── 7. 群主认领 ──
    # 现在没有暗号：首条非空 C2C 私聊自动认领，所以体检只看落盘的 owner.txt。
    owner_id = ""
    try:
        with open(config.OWNER_FILE, encoding="utf-8") as stream:
            owner_id = stream.read().strip()
    except OSError:
        pass
    if owner_id:
        worst = max(worst, rec(
            "ok", "群主已认领",
            f"{mask(owner_id)}（{os.path.relpath(config.OWNER_FILE, REPO)}）"))
    else:
        worst = max(worst, rec(
            "warn", "尚未认领群主",
            "首条非空 C2C 私聊会自动认领；请在 QQ 后台限制私聊范围，"
            "或手动把 OpenID 写进 owner.txt"))

    # ── 8. 供应商配置 ──
    if config.AI_KEYS:
        worst = max(worst, rec(
            "ok", f"主供应商 {config.PROVIDER['label']}",
            f"{len(config.AI_KEYS)} 把 Key（{mask(config.AI_KEYS[0])}）| "
            f"对话梯队 {' → '.join(config.CANDIDATE_MODELS)}"))
    else:
        worst = max(worst, rec("fail", f"{config.PROVIDER['label']} 的 API Key 缺失",
                               f".env 里填 {config.AI_PROVIDER.upper()}_API_KEY，"
                               f"或多把 Key 放 api_keys_{config.AI_PROVIDER}.txt（一行一把）"))
    if config.FALLBACK_PROVIDER and config.FALLBACK_PROVIDER != config.AI_PROVIDER:
        fb_keys = config.load_api_keys(config.FALLBACK_PROVIDER)
        if fb_keys:
            worst = max(worst, rec("ok", f"备用供应商 {config.FALLBACK_PROVIDER}",
                                   f"{len(fb_keys)} 把 Key"))
        else:
            worst = max(worst, rec("warn", f"备用供应商 {config.FALLBACK_PROVIDER} 没有 Key",
                                   "主供应商挂了不会降级，只会空等恢复"))

    # ── 9. 代理与 SSL ──
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") \
        or os.environ.get("HTTP_PROXY") or os.environ.get("http_proxy")
    if config.AI_PROVIDER == "gemini" and not proxy:
        worst = max(worst, rec("warn", "主供应商是 gemini 但没有代理",
                               "国内网络通常需要 HTTP(S)_PROXY；直连不了就换 zhipu 或开代理"))
    if not config.SSL_VERIFY:
        worst = max(worst, rec("warn", "SSL_VERIFY=false（本机证书兼容模式）",
                               "关着校验等于存在中间人攻击面；证书链正常时建议设"))
    else:
        worst = max(worst, rec("ok", "SSL 校验已开启"))

    # ── 10. 数据目录 / 状态文件可写 ──
    for raw, what in (
        (os.environ.get("STATE_FILE", "state.json"), "state.json"),
        (config.ARCHIVE_DIR, "归档 archive/"),
        (config.MEMORY_DIR, "摘要 memory/"),
    ):
        abs_path = raw if os.path.isabs(raw) else os.path.join(REPO, raw)
        container = abs_path if os.path.isdir(abs_path) else os.path.dirname(abs_path) or "."
        try:
            os.makedirs(container, exist_ok=True)
        except OSError as e:
            worst = max(worst, rec("fail", f"{what} 目录不可创建", str(e)[:120]))
            continue
        probe = os.path.join(container, ".probe-doctor")
        try:
            with open(probe, "w") as f:
                f.write("ok")
                f.flush()
                os.fsync(f.fileno())
            os.unlink(probe)
            worst = max(worst, rec("ok", f"{what} 可写", abs_path))
        except OSError as e:
            worst = max(worst, rec("fail", f"{what} 不可写", str(e)[:120]))

    # ── 11. 状态文件内容 ──
    state_path = os.path.join(REPO, os.environ.get("STATE_FILE", "state.json"))
    if os.path.isfile(state_path):
        try:
            with open(state_path, encoding="utf-8") as f:
                data = json.load(f)
            n = len(data.get("relations", {}))
            ng = len(data.get("modes", {}))
            ns = len(data.get("sessions", {}))
            worst = max(worst, rec("ok", "state.json 可解析",
                                   f"关系档案 {n} 条 / 群模式 {ng} 个 / 会话 {ns} 个"))
        except Exception as e:  # noqa: BLE001
            worst = max(worst, rec("fail", "state.json 损坏（可能是上次写盘被杀）",
                                   str(e)[:150]))
    else:
        worst = max(worst, rec("ok", "state.json 还没生成（首次启动会自动建）"))

    # ── 12. 敏感词表 ──
    sw = os.path.join(REPO, "sensitive_nicks.txt")
    if os.path.isfile(sw):
        worst = max(worst, rec("ok", "敏感词表在手", sw))
    else:
        worst = max(worst, rec("ok", "敏感词表未配（用代码内置词表）",
                               "要自定义就 cp sensitive_nicks.example.txt sensitive_nicks.txt"))

    # ── 14. 常驻进程 ──
    worst = max(worst, check_daemon())

    # ── 15. --net：供应商探活 ──
    if do_net:
        worst = max(worst, probe_net(config))

    # ── 16. QQ 平台侧人工核对项（代码验证不了，列出来提醒） ──
    print()
    print("🧾 QQ 开放平台人工核对（代码探测不到，两项都是「静默失效」的重灾区）:")
    print("   1) 群设置→机器人→「机器人可获取的群聊消息范围」= 获取群内全部消息")
    print("   2) 开放平台→沙箱配置里，测试群成员名单包含你")

    return finish(worst, is_json)


def auto_(worst, level):
    return max(worst, level)


NOTES = []


def check_daemon():
    """按系统探测常驻方式：launchd（macOS）或 systemd user unit（Linux）。"""
    if sys.platform == "darwin":
        cmd = ["bash", "-lc", "launchctl list 2>/dev/null | grep -i qqbot | awk '{print $3}'"]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=10,
                                 check=False).stdout.strip()
            if out:
                return rec("ok", "launchd 守护",
                           "、".join(dict.fromkeys(out.split())) or "已加载")
            return rec("warn", "launchd 守护未安装",
                       "bash tools/launchd/install.sh（挂掉秒起 + 防休眠）")
        except Exception as e:  # noqa: BLE001
            NOTES.append(f"launchctl 探测失败：{e}")
    elif sys.platform.startswith("linux"):
        try:
            out = subprocess.run(
                ["bash", "-lc",
                 "systemctl --user list-units 'qqbot*' --no-legend 2>/dev/null | head -3"],
                capture_output=True, text=True, timeout=10, check=False)
            if out.stdout.strip():
                return rec("ok", "systemd 守护", out.stdout.strip())
            return rec("warn", "systemd 守护未安装/未启动",
                       "bash tools/systemd/install.sh；或 docker compose up -d")
        except FileNotFoundError:
            NOTES.append("这台 Linux 没有 systemctl（可能是容器/WSL 无 systemd）——"
                         "用 docker compose 或 nohup 常驻")
    else:
        return rec("warn", f"未识别的平台 {sys.platform}",
                   "用 Docker 或直接 python bot.py 常驻")
    return 0


def probe_net(config):
    """对每个已配置供应商的每把 Key 探活 base_url/models，限时 10s。"""
    import httpx

    print()
    print("── 供应商探活（base_url/models，每把 Key 一条） ──")
    checked = 0
    worst = 0
    for name in [config.AI_PROVIDER] + (
            [config.FALLBACK_PROVIDER]
            if config.FALLBACK_PROVIDER and config.FALLBACK_PROVIDER != config.AI_PROVIDER
            else []):
        preset = config.PROVIDER_PRESETS.get(name)
        if not preset:
            continue
        keys = config.load_api_keys(name) or []
        base_url = preset.get("base_url")
        for i, key in enumerate(keys, 1):
            label = f"{name} #{i} {mask(key)}"
            t0 = time.time()
            try:
                with httpx.Client(verify=config.SSL_VERIFY, timeout=10.0) as c:
                    r = c.get(f"{base_url}/models",
                              headers={"Authorization": f"Bearer {key}"})
                dt = time.time() - t0
                if r.status_code == 200:
                    worst = max(worst, rec("ok", f"✔ 探活 {label}",
                                           f"{dt*1000:.0f}ms"))
                else:
                    worst = max(worst, rec(
                        "fail", f"✖ 探活 {label}",
                        f"HTTP {r.status_code}（{r.text[:120]}）"))
            except Exception as e:  # noqa: BLE001
                worst = max(worst, rec("fail", f"✖ 探活 {label}",
                                       f"{type(e).__name__}: {str(e)[:120]}"))
            checked += 1
    if not checked:
        worst = max(worst, rec("warn", "--net 无可探供应商", "先把 Key 配好"))
    return worst


def finish(worst, is_json):
    """换算并打印结论，返回进程退出码：0 绿 / 1 红（必填缺失）/ 2 黄（仅警告）。"""
    if worst == 2:
        code = EXIT["fail"]
    elif worst == 1:
        code = EXIT["warn"]
    else:
        code = 0
    if is_json:
        # --json 模式只输出 JSON 一份，别让人类可读摘要混在管道里
        print(json.dumps({"exit": code, "checks": [
            {"level": lv, "name": nm, "detail": dt} for lv, nm, dt in _results]},
            ensure_ascii=False, indent=2))
        return code
    for note in NOTES:
        print(f"ℹ️  {note}")
    print()
    if worst == 2:
        print("🔴 有必填项没有就位 —— 清掉 ✖ 再启动机器人")
    elif worst == 1:
        print("🟡 能跑，但有警告项 —— 建议把 ⚠️ 清一清")
    else:
        print("🟢 全绿，开跑：python bot.py（或 bash tools/launchd/install.sh）")
    return code


if __name__ == "__main__":
    sys.exit(main())
