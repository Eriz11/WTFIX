import asyncio
from unittest import mock
from unittest.mock import MagicMock

import pytest
from unsync import Unfuture

from wtfix.apps.admin import HeartbeatApp, SeqNumManagerApp, AuthenticationApp
from wtfix.apps.sessions import ClientSessionApp
from wtfix.apps.store import MessageStoreApp, MemoryStore
from wtfix.conf import settings
from wtfix.core.exceptions import StopMessageProcessing, SessionError
from wtfix.message import admin
from wtfix.message.admin import TestRequestMessage, HeartbeatMessage
from wtfix.message.message import OptimizedGenericMessage
from wtfix.pipeline import BasePipeline
from wtfix.protocol.common import MsgType, Tag


class TestAuthenticationApp:
    @pytest.mark.asyncio
    async def test_on_logon_raises_exception_on_wrong_heartbeat_response(self, unsync_event_loop, base_pipeline):
        with pytest.raises(SessionError):
            logon_msg = admin.LogonMessage("", "", heartbeat_int=60)
            logon_msg.ResetSeqNumFlag = True

            auth_app = AuthenticationApp(base_pipeline)
            await auth_app.on_logon(logon_msg)

    @pytest.mark.asyncio
    async def test_on_logon_sets_default_test_message_indicator_to_false(self, unsync_event_loop, base_pipeline):
        logon_msg = admin.LogonMessage("", "")
        logon_msg.ResetSeqNumFlag = True

        auth_app = AuthenticationApp(base_pipeline)
        await auth_app.on_logon(logon_msg)

        assert auth_app.test_mode is False

    @pytest.mark.asyncio
    async def test_on_logon_raises_exception_on_wrong_test_indicator_response(
        self, unsync_event_loop, base_pipeline
    ):
        with pytest.raises(SessionError):
            logon_msg = admin.LogonMessage("", "")
            logon_msg.ResetSeqNumFlag = True
            logon_msg.TestMessageIndicator = True

            auth_app = AuthenticationApp(base_pipeline)
            await auth_app.on_logon(logon_msg)

    @pytest.mark.asyncio
    async def test_on_logon_raises_exception_on_wrong_reset_sequence_number_response(
        self, unsync_event_loop, base_pipeline
    ):
        with pytest.raises(SessionError):
            logon_msg = admin.LogonMessage("", "")
            logon_msg.ResetSeqNumFlag = False

            auth_app = AuthenticationApp(base_pipeline)
            await auth_app.on_logon(logon_msg)


class TestHeartbeatApp:
    def test_heartbeat_getter_defaults_to_global_settings(self, base_pipeline):
        heartbeat_app = HeartbeatApp(base_pipeline)
        assert heartbeat_app.heartbeat == settings.default_connection.HEARTBEAT_INT

    @pytest.mark.asyncio
    async def test_server_stops_responding_after_three_test_requests(
        self, unsync_event_loop, failing_server_heartbeat_app
    ):
        await failing_server_heartbeat_app.monitor_heartbeat()

        assert failing_server_heartbeat_app.pipeline.send.call_count == 4
        assert failing_server_heartbeat_app.pipeline.stop.called

    @pytest.mark.asyncio
    async def test_monitor_heartbeat_test_request_not_necessary(
        self, unsync_event_loop, zero_heartbeat_app
    ):
        """Simulate normal heartbeat rhythm - message just received"""
        with mock.patch.object(
            HeartbeatApp, "send_test_request", return_value=Unfuture.from_value(None)
        ) as check:

            zero_heartbeat_app.sec_since_last_receive.return_value = 0
            try:
                await asyncio.wait_for(zero_heartbeat_app.monitor_heartbeat(), 0.1)
            except asyncio.futures.TimeoutError:
                pass

            assert check.call_count == 0

    @pytest.mark.asyncio
    async def test_monitor_heartbeat_heartbeat_exceeded(
        self, unsync_event_loop, zero_heartbeat_app
    ):
        """Simulate normal heartbeat rhythm - heartbeat exceeded since last message was received"""
        with mock.patch.object(
            HeartbeatApp, "send_test_request", return_value=Unfuture.from_value(None)
        ) as check:

            try:
                await asyncio.wait_for(zero_heartbeat_app.monitor_heartbeat(), 0.1)
            except asyncio.futures.TimeoutError:
                pass

            assert check.call_count > 1

    @pytest.mark.asyncio
    async def test_send_test_request(self, unsync_event_loop, zero_heartbeat_app):
        def simulate_heartbeat_response(message):
            zero_heartbeat_app.on_heartbeat(HeartbeatMessage(str(message.TestReqID)))

        zero_heartbeat_app.pipeline.send.side_effect = simulate_heartbeat_response

        try:
            await asyncio.wait_for(zero_heartbeat_app.monitor_heartbeat(), 0.1)
        except asyncio.futures.TimeoutError:
            pass

        assert not zero_heartbeat_app._server_not_responding.is_set()

    @pytest.mark.asyncio
    async def test_send_test_request_no_response(
        self, unsync_event_loop, zero_heartbeat_app
    ):
        await zero_heartbeat_app.send_test_request()
        assert zero_heartbeat_app._server_not_responding.is_set()

    @pytest.mark.asyncio
    async def test_logon_sets_heartbeat_increment(self, unsync_event_loop, logon_message, base_pipeline):
        heartbeat_app = HeartbeatApp(base_pipeline)

        logon_message.HeartBtInt = 45
        await heartbeat_app.on_logon(logon_message)

        assert heartbeat_app.heartbeat == 45

    @pytest.mark.asyncio
    async def test_sends_heartbeat_on_test_request(self, unsync_event_loop, zero_heartbeat_app):
        request_message = TestRequestMessage("test123")
        await zero_heartbeat_app.on_test_request(request_message)

        zero_heartbeat_app.pipeline.send.assert_called_with(
            admin.HeartbeatMessage("test123")
        )

    @pytest.mark.asyncio
    async def test_resets_request_id_when_heartbeat_received(self, unsync_event_loop, zero_heartbeat_app):
        heartbeat_message = HeartbeatMessage("test123")
        zero_heartbeat_app._test_request_id = "test123"

        await zero_heartbeat_app.on_heartbeat(heartbeat_message)

        assert zero_heartbeat_app._test_request_id is None

    @pytest.mark.asyncio
    async def test_on_heartbeat_handles_empty_request_id(self, unsync_event_loop, zero_heartbeat_app):
        test_request = OptimizedGenericMessage((Tag.MsgType, MsgType.TestRequest))

        assert await zero_heartbeat_app.on_heartbeat(test_request) == test_request

    @pytest.mark.asyncio
    async def test_on_receive_updated_timestamp(self, unsync_event_loop, zero_heartbeat_app):
        prev_timestamp = zero_heartbeat_app._last_receive

        await zero_heartbeat_app.on_receive(TestRequestMessage("test123"))
        assert zero_heartbeat_app._last_receive != prev_timestamp


class TestSeqNumManagerApp:

    @pytest.fixture
    @pytest.mark.asyncio
    async def pipeline_with_messages(self, unsync_event_loop, base_pipeline, messages):
        message_store_app = MessageStoreApp(base_pipeline, store=MemoryStore())
        base_pipeline.apps[MessageStoreApp.name] = message_store_app

        for message in messages:  # Sent messages
            await message_store_app.set_sent(message)

        for message in messages[0:3]:  # Received messages
            tmp = message.SenderCompID
            message.SenderCompID = message.TargetCompID
            message.TargetCompID = tmp
            await message_store_app.set_received(message)

        return base_pipeline

    @pytest.mark.asyncio
    async def test_start_resumes_sequence_numbers(self, unsync_event_loop, pipeline_with_messages):

        pipeline_with_messages.apps[ClientSessionApp.name]._is_resumed = True
        seq_num_app = SeqNumManagerApp(pipeline_with_messages)
        await seq_num_app.start()

        assert seq_num_app.send_seq_num == 5
        assert seq_num_app.receive_seq_num == 3

    @pytest.mark.asyncio
    async def test_start_resets_sequence_numbers_for_new_session(self, unsync_event_loop, pipeline_with_messages):

        pipeline_with_messages.apps[ClientSessionApp.name]._is_resumed = False
        seq_num_app = SeqNumManagerApp(pipeline_with_messages)
        await seq_num_app.start()

        assert seq_num_app.send_seq_num == 0
        assert seq_num_app.receive_seq_num == 0

    @pytest.mark.asyncio
    async def test_on_resend_request_sends_resend_request(
        self, unsync_event_loop, pipeline_with_messages
    ):
        seq_num_app = SeqNumManagerApp(pipeline_with_messages)
        seq_num_app._send_seq_num = max(
            message.seq_num
            for message in pipeline_with_messages.apps[
                MessageStoreApp.name
            ].store._store.values()
        )

        resend_begin_seq_num = 2

        await seq_num_app.on_resend_request(
            admin.ResendRequestMessage(resend_begin_seq_num)
        )

        await asyncio.sleep(0.1)  # Nothing to await. Sleep to give processes time to complete.

        assert pipeline_with_messages.send.call_count == 4

        for idx in range(pipeline_with_messages.send.call_count):
            message = pipeline_with_messages.send.mock_calls[idx][1][0]
            # Check sequence number
            assert message.seq_num == resend_begin_seq_num + idx
            # Check PossDup flag
            assert bool(message.PossDupFlag) is True
            # Check sending time
            assert message.OrigSendingTime == message.SendingTime

    @pytest.mark.asyncio
    async def test_on_resend_request_handles_admin_messages_correctly(
        self, unsync_event_loop, logon_message, pipeline_with_messages, messages
    ):
        seq_num_app = SeqNumManagerApp(pipeline_with_messages)

        admin_messages = [logon_message, HeartbeatMessage("test123")]

        # Inject admin messages
        messages = admin_messages + messages

        # Reset sequence numbers
        for idx, message in enumerate(messages):
            message.MsgSeqNum = idx + 1

        message_store_app = pipeline_with_messages.apps[MessageStoreApp.name]
        message_store_app.store._store.clear()

        for message in messages:
            await message_store_app.set_sent(message)

        seq_num_app._send_seq_num = max(
            message.seq_num
            for message in pipeline_with_messages.apps[
                MessageStoreApp.name
            ].store._store.values()
        )

        resend_begin_seq_num = 1

        await seq_num_app.on_resend_request(
            admin.ResendRequestMessage(resend_begin_seq_num)
        )

        assert pipeline_with_messages.send.call_count == 6

        admin_messages_resend = pipeline_with_messages.send.mock_calls[0][1][0]
        # Check SequenceReset message is constructed correctly
        assert admin_messages_resend.seq_num == 1
        assert int(admin_messages_resend.NewSeqNo) == 3
        assert bool(admin_messages_resend.PossDupFlag) is True

        # Check first non-admin message starts with correct sequence number
        first_non_admin_message_resend = pipeline_with_messages.send.mock_calls[1][
            1
        ][0]
        assert first_non_admin_message_resend.seq_num == 3

    @pytest.mark.asyncio
    async def test_on_receive_with_gaps_sends_resend_request(self, unsync_event_loop, messages):
        pipeline_mock = MagicMock(BasePipeline)
        seq_num_app = SeqNumManagerApp(pipeline_mock)

        await seq_num_app.on_receive(messages[0])

        try:
            await seq_num_app.on_receive(messages[-1])
            assert False  # Should not reach here
        except StopMessageProcessing:
            # Expected - ignore
            pass

        assert pipeline_mock.send.call_count == 1

    @pytest.mark.asyncio
    async def test_on_receive_handles_gapfill(self, unsync_event_loop, pipeline_with_messages, user_notification_message):
        seq_num_app = SeqNumManagerApp(pipeline_with_messages)

        seq_num_app._receive_seq_num = 3
        user_notification_message.seq_num = 6  # Simulate missing messages 4 and 5

        try:
            await seq_num_app.on_receive(user_notification_message)
            assert pipeline_with_messages.send.call_count == 1  # Resend request sent
        except StopMessageProcessing:
            # Expected
            pass

        # Simulate resend of 4 and 5
        for seq_num in [4, 5]:
            message = user_notification_message.copy()
            message.seq_num = seq_num
            message.PossDupFlag = True
            await seq_num_app.on_receive(message)

        assert pipeline_with_messages.receive.call_count == 1  # Queued messages processed

    # @pytest.mark.asyncio
    # async def test_on_receive_ignores_poss_dups(self, unsync_event_loop, messages):
    #     pipeline_mock = MagicMock(BasePipeline)
    #     seq_num_app = SeqNumManagerApp(pipeline_mock)
    #
    #     for next_message in messages:
    #         await seq_num_app.on_receive(next_message)
    #
    #     try:
    #         dup_message = messages[-1]
    #         dup_message.PossDupFlag = "Y"
    #         await seq_num_app.on_receive(dup_message)
    #
    #         assert False  # Should not reach here
    #     except StopMessageProcessing:
    #         # Expected - ignore
    #         pass

    def test_check_poss_dup_raises_exception_for_unexpected_sequence_numbers(
        self, user_notification_message
    ):
        with pytest.raises(SessionError):
            pipeline_mock = MagicMock(BasePipeline)
            seq_num_app = SeqNumManagerApp(pipeline_mock)
            seq_num_app._receive_seq_num = 10

            user_notification_message.MsgSeqNum = 1

            seq_num_app._check_poss_dup(user_notification_message)
