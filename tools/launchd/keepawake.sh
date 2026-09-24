#!/bin/bash
# 出门在外时保持这台机器「醒着 + bot 活着」。
#
# 两件事分开办，各管一段失效场景：
#   1) caffeinate 挡掉系统空闲休眠（这台机器的 sleep=1，也就是空闲 1 分钟就睡）
#      ⚠️ 它挡不住：合盖、拔电用到关机、按电源键。所以出门前提是插着电源、别合盖。
#   2) bot 进程不在就 kickstart 一下 —— 主进程本来由 launchd 的 KeepAlive 守着，
#      这里是兜底：万一那个服务被 bootout 了、或者 ThrottleInterval 正在压着，
#      至少五分钟内能自己爬回来。
#
# 由 com.qqbot.keepawake.plist 每 5 分钟调用一次，也可以手工跑一遍看状态。

UID_NUM=$(id -u)
LOG="$HOME/Library/Logs/com.qqbot.keepawake.log"

say() { echo "$(date '+%F %T') $*" >>"$LOG" 2>/dev/null; }

mkdir -p "$(dirname "$LOG")" 2>/dev/null

# ── 1) 保持不睡 ──
# -i 系统空闲不睡 / -m 磁盘不睡 / -s 接电源时系统不睡
# 不带 -d：显示器照常熄屏，没必要为一块没人看的屏幕耗电。
if ! pgrep -x caffeinate >/dev/null 2>&1; then
    nohup /usr/bin/caffeinate -ims >/dev/null 2>&1 &
    disown 2>/dev/null
    say "🍵 caffeinate 已拉起（阻止空闲休眠）"
fi

# ── 2) bot 还在吗 ──
# 匹配串要精确到具体那一条命令：只写 bot.py 容易把手上开着的其他 python 也框进来。
if pgrep -f "QQbot/.venv/bin/python -u bot.py" >/dev/null 2>&1; then
    say "✅ bot 在跑：$(pgrep -f 'QQbot/.venv/bin/python -u bot.py' | tr '\n' ' ')"
else
    say "⚠️ bot 不在了，正在 kickstart"
    # KeepAlive 的服务 kickstart -k 等于「重启它」；服务压根没加载则 bootstrap 回来
    if launchctl print "gui/$UID_NUM/com.qqbot.xiaowang" >/dev/null 2>&1; then
        launchctl kickstart -k "gui/$UID_NUM/com.qqbot.xiaowang" 2>>"$LOG"
    else
        launchctl bootstrap "gui/$UID_NUM" \
            "$HOME/Library/LaunchAgents/com.qqbot.xiaowang.plist" 2>>"$LOG"
    fi
fi
