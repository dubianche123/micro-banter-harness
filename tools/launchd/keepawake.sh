#!/bin/bash
# 出门在外时保持这台机器「醒着 + bot 活着」。每 5 分钟由 launchd 调一次。
#
# 三层防线，各管一段失效场景：
#   1) `sudo pmset -a disablesleep 1`（一次性设置，10-06 已开）—— 系统级硬防线，
#      连合盖、按电源键都拦得住。**脚本改不了它**（要管理员），所以只做体检。
#   2) caffeinate -ims —— 冗余保险。万一 disablesleep 哪天被谁关掉了，这层还在。
#   3) launchd KeepAlive + 本脚本的 kickstart —— bot 进程死了自己爬回来。
#
# 日志（~/Library/Logs/com.qqbot.keepawake.log）是人在外地时唯一能问「它还活着吗」
# 的地方：每轮留一行心跳，出现空档就是出事的第一个信号。
# 09-24 → 10-06 那次断档十二天，就是因为守护被卸了、而没有任何东西会喊你一声。

UID_NUM=$(id -u)
LOG="$HOME/Library/Logs/com.qqbot.keepawake.log"

say() { echo "$(date '+%F %T') $*" >>"$LOG" 2>/dev/null; }

mkdir -p "$(dirname "$LOG")" 2>/dev/null

# ── 1) 冗余保险：caffeinate ──
# -i 系统空闲不睡 / -m 磁盘不睡 / -s 接电源时系统不睡
# 不带 -d：显示器照常熄屏，没必要为一块没人看的屏幕耗电。
if ! pgrep -x caffeinate >/dev/null 2>&1; then
    nohup /usr/bin/caffeinate -ims >/dev/null 2>&1 &
    disown 2>/dev/null
    say "🍵 caffeinate 已拉起（阻止空闲休眠）"
fi

# ── 2) 硬防线体检 ──
# 只提醒不修复：pmset 要管理员权限，脚本擅自 sudo 是不合适的（会卡在等密码上）。
if pmset -g 2>/dev/null | grep -q "SleepDisabled[[:space:]]*1"; then
    say "🛡 硬防线在位（SleepDisabled=1，合盖也不睡）"
else
    say "⚠️ 硬防线不在了 —— 合盖/电源键会让它睡。想恢复：sudo pmset -a disablesleep 1"
fi

# ── 3) bot 还在吗 ──
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

# ── 4) 心跳 ──
say "--- 巡检 $(uptime | sed 's/.*up //;s/,.*//') ---"
