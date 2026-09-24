#!/bin/bash
# 装两只 launchd 守护：一个守机器人进程，一个守「别睡」。
#
#   bash install.sh           安装并启动
#   bash install.sh status    只看状态
#   bash install.sh stop      全部卸载（要带机器走 / 想让它正常休眠时用）
#
# ⚠️ 安装时会把手工起的那个 bot 实例关掉改由 launchd 托管 ——
#    两个实例同时在线会造成群里重复回复。
set -u

here="$(cd "$(dirname "$0")" && pwd)"
agents="$HOME/Library/LaunchAgents"
uid_num=$(id -u)

status() {
    echo "── bot（主进程守护） ──"
    if launchctl print "gui/$uid_num/com.qqbot.xiaowang" >/dev/null 2>&1; then
        echo "已加载"
        launchctl print "gui/$uid_num/com.qqbot.xiaowang" 2>/dev/null \
            | grep -E "^\s+(state|pid|last exit status)" | head -3
    else
        echo "未加载"
    fi
    echo "── keepawake（防休眠） ──"
    if launchctl print "gui/$uid_num/com.qqbot.keepawake" >/dev/null 2>&1; then
        echo "已加载"
    else
        echo "未加载"
    fi
    if pgrep -x caffeinate >/dev/null 2>&1; then
        echo "caffeinate 运行中：$(pgrep -x caffeinate | tr '\n' ' ')"
    else
        echo "⚠️ caffeinate 没在跑"
    fi
    pmset -g 2>/dev/null | grep -E "^ sleep" | sed 's/^/电源策略 /'
    echo "── 进程 ──"
    pgrep -fl "QQbot/.venv/bin/python -u bot.py" || echo "(bot 没在跑)"
}

stop() {
    for label in com.qqbot.keepawake com.qqbot.xiaowang; do
        if launchctl print "gui/$uid_num/$label" >/dev/null 2>&1; then
            launchctl bootout "gui/$uid_num/$label" && echo "已卸载 $label"
        fi
    done
    pkill -x caffeinate 2>/dev/null && echo "caffeinate 已停（机器恢复正常休眠）"
    echo "⚠️ 手工起的 bot 不受影响，需要的话自行 pkill"
}

install() {
    mkdir -p "$agents" "$HOME/Library/Logs"

    # 先清掉手工起的那份，否则会和 launchd 拉起来的打成双份
    if pgrep -f "QQbot/.venv/bin/python -u bot.py" >/dev/null 2>&1; then
        echo "发现手工起的 bot，先停掉（避免双实例重复回话）…"
        pkill -f "QQbot/.venv/bin/python -u bot.py"
        sleep 4
    fi

    for label in com.qqbot.xiaowang com.qqbot.keepawake; do
        # 旧的先 bootout，重装载不会留下两份定义
        launchctl bootout "gui/$uid_num/$label" 2>/dev/null
        cp "$here/$label.plist" "$agents/$label.plist"
        if launchctl bootstrap "gui/$uid_num" "$agents/$label.plist" 2>/dev/null; then
            echo "✅ $label 已启动"
        else
            echo "❌ $label 启动失败 —— 试试先跑 bash install.sh stop 再重装"
        fi
    done

    bash "$here/keepawake.sh"   # 立刻巡一次，不用等满 5 分钟
    echo
    status
}

case "${1:-install}" in
    install) install ;;
    status)  status ;;
    stop)    stop ;;
    *) echo "用法: bash install.sh [install|status|stop]" ;;
esac
