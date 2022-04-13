import appdirs
import os
from scrutiny.core.firmware_info_file import FirmwareInfoFile
import logging

class SFIStorage():

    STORAGE_FOLDER = 'sfi_sotrage'

    @classmethod
    def get_storage_dir(cls):
        folder = appdirs.user_data_dir(cls.STORAGE_FOLDER, 'scrutiny')
        os.makedirs(folder, exist_ok=True)
        return folder

    @classmethod
    def install(cls, filename):
        if not os.path.isfile(filename):
            raise ValueError('File "%s" does not exist' % (filename))

        sfi = FirmwareInfoFile(filename)
        output_file = os.path.join(SFIStorage.get_storage_dir(), sfi.get_firmware_id(ascii=True))
        
        if os.path.isfile(output_file):
            logging.warning('A Scrutiny Firmware Information  file with the same firmware ID was already installed. Overwriting.')

        sfi.write(output_file)  # Write the Firmware Information file in storage folder with firmware ID as name

    @classmethod
    def uninstall(cls, firmwareid):
        target_file = os.path.join(SFIStorage.get_storage_dir(), firmwareid)
        
        if not  os.path.isfile(target_file):
            raise ValueError('SFI file with firmware ID %s not found' % (firmwareid))

        os.remove(target_file)

    @classmethod
    def is_installed(cls, firmwareid):
        storage = cls.get_storage_dir()
        filename = os.path.join(storage, firmwareid)
        return os.path.isfile(filename)

    @classmethod
    def get(cls, firmwareid):
        storage = cls.get_storage_dir()
        filename = os.path.join(storage, firmwareid)
        if not os.path.isfile(filename):
            raise Exception('Scrutiny Firmware Info with firmware ID %s not installed on this system' % (firmwareid))

        return FirmwareInfoFile(filename)