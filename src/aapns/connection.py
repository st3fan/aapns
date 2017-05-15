import json
import os
from asyncio import get_event_loop, Protocol, Future
from ssl import create_default_context
from typing import Optional

import attr
from h2.connection import H2Connection
from h2.events import (
    ResponseReceived, DataReceived, StreamEnded,
    StreamReset,
)
from hyperframe.frame import SettingsFrame
from structlog import wrap_logger, PrintLogger

from . import errors, config, models


SIZE = 4096


@attr.s
class PendingResponse:
    logger = attr.ib()
    future = attr.ib(default=attr.Factory(Future))
    headers = attr.ib(default=None)
    body = attr.ib(default=b'')


class APNS(Protocol):
    def __init__(self, client_cert_path, server, logger=None):
        self._logger = logger or wrap_logger(PrintLogger(open(os.devnull, 'w')))
        self._client_cert_path = client_cert_path
        self._server = server
        self._conn = H2Connection()
        self._transport = None
        self._responses = {}

    async def send_notification(self,
                                token: str,
                                notification: models.Notification,
                                *,
                                apns_id: Optional[str]=None,
                                expiration: Optional[int]=None,
                                priority: config.Priority=config.Priority.normal,
                                topic: Optional[str]=None,
                                collapse_id: Optional[str]=None) -> str:
        stream_id = self._conn.get_next_available_stream_id()
        logger = self._logger.bind(stream_id=stream_id)
        request_body = notification.encode()
        request_headers = [
            (':method', 'POST'),
            (':authority', self._server.host),
            (':scheme', 'https'),
            (':path', f'/3/device/{token}'),
            ('content-length', str(len(request_body))),
            ('apns-priority', str(priority.value)),
        ]
        if apns_id:
            request_headers.append(('apns-id', apns_id))
        if expiration:
            request_headers.append(('apns-expiration', str(expiration)))
        if topic:
            request_headers.append(('apns-topic', topic))
        if collapse_id:
            request_headers.append(('apns-collapse-id', collapse_id))
        response = self._responses[stream_id] = PendingResponse(logger=logger)
        logger.debug('request', headers=request_headers, body=request_body)
        self._conn.send_headers(stream_id, request_headers)
        self._conn.send_data(stream_id, request_body, end_stream=True)
        await response.future
        logger.debug('response', headers=response.headers, body=response.body)

        headers = dict(response.headers)

        response_id = headers.get(b'apns-id', b'')
        if b':status' not in headers:
            logger.critical('nostatus')
            status = -1
        else:
            status = int(headers[b':status'].decode('ascii'))

        if status != 200:
            try:
                reason = json.loads(response.body)['reason']
            except:
                reason = response.body
            exc = errors.get(reason, response_id)
            logger.critical('error', exc=exc)
            raise exc
        else:
            ascii_response_id = response_id.decode('ascii')
            logger.debug('apns-id', apns_id=ascii_response_id)

    async def close(self):
        self._conn.close_connection()
        self._transport.write(self._conn.data_to_send())
        self._transport.close()

    async def reconnect(self):
        await self.close()
        return await connect(self._client_cert_path, self._server)

    def connection_made(self, transport):
        self._transport = transport
        self._conn.initiate_connection()

        # This reproduces the error in #396, by changing the header table size.
        self._conn.update_settings({SettingsFrame.HEADER_TABLE_SIZE: SIZE})

        self._transport.write(self._conn.data_to_send())

    def data_received(self, data: bytes):
        events = self._conn.receive_data(data)

        for event in events:
            if isinstance(event, ResponseReceived):
                self.handle_response(event.headers, event.stream_id)
            elif isinstance(event, DataReceived):
                self.handle_data(event.data, event.stream_id)
            elif isinstance(event, StreamEnded):
                self.end_stream(event.stream_id)
            elif isinstance(event, StreamReset):
                self.reset_stream(event.stream_id)
            else:
                self._logger.debug('ignored', h2event=event)

        data = self._conn.data_to_send()
        if data:
            self._transport.write(data)

    def handle_response(self, response_headers, stream_id):
        if stream_id in self._responses:
            self._responses[stream_id].logger.debug(
                'response-headers',
                headers=response_headers
            )
            self._responses[stream_id].headers = response_headers
        else:
            self._logger.warning(
                'unexpected-response',
                stream_id=stream_id,
                headers=response_headers
            )

    def handle_data(self, data, stream_id):
        if stream_id in self._responses:
            self._responses[stream_id].logger.debug(
                'response-body',
                data=data
            )
            self._responses[stream_id].body += data
        else:
            self._logger.warning(
                'unexpected-data',
                stream_id=stream_id,
                data=data
            )

    def end_stream(self, stream_id):
        if stream_id in self._responses:
            response = self._responses.pop(stream_id)
            response.logger.debug('end-stream')
            response.future.set_result(True)
        else:
            self._logger.warning('unexpected-end-stream', stream_id=stream_id)

    def reset_stream(self, stream_id):
        if stream_id in self._responses:
            response = self._responses.pop(stream_id)
            response.logger.debug('reset-stream')
            response.future.set_exception(errors.StreamResetError())
        else:
            self._logger.warning('unexpected-reset-stream', stream_id=stream_id)


async def connect(client_cert_path: str,
                  server: config.Server,
                  *,
                  ssl_context=None,
                  logger=None):
    if ssl_context is None:
        ssl_context = create_default_context()
        ssl_context.set_alpn_protocols(['h2'])
        ssl_context.set_npn_protocols(['h2'])
    ssl_context.load_cert_chain(client_cert_path)
    api = APNS(client_cert_path, server, logger)
    await get_event_loop().create_connection(
        lambda: api,
        server.host,
        server.port,
        ssl=ssl_context
    )
    return api
