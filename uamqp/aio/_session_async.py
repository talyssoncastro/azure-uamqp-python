#-------------------------------------------------------------------------
# Copyright (c) Microsoft Corporation. All rights reserved.
# Licensed under the MIT License. See License.txt in the project root for
# license information.
#--------------------------------------------------------------------------

import uuid
import logging
import time

from ._anyio import create_task_group, sleep
from ..constants import (
    INCOMING_WINDOW,
    OUTGOING_WIDNOW,
    ConnectionState,
    SessionState,
    SessionTransferState,
    Role
)
from ..endpoints import Source, Target
from ._sender_async import SenderLink
from ._receiver_async import ReceiverLink
from ..performatives import (
    BeginFrame,
    EndFrame,
    FlowFrame,
    AttachFrame,
    DetachFrame,
    TransferFrame,
    DispositionFrame
)

_LOGGER = logging.getLogger(__name__)


class Session(object):
    """
    :param int remote_channel: The remote channel for this Session.
    :param int next_outgoing_id: The transfer-id of the first transfer id the sender will send.
    :param int incoming_window: The initial incoming-window of the sender.
    :param int outgoing_window: The initial outgoing-window of the sender.
    :param int handle_max: The maximum handle value that may be used on the Session.
    :param list(str) offered_capabilities: The extension capabilities the sender supports.
    :param list(str) desired_capabilities: The extension capabilities the sender may use if the receiver supports
    :param dict properties: Session properties.
    """

    def __init__(self, connection, channel, **kwargs):
        self.name = kwargs.pop('name', None) or str(uuid.uuid4())
        self.state = SessionState.UNMAPPED
        self.handle_max = kwargs.get('handle_max', 4294967295)
        self.properties = kwargs.pop('properties', None)
        self.channel = channel
        self.remote_channel = None
        self.next_outgoing_id = kwargs.pop('next_outgoing_id', 0)
        self.next_incoming_id = None
        self.incoming_window = kwargs.pop('incoming_window', 1)
        self.outgoing_window = kwargs.pop('outgoing_window', 1)
        self.target_incoming_window = self.incoming_window
        self.remote_incoming_window = 0
        self.remote_outgoing_window = 0
        self.offered_capabilities = None
        self.desired_capabilities = kwargs.pop('desired_capabilities', None)

        self.allow_pipelined_open = kwargs.pop('allow_pipelined_open', True)
        self.idle_wait_time = kwargs.get('idle_wait_time', 0.1)

        self.links = {}
        self._connection = connection
        self._output_handles = {}
        self._input_handles = {}

    async def __aenter__(self):
        await self.begin()
        return self

    async def __aexit__(self, *args):
        await self.end()

    @classmethod
    def from_incoming_frame(cls, connection, channel, frame):
        # check session_create_from_endpoint in C lib
        new_session = cls(connection, channel)
        new_session._incoming_begin(frame)
        return new_session

    async def _set_state(self, new_state):
        # type: (SessionState) -> None
        """Update the session state."""
        if new_state is None:
            return
        previous_state = self.state
        self.state = new_state
        _LOGGER.info("Session '%s' state changed: %r -> %r", self.name, previous_state, new_state)
        async with create_task_group() as tg:
            for link in self.links.values():
                await tg.spawn(link._on_session_state_change)

    async def _evaluate_status(self):
        async with create_task_group() as tg:
            for link in self.links.values():
                await tg.spawn(link._evaluate_status)

    async def _on_connection_state_change(self):
        if self._connection.state in [ConnectionState.CLOSE_RCVD, ConnectionState.END]:
            if self.state not in [SessionState.DISCARDING, SessionState.UNMAPPED]:
                await self._set_state(SessionState.DISCARDING)

    def _get_next_output_handle(self):
        # type: () -> int
        """Get the next available outgoing handle number within the max handle limit.

        :raises ValueError: If maximum handle has been reached.
        :returns: The next available outgoing handle number.
        :rtype: int
        """
        if len(self._output_handles) >= self.handle_max:
            raise ValueError("Maximum number of handles ({}) has been reached.".format(self.handle_max))
        next_handle = next(i for i in range(1, self.handle_max) if i not in self._output_handles)
        return next_handle
    
    async def _outgoing_begin(self):
        begin_frame = BeginFrame(
            remote_channel=self.remote_channel if self.state == SessionState.BEGIN_RCVD else None,
            next_outgoing_id=self.next_outgoing_id,
            outgoing_window=self.outgoing_window,
            incoming_window=self.incoming_window,
            handle_max=self.handle_max,
            offered_capabilities=self.offered_capabilities if self.state == SessionState.BEGIN_RCVD else None,
            desired_capabilities=self.desired_capabilities if self.state == SessionState.UNMAPPED else None,
            properties=self.properties,
        )
        await self._connection._process_outgoing_frame(self.channel, begin_frame)

    async def _incoming_begin(self, frame):
        self.handle_max = frame.handle_max
        self.next_incoming_id = frame.next_outgoing_id
        self.remote_incoming_window = frame.incoming_window
        self.remote_outgoing_window = frame.outgoing_window
        if self.state == SessionState.BEGIN_SENT:
            self.remote_channel = frame.remote_channel
            await self._set_state(SessionState.MAPPED)
        elif self.state == SessionState.UNMAPPED:
            await self._set_state(SessionState.BEGIN_RCVD)
            await self._outgoing_begin()
            await self._set_state(SessionState.MAPPED)

    async def _outgoing_end(self, error=None):
        end_frame = EndFrame(error=error)
        await self._connection._process_outgoing_frame(self.channel, end_frame)

    async def _incoming_end(self, frame):
        if self.state not in [SessionState.END_RCVD, SessionState.END_SENT, SessionState.DISCARDING]:
            await self._set_state(SessionState.END_RCVD)
            # TODO: Clean up all links
            await self._outgoing_end()
        await self._set_state(SessionState.UNMAPPED)

    async def _outgoing_attach(self, frame):
        await self._connection._process_outgoing_frame(self.channel, frame)

    async def _incoming_attach(self, frame):
        try:
            self._input_handles[frame.handle] = self.links[frame.name]
            await self._input_handles[frame.handle]._incoming_attach(frame)
        except KeyError:
            outgoing_handle = self._get_next_output_handle()  # TODO: catch max-handles error
            if frame.role == Role.Sender:
                new_link = ReceiverLink.from_incoming_frame(self, outgoing_handle, frame)
            else:
                new_link = SenderLink.from_incoming_frame(self, outgoing_handle, frame)
            await new_link._incoming_attach(frame)
            self.links[frame.name] = new_link
            self._output_handles[outgoing_handle] = new_link
            self._input_handles[frame.handle] = new_link
        except ValueError:
            pass  # TODO: Reject link
    
    async def _outgoing_flow(self, frame=None):
        link_flow = frame or {}
        link_flow.update({
            'next_incoming_id': self.next_incoming_id,
            'incoming_window': self.incoming_window,
            'next_outgoing_id': self.next_outgoing_id,
            'outgoing_window': self.outgoing_window
        })
        await self._connection._process_outgoing_frame(self.channel, FlowFrame(**link_flow))

    async def _incoming_flow(self, frame):
        self.next_incoming_id = frame.next_outgoing_id
        remote_incoming_id = frame.next_incoming_id or self.next_outgoing_id  # TODO "initial-outgoing-id"
        self.remote_incoming_window = remote_incoming_id + frame.incoming_window - self.next_outgoing_id
        self.remote_outgoing_window = frame.outgoing_window
        if frame.handle:
            await self._input_handles[frame.handle]._incoming_flow(frame)
        else:
            for link in self._output_handles.values():
                if self.remote_incoming_window > 0 and not link._is_closed:
                    await link._incoming_flow(frame)

    async def _outgoing_transfer(self, delivery):
        if self.state != SessionState.MAPPED:
            delivery.transfer_state = SessionTransferState.Error
        if self.remote_incoming_window <= 0:
            delivery.transfer_state = SessionTransferState.Busy
        else:
            delivery.frame['delivery_id'] = self.next_outgoing_id
            await self._connection._process_outgoing_frame(self.channel, TransferFrame(**delivery.frame))
            self.next_outgoing_id += 1
            self.remote_incoming_window -= 1
            self.outgoing_window -= 1
            delivery.transfer_state = SessionTransferState.Okay
            # TODO validate max frame size and break into multipl deliveries

    async def _incoming_transfer(self, frame):
        self.next_incoming_id += 1
        self.remote_outgoing_window -= 1
        self.incoming_window -= 1
        try:
            await self._input_handles[frame.handle]._incoming_transfer(frame)
        except KeyError:
            pass  #TODO: "unattached handle"
        if self.incoming_window == 0:
            self.incoming_window = self.target_incoming_window
            await self._outgoing_flow()

    async def _outgoing_disposition(self, frame):
        await self._connection._process_outgoing_frame(self.channel, frame)

    async def _incoming_disposition(self, frame):
        async with create_task_group() as tg:
            for link in self._input_handles.values():
                await tg.spawn(link._incoming_disposition, frame)

    async def _outgoing_detach(self, frame):
        await self._connection._process_outgoing_frame(self.channel, frame)

    async def _incoming_detach(self, frame):
        try:
            link = self._input_handles[frame.handle]
            await link._incoming_detach(frame)
            # if link._is_closed:  TODO
            #     self.links.pop(link.name, None)
            #     self._input_handles.pop(link.remote_handle, None)
            #     self._output_handles.pop(link.handle, None)
        except KeyError:
            pass  # TODO: close session with unattached-handle

    async def _wait_for_response(self, wait, end_state):
        # type: (Union[bool, float], ConnectionState) -> None
        if wait == True:
            await self._connection.listen(wait=False)
            while self.state != end_state:
                await sleep(self.idle_wait_time)
                await self._connection.listen(wait=False)
        elif wait:
            await self._connection.listen(wait=False)
            timeout = time.time() + wait
            while self.state != end_state:
                if time.time() >= timeout:
                    break
                await sleep(self.idle_wait_time)
                await self._connection.listen(wait=False)

    async def begin(self, wait=False):
        await self._outgoing_begin()
        await self._set_state(SessionState.BEGIN_SENT)
        if wait:
            await self._wait_for_response(wait, SessionState.BEGIN_SENT)
        elif not self.allow_pipelined_open:
            raise ValueError("Connection has been configured to not allow piplined-open. Please set 'wait' parameter.")

    async def end(self, error=None, wait=False):
        # type: (Optional[AMQPError]) -> None
        if self.state not in [SessionState.UNMAPPED, SessionState.DISCARDING]:
            await self._outgoing_end(error=error)
        # TODO: destroy all links
        new_state = SessionState.DISCARDING if error else SessionState.END_SENT
        await self._set_state(new_state)
        await self._wait_for_response(wait, SessionState.UNMAPPED)

    def create_receiver_link(self, source_address, **kwargs):
        assigned_handle = self._get_next_output_handle()
        link = ReceiverLink(self, handle=assigned_handle, source_address=source_address, **kwargs)
        self.links[link.name] = link
        self._output_handles[assigned_handle] = link
        return link

    def create_sender_link(self, target_address, **kwargs):
        assigned_handle = self._get_next_output_handle()
        link = SenderLink(self, handle=assigned_handle, target_address=target_address, **kwargs)
        self._output_handles[assigned_handle] = link
        self.links[link.name] = link
        return link