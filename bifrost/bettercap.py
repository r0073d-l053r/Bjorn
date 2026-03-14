"""
Bifrost — Bettercap REST API client.
Ported from pwnagotchi/bettercap.py using urllib (no requests dependency).
"""
import json
import logging
import base64
import urllib.request
import urllib.error

from logger import Logger

logger = Logger(name="bifrost.bettercap", level=logging.DEBUG)


class BettercapClient:
    """Synchronous REST client for the bettercap API."""

    def __init__(self, hostname='127.0.0.1', scheme='http', port=8081,
                 username='user', password='pass'):
        self.hostname = hostname
        self.scheme = scheme
        self.port = port
        self.username = username
        self.password = password
        self.url = "%s://%s:%d/api" % (scheme, hostname, port)
        self.websocket = "ws://%s:%s@%s:%d/api" % (username, password, hostname, port)
        self._auth_header = 'Basic ' + base64.b64encode(
            ('%s:%s' % (username, password)).encode()
        ).decode()

    def _request(self, method, path, data=None, verbose_errors=True):
        """Make an HTTP request to bettercap API."""
        url = "%s%s" % (self.url, path)
        body = json.dumps(data).encode() if data else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header('Authorization', self._auth_header)
        if body:
            req.add_header('Content-Type', 'application/json')

        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                raw = resp.read().decode('utf-8')
                try:
                    return json.loads(raw)
                except json.JSONDecodeError:
                    return raw
        except urllib.error.HTTPError as e:
            err = "error %d: %s" % (e.code, e.read().decode('utf-8', errors='replace').strip())
            if verbose_errors:
                logger.info(err)
            raise Exception(err)
        except urllib.error.URLError as e:
            raise Exception("bettercap unreachable: %s" % e.reason)

    def session(self):
        """GET /api/session — current bettercap state."""
        return self._request('GET', '/session')

    def run(self, command, verbose_errors=True):
        """POST /api/session — execute a bettercap command."""
        return self._request('POST', '/session', {'cmd': command},
                             verbose_errors=verbose_errors)

    def events(self):
        """GET /api/events — poll recent events (REST fallback)."""
        try:
            result = self._request('GET', '/events', verbose_errors=False)
            # Clear after reading so we don't reprocess
            try:
                self.run('events.clear', verbose_errors=False)
            except Exception:
                pass
            return result if isinstance(result, list) else []
        except Exception:
            return []

    async def start_websocket(self, consumer, stop_event=None):
        """Connect to bettercap websocket event stream.

        Args:
            consumer: async callable that receives each message string.
            stop_event: optional threading.Event — exit when set.
        """
        import websockets
        import asyncio
        ws_url = "%s/events" % self.websocket
        while not (stop_event and stop_event.is_set()):
            try:
                async with websockets.connect(ws_url, ping_interval=60,
                                              ping_timeout=90) as ws:
                    async for msg in ws:
                        if stop_event and stop_event.is_set():
                            return
                        try:
                            await consumer(msg)
                        except Exception as ex:
                            logger.debug("Error parsing event: %s", ex)
            except Exception as ex:
                if stop_event and stop_event.is_set():
                    return
                logger.debug("Websocket error: %s — reconnecting...", ex)
                await asyncio.sleep(2)
