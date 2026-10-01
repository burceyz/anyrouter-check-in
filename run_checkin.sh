#!/bin/bash
#
# 多批次签到启动脚本
#
# 用法: ./run_checkin.sh <批次名称>
#   .env.<批次名称>          必须存在，内容为该批次的账号配置
#   checkin_<批次名称>.log   运行日志（stdout + stderr 全量落盘）
#
# 设计要点:
#   1. 参数校验、cd、日志重定向全部前移，任何失败都会写进日志而非只丢给 cron
#   2. flock 串行化同批次，每批使用自己的配置和余额哈希文件
#   3. 日志按 LOG_MAX_BYTES 自动轮转，避免长期 cron 无限增长
#   4. 退出码区分“本轮运行成功”和“全账号失败”，便于监控判断

set -uo pipefail

# ---------- 可调参数 ----------
# 项目绝对路径（可用环境变量覆盖，默认沿用原部署路径）
PROJECT_DIR="${PROJECT_DIR:-/web/anyrouter-check-in}"
# 日志轮转阈值（字节），默认 5MB；设为 0 关闭轮转
LOG_MAX_BYTES="${LOG_MAX_BYTES:-5242880}"
# 保留的历史日志份数
LOG_KEEP="${LOG_KEEP:-5}"

# 补全 PATH 路径，让 cron 能找到 uv 和 python3
export PATH="$HOME/.cargo/bin:$HOME/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:$PATH"

BATCH_NAME="${1:-}"
SCRIPT_NAME="$(basename "$0")"

# ---------- 辅助函数 ----------
usage() {
	echo "[ERROR] 请指定批次名称，例如: ./${SCRIPT_NAME} batch1" >&2
	exit 64  # EX_USAGE
}

# 轮转: checkin_x.log -> checkin_x.log.1 -> ... -> checkin_x.log.N
rotate_logs() {
	[ "$LOG_MAX_BYTES" -le 0 ] 2>/dev/null && return 0
	[ -f "$LOG_FILE" ] || return 0

	local size
	size=$(wc -c <"$LOG_FILE" 2>/dev/null) || return 0
	[ "$size" -le "$LOG_MAX_BYTES" ] 2>/dev/null && return 0

	local i
	i="$LOG_KEEP"
	while [ "$i" -gt 1 ]; do
		[ -f "${LOG_FILE}.$((i - 1))" ] && mv -f "${LOG_FILE}.$((i - 1))" "${LOG_FILE}.${i}"
		i=$((i - 1))
	done
	mv -f "$LOG_FILE" "${LOG_FILE}.1"
	return 0
}

# ========== 阶段 1: 校验参数并进入项目目录（此时还没有日志文件） ==========
[ -n "$BATCH_NAME" ] || usage

# 批次名会拼进文件名，做白名单校验，避免 ../ 之类的路径穿越
case "$BATCH_NAME" in
*[!A-Za-z0-9_-]*)
	echo "[ERROR] 批次名称只允许字母、数字、下划线和连字符: $BATCH_NAME" >&2
	exit 64
	;;
esac

if ! cd "$PROJECT_DIR" 2>/dev/null; then
	echo "[ERROR] 无法进入项目目录: $PROJECT_DIR" >&2
	exit 66  # EX_NOINPUT
fi

CONFIG_FILE=".env.${BATCH_NAME}"
HASH_FILE="balance_hash_${BATCH_NAME}.txt"
LOG_FILE="checkin_${BATCH_NAME}.log"

# 轮转必须在重定向之前执行：先把超限的旧日志挪走，再以追加方式打开
# 新的日志文件。否则 fd 仍指向已被改名的 inode，本轮日志会写进 .1 里，
# 而 checkin_<batch>.log 反而不存在。
rotate_logs

# 从这里开始，所有输出（含脚本自身报错和 checkin.py 的 stdout/stderr）都进日志。
# 加锁失败等极端情况日志不可写时，退回 stderr，保证不静默失败。
if ! exec >>"$LOG_FILE" 2>&1; then
	echo "[ERROR] 无法写入日志文件: $PROJECT_DIR/$LOG_FILE" >&2
	exit 73  # EX_CANTCREAT
fi

# ========== 阶段 2: 加锁，保证同批次串行 ==========
# 不同批次用不同的锁文件，可以并行；同一批次重叠触发时后来者直接跳过。
# flock 仅 Linux 可用，缺失时退回 mkdir 原子锁（含陈旧锁清理），避免误判为“正在运行”而永久跳过。
LOCK_FILE=".checkin_${BATCH_NAME}.lock"
LOCK_DIR="${LOCK_FILE}.d"

acquire_lock() {
	if command -v flock >/dev/null 2>&1; then
		exec 9>"$LOCK_FILE" || return 1
		flock -n 9 || return 2
		return 0
	fi

	if mkdir "$LOCK_DIR" 2>/dev/null; then
		:
	else
		# 锁已存在：确认持有者是否还活着，否则清理陈旧锁
		local old_pid
		old_pid=$(cat "$LOCK_DIR/pid" 2>/dev/null)
		if [ -n "$old_pid" ] && kill -0 "$old_pid" 2>/dev/null; then
			return 2
		fi
		echo "[WARN] 清理陈旧的锁目录（原 PID: ${old_pid:-未知}）"
		rm -rf "$LOCK_DIR"
		mkdir "$LOCK_DIR" 2>/dev/null || return 2
	fi
	echo $$ >"$LOCK_DIR/pid"

	# shellcheck disable=SC2064
	trap "rm -rf '$LOCK_DIR'" EXIT INT TERM
	return 0
}

acquire_lock
LOCK_STATUS=$?
if [ "$LOCK_STATUS" -eq 1 ]; then
	echo "[ERROR] 无法创建锁文件: $PROJECT_DIR/$LOCK_FILE"
	exit 73
elif [ "$LOCK_STATUS" -eq 2 ]; then
	echo "=========================================="
	echo "[SKIP] 批次 $BATCH_NAME 已有实例在运行，本次跳过 - $(date '+%Y-%m-%d %H:%M:%S')"
	echo "=========================================="
	exit 0
fi

# ========== 阶段 3: 检查配置文件 ==========
if [ ! -f "$CONFIG_FILE" ]; then
	echo "[ERROR] 配置文件 $CONFIG_FILE 不存在！(当前目录: $PROJECT_DIR)"
	exit 66
fi

echo "=========================================="
echo "[START] 开始执行批次: $BATCH_NAME - $(date '+%Y-%m-%d %H:%M:%S')"
echo "[INFO] 日志文件: $PROJECT_DIR/$LOG_FILE"

# 每批独立读取凭据和余额哈希，避免并行任务互相覆盖
CHECKIN_ENV_FILE="$CONFIG_FILE" CHECKIN_BALANCE_HASH_FILE="$HASH_FILE" \
	xvfb-run -a --server-args="-screen 0 1920x1080x24" uv run checkin.py
EXIT_CODE=$?

echo "[FINISH] 批次 $BATCH_NAME 执行结束，checkin.py 退出码: $EXIT_CODE - $(date '+%Y-%m-%d %H:%M:%S')"

# 把 checkin.py 的退出码翻译成更明确的结论
#    checkin.py: 全部失败 -> 1; 至少一个成功 -> 0
#    125 及以上留给 shell 自身的错误（如 127 command not found），不要误报为“签到失败”
if [ "$EXIT_CODE" -eq 0 ]; then
	echo "[RESULT] 至少一个账号成功"
elif [ "$EXIT_CODE" -eq 1 ]; then
	echo "[RESULT] 全部账号失败或程序异常"
elif [ "$EXIT_CODE" -ge 125 ]; then
	echo "[RESULT] 脚本/环境错误（如 xvfb-run、uv 缺失），签到未真正执行"
else
	echo "[RESULT] 程序被信号中断或异常退出"
fi

echo "=========================================="
exit "$EXIT_CODE"
