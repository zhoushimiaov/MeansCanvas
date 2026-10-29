import ipaddress
import logging
import re
import socket
import threading
import time
import urllib.parse
from typing import Tuple, Optional, Set, List, Dict, Any

logger = logging.getLogger("infinite_canvas.security")

_SAFE_DNS_CACHE: Dict[str, Tuple[float, List[Tuple[Any, ...]]]] = {}
_PINNED_HOST_REFS: Dict[str, int] = {}
_DNS_LOCK = threading.RLock()
_ORIG_GETADDRINFO = socket.getaddrinfo

def pin_host_dns(host: str, addr_info: List[Tuple[Any, ...]], ttl: float = 60.0) -> None:
    """Pin DNS resolution for a specific host during safe download execution."""
    clean = str(host or "").strip().lower()
    if not clean or not addr_info:
        return
    with _DNS_LOCK:
        _SAFE_DNS_CACHE[clean] = (time.time() + ttl, addr_info)
        _PINNED_HOST_REFS[clean] = _PINNED_HOST_REFS.get(clean, 0) + 1

def unpin_host_dns(host: str) -> None:
    """Unpin DNS resolution when safe download finishes."""
    clean = str(host or "").strip().lower()
    if not clean:
        return
    with _DNS_LOCK:
        count = _PINNED_HOST_REFS.get(clean, 0)
        if count <= 1:
            _PINNED_HOST_REFS.pop(clean, None)
            _SAFE_DNS_CACHE.pop(clean, None)
        else:
            _PINNED_HOST_REFS[clean] = count - 1

def clear_safe_dns_cache() -> None:
    """Clear all pinned DNS entries (useful for tests and cleanup)."""
    with _DNS_LOCK:
        _SAFE_DNS_CACHE.clear()
        _PINNED_HOST_REFS.clear()

def _safe_pinned_getaddrinfo(host, port, *args, **kwargs):
    if host:
        host_str = host.decode("utf-8") if isinstance(host, bytes) else str(host)
        clean = host_str.lower().strip()
        entry = None
        with _DNS_LOCK:
            entry = _SAFE_DNS_CACHE.get(clean)
            if entry:
                exp, addr_info = entry
                if time.time() < exp:
                    pass
                else:
                    _SAFE_DNS_CACHE.pop(clean, None)
                    _PINNED_HOST_REFS.pop(clean, None)
                    entry = None
        if entry:
            exp, addr_info = entry
            target_port = port if isinstance(port, int) else (int(port) if str(port).isdigit() else 0)
            return [
                (family, socktype, proto, canonname, (sockaddr[0], target_port) if len(sockaddr) >= 2 else (sockaddr[0],))
                for family, socktype, proto, canonname, sockaddr in addr_info
            ]
    return _ORIG_GETADDRINFO(host, port, *args, **kwargs)

if socket.getaddrinfo is not _safe_pinned_getaddrinfo:
    socket.getaddrinfo = _safe_pinned_getaddrinfo


COMFY_HOST_PATTERN = re.compile(r"^[a-zA-Z0-9_.-]+(:[0-9]{1,5})?$")

# Private and restricted IPv4 and IPv6 networks
RESTRICTED_IPV4_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("224.0.0.0/4"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("255.255.255.255/32"),
]


CLOUD_METADATA_HOSTS = {
    "metadata.google.internal",
    "instance-data",
}

METADATA_AND_LINK_LOCAL_NETWORKS = [
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("100.100.100.200/32"),  # Alibaba Cloud metadata IP
]


def is_cloud_metadata_or_link_local(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """Check if an IP belongs to cloud metadata (AWS/GCP/Alibaba/Azure) or link-local range."""
    if ip.is_link_local:
        return True
    for net in METADATA_AND_LINK_LOCAL_NETWORKS:
        if ip in net:
            return True
    return False


TUN_FAKE_IP_NETWORK = ipaddress.ip_network("198.18.0.0/15")


def _tun_fake_ip_allowed() -> bool:
    """Whether the TUN/Proxy fake-IP range (198.18.0.0/15) is treated as non-restricted.

    Default True for backward compatibility with Clash/Mihomo/Sing-box TUN setups
    where this range represents proxied *outbound* domains, not intranet hosts.
    Operators who do not use TUN proxying can set IC_ALLOW_TUN_FAKE_IP=0 to close
    this SSRF surface (B12-7).
    """
    import os
    return os.getenv("IC_ALLOW_TUN_FAKE_IP", "1").strip().lower() not in ("0", "false", "no", "off")


def is_ip_restricted(ip_str: str) -> bool:
    """Check if an IP address string belongs to private, loopback, link-local, or reserved ranges."""
    try:
        ip = ipaddress.ip_address(ip_str)
        # TUN/Proxy Fake-IP network (RFC 2544, 198.18.0.0/15) used by Clash/Mihomo/Sing-box
        # represents proxied outbound domains rather than internal intranet hosts.
        # Gated by IC_ALLOW_TUN_FAKE_IP so it can be closed on hosts that never use TUN.
        if ip in TUN_FAKE_IP_NETWORK:
            return not _tun_fake_ip_allowed()
        if ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return True
        for net in RESTRICTED_IPV4_NETWORKS:
            if ip in net:
                return True
        return False
    except ValueError:
        return True


def is_safe_remote_url(
    url: str,
    allow_private: bool = False,
    allowed_hosts: Optional[Set[str]] = None,
    pin_dns: bool = False,
) -> Tuple[bool, str]:
    """Validate that a URL is safe to fetch remotely (prevents SSRF).

    Args:
        url: The candidate URL string.
        allow_private: Whether private/internal network addresses are allowed.
        allowed_hosts: Optional explicit whitelist of hostnames or host:port strings.
        pin_dns: Whether to pin DNS resolution in _SAFE_DNS_CACHE (strictly for active downloads).

    Returns:
        (is_safe, reason_if_unsafe)
    """
    raw = str(url or "").strip()
    if not raw:
        return False, "URL 为空"

    try:
        parsed = urllib.parse.urlparse(raw)
    except Exception as exc:
        return False, f"URL 解析失败: {exc}"

    if parsed.scheme.lower() not in ("http", "https"):
        return False, f"不支持的协议方案: {parsed.scheme}"

    # Disallow userinfo (e.g. http://user:pass@host/) which is used in SSRF confusion attacks
    if parsed.username or parsed.password or "@" in parsed.netloc:
        return False, "URL 包含非法用户凭证或混淆字符 @"

    hostname = parsed.hostname
    if not hostname:
        return False, "URL 缺少主机名"

    clean_host = hostname.lower()

    if clean_host in CLOUD_METADATA_HOSTS:
        return False, f"禁止访问受限内网 IP (云元数据端点): {clean_host}"

    if allowed_hosts and (clean_host in allowed_hosts or parsed.netloc.lower() in allowed_hosts):
        return True, ""

    if clean_host in ("localhost", "0.0.0.0", "127.0.0.1", "::1"):
        if not allow_private:
            return False, f"禁止访问本地或回环地址: {clean_host}"

    # Check if host itself is a raw IP literal
    try:
        ip = ipaddress.ip_address(clean_host)
        if is_cloud_metadata_or_link_local(ip):
            return False, f"禁止访问受限内网 IP (云元数据/链路本地): {clean_host}"
        if not allow_private and is_ip_restricted(str(ip)):
            return False, f"禁止访问受限内网 IP 地址: {clean_host}"
        return True, ""
    except ValueError:
        pass  # It is a domain name, proceed to DNS resolution

    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        addr_info = socket.getaddrinfo(clean_host, port, proto=socket.IPPROTO_TCP)
        if not addr_info:
            return False, f"DNS 解析无返回: {clean_host}"
        for entry in addr_info:
            resolved_ip_str = entry[4][0]
            try:
                resolved_ip = ipaddress.ip_address(resolved_ip_str)
                if is_cloud_metadata_or_link_local(resolved_ip):
                    return False, f"域名解析到受限内网 IP (云元数据/链路本地) ({resolved_ip_str})"
            except ValueError:
                pass
            if not allow_private and is_ip_restricted(resolved_ip_str):
                return False, f"域名解析到受限内网 IP 地址 ({resolved_ip_str})"
        if pin_dns:
            pin_host_dns(clean_host, addr_info)
    except socket.gaierror as exc:
        if not allow_private:
            return False, f"DNS 域名解析失败: {exc}"
    except Exception as exc:
        if not allow_private:
            return False, f"网络地址校验失败: {exc}"

    return True, ""


def validate_safe_url(
    url: str,
    allow_private: bool = False,
    allowed_hosts: Optional[Set[str]] = None,
    pin_dns: bool = False,
) -> str:
    """Validate that a URL is safe to fetch remotely.

    Raises:
        ValueError: with failure reason if unsafe.
    Returns:
        The normalized URL string if safe.
    """
    safe, reason = is_safe_remote_url(
        url, allow_private=allow_private, allowed_hosts=allowed_hosts, pin_dns=pin_dns
    )
    if not safe:
        raise ValueError(f"SSRF 拦截: {reason} ({url})")
    return url.strip()


def _extract_peer_ip(response: Any) -> Optional[str]:
    """Extract connected remote IP from httpx/httpcore response stream if available."""
    stream = getattr(response, "extensions", {}).get("network_stream")
    if not stream:
        return None
    raw = getattr(stream, "_stream", stream)
    if hasattr(raw, "extra"):
        try:
            import anyio
            addr = raw.extra(anyio.abc.SocketAttribute.remote_address, None)
            if addr and isinstance(addr, tuple) and len(addr) >= 1:
                return str(addr[0])
        except Exception:
            pass
    if hasattr(stream, "get_extra_info"):
        try:
            peer = stream.get_extra_info("peername")
            if peer and isinstance(peer, tuple) and len(peer) >= 1:
                return str(peer[0])
        except Exception:
            pass
    return None


def get_configured_proxy_ips() -> Set[str]:
    """Retrieve IP addresses or hostnames of configured local or upstream HTTP/HTTPS/SOCKS proxies.

    When an outbound request is routed through a local proxy (e.g. 127.0.0.1:7897, Clash/v2ray),
    the TCP socket's peer IP is the proxy itself rather than the target server.
    """
    import os
    import urllib.request
    proxy_hosts = set()
    raw_urls = []
    try:
        raw_urls.extend(urllib.request.getproxies().values())
    except Exception:
        pass
    for env_key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        val = os.environ.get(env_key)
        if val:
            raw_urls.append(val)
    for raw in raw_urls:
        if not raw:
            continue
        try:
            p = urllib.parse.urlsplit(raw if "://" in raw else f"http://{raw}")
            h = (p.hostname or "").strip().lower()
            if h:
                proxy_hosts.add(h)
                if h in ("localhost", "127.0.0.1", "::1"):
                    proxy_hosts.update(["127.0.0.1", "::1", "localhost"])
        except Exception:
            pass
    return proxy_hosts


def assert_safe_peer_ip(response: Any, url: str) -> None:
    """连接建立后复检真实对端 IP，防御 DNS 重绑定（TOCTOU）。

    validate_safe_url() 只做一次预解析，随后 httpx 会独立再解析一次。
    攻击者可用 TTL=0 的域名让第一次解析返回公网 IP 通过校验、
    第二次解析返回 127.0.0.1，从而命中内网服务。

    本函数从已建立的 socket 中取出真实对端 IP 并复检，
    与 safe_download_bytes() 内部使用的是同一套判定逻辑。

    适用于携带凭据（API Key 等）的出站调用：这类调用的危害面远高于
    普通下载，一旦被重绑定命中即等于凭据外泄。

    Raises:
        ValueError: 对端 IP 落在受限网段。
    """
    peer_ip = _extract_peer_ip(response)
    if not peer_ip or not is_ip_restricted(peer_ip):
        return
    try:
        peer_is_loopback = ipaddress.ip_address(peer_ip).is_loopback
    except ValueError:
        peer_is_loopback = False
    if peer_ip in get_configured_proxy_ips() and peer_is_loopback:
        # 经本机回环代理转发时 TCP 对端是代理而非真实目标，
        # 无法在 TCP 层判定，降级为仅依赖预解析并显式记录日志。
        logger.warning(
            "DNS 重绑定防御降级: 请求经本机回环代理 (%s) 转发，无法校验真实对端 IP — 目标 %s",
            peer_ip,
            url,
        )
        return
    raise ValueError(f"SSRF 拦截: 远程连接对端 IP 受限 ({peer_ip})")


async def safe_json_post(
    url: str,
    *,
    method: str = "POST",
    headers: Optional[dict] = None,
    json_body: Optional[dict] = None,
    form_data: Optional[dict] = None,
    files: Optional[dict] = None,
    timeout: float = 120.0,
    allow_private: bool = False,
):
    """向云端 Provider 发起携带凭据的 POST，启用 DNS 钉扎与对端 IP 复检。

    与普通 httpx.post 的区别：
      1. pin_dns=True 预解析并钉扎，httpx 复用同一解析结果，消除重绑定窗口
      2. 连接建立后用 assert_safe_peer_ip() 复检真实对端 IP
      3. 不跟随重定向（3xx 直接返回给调用方自行处理）

    专用于会附带 Authorization / x-key 的出站调用。
    """
    import httpx

    safe_url = validate_safe_url(url, allow_private=allow_private, pin_dns=True)
    host = ""
    try:
        host = (urllib.parse.urlsplit(safe_url).hostname or "").lower().strip()
    except Exception:
        host = ""

    client_timeout = httpx.Timeout(connect=min(timeout, 15.0), read=timeout, write=30.0, pool=15.0)
    try:
        async with httpx.AsyncClient(timeout=client_timeout, follow_redirects=False) as client:
            request = client.build_request(
                method.upper(), safe_url, headers=headers or {}, json=json_body,
                data=form_data, files=files,
            )
            response = await client.send(request, stream=True)
            try:
                if not allow_private:
                    assert_safe_peer_ip(response, safe_url)
                await response.aread()
            finally:
                await response.aclose()
            # aread() 已把 body 读入 response.content，调用方可正常 .json()
            return response
    finally:
        if host:
            unpin_host_dns(host)


async def safe_download_bytes(
    url: str,
    timeout: float = 30.0,
    max_bytes: int = 50 * 1024 * 1024,
    headers: Optional[dict] = None,
    allow_private: bool = False,
    allowed_hosts: Optional[Set[str]] = None,
    max_redirects: int = 5,
) -> Tuple[bytes, str]:
    """Safely download remote media bytes with SSRF validation, redirect inspection, and size limits.

    Returns:
        (content_bytes, content_type)
    Raises:
        ValueError: if URL is unsafe or exceeds max_bytes.
        Exception: on network or HTTP errors.
    """
    import httpx
    pinned_hosts: List[str] = []
    try:
        current_url = validate_safe_url(url, allow_private=allow_private, allowed_hosts=allowed_hosts, pin_dns=True)
        try:
            h = urllib.parse.urlsplit(current_url).hostname
            if h:
                pinned_hosts.append(h.lower().strip())
        except Exception:
            pass
        redirects_left = max_redirects
        default_headers = {"User-Agent": "Infinite-Canvas/SafeMediaFetcher/1.0"}
        if headers:
            default_headers.update(headers)

        client_timeout = httpx.Timeout(connect=min(timeout, 15.0), read=timeout, write=15.0, pool=15.0)
        async with httpx.AsyncClient(timeout=client_timeout, follow_redirects=False) as client:
            while True:
                request = client.build_request("GET", current_url, headers=default_headers)
                response = await client.send(request, stream=True)
                if not allow_private:
                    peer_ip = _extract_peer_ip(response)
                    if peer_ip and is_ip_restricted(peer_ip):
                        # The peer-IP guard exists to defeat DNS rebinding: even if the
                        # pre-flight validate_safe_url() passed, we reject when the socket
                        # actually connected to a restricted (intranet/loopback) address.
                        #
                        # Behind a LOCAL proxy (e.g. Clash/v2ray on 127.0.0.1:7897) the TCP
                        # peer is always the proxy, not the real target, so this guard cannot
                        # see the true endpoint. We only exempt a *loopback* proxy (a process
                        # on this machine, high trust) and, when we do, we LOG the fact that
                        # rebinding defense is degraded to this request — rather than silently
                        # trusting it. A non-loopback (LAN) proxy whose peer resolves into a
                        # private range is indistinguishable from a direct intranet hit and is
                        # therefore NOT exempted.
                        configured_proxies = get_configured_proxy_ips()
                        try:
                            peer_is_loopback = ipaddress.ip_address(peer_ip).is_loopback
                        except ValueError:
                            peer_is_loopback = False
                        if peer_ip in configured_proxies and peer_is_loopback:
                            logger.warning(
                                "SSRF 纵深降级: 请求经本机回环代理 (%s) 转发，无法在 TCP 层校验真实对端 IP，"
                                "本次仅依赖预解析 DNS 校验 (rebinding 防御降级) — 目标 %s",
                                peer_ip,
                                urllib.parse.urlsplit(current_url).hostname or current_url,
                            )
                        else:
                            await response.aclose()
                            raise ValueError(f"SSRF 拦截: 远程连接对端 IP 受限 ({peer_ip})")
                if response.status_code in (301, 302, 303, 307, 308):
                    await response.aclose()
                    if redirects_left <= 0:
                        raise ValueError(f"重定向次数超过上限 ({max_redirects})")
                    location = response.headers.get("Location")
                    if not location:
                        raise ValueError("重定向缺少 Location 头")
                    next_url = urllib.parse.urljoin(current_url, location)
                    # Caller headers may contain provider-specific secrets. Never
                    # forward them to a different scheme, host or effective port.
                    source = httpx.URL(current_url)
                    target = httpx.URL(next_url)
                    if (source.scheme, source.host, source.port) != (target.scheme, target.host, target.port):
                        default_headers = {"User-Agent": "Infinite-Canvas/SafeMediaFetcher/1.0"}
                    current_url = validate_safe_url(next_url, allow_private=allow_private, allowed_hosts=allowed_hosts, pin_dns=True)
                    try:
                        h = urllib.parse.urlsplit(current_url).hostname
                        if h:
                            pinned_hosts.append(h.lower().strip())
                    except Exception:
                        pass
                    redirects_left -= 1
                    continue

                try:
                    response.raise_for_status()
                    content_length = response.headers.get("Content-Length")
                    if content_length and content_length.isdigit():
                        if int(content_length) > max_bytes:
                            raise ValueError(f"远程资源超出大小限制 ({int(content_length)} > {max_bytes} bytes)")

                    chunks = []
                    total_bytes = 0
                    async for chunk in response.aiter_bytes(chunk_size=65536):
                        total_bytes += len(chunk)
                        if total_bytes > max_bytes:
                            raise ValueError(f"远程资源内容超出大小限制 (已超过 {max_bytes} bytes)")
                        chunks.append(chunk)

                    content = b"".join(chunks)
                    content_type = response.headers.get("Content-Type", "")
                    return content, content_type
                finally:
                    await response.aclose()
    finally:
        for host_to_unpin in pinned_hosts:
            unpin_host_dns(host_to_unpin)


def is_valid_comfy_address(address: str) -> bool:
    """Validate ComfyUI server address format to prevent header injection or URL manipulation."""
    addr = str(address or "").strip()
    if not addr:
        return False
    if "@" in addr or "/" in addr or "\\" in addr or "?" in addr or "#" in addr:
        return False
    return bool(COMFY_HOST_PATTERN.match(addr))


def sanitize_secret_tokens(text: str) -> str:
    """Mask sensitive tokens, API keys, and authorization secrets in URLs, headers, and error strings.

    Prevents accidental leakage of user keys in logs, toasts, and exception messages.
    """
    if not text or not isinstance(text, str):
        return text or ""
    # Mask URL query params: ?key=..., &api_key=..., &token=..., &secret=...
    text = re.sub(
        r'([?&](?:key|api_key|apikey|token|access_token|secret|app_key)=)([^&\s"\']+)',
        r'\1***MASKED***',
        text,
        flags=re.IGNORECASE,
    )
    # Mask Authorization headers: Bearer ...
    text = re.sub(
        r'(Bearer\s+)[A-Za-z0-9_\-\.]{8,}',
        r'\1***MASKED***',
        text,
        flags=re.IGNORECASE,
    )
    # Mask sk-... keys: sk-[a-zA-Z0-9_-]{12,}
    text = re.sub(
        r'\b(sk-[a-zA-Z0-9_\-]{3})[a-zA-Z0-9_\-]{8,}([a-zA-Z0-9_\-]{3})\b',
        r'\1****\2',
        text,
    )
    # Mask CLI arguments: --api-key <val>, --api_key <val>, --key <val>, --token <val>
    text = re.sub(
        r'(--(?:api[-_]?key|key|token|access[-_]?token)\s+)[^\s"\']+',
        r'\1***MASKED***',
        text,
        flags=re.IGNORECASE,
    )
    return text

