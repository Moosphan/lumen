"""Bounded JSON reads for local runtime endpoints."""
import json
import socket
from contextlib import closing, suppress
from threading import Event, Timer
from time import monotonic


def fetch_json(connection, path, *, timeout, limit):
    deadline = monotonic() + timeout
    with closing(connection):
        connection.connect()
        stream = connection.sock
        expired = Event()

        def stop_reading():
            expired.set()
            with suppress(OSError):
                stream.shutdown(socket.SHUT_RDWR)

        # Enforce a single deadline even when a peer streams each chunk slowly.
        watchdog = Timer(max(0, deadline - monotonic()), stop_reading)
        watchdog.daemon = True
        watchdog.start()
        try:
            connection.request('GET', path, headers={'Accept': 'application/json', 'Connection': 'close'})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise ConnectionError('Runtime endpoint returned a non-200 response')
                if response.length is not None and response.length > limit:
                    raise ValueError('Runtime response exceeds the size limit')
                data = response.read(limit + 1)
                if expired.is_set() or monotonic() >= deadline:
                    raise TimeoutError('Runtime check reached its deadline')
                if len(data) > limit or response.length not in (None, 0):
                    raise ValueError('Runtime response is over the size limit or incomplete')
                return json.loads(data)
        finally:
            watchdog.cancel()
