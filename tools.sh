#!/usr/bin/env bash

## meshcore-tools/tools.sh v1.03 (16th November 2025)
##  Helpful things for MeshCore command line faffery.

usage() {
	echo "Usage: $0 <option> [options]"
	echo
	echo "  cmd <args>   :  Forwards args into meshcli with our settings."
	echo "  process      :  Enters the processing loop for MQTT and bot actions."
	echo "  purge        :  Purges all contacts."
	echo "  reset        :  Resets device using details in the config file."
	echo "  time         :  Syncs time to node."
	echo
	exit 1
}

[ "$#" -lt 1 ] || [[ "$1" == "-h" ]] || [[ "$1" == "--help" ]] && usage

SCRIPT_DIR="$(dirname "${BASH_SOURCE[0]}")"

check() {
	local file="$1"
	if [ -f "$SCRIPT_DIR/$file" ]; then
		source "$SCRIPT_DIR/$file"
	else
		echo "Warning: $file not found. Copy from $file.example."
		exit 1
	fi
}

check "config.sh"
check "responses.sh"

source "$(dirname "${BASH_SOURCE[0]}")/config.sh"
source "$(dirname "${BASH_SOURCE[0]}")/responses.sh"

mc() {
    meshcli -s "$mc_serial" "$@"
}

loop() {

	timeset
	mc_channels=$(mc -j get_channels)

	while true; do

		sleep 3

		# Read whatever's next in the node's queue.
		mc_data=$(mc -j r)

		# Only operate on what we think is json.
		if [[ "$mc_data" == "{"* ]]; then

			delivered_time=$(date +%s)

			mc_user="null"
			mc_channel_idx=$(echo "$mc_data" | jq -r '.channel_idx')
			mc_channel_name=$(echo "$mc_channels" | jq -r --argjson idx "$mc_channel_idx" '.[] | select(.channel_idx == $idx) | .channel_name')
			mc_hops=$(echo "$mc_data" | jq -r '.path_len')
			mc_sent=$(echo "$mc_data" | jq -r '.sender_timestamp')
			mc_text=$(echo "$mc_data" | jq -r '.text')
			mc_type=$(echo "$mc_data" | jq -r '.type')

			journey_time=$((delivered_time - mc_sent))

			# Interpret hopping.
			case "$mc_hops" in

				"0" | "255")
					mc_hopped="direct"
					journey_hops="0"
				;;

				"1")
					mc_hopped="in 1 hop"
					journey_hops="1"
				;;

				*)
					mc_hopped="in $mc_hops hops"
					journey_hops="$mc_hops"
				;;

			esac

			# Figure out who messaged us.
			case "$mc_type" in

				# Channels are easy. Grab the username from the message text.
				"CHAN")

					mc_user="${mc_text%%:*}"
					read -r mc_user <<< "$mc_user"
					mc_text="${mc_text#*:}"
					read -r mc_text <<< "$mc_text"

				;;

				# Direct messages should have known contacts. We need to read these from the node.
				"PRIV")

					mc_pubkey_prefix=$(echo "$mc_data" | jq -r '.pubkey_prefix')
					mc_contacts=$(mc -j contacts)
					mc_user=$(echo "$mc_contacts" | jq -r --arg key "$mc_pubkey_prefix" 'to_entries[] | select(.value.public_key | startswith($key)) | .value.adv_name')
				
				;;

			esac

			# Enhance the data with our values.
			mc_data=$(echo "$mc_data" | jq \
				--argjson dt "$delivered_time" \
				--argjson jt "$journey_time" \
				--argjson jh "$journey_hops" \
				--arg chn "$mc_channel_name" \
				--arg usr "$mc_user" \
				--arg msg "$mc_text" \
				'. + {channel_name: $chn, user: $usr, msg: $msg, delivered_timestamp: $dt, journey_time: $jt, journey_hops: $jh}')

			# Use the content of responses.sh to formulate the replies.
			mc_reply=$(handle_responses "$mc_channel_idx" "$mc_text" "$mc_user" "$mc_name" "$mc_hopped")

			mc_response="0"
			[[ "$mc_transmit" == "y" ]] && [[ "$mc_reply" != "" ]] && mc_response="1"

			# Include reponse decision status.
			mc_data=$(echo "$mc_data" | jq \
				--argjson rs "$mc_response" \
				--arg rp "$mc_reply" \
				'. + {responded: $rs, reply: $rp}')

			# Output for system logs.
			echo "$mc_data" | jq

			## MQTT
			[[ "$mc_mqtt" == "y" ]] && mosquitto_pub -h $mqtt_broker -p 1883 -u $mqtt_user -P $mqtt_pass -t "meshcore/messages" -m "$mc_data"

			## Transmission

			if [[ "$mc_channel_idx" == "null" ]]; then

				# This responds to the direct messages.
				[[ "$mc_transmit" == "y" ]] && [[ "$mc_reply" != "" ]] && mc msg "$mc_user" "$mc_reply" &>/dev/null
				
			else

				# This responds to channel messages.
				[[ "$mc_transmit" == "y" ]] && [[ "$mc_reply" != "" ]] && mc chan "$mc_channel_idx" "$mc_reply" &>/dev/null

			fi

		else

			[[ -z "$mc_data" ]] || echo "{\"error\":\"This does not appear to be JSON formatted.\"}" | jq

		fi
			
	done

}


purge() {

	## This removes all contacts instantly, matching contact last updated (u) being more than 1 minute ago.
	## Yes, the inequality test does appear to point the wrong way. Or is it just me?
	mc at u\<1m remove_contact

	## This could be used to retain some contacts, but it's a slow process.
	# mc -j contacts | jq -r '.[] | .adv_name' | while IFS= read -r thisname; do
	# 	echo "Removing: $thisname"
	# 	mc remove_contact "$thisname" 1>/dev/null
	# done

}


reset() {

	timeset
	purge
	## This mouthful sets manual contact adding, radio settings, and enables basic telemetry requests (battery).
	mc set manual_add_contacts "$mc_contacts_manual" set radio "$mc_radio" set telemetry_mode_base 2
	[[ "$mc_name" != "" ]] && mc set "$mc_name"

}


timeset() {

	mc clock st clock

}


fit_reply() {

    local prefix="$1"
    local text="$2"
    local suffix="$3"
    local max_length="$4"
    
    local overhead=$((${#prefix} + ${#suffix}))
    local available=$((max_length - overhead))
    
    if [[ ${#text} -le $available ]]; then
        echo "${prefix}${text}${suffix}"
        return
    fi
    
    # Show start and end with ... in middle
    local keep=$((available / 2 - 2))
    local start="${text:0:$keep}"
    local end="${text: -$keep}"
    echo "${prefix}${start} ... ${end}${suffix}"

}


### Triggers
case "${1,,}" in

	"process")
		loop
	;;

	"cmd")
		shift
		mc "$@"
	;;

	"purge")
		purge
	;;

	"reset")
		reset
	;;

	"time")
		timeset
	;;

	*)
		echo "No action '${1}'."
		exit 1
	;;

esac

exit 0
