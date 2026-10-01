from show_balance import AccountState, Run, aggregate, parse_log, render


def test_show_balance_separates_provider_totals(tmp_path, capsys):
	log = tmp_path / 'checkin_batch.log'
	log.write_text(
		'[START] 开始执行批次: batch - 2026-10-01 12:00:00\n'
		'[PROCESSING] Starting to process Happy\n'
		'[INFO] Happy: Using provider "happycoding" (https://happycoding.xyz)\n'
		':money: Current balance: $2.0, Used: $0.5\n'
		'[SUCCESS] Happy: Check-in successful!\n'
		':money: Current balance: $3.0, Used: $0.5\n'
		'[PROCESSING] Starting to process Any\n'
		'[INFO] Any: Using provider "anyrouter" (https://anyrouter.top)\n'
		':money: Current balance: $4.0, Used: $1.0\n'
		'[SUCCESS] Any: Already checked in today\n'
		'[FINISH] 批次 batch 执行结束，checkin.py 退出码: 0\n',
		encoding='utf-8',
	)

	runs = parse_log(str(log))
	merged = aggregate(runs)
	render(merged, runs)
	output = capsys.readouterr().out

	assert merged[('checkin_batch.log', 'batch', 'Happy')].provider == 'happycoding'
	assert merged[('checkin_batch.log', 'batch', 'Happy')].quota == 3.0
	assert 'happycoding: 1 个账号  |  可用余额: $3.00  |  已用: $0.50' in output
	assert 'anyrouter: 1 个账号  |  可用余额: $4.00  |  已用: $1.00' in output
	assert '总可用余额: $7.00' in output


def test_rotated_log_does_not_duplicate_account_balance():
	old = Run(
		source='checkin_batch.log.1',
		batch='batch',
		started_at='2026-09-30 12:00:00',
		accounts={'Happy': AccountState(name='Happy', provider='happycoding', quota=2.0, used=0.0)},
	)
	new = Run(
		source='checkin_batch.log',
		batch='batch',
		started_at='2026-10-01 12:00:00',
		accounts={'Happy': AccountState(name='Happy', provider='happycoding', quota=3.0, used=0.0)},
	)

	merged = aggregate([old, new])

	assert len(merged) == 1
	assert merged[('checkin_batch.log', 'batch', 'Happy')].quota == 3.0
