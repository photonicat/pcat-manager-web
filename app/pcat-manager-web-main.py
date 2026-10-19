#!/usr/bin/env python3

#@Author    : qichunren

import os
import sys
import pathlib
import getopt

# import pyprctl # patch:set real thread name
import pcat_config
import pcat_bootstrap

if __name__ == "__main__":
    current_working_path = pathlib.Path(__file__).parent.resolve()
    os.chdir(str(current_working_path))
    
    argv = sys.argv[1:]

    try:
        opts, args = getopt.getopt(argv, "hvp:", ["port=", "version", "help", "skip-auth", "skip-pin", "dev-mode", "dev-server-base="])
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
        elif opt == "--dev-mode":
            print("dev-mode flag enabled (dev/test endpoints reachable).")
            pcat_config.dev_mode = True
        elif opt == "--dev-server-base":
            print("dev-server-base set to", arg)
            pcat_config.dev_server_base = arg.rstrip("/")

    # The subsystems and the werkzeug listener both live in pcat_bootstrap now,
    # so that granian's entry point (pcat_wsgi) can bring up exactly the same
    # process without this script's argv handling. Running this file directly
    # remains the development path and is unchanged in behaviour.
    pcat_bootstrap.start_subsystems(serve_http=True)
