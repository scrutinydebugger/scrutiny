#    manual_test_device_info_dialog.py
#        Create an environment to manually test the DeviceInfoDialog window
#
#   - License : MIT - See LICENSE file.
#   - Project :  Scrutiny Debugger (github.com/scrutinydebugger/scrutiny-python)
#
#   Copyright (c) 2021 Scrutiny Debugger

if __name__ != '__main__' : 
    raise RuntimeError("This script is expected to run from the command line")

import sys, os
os.environ['SCRUTINY_MANUAL_TEST'] = '1'
project_root = os.path.realpath(os.path.join(os.path.dirname(__file__), '..', '..', '..'))
sys.path.insert(0, project_root)

import logging
from PySide6.QtWidgets import QApplication, QMainWindow, QPushButton, QWidget, QVBoxLayout
from scrutiny.gui.dialogs.device_info_dialog import DeviceInfoDialog
from scrutiny.gui import assets
from scrutiny.sdk import *
from dataclasses import dataclass

from typing import Optional, List
logging.basicConfig(level=logging.DEBUG)

@dataclass
class Config:
    add_ro_mem:bool = False
    add_forbidden_mem:bool = False
    add_datalogging:bool = False
    add_sampling_rates:bool = False

def make_device_info(config:Config) -> DeviceInfo:
    
    ro_mem:List[MemoryRegion] = []
    forbidden_mem:List[MemoryRegion] = []
    datalogging:Optional[List[DataloggingCapabilities]] = None
    sampling_rates:List[SamplingRate] = []
    supported_features = SupportedFeatureMap(
        datalogging=config.add_datalogging,
        memory_write=True,
        user_command=False,
        sixtyfour_bits=True
    )

    if config.add_ro_mem:
        ro_mem = [MemoryRegion(start = 0x10000000, size= 0x100), MemoryRegion(start = 0x20000000, size= 0x100)]
    
    if config.add_forbidden_mem:
        forbidden_mem = [MemoryRegion(start = 0x30000000, size= 0x100), MemoryRegion(start = 0x40000000, size= 0x100)]
    
    if config.add_sampling_rates:
        sampling_rates = [FixedFreqSamplingRate(0, "Loop1", 10000.0), VariableFreqSamplingRate(1, "Loop2")]

    if config.add_datalogging:
        datalogging = DataloggingCapabilities(
            buffer_size=4096,
            encoding=DataloggingEncoding.RAW,
            max_nb_signal=32,
            sampling_rates= sampling_rates
        )

    return DeviceInfo(
        device_id="The device ID",
        display_name="The display",
        address_size_bits=32,
        heartbeat_timeout=5,
        max_bitrate_bps=100000,
        max_rx_data_size=128,
        max_tx_data_size=256,
        protocol_major=1,
        protocol_minor=0,
        rx_timeout_us=50000,
        supported_features=supported_features,
        datalogging_capabilities=datalogging,
        readonly_memory_regions=ro_mem,
        forbidden_memory_regions=forbidden_mem
    )

dialogs = []
app = QApplication([])
app.setStyleSheet(assets.load_text(["stylesheets", "scrutiny_base.qss"]))

window = QMainWindow()
central_widget = QWidget()
btn_everything = QPushButton("With everything")
btn_nothing = QPushButton("Nothing")
btn_no_sampling_rate = QPushButton("No smapling rates")

window.setCentralWidget(central_widget)
layout = QVBoxLayout(central_widget)
layout.addWidget(btn_everything)
layout.addWidget(btn_nothing)
layout.addWidget(btn_no_sampling_rate)

def show_window(config):
    for dialog in dialogs:
        dialog.close()
    dialog = DeviceInfoDialog(window, make_device_info(config))
    dialogs.append(dialog)
    dialog.show()


def everything_button():
    config = Config(
        add_datalogging=True,
        add_forbidden_mem=True,
        add_ro_mem=True,
        add_sampling_rates=True
    )
    show_window(config)

def nothing_button():
    config = Config(
        add_datalogging=False,
        add_forbidden_mem=False,
        add_ro_mem=False,
        add_sampling_rates=False
    )
    show_window(config)

def no_sampling_rate_button():
    config = Config(
        add_datalogging=True,
        add_forbidden_mem=True,
        add_ro_mem=True,
        add_sampling_rates=False
    )
    show_window(config)

    

btn_everything.clicked.connect(everything_button)
btn_nothing.clicked.connect(nothing_button)
btn_no_sampling_rate.clicked.connect(no_sampling_rate_button)
window.show()

sys.exit(app.exec())
