import unittest
from scrutiny.core.firmware_description import AliasDefinition, FirmwareDescription
from scrutiny.core.variable import *
from test.artifacts import get_artifact
from binascii import unhexlify

from typing import Dict

class TestSFD(unittest.TestCase):

    def test_load_sfd(self):
        sfd = FirmwareDescription(get_artifact('test_sfd_1.sfd'))   # expects no exception
        sfd.validate()  # Expects no exception
    
    def test_check_content(self):
        sfd = FirmwareDescription(get_artifact('test_sfd_1.sfd'))
        self.assertEqual(sfd.get_firmware_id(), unhexlify('00000000000000000000000000000001'))
        self.assertEqual(sfd.get_firmware_id_ascii(), '00000000000000000000000000000001')
        
        vars = list(sfd.get_vars_for_datastore())
        var_as_dict:Dict[str, Variable]={}
        for pair in vars:
            display_path = pair[0]
            var = pair[1]
            assert display_path not in var_as_dict
            var_as_dict[display_path] = var

        self.assertIn("/path1/path2/some_int32", var_as_dict)
        self.assertIn("/path1/path2/some_uint32", var_as_dict)
        self.assertIn("/path1/path2/some_float32", var_as_dict)
        self.assertIn("/path1/path2/some_float64", var_as_dict)

        self.assertEqual(var_as_dict["/path1/path2/some_int32"].get_address(), 1000)
        self.assertEqual(var_as_dict["/path1/path2/some_int32"].get_type(), EmbeddedDataType.sint32)
        self.assertEqual(var_as_dict["/path1/path2/some_int32"].get_size(), 4)
        self.assertEqual(var_as_dict["/path1/path2/some_int32"].get_fullname(), "/path1/path2/some_int32")
        self.assertFalse(var_as_dict["/path1/path2/some_int32"].has_enum())
        self.assertIsNone(var_as_dict["/path1/path2/some_int32"].get_enum())

        self.assertEqual(var_as_dict["/path1/path2/some_uint32"].get_address(), 1004)
        self.assertEqual(var_as_dict["/path1/path2/some_uint32"].get_type(), EmbeddedDataType.uint32)
        self.assertEqual(var_as_dict["/path1/path2/some_uint32"].get_size(), 4)
        self.assertEqual(var_as_dict["/path1/path2/some_uint32"].get_fullname(), "/path1/path2/some_uint32")
        self.assertTrue(var_as_dict["/path1/path2/some_uint32"].has_enum())
        enum = var_as_dict["/path1/path2/some_uint32"].get_enum()
        self.assertEqual(enum.get_name(), 'EnumA')
        self.assertEqual(enum.get_val_name(0), 'eVal1')
        self.assertEqual(enum.get_val_name(1), 'eVal2')
        self.assertEqual(enum.get_val_name(100), 'eVal3')
        self.assertEqual(enum.get_val_name(101), 'eVal4')
        with self.assertRaises(Exception):
            enum.get_val_name(2)
        
        self.assertEqual(var_as_dict["/path1/path2/some_float32"].get_address(), 1008)
        self.assertEqual(var_as_dict["/path1/path2/some_float32"].get_type(), EmbeddedDataType.float32)
        self.assertEqual(var_as_dict["/path1/path2/some_float32"].get_size(), 4)
        self.assertEqual(var_as_dict["/path1/path2/some_float32"].get_fullname(), "/path1/path2/some_float32")
        self.assertFalse(var_as_dict["/path1/path2/some_float32"].has_enum())
        self.assertIsNone(var_as_dict["/path1/path2/some_float32"].get_enum())
        
        self.assertEqual(var_as_dict["/path1/path2/some_float64"].get_address(), 1012)
        self.assertEqual(var_as_dict["/path1/path2/some_float64"].get_type(), EmbeddedDataType.float64)
        self.assertEqual(var_as_dict["/path1/path2/some_float64"].get_size(), 8)
        self.assertEqual(var_as_dict["/path1/path2/some_float64"].get_fullname(), "/path1/path2/some_float64")
        self.assertFalse(var_as_dict["/path1/path2/some_float64"].has_enum())
        self.assertIsNone(var_as_dict["/path1/path2/some_float64"].get_enum())


        aliases_as_dict:Dict[str, AliasDefinition] = {}
        for fullpath, alias in sfd.get_aliases_for_datastore():
            aliases_as_dict[fullpath] = alias
        
        self.assertIn("/alias/some_float32", aliases_as_dict)
        self.assertIn("/alias/some_enum", aliases_as_dict)

        self.assertEqual(aliases_as_dict['/alias/some_float32'].get_fullpath(), '/alias/some_float32')
        self.assertEqual(aliases_as_dict['/alias/some_float32'].get_target(), "/path1/path2/some_float32")
        self.assertEqual(aliases_as_dict['/alias/some_float32'].get_gain(), 2.0)
        self.assertEqual(aliases_as_dict['/alias/some_float32'].get_offset(), 1.0)
        self.assertEqual(aliases_as_dict['/alias/some_float32'].get_min(), 0)
        self.assertEqual(aliases_as_dict['/alias/some_float32'].get_max(), 100)

        self.assertEqual(aliases_as_dict['/alias/some_enum'].get_fullpath(), '/alias/some_enum')
        self.assertEqual(aliases_as_dict['/alias/some_enum'].get_target(), "/path1/path2/some_uint32")
        self.assertEqual(aliases_as_dict['/alias/some_enum'].get_gain(), 1.0)
        self.assertEqual(aliases_as_dict['/alias/some_enum'].get_offset(), 0.0)
        self.assertEqual(aliases_as_dict['/alias/some_enum'].get_min(), float('-inf'))
        self.assertEqual(aliases_as_dict['/alias/some_enum'].get_max(), float('inf'))
