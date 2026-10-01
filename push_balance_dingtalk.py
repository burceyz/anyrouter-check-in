#!/usr/bin/env python3
"""读取签到日志并把 HappyCoding 账号余额推送到钉钉。"""

import argparse
import glob
import os
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

from show_balance import AccountState, Run, aggregate, parse_log
from utils.notify import NotificationKit

DEFAULT_ENV_FILE = '.env.batch6'
DEFAULT_LOG_PATTERN = 'checkin_batch6.log*'
DEFAULT_PROVIDER = 'happycoding'
DINGTALK_TITLE = '任务进度：HappyCoding 余额'


def _rotate_suffix(path: str) -> int:
	"""返回轮转日志的数字后缀，当前日志按 0 处理。"""
	tail = path.rsplit('.log', 1)[-1].lstrip('.')
	return int(tail) if tail.isdigit() else 0


def load_latest_balances(pattern: str, provider: str) -> dict[str, AccountState]:
	"""读取日志并返回指定平台每个账号的最新状态。"""
	paths = sorted(set(glob.glob(pattern)), key=lambda path: (path.rsplit('.log', 1)[0], -_rotate_suffix(path), path))
	if not paths:
		raise FileNotFoundError(f'未找到日志文件: {pattern}')

	runs: list[Run] = []
	for path in paths:
		runs.extend(parse_log(path))
	if not runs:
		raise RuntimeError(f'日志中没有可解析的运行记录: {pattern}')

	runs.sort(key=lambda run: (run.started_at is not None, run.started_at or ''))
	merged = aggregate(runs)
	return {name: state for (_source, _batch, name), state in merged.items() if state.provider == provider}


def _money(value: float | None, stale: bool = False) -> str:
	if value is None:
		return '-'
	return f'${value:.2f}' + ('*' if stale else '')


def build_message(balances: dict[str, AccountState], provider: str = DEFAULT_PROVIDER) -> tuple[str, str]:
	"""生成包含钉钉关键词和账号余额的文本消息。"""
	if not balances:
		raise RuntimeError(f'没有找到平台为 {provider} 的账号余额')

	lines = [DINGTALK_TITLE, f'平台：{provider}', f'统计时间：{datetime.now().strftime("%Y-%m-%d %H:%M:%S")}', '']
	total_quota = 0.0
	total_used = 0.0
	known_count = 0
	for name, state in sorted(balances.items()):
		stale = state.quota is not None and state.balance_time != state.status_time
		lines.extend(
			[
				f'账号：{name}',
				f'可用余额：{_money(state.quota, stale)}',
				f'已用额度：{_money(state.used, stale)}',
				f'签到状态：{state.status}',
				'',
			]
		)
		if state.quota is not None:
			known_count += 1
			total_quota += state.quota
			total_used += state.used or 0.0

	lines.extend(
		[
			f'账号数：{len(balances)}（余额有效 {known_count}）',
			f'合计可用余额：${total_quota:.2f}',
			f'合计已用额度：${total_used:.2f}',
			'* 余额带星号表示最近一次运行未取到新余额。',
		]
	)
	return DINGTALK_TITLE, '\n'.join(lines)


def _is_placeholder_webhook(webhook: str) -> bool:
	value = webhook.strip().lower()
	return not value or value.startswith(('your_', 'replace_', '<')) or 'example.com' in value


def main(argv: list[str] | None = None) -> int:
	parser = argparse.ArgumentParser(description='把 HappyCoding 最新余额推送到钉钉群')
	parser.add_argument('--env-file', default=DEFAULT_ENV_FILE, help=f'环境文件（默认 {DEFAULT_ENV_FILE}）')
	parser.add_argument('--pattern', default=DEFAULT_LOG_PATTERN, help=f'日志通配符（默认 {DEFAULT_LOG_PATTERN}）')
	parser.add_argument('--provider', default=DEFAULT_PROVIDER, help=f'平台名称（默认 {DEFAULT_PROVIDER}）')
	parser.add_argument('--dry-run', action='store_true', help='只打印消息，不发送钉钉请求')
	args = parser.parse_args(argv)

	try:
		env_path = Path(args.env_file)
		if not env_path.is_file():
			raise FileNotFoundError(f'环境文件不存在: {env_path}')
		load_dotenv(dotenv_path=env_path, override=True)
		balances = load_latest_balances(args.pattern, args.provider)
		title, content = build_message(balances, args.provider)
		if args.dry_run:
			print(content)
			return 0

		webhook = os.getenv('DINGDING_WEBHOOK', '')
		if _is_placeholder_webhook(webhook):
			raise ValueError('DINGDING_WEBHOOK 未配置真实钉钉机器人地址')
		NotificationKit().send_dingtalk(title, content)
		print(f'[SUCCESS] DingTalk balance message sent: {len(balances)} account(s)')
		return 0
	except (FileNotFoundError, RuntimeError, ValueError) as exc:
		print(f'[FAILED] {exc}', file=sys.stderr)
		return 1


if __name__ == '__main__':
	sys.exit(main())
