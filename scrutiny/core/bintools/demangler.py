#    demangler.py
#        Converts mangled linkage names to readable symbols names
#
#   - License : MIT - See LICENSE file.
#   - Project : Scrutiny Debugger (github.com/scrutinydebugger/scrutiny)
#
#   Copyright (c) 2021-2022 scrutinydebugger

import subprocess
import shutil
import abc

class BaseDemangler(abc.ABC):
    def can_run(self)->bool:
        return False
    
    def get_error(self)->str:
        return "virtual class"
    
    def demangle(self, mangler:str)->str:
        raise NotImplementedError('Trying to demangle with base class')

class GccDemangler(BaseDemangler):

    _default_binary_name:str = "c++filt"

    binary_name:str
    error_details:str

    def __init__(self, binary_name=_default_binary_name):
        if binary_name is None:
            binary_name = self._default_binary_name
        self.binary_name = binary_name
        self.error_details=""
    
    def can_run(self)->bool:
        can_run = True
        if shutil.which(self.binary_name) is None:
            can_run = False
            self.error_details = 'Demangler binary "%s" is not in the path' % self.binary_name
        
        return can_run
    
    def get_error(self)->str:
        return self.error_details

    def demangle(self, mangled:str)->str:
        if not self.can_run():
            raise Exception('Cannot run demangler. %s' % self.get_error())

        self.process = subprocess.Popen([self.binary_name, '--format', 'gnu-v3', '-n'], stdout=subprocess.PIPE, stdin=subprocess.PIPE, universal_newlines=True)
        return self.process.communicate(input=mangled)[0]
