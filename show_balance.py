#!/usr/bin/env python3
"""
从 checkin_*.log 日志中提取各账号最新余额和签到状态。

设计要点（针对多批次 / 多次运行互相污染的问题）:
  1. 以 [START] 为界把日志切成“运行批次”，只取每个账号最后一次运行的结果，
     避免旧余额配上新时间戳。
  2. 解析 checkin.py 结尾的通知转储块（notify_content）时整块忽略：
     该块里含有**其他账号**的余额行，是跨账号错配的根源。
  3. 账号按 (日志文件, 批次, 账号名) 建立索引，不同批次同名账号不再互相覆盖。

完全只读运行，不修改项目源码。

兼容 Python 3.9+（系统自带 python3 也能直接跑，无需项目虚拟环境）。
"""

import argparse
import glob
import os
import re
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

LOG_PATTERN = 'checkin_*.log'
# 轮转后的历史日志: checkin_x.log.1 / checkin_x.log.2 ...
ROTATED_PATTERN = 'checkin_*.log.*'

# ---- 运行批次边界 ----
RE_START = re.compile(
	r'^\[START\]\s*开始执行批次[:：]\s*(?P<batch>\S+?)\s*-\s*(?P<ts>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})'
)
RE_SEPARATOR = re.compile(r'^=+\s*$')
RE_FINISH = re.compile(r'^\[FINISH\]')
RE_EXIT_CODE = re.compile(r'退出码[:：]\s*(?P<code>\d+)')

# ---- 账号段落 ----
RE_PROCESSING = re.compile(r'^\[PROCESSING\]\s+Starting to process\s+(?P<name>.+?)\s*$')
RE_PROVIDER = re.compile(r'^\[INFO\]\s+(?P<name>.+?): Using provider "(?P<provider>[^"]+)"')
# 形如 [SUCCESS] 账号A: Check-in successful!
RE_ACCOUNT_LINE = re.compile(r'^\[(?P<tag>[A-Z]+)\]\s+(?P<name>.+?):\s(?P<msg>.*)$')

# ---- 通知转储块的起止（块内一律不解析余额/状态）----
RE_NOTIFY_ARM = re.compile(
	r'^\[NOTIFY\]\s+(?:.*failed, will send notification'
	r'|First run detected'
	r'|Balance changes detected)'
)
RE_NOTIFY_END = re.compile(r'^\[NOTIFY\]\s+Notification sent')
RE_TIME_LINE = re.compile(r'^\[TIME\]\s+Execution time:')

# ---- 余额行 ----
RE_BALANCE = re.compile(r':money:\s*Current balance:\s*\$([0-9.]+),\s*Used:\s*\$([0-9.]+)')

STATUS_SUCCESS = '✅ 签到成功'
STATUS_ALREADY = '👌 今日已签'
STATUS_FAILED = '❌ 签到失败'
STATUS_UNKNOWN = '❓ 未知'


@dataclass
class AccountState:
	name: str
	provider: Optional[str] = None
	quota: Optional[float] = None
	used: Optional[float] = None
	balance_time: Optional[str] = None
	status: str = STATUS_UNKNOWN
	status_time: Optional[str] = None


@dataclass
class Run:
	"""一次完整的脚本运行"""

	source: str
	batch: Optional[str] = None
	started_at: Optional[str] = None
	exit_code: Optional[int] = None
	accounts: Dict[str, AccountState] = field(default_factory=dict)


def _account(run: Run, name: str) -> AccountState:
	if name not in run.accounts:
		run.accounts[name] = AccountState(name=name)
	return run.accounts[name]


def _classify(msg: str) -> Optional[str]:
	"""把 checkin.py 的账号级消息映射成签到状态"""
	low = msg.lower()
	if 'check-in successful' in low:
		return STATUS_SUCCESS
	if 'already checked in' in low or 'already signed' in low:
		return STATUS_ALREADY
	if 'check-in failed' in low or 'login failed' in low:
		return STATUS_FAILED
	if 'unable to get waf cookies' in low or 'missing waf cookies' in low:
		return STATUS_FAILED
	if 'error during login' in low or 'browser launch failed' in low:
		return STATUS_FAILED
	return None


def parse_log(path: str) -> List[Run]:
	"""把一个日志文件解析成按时间顺序排列的运行列表"""
	try:
		with open(path, 'r', encoding='utf-8', errors='replace') as f:
			lines = f.read().splitlines()
	except OSError as e:
		print(f'[WARN] 无法读取 {path}: {e}', file=sys.stderr)
		return []

	source = os.path.basename(path)
	runs: List[Run] = []
	current: Optional[Run] = None

	current_acc: Optional[str] = None
	notify_armed = False
	in_notify = False
	pending_start = True  # 是否处于“等待 [START] 头”的空窗期

	def start_run(batch: Optional[str], ts: Optional[str]) -> Run:
		run = Run(source=source, batch=batch, started_at=ts)
		runs.append(run)
		return run

	for line in lines:
		# --- 新一次运行开始 ---
		m = RE_START.match(line)
		if m:
			current = start_run(m.group('batch'), m.group('ts'))
			current_acc = None
			notify_armed = False
			in_notify = False
			pending_start = False
			continue

		# 分隔线 / 首个 [START] 之前的头部内容，都不构成一次运行
		if RE_SEPARATOR.match(line):
			continue
		if pending_start:
			if line.startswith(('[START]', '[INFO]', '[SYSTEM]', '[TIME]')):
				# 无 [START] 头的旧版日志（或以 [SYSTEM] 开头），整文件算一次运行
				current = start_run(None, None)
				pending_start = False
			else:
				continue

		if current is None:
			current = start_run(None, None)
			pending_start = False

		# --- 结束信息 ---
		if RE_FINISH.match(line):
			m_code = RE_EXIT_CODE.search(line)
			if m_code:
				current.exit_code = int(m_code.group('code'))
			current_acc = None
			continue

		# --- 通知转储块：进入后整块跳过 ---
		if in_notify:
			if RE_NOTIFY_END.match(line):
				in_notify = False
			continue
		if RE_NOTIFY_ARM.match(line):
			notify_armed = True
			continue
		if notify_armed and RE_TIME_LINE.match(line):
			# notify_content 一定以 [TIME] Execution time: 开头，其后全是通知正文
			in_notify = True
			notify_armed = False
			continue

		# --- 账号段落切换 ---
		m = RE_PROCESSING.match(line)
		if m:
			current_acc = m.group('name').strip()
			_account(current, current_acc)
			continue

		m = RE_PROVIDER.match(line)
		if m and current_acc and m.group('name').strip() == current_acc:
			_account(current, current_acc).provider = m.group('provider')
			continue

		# --- 余额行：必须归属当前账号段落 ---
		m = RE_BALANCE.search(line)
		if m and current_acc:
			st = _account(current, current_acc)
			st.quota = float(m.group(1))
			st.used = float(m.group(2))
			st.balance_time = current.started_at
			continue

		# --- 账号级状态行：必须与当前账号同名，避免串号 ---
		m = RE_ACCOUNT_LINE.match(line)
		if m and current_acc and m.group('name').strip() == current_acc:
			status = _classify(m.group('msg'))
			if status:
				st = _account(current, current_acc)
				st.status = status
				st.status_time = current.started_at

	return runs


def aggregate(runs: List[Run]) -> Dict[Tuple[str, str, str], AccountState]:
	"""按 (文件, 批次, 账号名) 归并，同一账号以最后一次运行的观测为准"""
	merged: Dict[Tuple[str, str, str], AccountState] = {}

	for run in runs:
		batch = run.batch or '-'
		source = run.source.rsplit('.log', 1)[0] + '.log'
		for name, st in run.accounts.items():
			key = (source, batch, name)
			state = merged.get(key)
			if state is None:
				state = AccountState(name=name)
				merged[key] = state

			if st.provider:
				state.provider = st.provider

			# 状态：最后一次“看到”该账号的运行说了算
			state.status = st.status
			state.status_time = run.started_at

			# 余额：只有该次运行真的拿到了余额才更新，避免用旧值冒充新值
			if st.quota is not None:
				state.quota = st.quota
				state.used = st.used
				state.balance_time = run.started_at

	return merged


def _disp_width(text: str) -> int:
	"""显示宽度：CJK 等全角字符按 2 列计"""
	return sum(2 if ord(c) > 0x2E80 else 1 for c in text)


def _fmt_money(value: Optional[float]) -> str:
	return '-' if value is None else f'${value:.2f}'


def _clip(text: str, width: int) -> str:
	"""按显示宽度裁剪"""
	if _disp_width(text) <= width:
		return text
	out = ''
	used = 0
	for ch in text:
		cw = 2 if ord(ch) > 0x2E80 else 1
		if used + cw > width - 1:
			break
		out += ch
		used += cw
	return out + '…'


def _pad(text: str, width: int) -> str:
	return text + ' ' * max(0, width - _disp_width(text))


def render(merged: Dict[Tuple[str, str, str], AccountState], runs: List[Run]) -> None:
	rows = []
	for source, batch, name in sorted(merged, key=lambda k: (k[0], k[1], k[2])):
		st = merged[(source, batch, name)]
		label = source if batch == '-' else f'{source}:{batch}'

		# 余额观测时间早于状态时间 -> 说明最近一次运行没拿到余额，标记出来
		stale = st.quota is not None and st.balance_time != st.status_time
		rows.append((name, label, st, stale))

	# 列宽按实际数据计算，避免表格串行或截断
	name_w = max([_disp_width('账号名称')] + [_disp_width(r[0]) for r in rows])
	provider_w = max([_disp_width('平台')] + [_disp_width(r[2].provider or '-') for r in rows])
	src_w = max([_disp_width('来源')] + [_disp_width(r[1]) for r in rows])
	bal_w = max(_disp_width('可用余额'), 11)
	used_w = max(_disp_width('已用额度'), 11)
	st_w = max([_disp_width('最新状态')] + [_disp_width(r[2].status) for r in rows])

	sep = '=' * (name_w + provider_w + src_w + bal_w + used_w + st_w + 3 * 6 + 7)
	print('\n' + sep)
	print(
		_pad('账号名称', name_w)
		+ ' | '
		+ _pad('平台', provider_w)
		+ ' | '
		+ _pad('来源', src_w)
		+ ' | '
		+ _pad('可用余额', bal_w)
		+ ' | '
		+ _pad('已用额度', used_w)
		+ ' | '
		+ _pad('最新状态', st_w)
		+ ' | '
		+ '状态时间'
	)
	print('-' * len(sep))

	total_quota = 0.0
	total_used = 0.0
	unknown_balance = 0
	stale_count = 0
	provider_totals: Dict[str, Tuple[float, float, int]] = {}

	for name, label, st, stale in rows:
		if stale:
			stale_count += 1

		if st.quota is None:
			unknown_balance += 1
		else:
			total_quota += st.quota
			total_used += st.used or 0.0
			provider = st.provider or '未识别'
			quota, used, count = provider_totals.get(provider, (0.0, 0.0, 0))
			provider_totals[provider] = (quota + st.quota, used + (st.used or 0.0), count + 1)

		quota_txt = _fmt_money(st.quota) + ('*' if stale else '')
		used_txt = _fmt_money(st.used) + ('*' if stale else '')

		print(
			_pad(name, name_w)
			+ ' | '
			+ _pad(st.provider or '-', provider_w)
			+ ' | '
			+ _pad(label, src_w)
			+ ' | '
			+ _pad(quota_txt, bal_w)
			+ ' | '
			+ _pad(used_txt, used_w)
			+ ' | '
			+ _pad(st.status, st_w)
			+ ' | '
			+ (st.status_time or '-')
		)

	print(sep)
	print(
		f'📊 汇总: 共 {len(merged)} 个账号记录，跨 {len(runs)} 次运行  |  '
		f'总可用余额: ${total_quota:.2f}  |  总已用: ${total_used:.2f}'
	)
	for provider, (quota, used, count) in sorted(provider_totals.items()):
		print(f'   {provider}: {count} 个账号  |  可用余额: ${quota:.2f}  |  已用: ${used:.2f}')
	if unknown_balance:
		print(f'   ⚠️  {unknown_balance} 个账号无余额数据（该次运行未取到，不计入合计）')
	if stale_count:
		print(f'   *  {stale_count} 个账号的余额来自更早一次运行（最近一次未取到余额）')
	print()


def render_history(runs: List[Run]) -> None:
	print('\n' + '=' * 104)
	print('运行历史（按时间顺序）')
	print('-' * 104)
	for i, run in enumerate(runs, 1):
		code = '-' if run.exit_code is None else str(run.exit_code)
		print(f'#{i:<3} {run.source:<22} 批次={run.batch or "-":<12} 时间={run.started_at or "-":<20} 退出码={code}')
		for name, st in run.accounts.items():
			print(f'      - {_clip(name, 20):<22} {_fmt_money(st.quota):>10} / {_fmt_money(st.used):>10}  {st.status}')
	print('=' * 104 + '\n')


def _rotate_suffix(path: str) -> int:
	"""checkin_x.log -> 0, checkin_x.log.2 -> 2（数字越大越旧）"""
	tail = path.rsplit('.log', 1)[-1].lstrip('.')
	return int(tail) if tail.isdigit() else 0


def main() -> int:
	parser = argparse.ArgumentParser(description='从 checkin_*.log 汇总各账号最新余额与签到状态')
	parser.add_argument('--history', action='store_true', help='额外打印每次运行的明细')
	parser.add_argument(
		'--pattern', default=None, help=f'日志文件通配符（默认 {LOG_PATTERN} + 轮转文件 {ROTATED_PATTERN}）'
	)
	parser.add_argument('--no-rotated', action='store_true', help='只读当前日志，忽略 .log.1/.log.2 等轮转历史')
	args = parser.parse_args()

	if args.pattern:
		patterns = [args.pattern]
	else:
		patterns = [LOG_PATTERN] if args.no_rotated else [LOG_PATTERN, ROTATED_PATTERN]

	log_files = []
	for pat in patterns:
		log_files.extend(glob.glob(pat))
	# 同一日志的轮转文件按 .N 从旧到新排在当前日志之前，保证运行顺序正确
	log_files = sorted(set(log_files), key=lambda p: (p.rsplit('.log', 1)[0], -_rotate_suffix(p), p))

	if not log_files:
		print(f'未找到任何日志文件 ({", ".join(patterns)})，请先等待定时任务运行或手动执行一次。')
		return 1

	all_runs: List[Run] = []
	for path in log_files:
		runs = parse_log(path)
		if not runs:
			print(f'[WARN] {path} 中没有解析到运行记录', file=sys.stderr)
			continue
		all_runs.extend(runs)

	if not all_runs:
		print('日志中暂未解析到有效的运行记录。')
		return 1

	# 跨文件按运行时间归并；无时间的排在最前，保持文件内原有顺序
	all_runs.sort(key=lambda r: (r.started_at is not None, r.started_at or ''))

	merged = aggregate(all_runs)
	if not merged:
		print('日志中暂未解析到有效的账号签到记录。')
		return 1

	render(merged, all_runs)
	if args.history:
		render_history(all_runs)
	return 0


if __name__ == '__main__':
	sys.exit(main())
