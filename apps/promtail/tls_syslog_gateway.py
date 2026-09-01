import asyncio
import os
import signal
import ssl
from collections.abc import Mapping
from contextlib import suppress


LISTEN_HOST = os.getenv("RUNTIME_LOG_LISTEN_HOST", "0.0.0.0")
LISTEN_PORT = int(os.getenv("RUNTIME_LOG_LISTEN_PORT", "1514"))
UPSTREAM_HOST = os.getenv("RUNTIME_LOG_UPSTREAM_HOST", "127.0.0.1")
UPSTREAM_PORT = int(os.getenv("RUNTIME_LOG_UPSTREAM_PORT", "1515"))
CERT_FILE = os.getenv(
    "RUNTIME_LOG_TLS_CERT_FILE", "/etc/promtail/pki/server-cert.pem"
)
KEY_FILE = os.getenv(
    "RUNTIME_LOG_TLS_KEY_FILE", "/etc/promtail/pki/server-key.pem"
)
CA_FILE = os.getenv("RUNTIME_LOG_TLS_CA_FILE", "/etc/promtail/pki/ca.pem")
CRL_FILE = os.getenv("RUNTIME_LOG_TLS_CRL_FILE", "/etc/promtail/pki/crl.pem")
ALLOWED_CLIENTS = frozenset(
    name.strip()
    for name in os.getenv(
        "RUNTIME_LOG_ALLOWED_CLIENTS",
        "user-service,order-service,payment-service",
    ).split(",")
    if name.strip()
)
ACTIVE_CLIENTS: set[asyncio.StreamWriter] = set()


def peer_common_name(peer_certificate: Mapping[str, object] | None) -> str | None:
    if not peer_certificate:
        return None
    for relative_name in peer_certificate.get("subject", ()):  # type: ignore[union-attr]
        for key, value in relative_name:
            if key == "commonName":
                return str(value)
    return None


def create_server_ssl_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.verify_mode = ssl.CERT_REQUIRED
    context.load_cert_chain(certfile=CERT_FILE, keyfile=KEY_FILE)
    context.load_verify_locations(cafile=CA_FILE)
    if os.path.exists(CRL_FILE) and os.path.getsize(CRL_FILE) > 0:
        context.load_verify_locations(cafile=CRL_FILE)
        context.verify_flags |= ssl.VERIFY_CRL_CHECK_LEAF
    return context


async def pipe_stream(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    while data := await reader.read(64 * 1024):
        writer.write(data)
        await writer.drain()


async def connect_upstream() -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    last_error: OSError | None = None
    for _ in range(100):
        try:
            return await asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT)
        except OSError as exc:
            last_error = exc
            await asyncio.sleep(0.1)
    raise ConnectionError(
        f"Runtime log upstream unavailable after 10 seconds: {last_error}"
    )


async def handle_client(
    reader: asyncio.StreamReader, writer: asyncio.StreamWriter
) -> None:
    peer_certificate = writer.get_extra_info("peercert")
    client_name = peer_common_name(peer_certificate)
    if client_name not in ALLOWED_CLIENTS:
        print(f"Rejected runtime log client common_name={client_name!r}", flush=True)
        writer.close()
        await writer.wait_closed()
        return

    ACTIVE_CLIENTS.add(writer)
    try:
        _upstream_reader, upstream_writer = await connect_upstream()
    except ConnectionError as exc:
        print(f"Runtime log upstream unavailable: {exc}", flush=True)
        writer.close()
        with suppress(ConnectionError, ssl.SSLError):
            await writer.wait_closed()
        ACTIVE_CLIENTS.discard(writer)
        return

    try:
        await pipe_stream(reader, upstream_writer)
    except (ConnectionError, ssl.SSLError):
        pass
    finally:
        ACTIVE_CLIENTS.discard(writer)
        upstream_writer.close()
        writer.close()
        with suppress(ConnectionError, ssl.SSLError):
            await upstream_writer.wait_closed()
            await writer.wait_closed()


async def start_tls_server(context: ssl.SSLContext) -> asyncio.Server:
    return await asyncio.start_server(
        handle_client,
        LISTEN_HOST,
        LISTEN_PORT,
        ssl=context,
        reuse_port=True,
    )


async def reload_tls_server(server: asyncio.Server) -> asyncio.Server:
    """Replace only the listening socket; accepted log streams keep draining."""
    replacement = await start_tls_server(create_server_ssl_context())
    server.close()
    await server.wait_closed()
    reauthenticated_clients = len(ACTIVE_CLIENTS)
    for writer in tuple(ACTIVE_CLIENTS):
        writer.close()
    print(
        "Reloaded runtime log TLS certificate, trust bundle, and CRL; "
        f"reauthenticating {reauthenticated_clients} active client(s)",
        flush=True,
    )
    return replacement


async def run() -> None:
    server_ssl_context = create_server_ssl_context()
    if os.geteuid() == 0:
        os.setgroups([])
        os.setgid(65534)
        os.setuid(65534)
    promtail = await asyncio.create_subprocess_exec(
        "/usr/bin/promtail", "-config.file=/etc/promtail/config.yml"
    )
    stop_event = asyncio.Event()
    reload_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for watched_signal in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(watched_signal, stop_event.set)
    loop.add_signal_handler(signal.SIGHUP, reload_event.set)

    try:
        server = await start_tls_server(server_ssl_context)
    except BaseException:
        promtail.terminate()
        await promtail.wait()
        raise

    print(
        f"Runtime log mTLS gateway listening on {LISTEN_HOST}:{LISTEN_PORT}",
        flush=True,
    )
    promtail_exit = asyncio.create_task(promtail.wait())
    stop_requested = asyncio.create_task(stop_event.wait())
    while True:
        reload_requested = asyncio.create_task(reload_event.wait())
        done, _ = await asyncio.wait(
            {promtail_exit, stop_requested, reload_requested},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if reload_requested in done:
            reload_event.clear()
            try:
                server = await reload_tls_server(server)
            except (OSError, ssl.SSLError) as exc:
                print(f"Rejected runtime log TLS reload: {exc}", flush=True)
            continue
        reload_requested.cancel()
        server.close()
        await server.wait_closed()
        break

    if stop_requested in done:
        promtail.terminate()
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(promtail.wait(), timeout=10)
        if promtail.returncode is None:
            promtail.kill()
            await promtail.wait()
    else:
        stop_requested.cancel()
        raise RuntimeError(f"Promtail exited unexpectedly with {promtail.returncode}")


if __name__ == "__main__":
    asyncio.run(run())
