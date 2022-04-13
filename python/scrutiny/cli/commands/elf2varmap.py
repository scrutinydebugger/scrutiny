import argparse
import os
import logging

from .base_command import BaseCommand

from scrutiny.core.bintools.elf_dwarf_var_extractor import ElfDwarfVarExtractor

class Elf2VarMap(BaseCommand):
    _cmd_name_ = 'elf2varmap'
    _brief_ = 'Extract the variables definition from an ELF file through DWARF debugging symbols.'
    _group_ = 'Build Toochain'

    def __init__(self, args):
        self.args = args
        self.parser = argparse.ArgumentParser( prog = self.get_prog() )
        self.parser.add_argument('file',  help='The ELF file to read')
        self.parser.add_argument('--output', default=None, help='The varmap output file. Will go to STDOUT if not set')


    def run(self):
        args = self.parser.parse_args(self.args)
        extractor = ElfDwarfVarExtractor(args.file)
        varmap = extractor.get_varmap()

        if args.output is None:
            print(varmap.get_json())
        else:
            if os.path.isdir(args.output):
                output_file = os.path.join(args.output, 'varmap.json')
            else:
                output_file = args.output

            if os.path.isfile(output_file):
                logging.warning('File %s already exist. Overwritting' % output_file)    
            
            varmap.write(output_file)