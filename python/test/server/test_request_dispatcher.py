#    test_request_dispatcher.py
#        Test the request dispatcher.
#        Priorities, throttling, size limits.
#
#   - License : MIT - See LICENSE file.
#   - Project : Scrutiny Debugger (github.com/scrutinydebugger/scrutiny)
#
#   Copyright (c) 2021-2022 scrutinydebugger

import unittest

from scrutiny.server.device.request_dispatcher import RequestDispatcher, RequestQueue, Throttler
from scrutiny.server.protocol.commands import DummyCommand
from scrutiny.server.protocol import Request, Response, ResponseCode
import time


class TestPriorityQueue(unittest.TestCase):
    def test_priority_queue_no_priority(self):
        q = RequestQueue()

        self.assertIsNone(q.peek())
        self.assertIsNone(q.pop())

        q.push(10)
        q.push(20)
        q.push(30)

        self.assertEqual(q.peek(), 10)
        self.assertEqual(q.peek(), 10)
        self.assertEqual(q.peek(), 10)

        self.assertEqual(q.pop(), 10)
        self.assertEqual(q.pop(), 20)
        self.assertEqual(q.pop(), 30)

        self.assertIsNone(q.pop())

    def test_priority_queuewith_priority(self):
        q = RequestQueue()

        q.push(10, priority=0)
        q.push(20, priority=1)
        q.push(30, priority=0)
        q.push(40, priority=1)
        q.push(50, priority=0)

        self.assertEqual(q.pop(), 20)
        self.assertEqual(q.pop(), 40)
        self.assertEqual(q.pop(), 10)
        self.assertEqual(q.pop(), 30)
        self.assertEqual(q.pop(), 50)


class TestRequestDispatcher(unittest.TestCase):
    def setUp(self):
        self.success_list = []
        self.failure_list = []

    def make_payload(self, size):
        return b'\x01' * size

    def make_dummy_request(self, subfn=0, payload=b'', response_payload_size=0):
        return Request(DummyCommand, subfn=subfn, payload=payload, response_payload_size=response_payload_size)

    def success_callback(self, request, response_code, response_data, params=None):
        self.success_list.append({
            'request': request,
            'response_code': response_code,
            'response_data': response_data,
            'params': params
        })

    def failure_callback(self, request, params=None):
        self.failure_list.append({
            'request': request,
            'params': params
        })

    def test_priority_respect(self):
        dispatcher = RequestDispatcher()
        req1 = self.make_dummy_request()
        req2 = self.make_dummy_request()
        req3 = self.make_dummy_request()

        dispatcher.register_request(request=req1, success_callback=self.success_callback, failure_callback=self.failure_callback, priority=0)
        dispatcher.register_request(request=req2, success_callback=self.success_callback, failure_callback=self.failure_callback, priority=1)
        dispatcher.register_request(request=req3, success_callback=self.success_callback, failure_callback=self.failure_callback, priority=0)

        self.assertEqual(dispatcher.next().request, req2)
        self.assertEqual(dispatcher.next().request, req1)
        self.assertEqual(dispatcher.next().request, req3)

    def test_throttling_basics(self):
        dispatcher = RequestDispatcher()
        req1 = self.make_dummy_request(payload=self.make_payload(512), response_payload_size=512)
        dispatcher.register_request(request=req1, success_callback=self.success_callback, failure_callback=self.failure_callback, priority=0)
        dispatcher.enable_throttling(1024 * 1024)  # 1Mbit bps
        allowed_bits_initial = dispatcher.throttler.allowed_bits()
        record = dispatcher.next()
        self.assertIsNotNone(record)
        self.assertEqual(record.request, req1)
        self.assertLess(dispatcher.throttler.allowed_bits(), allowed_bits_initial)  # Check that less bit is allowed
        dispatcher.process()
        time.sleep(0.2)
        dispatcher.process()
        self.assertEqual(dispatcher.throttler.allowed_bits(), allowed_bits_initial)  # Check that we are back on our feet

    def test_callbacks(self):
        dispatcher = RequestDispatcher()
        req1 = self.make_dummy_request()
        req2 = self.make_dummy_request()

        dispatcher.register_request(request=req1, success_callback=self.success_callback,
                                    failure_callback=self.failure_callback, success_params=[1, 2], failure_params=[3, 4])
        dispatcher.register_request(request=req2, success_callback=self.success_callback,
                                    failure_callback=self.failure_callback, success_params=[5, 6], failure_params=[7, 8])

        record = dispatcher.next()
        record.complete(success=True, response=Response(DummyCommand, subfn=0, code=ResponseCode.OK), response_data="data1")
        record = dispatcher.next()
        record.complete(success=False)

        self.assertEqual(len(self.success_list), 1)
        self.assertEqual(self.success_list[0]['request'], req1)
        self.assertEqual(self.success_list[0]['response_code'], ResponseCode.OK)
        self.assertEqual(self.success_list[0]['response_data'], "data1")
        self.assertEqual(self.success_list[0]['params'], [1, 2])

        self.assertEqual(len(self.failure_list), 1)
        self.assertEqual(self.failure_list[0]['request'], req2)
        self.assertEqual(self.failure_list[0]['params'], [7, 8])

    def test_drops_overflowing_requests(self):
        dispatcher = RequestDispatcher()
        req1 = self.make_dummy_request(payload=self.make_payload(128 - 8), response_payload_size=256 - 9)
        req2 = self.make_dummy_request(payload=self.make_payload(129 - 8), response_payload_size=256 - 9)
        req3 = self.make_dummy_request(payload=self.make_payload(128 - 8), response_payload_size=257 - 9)
        dispatcher.set_size_limits(rx_size_limit=128, tx_size_limit=256)

        dispatcher.logger.disabled = True
        dispatcher.register_request(request=req1, success_callback=self.success_callback, failure_callback=self.failure_callback)
        dispatcher.register_request(request=req2, success_callback=self.success_callback, failure_callback=self.failure_callback)
        dispatcher.register_request(request=req3, success_callback=self.success_callback, failure_callback=self.failure_callback)
        dispatcher.logger.disabled = False

        self.assertEqual(dispatcher.next().request, req1)
        self.assertIsNone(dispatcher.next())
