import asyncio
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware

from canvas_connector.__main__ import parse_trusted_proxies


def test_absent_configuration_disables_trust_and_explicit_allowlist_is_normalized():
    assert parse_trusted_proxies(None) == []
    assert parse_trusted_proxies(" 127.0.0.1,10.20.0.0/16,::1,2001:db8::/32,127.0.0.1 ") == [
        "127.0.0.1", "10.20.0.0/16", "::1", "2001:db8::/32"]


@pytest.mark.parametrize("value", ["", " ", "*", "127.0.0.1,,::1", "proxy.example", "0.0.0.0/0", "::/0",
    "0.0.0.0/1,128.0.0.0/1", "[::1]:8000", "10.20.1.2/16", "fe80::1%lo0", "0.0.0.0", "::",
    ",".join(f"10.0.0.{n}" for n in range(1, 66))])
def test_malformed_empty_or_unbounded_proxy_allowlists_are_rejected(value):
    with pytest.raises(ValueError):
        parse_trusted_proxies(value)


def forwarded_scope(peer, allowlist, forwarded_for, proto="https"):
    observed = {}
    async def app(scope, receive, send):
        observed.update(client=scope["client"], scheme=scope["scheme"], headers=scope["headers"])
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    async def send(message):
        pass
    middleware = ProxyHeadersMiddleware(app, trusted_hosts=parse_trusted_proxies(allowlist))
    scope = {"type": "http", "scheme": "http", "client": (peer, 12345),
        "headers": [(b"host", b"connector.example"), (b"x-forwarded-for", forwarded_for.encode()),
                    (b"x-forwarded-proto", proto.encode()), (b"x-forwarded-host", b"attacker.example")]}
    asyncio.run(middleware(scope, receive, send))
    return observed


def test_only_trusted_actual_peer_can_supply_client_identity():
    trusted = forwarded_scope("10.20.1.7", "10.20.0.0/16", "198.51.100.9")
    assert trusted["client"][0] == "198.51.100.9"
    assert trusted["scheme"] == "https"
    assert dict(trusted["headers"])[b"host"] == b"connector.example"
    untrusted = forwarded_scope("198.51.100.40", "10.20.0.0/16", "203.0.113.9")
    assert untrusted["client"] == ("198.51.100.40", 12345)
    assert untrusted["scheme"] == "http"


def test_forwarded_chain_stops_at_last_untrusted_hop_and_default_ignores_spoofing():
    chained = forwarded_scope("10.20.1.7", "10.20.0.0/16", "192.0.2.99, 203.0.113.9, 10.20.1.8")
    assert chained["client"][0] == "203.0.113.9"
    disabled = forwarded_scope("127.0.0.1", None, "203.0.113.9")
    assert disabled["client"] == ("127.0.0.1", 12345)
    assert disabled["scheme"] == "http"
    ipv6 = forwarded_scope("::1", "::1", "2001:db8::25")
    assert ipv6["client"][0] == "2001:db8::25"


@pytest.mark.parametrize("configured,enabled,expected", [(None, False, []), ("127.0.0.1,::1", True, ["127.0.0.1", "::1"])])
def test_cli_passes_validated_allowlist_to_uvicorn(monkeypatch, configured, enabled, expected):
    import canvas_connector.__main__ as cli
    import uvicorn
    if configured is None:
        monkeypatch.delenv("CONNECTOR_TRUSTED_PROXIES", raising=False)
    else:
        monkeypatch.setenv("CONNECTOR_TRUSTED_PROXIES", configured)
    run = Mock()
    monkeypatch.setattr(uvicorn, "run", run)
    monkeypatch.setattr(cli, "provider_from_env", lambda: SimpleNamespace(store=SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr("sys.argv", ["canvas-connector", "serve", "--workers", "2"])
    cli.main()
    assert run.call_args.kwargs["proxy_headers"] is enabled
    assert run.call_args.kwargs["forwarded_allow_ips"] == expected
    assert run.call_args.kwargs["access_log"] is False
