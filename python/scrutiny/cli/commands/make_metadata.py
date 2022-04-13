import argparse
from .base_command import BaseCommand
import json
import os
import datetime 
import platform
import scrutiny

class MakeMetadata(BaseCommand):
    _cmd_name_ = 'make-metadata'
    _brief_ = 'Generate a .json file containing the metadatas used inside Firmware Info File'
    _group_ = 'Build Toochain'

    DEFAULT_NAME = 'metadata.json'

    def __init__(self, args):
        self.args = args
        self.parser = argparse.ArgumentParser(prog = self.get_prog() )

        self.parser.add_argument('--output', default=None, help='Output filename. If a directory is given, the file will defautly be name "%s" ' % self.DEFAULT_NAME)
        self.parser.add_argument('--project-name', default='', help='A project name to be displayed in the GUI when connecting to a device that match the Firmware Info File that includes this metadata')
        self.parser.add_argument('--author', default='', help='The author of the project. For display in the GUI only.')
        self.parser.add_argument('--version', default='', help='Version of the project, for display in the GUI only.')

    def run(self):
        args = self.parser.parse_args(self.args)
        
        if args.output is None:
            output_file = self.DEFAULT_NAME
        elif os.path.isdir(args.output):
            output_file = os.path.join(args.output, self.DEFAULT_NAME)
        else:
            output_file = args.output

        try:
            scrutiny_version = scrutiny.__version__
        except:
            scrutiny_version = '0.0.0'
        
        metadata = {
            'project-name' : args.project_name,
            'author' : args.author,
            'version' : args.version,
            'generation-info': {
                'time' : round(datetime.datetime.now().timestamp()),
                'python-version' : platform.python_version(),
                'scrutiny-version' : scrutiny_version,
                'system-type' : platform.system()
                } 
        }

        with open(output_file, 'w') as f:
            f.write(json.dumps(metadata, indent=4))


