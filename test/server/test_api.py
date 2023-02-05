#    test_api.py
#        Test the client API through a fake handler
#
#   - License : MIT - See LICENSE file.
#   - Project :  Scrutiny Debugger (github.com/scrutinydebugger/scrutiny-python)
#
#   Copyright (c) 2021-2023 Scrutiny Debugger

import time
import random
import string
import json
import math
from scrutiny.core.basic_types import RuntimePublishedValue

from scrutiny.server.api.API import API
from scrutiny.server.datastore.datastore import Datastore
from scrutiny.server.datastore.datastore_entry import *
from scrutiny.server.datastore.entry_type import EntryType
from scrutiny.core.sfd_storage import SFDStorage
from scrutiny.server.api.dummy_client_handler import DummyConnection, DummyClientHandler
from scrutiny.server.device.device_handler import DeviceHandler
from scrutiny.server.device.device_info import DeviceInfo, FixedFreqLoop, VariableFreqLoop
from scrutiny.server.active_sfd_handler import ActiveSFDHandler
from scrutiny.server.device.links.dummy_link import DummyLink
from scrutiny.core.variable import *
from scrutiny.core.alias import Alias
import scrutiny.server.datalogging.definitions.api as api_datalogging
import scrutiny.server.datalogging.definitions.device as device_datalogging
from test.artifacts import get_artifact
from test import ScrutinyUnitTest
import scrutiny.server.api.typing as api_typing
from typing import cast
from scrutiny.server.datalogging.datalogging_storage import DataloggingStorage

# todo
# - Test rate limiter/data streamer


class StubbedDeviceHandler:
    connection_status: DeviceHandler.ConnectionStatus
    device_id: str
    link_type: str
    link_config: Dict[Any, Any]
    reject_link_config: bool
    datalogging_callbacks: Dict[str, GenericCallback]
    datalogger_state: device_datalogging.DataloggerState
    datalogging_setup: Optional[device_datalogging.DataloggingSetup]

    def __init__(self, device_id, connection_status=DeviceHandler.ConnectionStatus.UNKNOWN):
        self.device_id = device_id
        self.connection_status = connection_status
        self.link_type = 'none'
        self.link_config = {}
        self.reject_link_config = False
        self.datalogger_state = device_datalogging.DataloggerState.IDLE
        self.datalogging_setup = device_datalogging.DataloggingSetup(
            buffer_size=1024,
            encoding=device_datalogging.Encoding.RAW,
            max_signal_count=32
        )

    def get_connection_status(self) -> DeviceHandler.ConnectionStatus:
        return self.connection_status

    def set_connection_status(self, connection_status: DeviceHandler.ConnectionStatus) -> None:
        self.connection_status = connection_status

    def set_datalogging_setup(self, setup: Optional[device_datalogging.DataloggingSetup]) -> None:
        self.datalogging_setup = setup

    def get_datalogging_setup(self) -> Optional[device_datalogging.DataloggingSetup]:
        return self.datalogging_setup

    def get_datalogger_state(self) -> device_datalogging.DataloggerState:
        return self.datalogger_state

    def set_datalogger_state(self, state: device_datalogging.DataloggerState) -> None:
        self.datalogger_state = state

    def get_device_id(self) -> str:
        return self.device_id

    def set_datalogging_callbacks(self, **kwargs) -> None:
        self.datalogging_callbacks = kwargs

    def get_link_type(self) -> str:
        return 'dummy'

    def get_comm_link(self) -> DummyLink:
        return DummyLink()

    def get_device_info(self) -> DeviceInfo:
        info = DeviceInfo()
        info.device_id = self.device_id
        info.display_name = self.__class__.__name__
        info.max_tx_data_size = 128
        info.max_rx_data_size = 64
        info.max_bitrate_bps = 10000
        info.rx_timeout_us = 50000
        info.heartbeat_timeout_us = 4000000
        info.address_size_bits = 32
        info.protocol_major = 1
        info.protocol_minor = 0
        info.supported_feature_map = {
            'memory_read': True,
            'memory_write': True,
            'datalog_acquire': False,
            'user_command': False,
            '_64bits': True}
        info.forbidden_memory_regions = [{'start': 0x1000, 'end': 0x2000}]
        info.readonly_memory_regions = [{'start': 0x2000, 'end': 0x3000}, {'start': 0x3000, 'end': 0x4000}]
        info.runtime_published_values = []
        info.loops = [
            FixedFreqLoop(1000, "Fixed Freq 1KHz"),
            FixedFreqLoop(10000, "Fixed Freq 10KHz"),
            VariableFreqLoop("Variable Freq"),
            VariableFreqLoop("Variable Freq No DL", support_datalogging=False)
        ]
        return info

    def configure_comm(self, link_type: str, link_config: Dict[Any, Any]) -> None:
        self.link_type = link_type
        self.link_config = link_config

    def validate_link_config(self, link_type: str, link_config: Dict[Any, Any]):
        if self.reject_link_config:
            raise Exception('Bad config')


class StubbedDataloggingManager:
    datastore: Datastore
    fake_device_handler: StubbedDeviceHandler
    datalogging_setup: device_datalogging.DataloggingSetup
    request_queue: "Queue[api_datalogging.AcquisitionRequest]"

    def __init__(self, datastore: Datastore, fake_device_handler: StubbedDeviceHandler):
        self.datastore = datastore
        self.fake_device_handler = fake_device_handler
        self.request_queue = Queue()

    def get_device_setup(self) -> Optional[device_datalogging.DataloggingSetup]:
        return self.fake_device_handler.get_datalogging_setup()

    def set_device_setup(self, setup: Optional[device_datalogging.DataloggingSetup]) -> None:
        self.fake_device_handler.set_datalogging_setup(setup)

    def request_acquisition(self, acquisition: api_datalogging.AcquisitionRequest) -> None:
        self.request_queue.put(acquisition)

    def is_valid_sample_rate_id(self, identifier: int) -> bool:
        return (identifier >= 0 and identifier < 10)

    def is_ready_for_request(self) -> bool:
        return True
    
    def get_available_sampling_rates(self) -> List[api_datalogging.SamplingRate]:
        sampling_rates : List[api_datalogging.SamplingRate] = []
        info = self.fake_device_handler.get_device_info()
        if info is None:
            return
        
        i=0
        for loop in info.loops:
            if loop.support_datalogging:
                freq:Optional[float] = None
                if isinstance(loop, FixedFreqLoop):
                    freq = 1e7/loop.get_timestep_100ns()
                sampling_rates.append(api_datalogging.SamplingRate(
                    name=loop.get_name(),
                    device_identifier=i,
                    rate_type=loop.get_loop_type(),
                    frequency=freq
                ))
            i+=1
        return sampling_rates


class TestAPI(ScrutinyUnitTest):

    datastore: Datastore
    fake_device_handler: StubbedDeviceHandler
    fake_datalogging_manager: StubbedDataloggingManager
    sfd_handler: ActiveSFDHandler
    api: API

    def setUp(self):
        self.connections = [DummyConnection(), DummyConnection(), DummyConnection()]
        for conn in self.connections:
            conn.open()

        config = {
            'client_interface_type': 'dummy',
            'client_interface_config': {
            }
        }

        self.datastore = Datastore()
        self.fake_device_handler = StubbedDeviceHandler('0' * 64, DeviceHandler.ConnectionStatus.DISCONNECTED)
        self.fake_datalogging_manager = StubbedDataloggingManager(self.datastore, self.fake_device_handler)
        self.sfd_handler = ActiveSFDHandler(device_handler=self.fake_device_handler, datastore=self.datastore, autoload=False)
        self.api = API(
            config=config,
            datastore=self.datastore,
            device_handler=self.fake_device_handler,
            sfd_handler=self.sfd_handler,
            datalogging_manager=self.fake_datalogging_manager
        )
        client_handler = self.api.get_client_handler()
        assert isinstance(client_handler, DummyClientHandler)
        client_handler.set_connections(self.connections)
        self.api.start_listening()

    def tearDown(self):
        self.api.close()

    def wait_for_response(self, conn_idx=0, timeout=0.4):
        t1 = time.time()
        self.api.process()
        self.sfd_handler.process()
        while not self.connections[conn_idx].from_server_available():
            if time.time() - t1 >= timeout:
                break
            self.api.process()
            self.sfd_handler.process()
            time.sleep(0.01)

        return self.connections[conn_idx].read_from_server()

    def wait_and_load_response(self, conn_idx=0, timeout=0.4):
        json_str = self.wait_for_response(conn_idx=conn_idx, timeout=timeout)
        self.assertIsNotNone(json_str)
        return json.loads(json_str)

    def send_request(self, req, conn_idx=0):
        self.connections[conn_idx].write_to_server(json.dumps(req))

    def assert_no_error(self, response, msg=None):
        self.assertIsNotNone(response)
        if 'cmd' in response:
            if 'msg' in response and msg is None:
                msg = response['msg']
            self.assertNotEqual(response['cmd'], API.Command.Api2Client.ERROR_RESPONSE, msg)

    def assert_is_error(self, response, msg=""):
        if 'cmd' in response:
            self.assertEqual(response['cmd'], API.Command.Api2Client.ERROR_RESPONSE, msg)
        else:
            raise Exception('Missing cmd field in response')

    def make_dummy_entries(self, n, entry_type=EntryType.Var, prefix='path', alias_bucket: List[DatastoreEntry] = []) -> List[DatastoreEntry]:

        entries = []
        if entry_type == EntryType.Alias:
            assert len(alias_bucket) >= n
            for entry in alias_bucket:
                assert not isinstance(entry, DatastoreAliasEntry)

        for i in range(n):
            name = '%s_%d' % (prefix, i)
            if entry_type == EntryType.Var:
                dummy_var = Variable('dummy', vartype=EmbeddedDataType.float32, path_segments=[
                    'a', 'b', 'c'], location=0x12345678, endianness=Endianness.Little)
                entry = DatastoreVariableEntry(name, variable_def=dummy_var)
            elif entry_type == EntryType.Alias:
                entry = DatastoreAliasEntry(Alias(name, target='none'), refentry=alias_bucket[i])
            else:
                dummy_rpv = RuntimePublishedValue(id=i, datatype=EmbeddedDataType.float32)
                entry = DatastoreRPVEntry(name, rpv=dummy_rpv)
            entries.append(entry)
        return entries

    def make_random_string(self, n):
        letters = string.ascii_lowercase
        return ''.join(random.choice(letters) for i in range(n))

# ===== Test section ===============
    def test_echo(self):
        payload = self.make_random_string(100)
        req = {
            'cmd': 'echo',
            'payload': payload
        }
        self.send_request(req)
        response = self.wait_and_load_response()

        self.assertEqual(response['cmd'], 'response_echo')
        self.assertEqual(response['payload'], payload)

    # Fetch count of var/alias. Ensure response is well formatted and accurate
    def test_get_watchable_count(self):
        var_entries = self.make_dummy_entries(5, entry_type=EntryType.Var, prefix='var')
        alias_entries = self.make_dummy_entries(3, entry_type=EntryType.Alias, prefix='alias', alias_bucket=var_entries)
        rpv_entries = self.make_dummy_entries(8, entry_type=EntryType.RuntimePublishedValue, prefix='rpv')

        # Add entries in the datastore that we will reread through the API
        self.datastore.add_entries(var_entries)
        self.datastore.add_entries(alias_entries)
        self.datastore.add_entries(rpv_entries)

        req = {
            'cmd': 'get_watchable_count'
        }

        self.send_request(req)
        response = self.wait_and_load_response()
        self.assert_no_error(response)

        self.assertIn('cmd', response)
        self.assertIn('qty', response)
        self.assertIn('var', response['qty'])
        self.assertIn('alias', response['qty'])
        self.assertIn('rpv', response['qty'])

        self.assertEqual(response['cmd'], 'response_get_watchable_count')
        self.assertEqual(response['qty']['var'], 5)
        self.assertEqual(response['qty']['alias'], 3)
        self.assertEqual(response['qty']['rpv'], 8)

    def assert_get_watchable_list_response_format(self, response):
        self.assertIn('cmd', response)
        self.assertIn('qty', response)
        self.assertIn('done', response)
        self.assertIn('var', response['qty'])
        self.assertIn('alias', response['qty'])
        self.assertIn('rpv', response['qty'])
        self.assertIn('content', response)
        self.assertIn('var', response['content'])
        self.assertIn('alias', response['content'])
        self.assertIn('rpv', response['content'])
        self.assertEqual(response['cmd'], 'response_get_watchable_list')

    # Fetch list of var/alias. Ensure response is well formatted, accurate, complete, no duplicates
    def test_get_watchable_list_basic(self):
        var_entries = self.make_dummy_entries(5, entry_type=EntryType.Var, prefix='var')
        alias_entries = self.make_dummy_entries(2, entry_type=EntryType.Alias, prefix='alias', alias_bucket=var_entries)
        rpv_entries = self.make_dummy_entries(8, entry_type=EntryType.RuntimePublishedValue, prefix='rpv')

        expected_entries_in_response = {}
        for entry in var_entries:
            expected_entries_in_response[entry.get_id()] = entry
        for entry in alias_entries:
            expected_entries_in_response[entry.get_id()] = entry
        for entry in rpv_entries:
            expected_entries_in_response[entry.get_id()] = entry
        # Add entries in the datastore that we will reread through the API
        self.datastore.add_entries(var_entries)
        self.datastore.add_entries(alias_entries)
        self.datastore.add_entries(rpv_entries)

        req = {
            'cmd': 'get_watchable_list'
        }
        self.send_request(req)
        response = self.wait_and_load_response()
        self.assert_no_error(response)

        self.assert_get_watchable_list_response_format(response)

        self.assertEqual(response['done'], True)
        self.assertEqual(response['qty']['var'], 5)
        self.assertEqual(response['qty']['alias'], 2)
        self.assertEqual(response['qty']['rpv'], 8)
        self.assertEqual(len(response['content']['var']), 5)
        self.assertEqual(len(response['content']['alias']), 2)
        self.assertEqual(len(response['content']['rpv']), 8)

        # Put all entries in a single list, paired with the name of the parent key.
        all_entries_same_level = []
        all_entries_same_level += [(EntryType.Var, entry) for entry in response['content']['var']]
        all_entries_same_level += [(EntryType.Alias, entry) for entry in response['content']['alias']]
        all_entries_same_level += [(EntryType.RuntimePublishedValue, entry) for entry in response['content']['rpv']]

        # We make sure that the list is exact.
        for item in all_entries_same_level:
            entrytype = item[0]
            api_entry = item[1]

            self.assertIn('id', api_entry)
            self.assertIn('display_path', api_entry)

            self.assertIn(api_entry['id'], expected_entries_in_response)
            entry: DatastoreEntry = expected_entries_in_response[api_entry['id']]

            self.assertEqual(entry.get_id(), api_entry['id'])
            self.assertEqual(entry.get_type(), entrytype)
            self.assertEqual(API.get_datatype_name(entry.get_data_type()), api_entry['datatype'])
            self.assertEqual(entry.get_display_path(), api_entry['display_path'])

            del expected_entries_in_response[api_entry['id']]

        self.assertEqual(len(expected_entries_in_response), 0)

    # Fetch list of var/alias and sets all sort of type filter.
    def test_get_watchable_list_with_type_filter(self):
        self.do_test_get_watchable_list_with_type_filter(None)
        self.do_test_get_watchable_list_with_type_filter('')
        self.do_test_get_watchable_list_with_type_filter([])
        self.do_test_get_watchable_list_with_type_filter(['var'])
        self.do_test_get_watchable_list_with_type_filter(['alias'])
        self.do_test_get_watchable_list_with_type_filter(['rpv'])
        self.do_test_get_watchable_list_with_type_filter(['var', 'alias'])
        self.do_test_get_watchable_list_with_type_filter(['rpv', 'var'])
        self.do_test_get_watchable_list_with_type_filter(['var', 'alias', 'rpv'])

    # Fetch list of var/alias and sets a type filter.
    def do_test_get_watchable_list_with_type_filter(self, type_filter):
        self.datastore.clear()
        var_entries = self.make_dummy_entries(5, entry_type=EntryType.Var, prefix='var')
        alias_entries = self.make_dummy_entries(3, entry_type=EntryType.Alias, prefix='alias', alias_bucket=var_entries)
        rpv_entries = self.make_dummy_entries(8, entry_type=EntryType.RuntimePublishedValue, prefix='rpv')

        no_filter = True if type_filter is None or type_filter == '' or isinstance(type_filter, list) and len(type_filter) == 0 else False

        nbr_expected_var = 0
        nbr_expected_alias = 0
        nbr_expected_rpv = 0
        expected_entries_in_response = {}
        if no_filter or 'var' in type_filter:
            nbr_expected_var = len(var_entries)
            for entry in var_entries:
                expected_entries_in_response[entry.get_id()] = entry

        if no_filter or 'alias' in type_filter:
            nbr_expected_alias = len(alias_entries)
            for entry in alias_entries:
                expected_entries_in_response[entry.get_id()] = entry

        if no_filter or 'rpv' in type_filter:
            nbr_expected_rpv = len(rpv_entries)
            for entry in rpv_entries:
                expected_entries_in_response[entry.get_id()] = entry

        # Add entries in the datastore that we will reread through the API
        self.datastore.add_entries(var_entries)
        self.datastore.add_entries(alias_entries)
        self.datastore.add_entries(rpv_entries)

        req = {
            'cmd': 'get_watchable_list',
            'filter': {
                'type': type_filter
            }
        }
        self.send_request(req)
        response = self.wait_and_load_response()
        self.assert_no_error(response, 'type_filter = %s' % (str(type_filter)))

        self.assert_get_watchable_list_response_format(response)

        self.assertEqual(response['done'], True)
        self.assertEqual(response['qty']['var'], nbr_expected_var)
        self.assertEqual(response['qty']['alias'], nbr_expected_alias)
        self.assertEqual(response['qty']['rpv'], nbr_expected_rpv)
        self.assertEqual(len(response['content']['var']), nbr_expected_var)
        self.assertEqual(len(response['content']['alias']), nbr_expected_alias)
        self.assertEqual(len(response['content']['rpv']), nbr_expected_rpv)

        # Put all entries in a single list, paired with the name of the parent key.
        all_entries_same_level = []
        all_entries_same_level += [(EntryType.Var, entry) for entry in response['content']['var']]
        all_entries_same_level += [(EntryType.Alias, entry) for entry in response['content']['alias']]
        all_entries_same_level += [(EntryType.RuntimePublishedValue, entry) for entry in response['content']['rpv']]

        for item in all_entries_same_level:
            entrytype = item[0]
            api_entry = item[1]

            self.assertIn('id', api_entry)
            self.assertIn('display_path', api_entry)

            self.assertIn(api_entry['id'], expected_entries_in_response)
            entry = expected_entries_in_response[api_entry['id']]

            self.assertEqual(entry.get_id(), api_entry['id'])
            self.assertEqual(entry.get_type(), entrytype)
            self.assertEqual(entry.get_display_path(), api_entry['display_path'])

            del expected_entries_in_response[api_entry['id']]

        self.assertEqual(len(expected_entries_in_response), 0)

    # Fetch list of var/alias and sets a limit of items per response.
    # List should be broken in multiple messages

    def test_get_watchable_list_with_item_limit(self):
        nVar = 19
        nAlias = 17
        nRpv = 21
        max_per_response = 10
        var_entries = self.make_dummy_entries(nVar, entry_type=EntryType.Var, prefix='var')
        alias_entries = self.make_dummy_entries(nAlias, entry_type=EntryType.Alias, prefix='alias', alias_bucket=var_entries)
        rpv_entries = self.make_dummy_entries(nRpv, entry_type=EntryType.RuntimePublishedValue, prefix='rpv')
        expected_entries_in_response = {}

        for entry in var_entries:
            expected_entries_in_response[entry.get_id()] = entry

        for entry in alias_entries:
            expected_entries_in_response[entry.get_id()] = entry

        for entry in rpv_entries:
            expected_entries_in_response[entry.get_id()] = entry

        # Add entries in the datastore that we will reread through the API
        self.datastore.add_entries(var_entries)
        self.datastore.add_entries(alias_entries)
        self.datastore.add_entries(rpv_entries)

        req = {
            'cmd': 'get_watchable_list',
            'max_per_response': max_per_response
        }

        self.send_request(req)
        responses = []
        nresponse = math.ceil((nVar + nAlias + nRpv) / max_per_response)
        for i in range(nresponse):
            responses.append(self.wait_and_load_response())

        received_vars = []
        received_alias = []
        received_rpvs = []

        for i in range(len(responses)):
            response = responses[i]
            self.assert_no_error(response)
            self.assert_get_watchable_list_response_format(response)

            received_vars += response['content']['var']
            received_alias += response['content']['alias']
            received_rpvs += response['content']['rpv']

            if i < len(responses) - 1:
                self.assertEqual(response['done'], False)
                self.assertEqual(response['qty']['var'] + response['qty']['alias'] + response['qty']['rpv'], max_per_response)
                self.assertEqual(len(response['content']['var']) + len(response['content']['alias']) +
                                 len(response['content']['rpv']), max_per_response)
            else:
                remaining_items = nVar + nAlias + nRpv - (len(responses) - 1) * max_per_response
                self.assertEqual(response['done'], True)
                self.assertEqual(response['qty']['var'] + response['qty']['alias'] + response['qty']['rpv'], remaining_items)
                self.assertEqual(len(response['content']['var']) + len(response['content']['alias']) +
                                 len(response['content']['rpv']), remaining_items)

            # Put all entries in a single list, paired with the name of the parent key.
            all_entries_same_level = []
            all_entries_same_level += [(EntryType.Var, entry) for entry in response['content']['var']]
            all_entries_same_level += [(EntryType.Alias, entry) for entry in response['content']['alias']]
            all_entries_same_level += [(EntryType.RuntimePublishedValue, entry) for entry in response['content']['rpv']]

            for item in all_entries_same_level:

                entrytype = item[0]
                api_entry = item[1]

                self.assertIn('id', api_entry)
                self.assertIn('display_path', api_entry)

                self.assertIn(api_entry['id'], expected_entries_in_response)
                entry = expected_entries_in_response[api_entry['id']]

                self.assertEqual(entry.get_id(), api_entry['id'])
                self.assertEqual(entry.get_type(), entrytype)
                self.assertEqual(entry.get_display_path(), api_entry['display_path'])

                del expected_entries_in_response[api_entry['id']]

        self.assertEqual(len(expected_entries_in_response), 0)

    def assert_valid_value_update_message(self, msg):
        self.assert_no_error(msg)
        self.assertIn('cmd', msg)
        self.assertIn('updates', msg)

        self.assertEqual(msg['cmd'], 'watchable_update')
        self.assertIsInstance(msg['updates'], list)

        for update in msg['updates']:
            self.assertIn('id', update)
            self.assertIn('value', update)

    def test_subscribe_single_var(self):
        entries = self.make_dummy_entries(10, entry_type=EntryType.Var, prefix='var')
        self.datastore.add_entries(entries)

        subscribed_entry = entries[2]
        req = {
            'cmd': 'subscribe_watchable',
            'watchables': [subscribed_entry.get_id()]
        }

        self.send_request(req, 0)
        response = self.wait_and_load_response()
        self.assert_no_error(response)

        self.assertIn('cmd', response)
        self.assertIn('watchables', response)
        self.assertIsInstance(response['watchables'], list)

        self.assertEqual(response['cmd'], 'response_subscribe_watchable')
        self.assertEqual(len(response['watchables']), 1)
        self.assertEqual(response['watchables'][0], subscribed_entry.get_id())

        self.assertIsNone(self.wait_for_response(timeout=0.2))

        self.datastore.set_value(subscribed_entry.get_id(), 1234)

        var_update_msg = self.wait_and_load_response(timeout=0.5)
        self.assert_valid_value_update_message(var_update_msg)
        self.assertEqual(len(var_update_msg['updates']), 1)

        update = var_update_msg['updates'][0]

        self.assertEqual(update['id'], subscribed_entry.get_id())
        self.assertEqual(update['value'], 1234)

    def test_stop_watching_on_disconnect(self):
        entries = self.make_dummy_entries(2, entry_type=EntryType.Var, prefix='var')
        self.datastore.add_entries(entries)

        req = {
            'cmd': 'subscribe_watchable',
            'watchables': [entries[0].get_id(), entries[1].get_id()]
        }

        self.send_request(req, 0)   # connection 0
        response = self.wait_and_load_response(0)
        self.assert_no_error(response)

        self.send_request(req, 1)   # connection 1
        response = self.wait_and_load_response(1)
        self.assert_no_error(response)

        # Make sure we stop watching on disconnect
        self.assertEqual(len(self.datastore.get_watchers(entries[0])), 2)
        self.assertEqual(len(self.datastore.get_watchers(entries[1])), 2)
        self.connections[1].close()
        self.api.process()
        self.assertEqual(len(self.datastore.get_watchers(entries[0])), 1)
        self.assertEqual(len(self.datastore.get_watchers(entries[1])), 1)

    # Make sure that we can unsubscribe correctly to a variable and value update stops

    def test_subscribe_unsubscribe(self):
        entries = self.make_dummy_entries(10, entry_type=EntryType.Var, prefix='var')
        self.datastore.add_entries(entries)
        subscribed_entry = entries[2]
        subscribe_cmd = {
            'cmd': 'subscribe_watchable',
            'watchables': [subscribed_entry.get_id()]
        }

        # Subscribe through conn 0
        self.send_request(subscribe_cmd, 0)
        response = self.wait_and_load_response(0)
        self.assert_no_error(response)

        unsubscribe_cmd = {
            'cmd': 'unsubscribe_watchable',
            'watchables': [subscribed_entry.get_id()]
        }

        self.send_request(unsubscribe_cmd, 0)
        response = self.wait_and_load_response(0)
        self.assert_no_error(response)

        self.datastore.set_value(subscribed_entry.get_id(), 1111)
        self.assertIsNone(self.wait_for_response(0, timeout=0.1))

    # Make sure that the streamer send the value update once if many update happens before the value is outputted to the client.
    def test_do_not_send_duplicate_changes(self):
        entries = self.make_dummy_entries(10, entry_type=EntryType.Var, prefix='var')
        self.datastore.add_entries(entries)

        subscribed_entry = entries[2]
        req = {
            'cmd': 'subscribe_watchable',
            'watchables': [subscribed_entry.get_id()]
        }

        self.send_request(req, 0)
        response = self.wait_and_load_response(0)
        self.assert_no_error(response)

        self.api.streamer.freeze_connection(self.connections[0].get_id())
        self.datastore.set_value(subscribed_entry.get_id(), 1234)
        self.datastore.set_value(subscribed_entry.get_id(), 4567)
        self.api.streamer.unfreeze_connection(self.connections[0].get_id())

        var_update_msg = self.wait_and_load_response(timeout=0.5)
        self.assert_valid_value_update_message(var_update_msg)
        self.assertEqual(len(var_update_msg['updates']), 1)     # Only one update

        self.assertEqual(var_update_msg['updates'][0]['id'], subscribed_entry.get_id())
        self.assertEqual(var_update_msg['updates'][0]['value'], 4567)   # Got latest value

        self.assertIsNone(self.wait_for_response(0, timeout=0.1))   # No more message to send

    # Make sure we can read the list of installed SFD

    def test_get_sfd_list(self):
        dummy_sfd1_filename = get_artifact('test_sfd_1.sfd')
        dummy_sfd2_filename = get_artifact('test_sfd_2.sfd')
        with SFDStorage.use_temp_folder():
            sfd1 = SFDStorage.install(dummy_sfd1_filename, ignore_exist=True)
            sfd2 = SFDStorage.install(dummy_sfd2_filename, ignore_exist=True)

            req = {
                'cmd': 'get_installed_sfd'
            }

            self.send_request(req, 0)
            response = self.wait_and_load_response(timeout=0.5)
            self.assert_no_error(response)
            self.assertEqual(response['cmd'], 'response_get_installed_sfd')
            self.assertIn('sfd_list', response)

            installed_list = SFDStorage.list()
            self.assertEqual(len(installed_list), len(response['sfd_list']))

            for installed_firmware_id in installed_list:
                self.assertIn(installed_firmware_id, response['sfd_list'])
                gotten_metadata = response['sfd_list'][installed_firmware_id]
                real_metadata = SFDStorage.get_metadata(installed_firmware_id)
                self.assertEqual(real_metadata, gotten_metadata)

            SFDStorage.uninstall(sfd1.get_firmware_id_ascii())
            SFDStorage.uninstall(sfd2.get_firmware_id_ascii())

    # Check that we can load a SFD through the API and read the actually loaded SFD

    def test_load_and_get_loaded_sfd(self):
        dummy_sfd1_filename = get_artifact('test_sfd_1.sfd')
        dummy_sfd2_filename = get_artifact('test_sfd_2.sfd')
        with SFDStorage.use_temp_folder():
            sfd1 = SFDStorage.install(dummy_sfd1_filename, ignore_exist=True)
            sfd2 = SFDStorage.install(dummy_sfd2_filename, ignore_exist=True)

            # load #1
            req = {
                'cmd': 'load_sfd',
                'firmware_id': sfd1.get_firmware_id_ascii()
            }

            self.send_request(req, 0)

            # inform status should be trigger by callback
            response = self.wait_and_load_response(timeout=0.5)

            self.assertEqual(response['cmd'], 'inform_server_status')
            self.assertIn('loaded_sfd', response)
            self.assertIn('firmware_id', response['loaded_sfd'])
            self.assertEqual(response['loaded_sfd']['firmware_id'], sfd1.get_firmware_id_ascii())

            # load #2
            req = {
                'cmd': 'load_sfd',
                'firmware_id': sfd2.get_firmware_id_ascii()
            }

            self.send_request(req, 0)

            # inform status should be trigger by callback
            response = self.wait_and_load_response(timeout=0.5)
            self.assert_no_error(response)

            self.assertEqual(response['cmd'], 'inform_server_status')
            self.assertIn('loaded_sfd', response)
            self.assertIn('firmware_id', response['loaded_sfd'])
            self.assertEqual(response['loaded_sfd']['firmware_id'], sfd2.get_firmware_id_ascii())

            SFDStorage.uninstall(sfd1.get_firmware_id_ascii())
            SFDStorage.uninstall(sfd2.get_firmware_id_ascii())

    def test_get_server_status(self):
        device_info_exlude_propeties = ['runtime_published_values', 'loops']
        dummy_sfd1_filename = get_artifact('test_sfd_1.sfd')
        dummy_sfd2_filename = get_artifact('test_sfd_2.sfd')
        with SFDStorage.use_temp_folder():
            sfd1 = SFDStorage.install(dummy_sfd1_filename, ignore_exist=True)
            sfd2 = SFDStorage.install(dummy_sfd2_filename, ignore_exist=True)

            self.sfd_handler.request_load_sfd(sfd2.get_firmware_id_ascii())
            self.sfd_handler.process()
            self.fake_device_handler.set_connection_status(DeviceHandler.ConnectionStatus.CONNECTED_READY)

            req = {
                'cmd': 'get_server_status'
            }

            self.send_request(req, 0)
            response = self.wait_and_load_response(timeout=0.5)
            self.assert_no_error(response)

            self.assertEqual(response['cmd'], 'inform_server_status')
            self.assertIn('device_status', response)
            self.assertEqual(response['device_status'], 'connected_ready')
            self.assertIn('loaded_sfd', response)
            self.assertIn('firmware_id', response['loaded_sfd'])
            self.assertEqual(response['loaded_sfd']['firmware_id'], sfd2.get_firmware_id_ascii())
            self.assertIn('metadata', response['loaded_sfd'])
            self.assertEqual(response['loaded_sfd']['metadata'], sfd2.get_metadata())
            self.assertIn('device_comm_link', response)
            self.assertIn('link_type', response['device_comm_link'])
            self.assertEqual(response['device_comm_link']['link_type'], 'dummy')
            self.assertIn('link_config', response['device_comm_link'])
            self.assertEqual(response['device_comm_link']['link_config'], {})
            self.assertIn('device_info', response)
            self.assertIsNotNone(response['device_info'])
            device_info = self.fake_device_handler.get_device_info()
            for attr in device_info.get_attributes():
                if attr not in device_info_exlude_propeties:    # Exclude list
                    self.assertIn(attr, response['device_info'])
                    self.assertEqual(getattr(device_info, attr), response['device_info'][attr])

            # Redo the test, but with no SFD loaded. We should get None
            self.sfd_handler.reset_active_sfd()
            self.sfd_handler.process()
            self.fake_device_handler.set_connection_status(DeviceHandler.ConnectionStatus.CONNECTED_READY)

            req = {
                'cmd': 'get_server_status'
            }

            self.send_request(req, 0)
            response = self.wait_and_load_response(timeout=0.5)
            self.assert_no_error(response)

            self.assertEqual(response['cmd'], 'inform_server_status')
            self.assertIn('device_status', response)
            self.assertEqual(response['device_status'], 'connected_ready')
            self.assertIn('loaded_sfd', response)
            self.assertIsNone(response['loaded_sfd'])

            self.assertIn('device_comm_link', response)
            self.assertIn('link_type', response['device_comm_link'])
            self.assertEqual(response['device_comm_link']['link_type'], 'dummy')
            self.assertIn('link_config', response['device_comm_link'])
            self.assertEqual(response['device_comm_link']['link_config'], {})
            self.assertIn('device_info', response)
            device_info = self.fake_device_handler.get_device_info()
            for attr in device_info.get_attributes():
                if attr not in device_info_exlude_propeties:
                    self.assertIn(attr, response['device_info'])
                    self.assertEqual(getattr(device_info, attr), response['device_info'][attr])

            SFDStorage.uninstall(sfd1.get_firmware_id_ascii())
            SFDStorage.uninstall(sfd2.get_firmware_id_ascii())

    def test_set_device_link(self):
        self.assertEqual(self.fake_device_handler.link_type, 'none')
        self.assertEqual(self.fake_device_handler.link_config, {})

        # Switch the device link for real
        req = {
            'cmd': 'set_link_config',
            'link_type': 'dummy',
            'link_config': {
                'channel_id': 10
            }
        }
        self.send_request(req, 0)
        response = self.wait_and_load_response(timeout=0.5)
        self.assert_no_error(response)
        self.assertEqual(self.fake_device_handler.link_type, 'dummy')
        self.assertEqual(self.fake_device_handler.link_config, {'channel_id': 10})

        # Simulate that the device handler refused the configuration. Make sure we return a proper error
        req = {
            'cmd': 'set_link_config',
            'link_type': 'potato',
            'link_config': {
                'mium': 'mium'
            }
        }
        self.fake_device_handler.reject_link_config = True   # Emulate a bad config
        self.send_request(req, 0)
        response = self.wait_and_load_response(timeout=0.5)
        self.assert_is_error(response)
        self.assertNotEqual(self.fake_device_handler.link_type, 'potato')
        self.assertNotEqual(self.fake_device_handler.link_config, {'mium': 'mium'})
        self.fake_device_handler.reject_link_config = False

        # Missing link_config
        req = {
            'cmd': 'set_link_config',
            'link_type': 'potato'
        }
        self.send_request(req, 0)
        response = self.wait_and_load_response(timeout=0.5)
        self.assert_is_error(response)

        # Missing link_type
        req = {
            'cmd': 'set_link_config',
            'link_config': {}
        }
        self.send_request(req, 0)
        response = self.wait_and_load_response(timeout=0.5)
        self.assert_is_error(response)

        # Missing 2 fields
        req = {
            'cmd': 'set_link_config'
        }
        self.send_request(req, 0)
        response = self.wait_and_load_response(timeout=0.5)
        self.assert_is_error(response)

    def test_write_watchable(self):
        entries = self.make_dummy_entries(10, entry_type=EntryType.Var, prefix='var')
        self.datastore.add_entries(entries)

        subscribed_entry1 = entries[2]
        subscribed_entry2 = entries[5]
        req = {
            'cmd': 'subscribe_watchable',
            'watchables': [subscribed_entry1.get_id(), subscribed_entry2.get_id()]
        }

        self.send_request(req, 0)
        response = self.wait_and_load_response()
        self.assert_no_error(response)

        req = {
            'cmd': 'write_value',
            'updates': [
                {
                    'watchable': subscribed_entry1.get_id(),
                    'value': 1234
                },
                {
                    'watchable': subscribed_entry2.get_id(),
                    'value': 3.1415926
                }
            ]
        }

        self.assertFalse(subscribed_entry1.has_pending_target_update())
        self.assertFalse(subscribed_entry2.has_pending_target_update())

        self.send_request(req, 0)
        response = self.wait_and_load_response()
        self.assert_no_error(response)

        self.assertIn(response['cmd'], 'response_write_value')
        self.assertIn('watchables', response)
        self.assertEqual(len(response['watchables']), 2)
        self.assertIn(subscribed_entry1.get_id(), response['watchables'])
        self.assertIn(subscribed_entry2.get_id(), response['watchables'])

        self.assertTrue(subscribed_entry1.has_pending_target_update())
        self.assertEqual(subscribed_entry1.pop_target_update_request().get_value(), 1234)
        self.assertTrue(subscribed_entry2.has_pending_target_update())
        self.assertEqual(subscribed_entry2.pop_target_update_request().get_value(), 3.1415926)

    def test_subscribe_watchable_bad_ID(self):
        req = {
            'cmd': 'subscribe_watchable',
            'reqid': 123,
            'watchables': ['qwerty']
        }

        self.send_request(req, 0)
        response = self.wait_and_load_response()
        self.assert_is_error(response)
        self.assertEqual(response['reqid'], 123)

    def test_write_watchable_bad_ID(self):
        req = {
            'cmd': 'write_value',
            'reqid': 555,
            'updates': [
                {
                    'watchable': 'qwerty',
                    'value': 1234
                }
            ]
        }

        self.send_request(req, 0)
        response = self.wait_and_load_response()
        self.assert_is_error(response)
        self.assertEqual(response['reqid'], 555)

    def test_write_watchable_not_subscribed(self):
        entries = self.make_dummy_entries(1, entry_type=EntryType.Var, prefix='var')
        self.datastore.add_entries(entries)

        req = {
            'cmd': 'write_value',
            'reqid': 555,
            'updates': [
                {
                    'watchable': entries[0].get_id(),
                    'value': 1234
                }
            ]
        }

        self.send_request(req, 0)
        response = self.wait_and_load_response()
        self.assert_is_error(response)
        self.assertEqual(response['reqid'], 555)

    def test_notified_on_successful_write(self):
        entries = self.make_dummy_entries(10, entry_type=EntryType.Var, prefix='var')
        self.datastore.add_entries(entries)

        subscribed_entry1 = entries[2]
        subscribed_entry2 = entries[5]
        req = {
            'cmd': 'subscribe_watchable',
            'watchables': [subscribed_entry1.get_id(), subscribed_entry2.get_id()]
        }

        self.send_request(req, 0)
        response = self.wait_and_load_response()
        self.assert_no_error(response)

        entry1_update_request = subscribed_entry1.update_target_value(1234, self.api.entry_target_update_callback)
        entry1_update_request.complete(success=True)

        entry2_update_request = subscribed_entry2.update_target_value(4567, self.api.entry_target_update_callback)
        entry2_update_request.complete(success=False)

        for i in range(2):
            response = self.wait_and_load_response()
            self.assert_no_error(response, 'i=%d' % i)

            self.assertEqual(response['cmd'], 'inform_write_completion', 'i=%d' % i)
            self.assertIn('watchable', response, 'i=%d' % i)
            self.assertIn('status', response, 'i=%d' % i)
            self.assertIn('timestamp', response, 'i=%d' % i)

            if response['watchable'] == subscribed_entry1.get_id():
                self.assertEqual(response['status'], 'ok', 'i=%d' % i)
                self.assertEqual(response['timestamp'], entry1_update_request.get_completion_timestamp(), 'i=%d' % i)
            elif response['watchable'] == subscribed_entry2.get_id():
                self.assertEqual(response['status'], 'failed', 'i=%d' % i)
                self.assertEqual(response['timestamp'], entry2_update_request.get_completion_timestamp(), 'i=%d' % i)

    def test_write_watchable_bad_values(self):
        varf32 = Variable('dummyf32', vartype=EmbeddedDataType.float32, path_segments=[
                          'a', 'b', 'c'], location=0x12345678, endianness=Endianness.Little)
        vars32 = Variable('dummys32', vartype=EmbeddedDataType.sint32, path_segments=[
                          'a', 'b', 'c'], location=0x12345678, endianness=Endianness.Little)
        varu32 = Variable('dummyu32', vartype=EmbeddedDataType.uint32, path_segments=[
                          'a', 'b', 'c'], location=0x12345678, endianness=Endianness.Little)
        varbool = Variable('dummybool', vartype=EmbeddedDataType.boolean, path_segments=[
                           'a', 'b', 'c'], location=0x12345678, endianness=Endianness.Little)

        entryf32 = DatastoreVariableEntry(varf32.name, variable_def=varf32)
        entrys32 = DatastoreVariableEntry(vars32.name, variable_def=vars32)
        entryu32 = DatastoreVariableEntry(varu32.name, variable_def=varu32)
        entrybool = DatastoreVariableEntry(varbool.name, variable_def=varbool)

        alias_f32 = Alias("alias_f32", target="xxx", target_type=EntryType.Var, gain=2.0, offset=-10, min=-100, max=100)
        alias_u32 = Alias("alias_u32", target="xxx", target_type=EntryType.Var, gain=2.0,
                          offset=-10, min=-100, max=100)  # Notice the min that can go oob
        alias_s32 = Alias("alias_s32", target="xxx", target_type=EntryType.Var, gain=2.0, offset=-10, min=-100, max=100)
        entry_alias_f32 = DatastoreAliasEntry(alias_f32, entryf32)
        entry_alias_u32 = DatastoreAliasEntry(alias_u32, entryu32)
        entry_alias_s32 = DatastoreAliasEntry(alias_s32, entrys32)

        entries: List[DatastoreEntry] = [entryf32, entrys32, entryu32, entrybool, entry_alias_f32, entry_alias_u32, entry_alias_s32]
        self.datastore.add_entries(entries)

        req = {
            'cmd': 'subscribe_watchable',
            'watchables': [entry.get_id() for entry in entries]
        }

        self.send_request(req, 0)
        response = self.wait_and_load_response()
        self.assert_no_error(response)

        class TestCaseDef(TypedDict, total=False):
            inval: any
            outval: any
            valid: bool

        testcases: List[TestCaseDef] = [
            dict(inval=math.nan, valid=False),
            dict(inval=None, valid=False),
            dict(inval="asdasd", valid=False),
            dict(inval=int(123), valid=True, outval=int(123)),
            dict(inval="1234", valid=True, outval=1234),
            dict(inval="-2000.2", valid=True, outval=-2000.2),
            dict(inval="0x100", valid=True, outval=256),
            dict(inval="-0x100", valid=True, outval=-256),
            dict(inval=-1234.2, valid=True, outval=-1234.2),
            dict(inval=True, valid=True, outval=True),
            dict(inval="true", valid=True, outval=True),

        ]

        reqid = 0
        # The job of the API is to parse the request. Not interpret the data.
        # So we want the data to reach the datastore entry, but without conversion.
        # Value conversion and validation is done by the memory writer.

        for entry in entries:
            for testcase in testcases:
                reqid += 1
                req = {
                    'cmd': 'write_value',
                    'reqid': reqid,
                    'updates': [
                        {
                            'watchable': entry.get_id(),
                            'value': testcase['inval']
                        }
                    ]
                }

                self.send_request(req)
                response = self.wait_and_load_response()
                error_msg = "Reqid = %d. Entry=%s.  Testcase=%s" % (reqid, entry.get_display_path(), testcase)
                if not testcase['valid']:
                    self.assert_is_error(response, error_msg)
                    self.assertFalse(entry.has_pending_target_update())
                else:
                    self.assert_no_error(response, error_msg)
                    self.assertTrue(entry.has_pending_target_update())
                    self.assertEqual(entry.pop_target_update_request().get_value(), testcase['outval'], error_msg)
                    self.assertFalse(entry.has_pending_target_update())

                    if isinstance(entry, DatastoreAliasEntry):
                        self.assertTrue(entry.refentry.has_pending_target_update())
                        entry.refentry.pop_target_update_request()
                        self.assertFalse(entry.refentry.has_pending_target_update())
# region Datalogging

# REQUEST_ACQUISITION

    def test_get_datalogging_capabilities(self):
        # Check that we can read the datalogging capabilities
        req: api_typing.C2S.GetDataloggingCapabilities = {
            'cmd': 'get_datalogging_capabilities'
        }
        datalogging_device_setup = device_datalogging.DataloggingSetup(
            buffer_size=256,
            encoding=device_datalogging.Encoding.RAW,
            max_signal_count=32
        )

        self.fake_datalogging_manager.set_device_setup(datalogging_device_setup)

        self.send_request(req)
        response = cast(api_typing.S2C.GetDataloggingCapabilities, self.wait_and_load_response())
        self.assert_no_error(response)
        self.assertEqual(response['cmd'], 'get_datalogging_capabilities_response')

        self.assertTrue(response['available'])
        self.assertIsNotNone(response['capabilities'])
        capabilities = response['capabilities']
        self.assertEqual(capabilities['buffer_size'], 256)
        self.assertEqual(capabilities['encoding'], 'raw')
        self.assertEqual(capabilities['max_nb_signal'], 32)

        self.assertEqual(len(capabilities['sampling_rates']), 3)
        self.assertEqual(capabilities['sampling_rates'][0]['identifier'], 0)
        self.assertEqual(capabilities['sampling_rates'][0]['name'], 'Fixed Freq 1KHz')
        self.assertEqual(capabilities['sampling_rates'][0]['frequency'], 1000)
        self.assertEqual(capabilities['sampling_rates'][0]['type'], 'fixed_freq')

        self.assertEqual(capabilities['sampling_rates'][1]['identifier'], 1)
        self.assertEqual(capabilities['sampling_rates'][1]['name'], 'Fixed Freq 10KHz')
        self.assertEqual(capabilities['sampling_rates'][1]['frequency'], 10000)
        self.assertEqual(capabilities['sampling_rates'][1]['type'], 'fixed_freq')

        self.assertEqual(capabilities['sampling_rates'][2]['identifier'], 2)
        self.assertEqual(capabilities['sampling_rates'][2]['name'], 'Variable Freq')
        self.assertEqual(capabilities['sampling_rates'][2]['frequency'], None)
        self.assertEqual(capabilities['sampling_rates'][2]['type'], 'variable_freq')

        self.fake_datalogging_manager.set_device_setup(None)

        self.send_request(req)
        response = cast(api_typing.S2C.GetDataloggingCapabilities, self.wait_and_load_response())
        self.assert_no_error(response)
        self.assertFalse(response['available'])
        self.assertIsNone(response['capabilities'])

    def test_list_datalogging_acquisition(self):
        # Check that we can read the list of acquisition in datalogging storage
        with SFDStorage.use_temp_folder():
            sfd1 = SFDStorage.install(get_artifact('test_sfd_1.sfd'), ignore_exist=True)
            sfd2 = SFDStorage.install(get_artifact('test_sfd_2.sfd'), ignore_exist=True)

            with DataloggingStorage.use_temp_storage():
                acq1 = api_datalogging.DataloggingAcquisition(firmware_id=sfd1.get_firmware_id_ascii(),
                                                              reference_id="refid1", timestamp=123, name="foo")
                acq2 = api_datalogging.DataloggingAcquisition(firmware_id=sfd1.get_firmware_id_ascii(),
                                                              reference_id="refid2", timestamp=456, name="bar")
                acq3 = api_datalogging.DataloggingAcquisition(firmware_id=sfd2.get_firmware_id_ascii(),
                                                              reference_id="refid3", timestamp=789, name="baz")
                acq4 = api_datalogging.DataloggingAcquisition(firmware_id="unknown_sfd", reference_id="refid4", timestamp=555, name="meow")
                acq1.set_xaxis(api_datalogging.DataSeries())
                acq2.set_xaxis(api_datalogging.DataSeries())
                acq3.set_xaxis(api_datalogging.DataSeries())
                acq4.set_xaxis(api_datalogging.DataSeries())

                DataloggingStorage.save(acq1)
                DataloggingStorage.save(acq2)
                DataloggingStorage.save(acq3)
                DataloggingStorage.save(acq4)

                req: api_typing.C2S.ListDataloggingAcquisitions = {
                    'cmd': 'list_datalogging_acquisitions',
                }

                self.send_request(req)
                response = cast(api_typing.S2C.ListDataloggingAcquisition, self.wait_and_load_response())
                self.assert_no_error(response)

                self.assertEqual(len(response['acquisitions']), 4)
                self.assertEqual(response['acquisitions'][0]['firmware_id'], sfd1.get_firmware_id_ascii())
                self.assertEqual(response['acquisitions'][0]['reference_id'], 'refid1')
                self.assertEqual(response['acquisitions'][0]['timestamp'], 123)
                self.assertEqual(response['acquisitions'][0]['name'], 'foo')
                self.assertEqual(response['acquisitions'][0]['firmware_metadata'], sfd1.get_metadata())

                self.assertEqual(response['acquisitions'][1]['firmware_id'], sfd1.get_firmware_id_ascii())
                self.assertEqual(response['acquisitions'][1]['reference_id'], 'refid2')
                self.assertEqual(response['acquisitions'][1]['timestamp'], 456)
                self.assertEqual(response['acquisitions'][1]['name'], 'bar')
                self.assertEqual(response['acquisitions'][1]['firmware_metadata'], sfd1.get_metadata())

                self.assertEqual(response['acquisitions'][2]['firmware_id'], sfd2.get_firmware_id_ascii())
                self.assertEqual(response['acquisitions'][2]['reference_id'], 'refid3')
                self.assertEqual(response['acquisitions'][2]['timestamp'], 789)
                self.assertEqual(response['acquisitions'][2]['name'], 'baz')
                self.assertEqual(response['acquisitions'][2]['firmware_metadata'], sfd2.get_metadata())

                self.assertEqual(response['acquisitions'][3]['firmware_id'], "unknown_sfd")
                self.assertEqual(response['acquisitions'][3]['reference_id'], 'refid4')
                self.assertEqual(response['acquisitions'][3]['timestamp'], 555)
                self.assertEqual(response['acquisitions'][3]['name'], 'meow')
                self.assertEqual(response['acquisitions'][3]['firmware_metadata'], None)

                req: api_typing.C2S.ListDataloggingAcquisitions = {
                    'cmd': 'list_datalogging_acquisitions',
                    'firmware_id': sfd1.get_firmware_id_ascii()
                }

                self.send_request(req)
                response = cast(api_typing.S2C.ListDataloggingAcquisition, self.wait_and_load_response())
                self.assert_no_error(response)

                self.assertIn('acquisitions', response)
                self.assertEqual(len(response['acquisitions']), 2)
                self.assertEqual(response['acquisitions'][0]['firmware_id'], sfd1.get_firmware_id_ascii())
                self.assertEqual(response['acquisitions'][0]['reference_id'], 'refid1')
                self.assertEqual(response['acquisitions'][0]['timestamp'], 123)
                self.assertEqual(response['acquisitions'][0]['name'], 'foo')
                self.assertEqual(response['acquisitions'][0]['firmware_metadata'], sfd1.get_metadata())

                self.assertEqual(response['acquisitions'][1]['firmware_id'], sfd1.get_firmware_id_ascii())
                self.assertEqual(response['acquisitions'][1]['reference_id'], 'refid2')
                self.assertEqual(response['acquisitions'][1]['timestamp'], 456)
                self.assertEqual(response['acquisitions'][1]['name'], 'bar')
                self.assertEqual(response['acquisitions'][1]['firmware_metadata'], sfd1.get_metadata())

                req: api_typing.C2S.ListDataloggingAcquisitions = {
                    'cmd': 'list_datalogging_acquisitions',
                    'firmware_id': None
                }

                self.send_request(req)
                response = cast(api_typing.S2C.ListDataloggingAcquisition, self.wait_and_load_response())
                self.assert_no_error(response)
                self.assertEqual(len(response['acquisitions']), 4)

                req: api_typing.C2S.ListDataloggingAcquisitions = {
                    'cmd': 'list_datalogging_acquisitions',
                    'firmware_id': 'inexistant_id'
                }

                self.send_request(req)
                response = cast(api_typing.S2C.ListDataloggingAcquisition, self.wait_and_load_response())
                self.assert_no_error(response)
                self.assertEqual(len(response['acquisitions']), 0)

    def test_update_datalogging_acquisition(self):
        # Rename an acquisition in datalogging storage through API
        with DataloggingStorage.use_temp_storage():
            acq1 = api_datalogging.DataloggingAcquisition(firmware_id='some_firmware_id', reference_id="refid1", timestamp=123, name="foo")
            acq1.set_xaxis(api_datalogging.DataSeries())
            acq2 = api_datalogging.DataloggingAcquisition(firmware_id='some_firmware_id', reference_id="refid2", timestamp=456, name="bar")
            acq2.set_xaxis(api_datalogging.DataSeries())
            acq3 = api_datalogging.DataloggingAcquisition(firmware_id='some_firmware_id', reference_id="refid3", timestamp=789, name="baz")
            acq3.set_xaxis(api_datalogging.DataSeries())
            DataloggingStorage.save(acq1)
            DataloggingStorage.save(acq2)
            DataloggingStorage.save(acq3)

            req: api_typing.C2S.UpdateDataloggingAcquisition = {
                'cmd': 'update_datalogging_acquisition',
                'reference_id': 'refid2',
                'name': 'new_name!'
            }

            self.send_request(req)
            response = cast(api_typing.S2C.UpdateDataloggingAcquisition, self.wait_and_load_response())
            self.assert_no_error(response)
            acq2_reloaded = DataloggingStorage.read('refid2')
            self.assertEqual(acq2_reloaded.name, 'new_name!')
            self.assertEqual(acq2_reloaded.firmware_id, acq2.firmware_id)
            self.assertEqual(acq2_reloaded.timestamp, acq2.timestamp)

            req: api_typing.C2S.UpdateDataloggingAcquisition = {
                'cmd': 'update_datalogging_acquisition',
                'reference_id': 'bad_ref_id',
                'name': 'meow'
            }

            self.send_request(req)
            self.assert_is_error(self.wait_and_load_response())

    def test_delete_datalogging_acquisition(self):
        # Rename an acquisition in datalogging storage through API
        with DataloggingStorage.use_temp_storage():
            acq1 = api_datalogging.DataloggingAcquisition(firmware_id='some_firmware_id', reference_id="refid1", timestamp=123, name="foo")
            acq1.set_xaxis(api_datalogging.DataSeries())
            acq2 = api_datalogging.DataloggingAcquisition(firmware_id='some_firmware_id', reference_id="refid2", timestamp=456, name="bar")
            acq2.set_xaxis(api_datalogging.DataSeries())
            acq3 = api_datalogging.DataloggingAcquisition(firmware_id='some_firmware_id', reference_id="refid3", timestamp=789, name="baz")
            acq3.set_xaxis(api_datalogging.DataSeries())
            DataloggingStorage.save(acq1)
            DataloggingStorage.save(acq2)
            DataloggingStorage.save(acq3)

            self.assertEqual(DataloggingStorage.count(), 3)
            req: api_typing.C2S.DeleteDataloggingAcquisition = {
                'cmd': 'delete_datalogging_acquisition',
                'reference_id': 'refid2',
            }

            self.send_request(req)
            response = cast(api_typing.S2C.DeleteDataloggingAcquisition, self.wait_and_load_response())
            self.assert_no_error(response)
            self.assertEqual(DataloggingStorage.count(), 2)
            with self.assertRaises(LookupError):
                DataloggingStorage.read('refid2')

            DataloggingStorage.read('refid1')
            DataloggingStorage.read('refid3')

            req: api_typing.C2S.DeleteDataloggingAcquisition = {
                'cmd': 'delete_datalogging_acquisition',
                'reference_id': 'bad_ref_id'
            }

            self.send_request(req)
            self.assert_is_error(self.wait_and_load_response())

    def test_read_datalogging_acquisition_data(self):
        with DataloggingStorage.use_temp_storage():
            acq = api_datalogging.DataloggingAcquisition(firmware_id='some_firmware_id', reference_id="refid1", timestamp=123, name="foo")
            acq.set_xaxis(api_datalogging.DataSeries([0, 1, 2, 3, 4, 5, 6, 7, 8, 9], name='the x-axis', logged_element='/var/xaxis'))
            acq.add_data(api_datalogging.DataSeries([10, 20, 30, 40, 50, 60, 70, 80, 90], name='serie 1', logged_element='/var/data1'))
            acq.add_data(api_datalogging.DataSeries([100, 200, 300, 400, 500, 600, 700, 800, 900], name='serie 2', logged_element='/var/data2'))
            DataloggingStorage.save(acq)

            req: api_typing.C2S.ReadDataloggingAcquisition = {
                'cmd': 'read_datalogging_acquisition_data',
                'reference_id': 'refid1'
            }

            self.send_request(req)
            response = cast(api_typing.S2C.ReadDataloggingAcquisitionData, self.wait_and_load_response())
            self.assert_no_error(response)

            self.assertEqual(len(response['acquisition']['signals']), 2)

            self.assertEqual(response['acquisition']['xaxis']['name'], 'the x-axis')
            self.assertEqual(response['acquisition']['xaxis']['data'], [0, 1, 2, 3, 4, 5, 6, 7, 8, 9])
            self.assertEqual(response['acquisition']['xaxis']['logged_element'], '/var/xaxis')

            self.assertEqual(response['acquisition']['signals'][0]['name'], 'serie 1')
            self.assertEqual(response['acquisition']['signals'][0]['data'], [10, 20, 30, 40, 50, 60, 70, 80, 90])
            self.assertEqual(response['acquisition']['signals'][0]['logged_element'], '/var/data1')

            self.assertEqual(response['acquisition']['signals'][1]['name'], 'serie 2')
            self.assertEqual(response['acquisition']['signals'][1]['data'], [100, 200, 300, 400, 500, 600, 700, 800, 900])
            self.assertEqual(response['acquisition']['signals'][1]['logged_element'], '/var/data2')

            req: api_typing.C2S.ReadDataloggingAcquisition = {
                'cmd': 'read_datalogging_acquisition_data',
                'reference_id': 'bad_id'
            }

            self.send_request(req)
            response = cast(api_typing.S2C.ReadDataloggingAcquisitionData, self.wait_and_load_response())
            self.assert_is_error(response)

    def send_request_datalogging_acquisition_and_fetch_result(self, req: api_typing.C2S.RequestDataloggingAcquisition) -> api_datalogging.AcquisitionRequest:
        self.send_request(req)
        response = cast(api_typing.S2C.RequestDataloggingAcquisition, self.wait_and_load_response())
        self.assert_no_error(response)

        self.assertFalse(self.fake_datalogging_manager.request_queue.empty())
        ar = self.fake_datalogging_manager.request_queue.get()
        self.assertTrue(self.fake_datalogging_manager.request_queue.empty())

        return ar

    def test_request_datalogging_acquisition(self):
        with DataloggingStorage.use_temp_storage():

            var_entries: List[DatastoreVariableEntry] = self.make_dummy_entries(5, entry_type=EntryType.Var, prefix='var')
            rpv_entries: List[DatastoreRPVEntry] = self.make_dummy_entries(5, entry_type=EntryType.RuntimePublishedValue, prefix='rpv')
            alias_entries_var: List[DatastoreAliasEntry] = self.make_dummy_entries(
                2, entry_type=EntryType.Alias, prefix='alias', alias_bucket=var_entries)
            alias_entries_rpv: List[DatastoreAliasEntry] = self.make_dummy_entries(
                3, entry_type=EntryType.Alias, prefix='alias', alias_bucket=rpv_entries)

            # Add entries in the datastore that we will reread through the API
            self.datastore.add_entries(var_entries)
            self.datastore.add_entries(rpv_entries)
            self.datastore.add_entries(alias_entries_var)
            self.datastore.add_entries(alias_entries_rpv)

            def create_default_request() -> api_typing.C2S.RequestDataloggingAcquisition:
                req: api_typing.C2S.RequestDataloggingAcquisition = {
                    'cmd': 'request_datalogging_acquisition',
                    'decimation': 100,
                    'probe_location': 0.7,
                    'sampling_rate_id': 1,
                    'timeout': 100.1,
                    'trigger_hold_time': 0.1,
                    'x_axis_type': 'ideal_time',
                    'condition': 'eq',
                    'operands': [
                        dict(type='literal', value=123),
                        dict(type='watchable', value=var_entries[0].get_id())
                    ],
                    'signals': [
                        dict(id=var_entries[1].get_id(), name='var1'),
                        dict(id=alias_entries_var[0].get_id(), name='alias_var_1'),
                        dict(id=alias_entries_rpv[0].get_id(), name='alias_rpv_1'),
                        dict(id=rpv_entries[0].get_id(), name='rpv0'),
                    ]
                }
                return req

            req = create_default_request()
            ar = self.send_request_datalogging_acquisition_and_fetch_result(req)

            self.assertEqual(ar.decimation, 100)
            self.assertEqual(ar.probe_location, 0.7)
            self.assertEqual(ar.rate_identifier, 1)
            self.assertEqual(ar.timeout, 100.1)
            self.assertEqual(ar.trigger_hold_time, 0.1)
            self.assertEqual(ar.x_axis_type, api_datalogging.XAxisType.IdealTime)
            self.assertIsNone(ar.x_axis_signal)
            self.assertEqual(ar.trigger_condition.condition_id, api_datalogging.TriggerConditionID.Equal)

            self.assertEqual(ar.trigger_condition.operands[0].type, api_datalogging.TriggerConditionOperandType.LITERAL)
            self.assertEqual(ar.trigger_condition.operands[0].value, 123)
            self.assertEqual(ar.trigger_condition.operands[1].type, api_datalogging.TriggerConditionOperandType.WATCHABLE)
            self.assertIs(ar.trigger_condition.operands[1].value, var_entries[0])

            self.assertIs(ar.signals[0].entry, var_entries[1])
            self.assertIs(ar.signals[1].entry, alias_entries_var[0])
            self.assertIs(ar.signals[2].entry, alias_entries_rpv[0])
            self.assertIs(ar.signals[3].entry, rpv_entries[0])

            self.assertEqual(ar.signals[0].name, 'var1')
            self.assertEqual(ar.signals[1].name, 'alias_var_1')
            self.assertEqual(ar.signals[2].name, 'alias_rpv_1')
            self.assertEqual(ar.signals[3].name, 'rpv0')

            # conditions
            all_conditions = {
                'true': dict(condition_id=api_datalogging.TriggerConditionID.AlwaysTrue, nb_operands=0),
                'eq': dict(condition_id=api_datalogging.TriggerConditionID.Equal, nb_operands=2),
                'lt': dict(condition_id=api_datalogging.TriggerConditionID.LessThan, nb_operands=2),
                'let': dict(condition_id=api_datalogging.TriggerConditionID.LessOrEqualThan, nb_operands=2),
                'gt': dict(condition_id=api_datalogging.TriggerConditionID.GreaterThan, nb_operands=2),
                'get': dict(condition_id=api_datalogging.TriggerConditionID.GreaterOrEqualThan, nb_operands=2),
                'cmt': dict(condition_id=api_datalogging.TriggerConditionID.ChangeMoreThan, nb_operands=2),
                'within': dict(condition_id=api_datalogging.TriggerConditionID.IsWithin, nb_operands=3)
            }

            for api_cond in all_conditions:
                for nb_operand in range(all_conditions[api_cond]['nb_operands'] + 1):
                    req = create_default_request()
                    req['condition'] = api_cond
                    req['operands'] = []
                    for i in range(nb_operand):
                        req['operands'].append(dict(type='literal', value=i))
                    if nb_operand == all_conditions[api_cond]['nb_operands']:
                        ar = self.send_request_datalogging_acquisition_and_fetch_result(req)
                        self.assertEqual(ar.trigger_condition.condition_id, all_conditions[api_cond]['condition_id'])
                    else:
                        self.send_request(req)
                        self.assert_is_error(self.wait_and_load_response())

            req = create_default_request()
            req['condition'] = 'meow'
            self.send_request(req)
            self.assert_is_error(self.wait_and_load_response())

            # x axis
            # measured time ok
            req = create_default_request()
            req['x_axis_type'] = 'measured_time'
            ar = self.send_request_datalogging_acquisition_and_fetch_result(req)
            self.assertEqual(ar.x_axis_type, api_datalogging.XAxisType.MeasuredTime)
            self.assertIsNone(ar.x_axis_signal)

            # watchable ok
            req = create_default_request()
            req['x_axis_type'] = 'signal'
            req['x_axis_signal'] = dict(name="hello", id=rpv_entries[1].get_id())
            ar = self.send_request_datalogging_acquisition_and_fetch_result(req)
            self.assertEqual(ar.x_axis_type, api_datalogging.XAxisType.Signal)
            self.assertIs(ar.x_axis_signal.entry, rpv_entries[1])
            self.assertEqual(ar.x_axis_signal.name, 'hello')

            # watchable no name is ok
            req = create_default_request()
            req['x_axis_type'] = 'signal'
            req['x_axis_signal'] = dict(id=rpv_entries[1].get_id())
            ar = self.send_request_datalogging_acquisition_and_fetch_result(req)
            self.assertEqual(ar.x_axis_type, api_datalogging.XAxisType.Signal)
            self.assertIs(ar.x_axis_signal.entry, rpv_entries[1])
            self.assertEqual(ar.x_axis_signal.name, None)

            # watchable bad format
            req = create_default_request()
            req['x_axis_type'] = 'signal'
            req['x_axis_signal'] = "bad format"
            self.send_request(req)
            self.assert_is_error(self.wait_and_load_response())

            # watchable unknown ID
            req = create_default_request()
            req['x_axis_type'] = 'signal'
            req['x_axis_signal'] = dict(watchable='unknown_id')
            self.send_request(req)
            self.assert_is_error(self.wait_and_load_response())

            # watchable is missing
            req = create_default_request()
            req['x_axis_type'] = 'signal'
            self.send_request(req)
            self.assert_is_error(self.wait_and_load_response())

            # unknown type
            req = create_default_request()
            req['x_axis_type'] = 'meow'
            self.send_request(req)
            self.assert_is_error(self.wait_and_load_response())

            # unknown watchable
            req = create_default_request()
            req['signals'][0]['id'] = 'unknown_id'
            self.send_request(req)
            self.assert_is_error(self.wait_and_load_response())

            # Bad decimation
            for bad_decimation in ['meow', -1, 0, 1.5, None, [1]]:
                req = create_default_request()
                req['decimation'] = bad_decimation
                self.send_request(req)
                self.assert_is_error(self.wait_and_load_response())

            # Bad decimation
            for bad_hold_time in ['meow', -1, None, [1]]:
                req = create_default_request()
                req['trigger_hold_time'] = bad_hold_time
                self.send_request(req)
                self.assert_is_error(self.wait_and_load_response())

            # Bad Timeout
            for bad_timeout in ['meow', -1, None, [1]]:
                req = create_default_request()
                req['timeout'] = bad_timeout
                self.send_request(req)
                self.assert_is_error(self.wait_and_load_response())

            # Bad Timeout
            for bad_probe_location in ['meow', -1, 1.1, 2, [1]]:
                req = create_default_request()
                req['probe_location'] = bad_probe_location
                self.send_request(req)
                self.assert_is_error(self.wait_and_load_response())

            # Bad Timeout
            for bad_freq_id in ['meow', -1, 1.1, [1]]:
                req = create_default_request()
                req['sampling_rate_id'] = bad_freq_id
                self.send_request(req)
                self.assert_is_error(self.wait_and_load_response())

                # Bad Timeout
            for bad_rate_id in ['meow', -1, 11, [1]]:   # Fake datalogging manager consider all sample rate id > 10 to be bad.
                req = create_default_request()
                req['sampling_rate_id'] = bad_rate_id
                self.send_request(req)
                self.assert_is_error(self.wait_and_load_response())

            for bad_watchable_format in ['meow', -1, 11, [1]]:   # Fake datalogging manager consider all sample rate id > 10 to be bad.
                req = create_default_request()
                req['signals'][0] = bad_watchable_format
                self.send_request(req)
                self.assert_is_error(self.wait_and_load_response())

    
# endregion
if __name__ == '__main__':
    import unittest
    unittest.main()
