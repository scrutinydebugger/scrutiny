#    sfd_add_alias.py
#        Defines the sfd-add-alias command used to embed an alias file into an SFD file in
#        the making
#
#   - License : MIT - See LICENSE file.
#   - Project :  Scrutiny Debugger (github.com/scrutinydebugger/scrutiny-python)
#
#   Copyright (c) 2021-2022 Scrutiny Debugger

import argparse
from .base_command import BaseCommand
from typing import Optional, List
import logging
import os
import json
import math

class SFDAddAlias(BaseCommand):
    _cmd_name_ = 'sfd-add-alias'
    _brief_ = 'Append an alias to a SFD work folder. Definition can be passed with a file or command line arguments'
    _group_ = 'Build Toochain'

    args: List[str]
    parser: argparse.ArgumentParser

    def __init__(self, args: List[str], requested_log_level: Optional[str] = None):
        self.args = args
        self.parser = argparse.ArgumentParser(prog=self.get_prog())
        self.parser.add_argument('folder', help='Folder containing the firmware description files.')
        self.parser.add_argument('--file', help='The input alias file in .json format')
        

        self.parser.add_argument('--fullpath', help='The alias fullpath')
        self.parser.add_argument('--target', help='The target of the alias')
        self.parser.add_argument('--gain',  help='The gain to apply when reading the alias')
        self.parser.add_argument('--offset',  help='The offset to apply when reading the alias')
        self.parser.add_argument('--min', help='The mimimum value for this alias')
        self.parser.add_argument('--max', help='The maximum value for this alias')

    def run(self) -> Optional[int]:
        from scrutiny.core.firmware_description import FirmwareDescription, AliasDefinition

        args = self.parser.parse_args(self.args)

        if args.fullpath is not None and args.file is not None:
            raise Exception('Alias must be defined by a file (--file) or command line parameters (--fullpath + others), but not both.')
        
        varmap = FirmwareDescription.read_varmap_from_filesystem(args.folder)
        target_alias_file = os.path.join(args.folder, FirmwareDescription.alias_file)
        
        all_alliases = {}
        if os.path.isfile(target_alias_file):
            with open(target_alias_file, 'rb') as f:
                all_alliases = FirmwareDescription.read_aliases(f)

            
        if args.file is not None:
            with open(args.file, 'rb') as f:
                new_aliases = FirmwareDescription.read_aliases(f)
        elif args.fullpath is not None:
            if args.target is None:
                raise Exception('No target specified')

            d = {
                'fullpath' : args.fullpath,
                'target' : args.target,
            }

            if args.gain:
                d['gain'] = args.gain

            if args.offset:
                d['offset'] = args.offset

            if args.min:
                d['min'] = args.min  # Should handle '-inf'

            if args.max:
                d['max'] = args.max

            alias = AliasDefinition.from_dict(d['fullpath'], d)
            new_aliases = {}
            new_aliases[alias.get_fullpath()] = alias
        else:
            raise Exception('Alias must be defined through a file or command line by specifying the --target option.')
        
        for k in new_aliases:
            alias = new_aliases[k]
            assert k == alias.get_fullpath()

            try:
                alias.validate()
            except Exception as e:
                logging.error('Alias %s refers is invalid. %s' % (alias.get_fullpath(), str(e)))
                continue                

            try:
                varmap.get_var(alias.get_target())
            except:
                logging.error('Alias %s refers to non-existent variable %s' % (alias.get_fullpath(), alias.get_target()))
                continue

            if k in all_alliases:
                logging.error('Duplicate alias with path %s' % k)
                continue
            
            all_alliases[alias.get_fullpath()] = alias 

        all_alias_dict = {}
        for k in all_alliases:
            all_alias_dict[k] = all_alliases[k].to_dict()

        with open(target_alias_file, 'wb') as f:
            f.write(json.dumps(all_alias_dict).encode('utf8'))

        return 0
