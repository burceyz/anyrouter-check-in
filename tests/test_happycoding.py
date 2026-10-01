import json

import httpx

import checkin
from utils.config import AccountConfig, AppConfig, load_accounts_config


def test_happycoding_provider_and_username_account(monkeypatch):
	monkeypatch.delenv('PROVIDERS', raising=False)
	monkeypatch.setenv(
		'ANYROUTER_ACCOUNTS',
		json.dumps([{'provider': 'happycoding', 'username': 'tester', 'password': 'secret'}]),
	)

	provider = AppConfig.load_from_env().get_provider('happycoding')
	accounts = load_accounts_config()

	assert provider is not None
	assert provider.auth_mode == 'bearer_login'
	assert provider.sign_in_path == '/api/user/checkin'
	assert accounts is not None
	assert accounts[0].username == 'tester'
	assert accounts[0].has_login_credentials()


def test_happycoding_login_checkin_and_balance(monkeypatch):
	provider = AppConfig.load_from_env().get_provider('happycoding')
	assert provider is not None
	account = AccountConfig(cookies=None, provider='happycoding', username='tester', password='secret')
	requests = []

	def handle(request):
		requests.append((request.method, request.url.path))
		if request.url.path == '/api/user/login':
			assert json.loads(request.content) == {'username': 'tester', 'password': 'secret'}
			return httpx.Response(200, json={'success': True, 'data': {'access_token': 'test-token'}})
		assert request.headers['Authorization'] == 'Bearer test-token'
		if request.url.path == '/api/user/self':
			quota = 1_500_000 if ('POST', '/api/user/checkin') in requests else 1_000_000
			return httpx.Response(200, json={'success': True, 'data': {'quota': quota, 'used_quota': 250_000}})
		if request.url.path == '/api/user/checkin':
			if request.method == 'GET':
				assert request.url.params.get('month')
				return httpx.Response(200, json={'success': True, 'data': {'stats': {'checked_in_today': False}}})
			return httpx.Response(200, json={'success': True, 'message': 'Check-in successful'})
		raise AssertionError(f'Unexpected request: {request.url}')

	client_class = httpx.Client
	transport = httpx.MockTransport(handle)
	monkeypatch.setattr(checkin.httpx, 'Client', lambda **kwargs: client_class(transport=transport, **kwargs))

	success, before, after = checkin.run_check_in_requests({}, account, 'HappyCoding', provider)

	assert success is True
	assert before is not None and before['quota'] == 2.0
	assert after is not None and after['quota'] == 3.0
	assert requests == [
		('POST', '/api/user/login'),
		('GET', '/api/user/self'),
		('GET', '/api/user/checkin'),
		('POST', '/api/user/checkin'),
		('GET', '/api/user/self'),
	]


def test_happycoding_already_checked_in_skips_post(monkeypatch, capsys):
	provider = AppConfig.load_from_env().get_provider('happycoding')
	assert provider is not None
	account = AccountConfig(cookies=None, provider='happycoding', username='tester', password='secret')
	requests = []

	def handle(request):
		requests.append((request.method, request.url.path))
		if request.url.path == '/api/user/login':
			return httpx.Response(200, json={'success': True, 'data': {'access_token': 'test-token'}})
		if request.url.path == '/api/user/self':
			return httpx.Response(200, json={'success': True, 'data': {'quota': 1_000_000, 'used_quota': 0}})
		return httpx.Response(200, json={'success': True, 'data': {'stats': {'checked_in_today': True}}})

	client_class = httpx.Client
	transport = httpx.MockTransport(handle)
	monkeypatch.setattr(checkin.httpx, 'Client', lambda **kwargs: client_class(transport=transport, **kwargs))

	success, before, after = checkin.run_check_in_requests({}, account, 'HappyCoding', provider)

	assert success is True
	assert before == after
	assert requests == [
		('POST', '/api/user/login'),
		('GET', '/api/user/self'),
		('GET', '/api/user/checkin'),
	]
	assert 'Already checked in today' in capsys.readouterr().out


def test_happycoding_login_without_access_token_stops_before_checkin(monkeypatch, capsys):
	provider = AppConfig.load_from_env().get_provider('happycoding')
	assert provider is not None
	account = AccountConfig(cookies=None, provider='happycoding', email='tester@example.com', password='secret')
	requests = []

	def handle(request):
		requests.append(request.url.path)
		return httpx.Response(200, json={'success': True, 'data': {'requires_2fa': True}})

	client_class = httpx.Client
	transport = httpx.MockTransport(handle)
	monkeypatch.setattr(checkin.httpx, 'Client', lambda **kwargs: client_class(transport=transport, **kwargs))

	assert checkin.run_check_in_requests({}, account, 'HappyCoding', provider) == (False, None, None)
	assert requests == ['/api/user/login']
	assert 'Access token missing' in capsys.readouterr().out


def test_anyrouter_cookie_checkin_still_uses_original_endpoint(monkeypatch):
	provider = AppConfig.load_from_env().get_provider('anyrouter')
	assert provider is not None
	account = AccountConfig(cookies={'session': 'saved'}, api_user='12345')
	requests = []

	def handle(request):
		requests.append((request.method, request.url.path))
		assert request.headers['new-api-user'] == '12345'
		assert request.headers['cookie'] == 'session=saved'
		if request.url.path == '/api/user/self':
			return httpx.Response(200, json={'success': True, 'data': {'quota': 500_000, 'used_quota': 0}})
		return httpx.Response(200, json={'success': True})

	client_class = httpx.Client
	transport = httpx.MockTransport(handle)
	monkeypatch.setattr(checkin.httpx, 'Client', lambda **kwargs: client_class(transport=transport, **kwargs))

	success, _, _ = checkin.run_check_in_requests({'session': 'saved'}, account, 'AnyRouter', provider)

	assert success is True
	assert requests == [
		('GET', '/api/user/self'),
		('POST', '/api/user/sign_in'),
		('GET', '/api/user/self'),
	]


def test_agentrouter_auto_checkin_still_uses_user_info(monkeypatch):
	provider = AppConfig.load_from_env().get_provider('agentrouter')
	assert provider is not None
	account = AccountConfig(cookies={'session': 'saved'}, api_user='12345', provider='agentrouter')
	requests = []

	def handle(request):
		requests.append((request.method, request.url.path))
		return httpx.Response(200, json={'success': True, 'data': {'quota': 500_000, 'used_quota': 0}})

	client_class = httpx.Client
	transport = httpx.MockTransport(handle)
	monkeypatch.setattr(checkin.httpx, 'Client', lambda **kwargs: client_class(transport=transport, **kwargs))

	success, _, _ = checkin.run_check_in_requests({'session': 'saved'}, account, 'AgentRouter', provider)

	assert success is True
	assert requests == [('GET', '/api/user/self'), ('GET', '/api/user/self')]
