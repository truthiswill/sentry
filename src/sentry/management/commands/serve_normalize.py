"""
sentry.management.commands.serve_normalize
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

:copyright: (c) 2018 by the Sentry Team, see AUTHORS for more details.
:license: BSD, see LICENSE for more details.
"""
from __future__ import absolute_import, print_function

import SocketServer
import base64
import os
import stat
import sys
import time
import traceback
import json
import resource
import multiprocessing

from django.core.management.base import BaseCommand, CommandError, make_option
from django.utils.encoding import force_str


def catch_errors(f):
    def wrapper(*args, **kwargs):
        error = None
        try:
            return f(*args, **kwargs)
        except Exception as e:
            error = force_str(e.message) + ' ' + force_str(traceback.format_exc())

        try:
            return encode({
                'result': None,
                'error': error,
                'metrics': None
            })
        except (ValueError, TypeError) as e:
            try:
                # Encoding error, try to send the exception instead
                return encode({
                    'result': None,
                    'error': force_str(e.message) + ' ' + force_str(traceback.format_exc()),
                    'metrics': None,
                    'encoding_error': True,
                })
            except Exception:
                return b'{}'

    return wrapper


# Here's where the normalization itself happens
def process_event(data, meta):
    from sentry.event_manager import EventManager
    from sentry.tasks.store import should_process

    event_manager = EventManager(
        data,
        client_ip=meta.get('REMOTE_ADDR'),
        user_agent=meta.get('HTTP_USER_AGENT'),
        auth=None,
        key=None,
        content_encoding=meta.get('HTTP_CONTENT_ENCODING')
    )
    event_manager.normalize()

    event = event_manager.get_data()
    group_hash = None

    if not should_process(event):
        group_hash = event_manager._get_event_instance(project_id=1).get_hashes()
    return {
        "event": dict(event),
        "group_hash": group_hash,
    }


def decode(message):
    meta, data_encoded = json.loads(message)
    data = base64.b64decode(data_encoded)
    return data, meta


def encode(data):
    # Normalized data should be serializable
    return json.dumps(data)


@catch_errors
def handle_data(data):
    mc = MetricCollector()

    metrics_before = mc.collect_metrics()
    data, meta = decode(data)
    rv = process_event(data, meta)
    metrics_after = mc.collect_metrics()

    return encode({
        'result': rv,
        'metrics': {'before': metrics_before, 'after': metrics_after},
        'error': None
    })


def handle_data_piped(pipe, data):
    pipe.send(handle_data(data))


class MetricCollector(object):
    def __init__(self):
        self.is_linux = sys.platform.startswith('linux')
        self.pid = os.getpid()

    def collect_metrics(self):
        metrics = {
            'time': time.time(),
        }

        usage = resource.getrusage(resource.RUSAGE_SELF)
        usage_dict = {attr: getattr(usage, attr) for attr in dir(usage) if attr.startswith('ru_')}
        metrics.update(usage_dict)

        if self.is_linux:
            with open('/proc/{}/status'.format(self.pid)) as procfh:
                metrics['proc'] = procfh.read()

        return metrics


class EventNormalizeHandler(SocketServer.BaseRequestHandler):
    """
    The request handler class for our server.

    It is instantiated once per connection to the server, and must
    override the handle() method to implement communication to the
    client.
    """

    BUFFER_SIZE = 4096
    SOCKET_TIMEOUT = 10.0

    def handle(self):
        self.server.socket.settimeout(self.SOCKET_TIMEOUT)

        chunks = []

        # Receive the data
        while True:
            rcvd = self.request.recv(self.BUFFER_SIZE)
            if rcvd is None:
                raise ValueError('Received None')

            if not rcvd:
                break
            chunks.append(rcvd)

        self.data = ''.join(chunks)

        response = self.handle_data()
        self.request.sendall(response)
        self.request.close()

    def handle_data(self):
        from sentry.event_manager import ENABLE_RUST
        if not ENABLE_RUST:
            return handle_data(self.data)

        @catch_errors
        def inner():
            # TODO: Remove this contraption once we no longer get segfaults
            parent_conn, child_conn = multiprocessing.Pipe()
            p = multiprocessing.Process(
                target=handle_data_piped,
                args=(child_conn, self.data,)
            )
            p.start()
            p.join(1)
            assert parent_conn.poll(), "Process crashed"
            return parent_conn.recv()

        return inner()


class Command(BaseCommand):
    help = 'Start a socket server for event normalization'

    option_list = BaseCommand.option_list + (
        make_option('--unix', dest='socket_file',
                    help='Unix socket to bind to. Example: "/tmp/normalize.sock"'),
        make_option('--net', dest='network_socket',
                    help='Network socket to bind to. Example: "127.0.0.1:1234"'),
    )

    def _check_socket_path(self, socket_file):
        if os.path.exists(socket_file):
            file_mode = os.stat(socket_file).st_mode
            if not stat.S_ISSOCK(file_mode):
                raise CommandError('File already exists and is not a socket')

        # Make sure the socket does not already exist
        try:
            os.unlink(socket_file)
        except OSError:
            if os.path.exists(socket_file):
                raise

    def handle(self, **options):
        socket_file = options.get('socket_file')
        network_socket = options.get('network_socket')
        if socket_file and network_socket:
            raise CommandError('Only one socket allowed at a time')
        elif socket_file:
            self.socket_file = os.path.abspath(socket_file)
            self._check_socket_path(socket_file)
            self.stdout.write('Binding to unix socket: %s' % (socket_file,))
            server = SocketServer.UnixStreamServer(socket_file, EventNormalizeHandler)
        elif network_socket:
            host, port = network_socket.split(':')
            port = int(port)
            self.stdout.write('Binding to network socket: %s:%s' % (host, port))
            server = SocketServer.TCPServer((host, port), EventNormalizeHandler)
        else:
            raise CommandError('No connection option specified')

        server.serve_forever()
