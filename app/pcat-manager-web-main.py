#!/usr/bin/env python3

#@Author    : qichunren

import os
import sys
import pathlib
import getopt
import time
from pcat_util import pcat_logger

# import pyprctl # patch:set real thread name
import pcat_config
from pcat_manager_web import FlaskAppThread
from pc_socket_client import PcSocketClient
from pc_modem_client import PcModemClient
from pc_sms_client import PcSmsClient
from pc_screen_client import PcScreenClient
from pcat_timer_manager import PcatTimerManager
import app

if __name__ == "__main__":
    current_working_path = pathlib.Path(__file__).parent.resolve()
    os.chdir(str(current_working_path))
    
    argv = sys.argv[1:]

    try:
        opts, args = getopt.getopt(argv, "hvp:", ["port=", "version", "help", "skip-auth", "skip-pin"])
    except getopt.GetoptError as error_msg:
        print ('pcat-manager-web-main.py got commandline arg error:', error_msg)
        sys.exit(2)
    for opt, arg in opts:
        if opt == "-v" or opt == "--version":
            print(pcat_config.version)
            sys.exit(0)
        elif opt == "-h" or opt == "--help":
            help_doc="""
device web interface tool, v{version}
Listen on http://0.0.0.0:{port} 
            """.strip()
            print(help_doc.format(version = pcat_config.version, port = pcat_config.web_port))
            sys.exit(0)
        elif opt == "-p" or opt == "--port":
            print("Config web port to", arg)
            pcat_config.web_port = int(arg)
        elif opt == "--skip-auth":
            print("skip-auth flag enabled.")
            pcat_config.skip_auth = True
        elif opt == "--skip-pin":
            print("skip-pin flag enabled.")
            pcat_config.skip_pin = True

    socket_client_obj = PcSocketClient()
    app.socket_client = socket_client_obj
    socket_client_obj.start()

    # Register battery snapshot callback for power event logging
    import power_event_log
    def _battery_snapshot():
        v = socket_client_obj.battery_voltage
        c = socket_client_obj.battery_current
        soc = socket_client_obj.charge_percent
        return {
            "v": round(v / 1000.0, 2) if v and v > 100 else v,
            "mA": round(c, 0) if c is not None else None,
            "soc": soc,
        }
    power_event_log.set_battery_snapshot_fn(_battery_snapshot)
    
    # Initialize timer manager
    timer_manager_obj = PcatTimerManager()
    timer_manager_obj.set_socket_client(socket_client_obj)
    app.timer_manager = timer_manager_obj
    timer_manager_obj.start()
    
    
    # Apply saved network mode configuration at startup
    saved_network_mode = pcat_config.get_network_mode()
    pcat_logger.logger.info(f"Applying saved network mode: {saved_network_mode}")
    app.config_ethernet_wwan(saved_network_mode)

    # Query initial PMU I/O status
    socket_client_obj.pmu_io_get()
    socket_client_obj.charge_threshold_get()
    
    flask_web_thread = FlaskAppThread()
    app.flask_app = flask_web_thread
    flask_web_thread.start()

    modem_client = PcModemClient(pcat_config.modem_dev, pcat_config.modem_db)
    app.modem_client = modem_client
    modem_client.start()

    screen_client = PcScreenClient()
    app.screen_client = screen_client
    screen_client.start()

    main_app = app.App()
    app.main_app = main_app
    main_app.start()

    sms_client = PcSmsClient(pcat_config.modem_dev, modem_client, pcat_config.modem_db)
    app.sms_client = sms_client
    sms_client.start()
