from unittest.mock import MagicMock

import push_balance_dingtalk
from push_balance_dingtalk import build_message, load_latest_balances, main
from show_balance import AccountState


def test_build_message_contains_keyword_and_account_balances():
	balances = {
		'账号1': AccountState(
			name='账号1',
			provider='happycoding',
			quota=12.5,
			used=1.25,
			status='✅ 签到成功',
			balance_time='2026-10-01 09:00:00',
			status_time='2026-10-01 09:00:00',
		)
	}

	title, content = build_message(balances)

	assert '任务进度' in title
	assert '任务进度' in content
	assert '账号：账号1' in content
	assert '可用余额：$12.50' in content
	assert '已用额度：$1.25' in content


def test_load_latest_balances_only_returns_requested_provider(tmp_path):
	log = tmp_path / 'checkin_batch6.log'
	log.write_text(
		'[START] 开始执行批次: batch6 - 2026-10-01 09:00:00\n'
		'[PROCESSING] Starting to process Happy\n'
		'[INFO] Happy: Using provider "happycoding" (https://happycoding.xyz)\n'
		':money: Current balance: $12.5, Used: $1.25\n'
		'[SUCCESS] Happy: Check-in successful!\n'
		'[PROCESSING] Starting to process Other\n'
		'[INFO] Other: Using provider "anyrouter" (https://anyrouter.top)\n'
		':money: Current balance: $99.0, Used: $2.0\n'
		'[SUCCESS] Other: Already checked in today\n',
		encoding='utf-8',
	)

	balances = load_latest_balances(str(tmp_path / 'checkin_batch6.log*'), 'happycoding')

	assert list(balances) == ['Happy']
	assert balances['Happy'].quota == 12.5


def test_main_dry_run_does_not_need_webhook(tmp_path, monkeypatch, capsys):
	log = tmp_path / 'checkin_batch6.log'
	log.write_text(
		'[START] 开始执行批次: batch6 - 2026-10-01 09:00:00\n'
		'[PROCESSING] Starting to process Happy\n'
		'[INFO] Happy: Using provider "happycoding" (https://happycoding.xyz)\n'
		':money: Current balance: $12.5, Used: $1.25\n'
		'[SUCCESS] Happy: Check-in successful!\n',
		encoding='utf-8',
	)
	env_file = tmp_path / '.env.batch6'
	env_file.write_text('DINGDING_WEBHOOK=YOUR_DINGTALK_WEBHOOK_URL\n', encoding='utf-8')

	result = main(
		[
			'--env-file',
			str(env_file),
			'--pattern',
			str(tmp_path / 'checkin_batch6.log*'),
			'--dry-run',
		]
	)

	assert result == 0
	assert '任务进度' in capsys.readouterr().out


def test_main_rejects_placeholder_webhook(tmp_path):
	log = tmp_path / 'checkin_batch6.log'
	log.write_text(
		'[START] 开始执行批次: batch6 - 2026-10-01 09:00:00\n'
		'[PROCESSING] Starting to process Happy\n'
		'[INFO] Happy: Using provider "happycoding" (https://happycoding.xyz)\n'
		':money: Current balance: $12.5, Used: $1.25\n'
		'[SUCCESS] Happy: Check-in successful!\n',
		encoding='utf-8',
	)
	env_file = tmp_path / '.env.batch6'
	env_file.write_text('DINGDING_WEBHOOK=YOUR_DINGTALK_WEBHOOK_URL\n', encoding='utf-8')

	assert main(['--env-file', str(env_file), '--pattern', str(tmp_path / 'checkin_batch6.log*')]) == 1


def test_main_sends_only_balance_message_with_real_webhook(tmp_path, monkeypatch):
	log = tmp_path / 'checkin_batch6.log'
	log.write_text(
		'[START] 开始执行批次: batch6 - 2026-10-01 09:00:00\n'
		'[PROCESSING] Starting to process Happy\n'
		'[INFO] Happy: Using provider "happycoding" (https://happycoding.xyz)\n'
		':money: Current balance: $12.5, Used: $1.25\n'
		'[SUCCESS] Happy: Check-in successful!\n',
		encoding='utf-8',
	)
	env_file = tmp_path / '.env.batch6'
	env_file.write_text('DINGDING_WEBHOOK=https://oapi.dingtalk.com/robot/send?access_token=test\n', encoding='utf-8')
	kit = MagicMock()
	monkeypatch.setattr(push_balance_dingtalk, 'NotificationKit', MagicMock(return_value=kit))

	result = main(['--env-file', str(env_file), '--pattern', str(tmp_path / 'checkin_batch6.log*')])

	assert result == 0
	kit.send_dingtalk.assert_called_once()
	title, content = kit.send_dingtalk.call_args.args
	assert '任务进度' in title
	assert '可用余额：$12.50' in content
