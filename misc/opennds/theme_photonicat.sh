#!/bin/sh
#Copyright (C) The openNDS Contributors 2004-2022
#Copyright (C) BlueWave Projects and Services 2015-2024
#This software is released under the GNU GPL license.
#
# Warning - shebang sh is for compatibliity with busybox ash (eg on OpenWrt)
# This is changed to bash automatically by Makefile for generic Linux
#

# Title of this theme:
title="theme_photonicat"

# functions:

generate_splash_sequence() {
	click_to_continue
}

header() {
# Define a common header html for every page served.
# Photonicat-branded, fully self-contained (inline CSS) so it renders before
# the client has any internet access and needs no external assets.
	gatewayurl=$(printf "${gatewayurl//%/\\x}")
	echo "<!DOCTYPE html>
		<html>
		<head>
		<meta http-equiv=\"Cache-Control\" content=\"no-cache, no-store, must-revalidate\">
		<meta http-equiv=\"Pragma\" content=\"no-cache\">
		<meta http-equiv=\"Expires\" content=\"0\">
		<meta charset=\"utf-8\">
		<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">
		<title>$gatewayname</title>
		<style>
			:root{--pcatblue:#008AD3;--pcatgray:#00274D;--pcatyellow:#FDE021;}
			*{box-sizing:border-box;}
			body{margin:0;font-family:-apple-system,'Segoe UI',Roboto,'Titillium Web',Helvetica,Arial,sans-serif;
				background:#f3f4f6;color:var(--pcatgray);line-height:1.5;}
			.offset{min-height:100vh;display:flex;align-items:center;justify-content:center;padding:20px;}
			.insert{background:#fff;max-width:420px;width:100%;border-radius:14px;
				box-shadow:0 10px 30px rgba(0,39,77,.12);overflow:hidden;}
			.brandbar{background:var(--pcatgray);padding:22px 24px;text-align:center;}
			.brandbar .wordmark{color:#fff;font-weight:800;font-size:24px;letter-spacing:.5px;}
			.brandbar .wordmark b{color:var(--pcatyellow);}
			.body{padding:24px;}
			h1{font-size:20px;margin:0 0 6px;color:var(--pcatgray);}
			h2{font-size:15px;margin:18px 0 4px;color:var(--pcatgray);}
			p{font-size:14px;color:#374151;margin:10px 0;}
			ol{font-size:13px;color:#4b5563;padding-left:20px;}
			ol li{margin:4px 0;}
			.agree{background:#f8fafc;border:1px solid #e5e7eb;border-radius:10px;
				padding:12px 14px;font-size:13px;color:#4b5563;margin:14px 0;}
			.badge{display:inline-block;width:52px;height:52px;line-height:52px;border-radius:50%;
				text-align:center;font-size:26px;color:#fff;margin:2px 0 10px;}
			.badge-ok{background:#16A34A;}
			.badge-err{background:#DC2626;}
			.btn{display:block;width:100%;border:0;cursor:pointer;border-radius:10px;
				padding:13px 16px;font-size:16px;font-weight:700;color:#fff;background:var(--pcatblue);
				transition:background .2s;text-align:center;text-decoration:none;}
			.btn:hover{background:#0072ad;}
			.btn-secondary{background:transparent;color:var(--pcatblue);font-weight:600;font-size:13px;
				padding:8px;text-decoration:underline;}
			.btn-secondary:hover{background:transparent;color:#0072ad;}
			form{margin:10px 0 0;}
			a.btn{margin:10px 0 0;}
			hr{border:0;border-top:1px solid #eef0f3;margin:16px 0;}
			.foot{padding:14px 24px;font-size:11px;color:#9ca3af;text-align:center;}
			big-red{display:block;font-size:20px;font-weight:700;color:var(--pcatgray);}
			med-blue{display:block;font-size:14px;color:var(--pcatblue);margin:6px 0;}
			italic-black{display:block;font-style:normal;font-size:14px;color:#374151;}
		</style>
		</head>
		<body>
		<div class=\"offset\">
		<div class=\"insert\">
		<div class=\"brandbar\"><span class=\"wordmark\">photoni<b>cat</b></span></div>
		<div class=\"body\">
	"
}

footer() {
	# Define a common footer html for every page served
	year=$(date +'%Y')
	echo "
		</div>
		<div class=\"foot\">&copy; $year Photonicat</div>
		</div>
		</div>
		</body>
		</html>
	"

	exit 0
}



click_to_continue() {
	# Single-tap click to continue: tapping "Agree & Connect" authenticates the
	# client immediately (no intermediate thankyou/landing round-trips - those
	# extra steps were where stale tokens stranded users on error pages).

	if [ "$continue" = "clicked" ]; then
		authenticate_client
		footer
	fi

	continue_form
	footer
}

continue_form() {
	# Photonicat click-to-continue with a simple user agreement.
	# Note: deliberately no $client_zone here - its interface lookup can return
	# garbage (eg "br-lanbr-lan") when a client has stale neighbor entries.

	echo "
		<h1>Welcome to Wi-Fi</h1>
		<p>You are connected to <b>$gatewayname</b>.</p>
		<div class=\"agree\">
			By tapping <b>Agree &amp; Connect</b> you accept the Terms of Service and
			Privacy Policy, and agree to use this network lawfully and responsibly.
		</div>
		<form action=\"/opennds_preauth/\" method=\"get\">
			<input type=\"hidden\" name=\"fas\" value=\"$fas\">
			<input type=\"hidden\" name=\"continue\" value=\"clicked\">
			<input class=\"btn\" type=\"submit\" value=\"Agree &amp; Connect\" >
		</form>
	"

	read_terms
	footer
}

authenticate_client() {
	# Authenticate the client with the openNDS daemon and serve a branded
	# success or failure page. Both outcomes end the splash sequence here:
	# no further taps are required to get online.

	originurl=$(printf "${originurl//%/\\x}")

	# Retry target for the failure page. It must be a FOREIGN host: openNDS
	# only answers pre-auth requests to foreign hosts with a fresh splash
	# redirect - requests addressed to the gateway itself get the bare 511
	# status page, a dead end in captive-portal mini-browsers. The client's
	# original URL is ideal; fall back to a public IP (no DNS needed).
	case "$originurl" in
		*status.client*|*"$gatewayaddress"*|"") retryurl="http://1.1.1.1/" ;;
		http*) retryurl="$originurl" ;;
		*) retryurl="http://1.1.1.1/" ;;
	esac

	configure_log_location
	. $mountpoint/ndscids/ndsinfo

	# authenticate and write to the log - returns with $ndsstatus set
	auth_log

	if [ "$ndsstatus" = "authenticated" ]; then
		# Many captive portal browsers close automatically as soon as the OS
		# detects internet access, so this page may only be seen briefly.
		echo "
			<div style=\"text-align:center;\"><span class=\"badge badge-ok\">&#10003;</span></div>
			<h1 style=\"text-align:center;\">You're online</h1>
			<p style=\"text-align:center;\">Thanks for accepting. You now have internet access -
				this window can be closed.</p>
		"

		# Offer the client's originally requested page when we have one
		case "$originurl" in
			http*) echo "<a class=\"btn\" href=\"$originurl\">Continue</a>" ;;
		esac
	else
		# Stale token (cached splash page, portal restarted, or session timed
		# out). Send the client to the gateway root by IP: it always answers
		# with a fresh splash redirect, needs no DNS and works in captive
		# portal mini-browsers.
		echo "
			<div style=\"text-align:center;\"><span class=\"badge badge-err\">&#33;</span></div>
			<h1 style=\"text-align:center;\">Connection didn't complete</h1>
			<p style=\"text-align:center;\">This page has expired.
				Tap the button below to reload it, then accept again.</p>
			<a class=\"btn\" href=\"$retryurl\">Try again</a>
		"
	fi
}

landing_page() {
	# Kept for compatibility: older cached "thank you" pages submit landing=yes.
	# Behaves exactly like the one-tap accept.
	gatewayurl=$(printf "${gatewayurl//%/\\x}")

	authenticate_client
	footer
}

read_terms() {
	#terms of service button
	echo "
		<form action=\"/opennds_preauth/\" method=\"get\">
			<input type=\"hidden\" name=\"fas\" value=\"$fas\">
			<input type=\"hidden\" name=\"terms\" value=\"yes\">
			<input class=\"btn-secondary\" type=\"submit\" value=\"Read Terms of Service\" >
		</form>
	"
}

display_terms() {
	# Terms of service and privacy statement, Photonicat-styled.
	####
	# WARNING #
	# It is your responsibility to ensure these "Terms of Service" are compliant with the REGULATIONS and LAWS of your Country or State.
	# In most locations, a Privacy Statement is an essential part of the Terms of Service.
	####

	echo "
		<h1>Terms of Service</h1>
		<p>Use of this Wi-Fi hotspot is provided in return for your full
			acceptance of these Terms of Service.</p>

		<h2>Privacy</h2>
		<p>To operate, this system stores the network parameters of your device
			(such as its IP and MAC address) and basic session records. This
			information is used only to provide the service, is stored securely
			and is not shared with third parties.</p>

		<h2>Proper use</h2>
		<p>Access is granted on the basis that you will not misuse or abuse it.
			You agree not to:</p>
		<ol>
			<li>degrade the service for other users with excessive traffic;</li>
			<li>attempt to break security or access unauthorized areas of the service;</li>
			<li>collect information about other users of the service;</li>
			<li>spoof another IP or MAC address, or resell any part of the service;</li>
			<li>send SPAM, or transmit viruses or other malicious software;</li>
			<li>use the service for any unlawful, harassing or fraudulent purpose.</li>
		</ol>
		<p>You are responsible for securing your own device and data while using
			the service. We will assign your device an IP address each time it
			connects, and that address may change.</p>

		<h2>Disclaimer and liability</h2>
		<p>The owners of this hotspot do not control and are not responsible for
			content accessed through the service, and disclaim all warranties to
			the fullest extent permitted by law, including merchantability and
			fitness for a particular purpose. Under no circumstances shall the
			owners be liable to any user or third party on account of that
			party's use or misuse of the service.</p>

		<h2>Changes and termination</h2>
		<p>We may modify or terminate the service and these terms at any time,
			for any reason, without notice. We reserve the right to terminate
			your use of the service at any time.</p>

		<h2>Indemnity</h2>
		<p>You agree to hold harmless and indemnify the owners of this hotspot
			from and against any third-party claim arising from or related to
			your use of the service.</p>

		<hr>
		<form>
			<input class=\"btn\" type=\"button\" value=\"Back\" onClick=\"history.go(-1);return true;\">
		</form>
	"
	footer
}

#### end of functions ####


#################################################
#						#
#  Start - Main entry point for this Theme	#
#						#
#  Parameters set here overide those		#
#  set in libopennds.sh			#
#						#
#################################################

# Quotas and Data Rates
#########################################
# Set length of session in minutes (eg 24 hours is 1440 minutes - if set to 0 then defaults to global sessiontimeout value):
# eg for 100 mins:
# session_length="100"
#
# eg for 20 hours:
# session_length=$((20*60))
#
# eg for 20 hours and 30 minutes:
# session_length=$((20*60+30))
session_length="0"

# Set Rate and Quota values for the client
# The session length, rate and quota values could be determined by this script, on a per client basis.
# rates are in kb/s, quotas are in kB. - if set to 0 then defaults to global value).
upload_rate="0"
download_rate="0"
upload_quota="0"
download_quota="0"

quotas="$session_length $upload_rate $download_rate $upload_quota $download_quota"

# Define the list of Parameters we expect to be sent sent from openNDS ($ndsparamlist):
# Note you can add custom parameters to the config file and to read them you must also add them here.
# Custom parameters are "Portal" information and are the same for all clients eg "admin_email" and "location"
ndscustomparams=""
ndscustomimages=""
ndscustomfiles=""

ndsparamlist="$ndsparamlist $ndscustomparams $ndscustomimages $ndscustomfiles"

# The list of FAS Variables used in the Login Dialogue generated by this script is $fasvarlist and defined in libopennds.sh
#
# Additional custom FAS variables defined in this theme should be added to $fasvarlist here.
additionalthemevars=""

fasvarlist="$fasvarlist $additionalthemevars"

# You can choose to define a custom string. This will be b64 encoded and sent to openNDS.
# There it will be made available to be displayed in the output of ndsctl json as well as being sent
#	to the BinAuth post authentication processing script if enabled.
# Set the variable $binauth_custom to the desired value.
# Values set here can be overridden by the themespec file

#binauth_custom="This is sample text sent from \"$title\" to \"BinAuth\" for post authentication processing."

# Encode and activate the custom string
#encode_custom

# Set the user info string for logs (this can contain any useful information)
userinfo="$title"

# Customise the Logfile location. Note: the default uses the tmpfs "temporary" directory to prevent flash wear.
# Override the defaults to a custom location eg a mounted USB stick.
#mountpoint="/mylogdrivemountpoint"
#logdir="$mountpoint/ndslog/"
#logname="ndslog.log"
