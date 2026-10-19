#!/usr/bin/env python3

"""Process startup, shared by the two entry points.

This used to live inline under `if __name__ == "__main__"` in
pcat-manager-web-main.py, which was fine while that script *was* the
process. Under granian it is not: granian owns the process, and the worker
it forks imports its target module and calls that (mp.py:93 runs
`callback_loader()` inside the child). So the subsystems have to come up on
import of pcat_wsgi, not on execution of a __main__ block.

Rather than duplicate ~120 lines of startup in both places -- and let them
drift -- both call start_subsystems() here. The only difference between the
two paths is who listens on the HTTP port, which is what serve_http selects.
"""

import threading

from pcat_util import pcat_logger

import pcat_config
from pc_socket_client import PcSocketClient
from pc_modem_client import PcModemClient
from pc_sms_client import PcSmsClient
from pc_screen_client import PcScreenClient
from pcat_timer_manager import PcatTimerManager
import app


def start_subsystems(serve_http=True):
    """Bring up the daemon threads and hardware clients.

    serve_http=True starts FlaskAppThread, i.e. the in-process werkzeug
    listener -- the legacy path, still used when running this app directly
    for development.

    serve_http=False leaves the socket to granian, which is serving
    pcat_wsgi:flask_app in this same process. Everything else is identical:
    the modem, SMS, socket and timer clients all still live here, so there
    is exactly one owner of the AT and NMEA ttys either way.
    """
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

    # Everything below runs off the main thread. Each item shells out to uci /
    # ubus / netifd or waits on the daemon socket, and serially they delayed
    # the web service (and the modem and SMS threads) by anywhere from a few
    # seconds to a minute on a cold boot -- all of it while the device is still
    # bringing its upstream up. None of it is a prerequisite for serving pages,
    # so it is deferred and the startup path continues immediately.
    def _deferred_startup():
        # Retire the legacy fixed network modes: fold whatever this device has
        # stored onto the equivalent priority table before anything is enacted,
        # so the mode applied just below is already the migrated one and the
        # box comes up on the new logic (metric ladder + health keeper) on its
        # very first boot after the upgrade. One-shot -- the stored table is
        # the marker, so a user-edited table is never overwritten.
        #
        # The App thread runs wan_manager.startup() concurrently with this, so
        # the two orderings both have to converge: if that thread wins it sees
        # an unmigrated mode and skips its apply, and we migrate here; if we
        # win, config_ethernet_wwan() below is a no-op in priority mode and its
        # apply() re-runs idempotently.
        try:
            from pcat_util import wan_manager
            wan_manager.migrate_legacy_mode()
        except Exception as e:
            pcat_logger.logger.warning(f"Priority table migration failed: {e}")

        # Apply saved network mode configuration at startup
        saved_network_mode = pcat_config.get_network_mode()
        pcat_logger.logger.info(f"Applying saved network mode: {saved_network_mode}")
        try:
            app.config_ethernet_wwan(saved_network_mode, at_boot=True)
        except Exception as e:
            pcat_logger.logger.error(f"Failed to apply saved network mode: {e!r}")

        # Re-apply saved CPU powersave preference at startup. The big-core
        # online/offline state (and cpufreq governor) is volatile and reset by the
        # kernel on every boot, so the user's saved choice must be pushed back to
        # the hardware here or it would silently revert after each reboot.
        try:
            saved_cpu_powersave = pcat_config.apply_saved_cpu_scheduling()
            pcat_logger.logger.info(
                f"Applying saved CPU powersave: {saved_cpu_powersave}"
            )
        except Exception as e:
            pcat_logger.logger.error(f"Failed to apply saved CPU powersave: {e!r}")

        # An add-on PCIe Wi-Fi card comes out of a flash either missing from
        # /etc/config/wireless entirely (first boot: it had not enumerated when
        # the config was generated) or present but written with disabled='1'.
        # Either way its AP never goes on the air until someone finds the
        # Enable button. Reconcile it here, where the phy has certainly settled.
        try:
            from pcat_util import wifi_radio
            radio_result = wifi_radio.startup()
            if radio_result:
                pcat_logger.logger.info("WiFi radio startup: %s", radio_result)
        except Exception as e:
            pcat_logger.logger.error(f"WiFi radio startup failed: {e!r}")

        # LXC needs the unified cgroup hierarchy, and the image ships a v1 shim
        # that takes it away on every boot -- including the first boot after a
        # firmware upgrade, which is what silently un-fixes it. Install the
        # boot-time guard so a device with the LXC runtime comes up ready
        # without anyone visiting the Setup tab. No-op when already installed
        # or when the runtime is absent.
        try:
            from pcat_util import lxc_manager
            lxc_result = lxc_manager.startup()
            if lxc_result:
                pcat_logger.logger.info("LXC startup: %s", lxc_result)
        except Exception as e:
            pcat_logger.logger.error(f"LXC startup failed: {e!r}")

        # Query initial PMU I/O status
        try:
            socket_client_obj.pmu_io_get()
            socket_client_obj.charge_threshold_get()
            socket_client_obj.battery_info_get()
        except Exception as e:
            pcat_logger.logger.error(f"Failed to query initial PMU status: {e!r}")

    threading.Thread(target=_deferred_startup, name="deferred-startup",
                     daemon=True).start()

    if serve_http:
        from pcat_manager_web import FlaskAppThread
        flask_web_thread = FlaskAppThread()
        app.flask_app = flask_web_thread
        flask_web_thread.start()

    # TLS twin of the web service on :9443; the phone page's microphone
    # needs a secure context. No-op when the cert has not been generated.
    #
    # Left on werkzeug even under granian: it is a second listener inside this
    # one process, it carries only the phone page's traffic, and moving it
    # would mean a second granian instance with its own --ssl-* flags for no
    # measurable gain. It is also why granian must stay at one worker -- a
    # second worker would fork a second thread trying to bind :9443.
    from pcat_manager_web import TlsFlaskAppThread
    TlsFlaskAppThread().start()

    # Create the shared secret the pcat-call-audio daemon authenticates
    # against, before it starts. Minting is lazy otherwise, and the daemon
    # refuses to start without the file -- which on a fresh device would mean
    # no C audio path until someone happened to open the phone page.
    try:
        import pc_audio_token
        pc_audio_token.mint()
    except Exception as e:
        print("Audio token secret not created:", e)

    modem_client = PcModemClient(pcat_config.modem_dev, pcat_config.modem_db)
    app.modem_client = modem_client
    modem_client.start()

    # GNSS rides the modem's serial port rather than opening one of its own,
    # so the app has a single AT channel (one tty index to survive a USB
    # re-enumeration, one writer, no exclusive-open races).
    try:
        import pc_gps_client
        pc_gps_client.gps_client.set_at_sender(modem_client.at_command)
        # Binary-upload channel for AGPS (gpsOneXTRA) file injection.
        pc_gps_client.gps_client.set_at_payload_sender(
            modem_client.at_command_with_payload)
        # Serving-cell reads for the /gps map's tower locator.
        from pcat_util import cell_locate
        cell_locate.set_at_sender(modem_client.at_command)
    except Exception as e:
        print("GNSS AT channel not wired:", e)

    # PcScreenClient's run loop is a no-op (while True: sleep(1)); its methods
    # are all request-driven helpers, so we keep the instance but never start
    # the thread to save a needless 1Hz wakeup.
    screen_client = PcScreenClient()
    app.screen_client = screen_client

    main_app = app.App()
    app.main_app = main_app
    main_app.start()

    sms_client = PcSmsClient(pcat_config.modem_dev, modem_client, pcat_config.modem_db)
    app.sms_client = sms_client
    sms_client.start()
