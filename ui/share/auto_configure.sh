#!/bin/bash -eu
#
# auto_configure.sh setup a Postgres database and temboard UI service.
#
# Run auto_configure.sh as root. You configure it like any libpq software. By
# default, the script access the running cluster on port 5432, using postgres
# UNIX and PostgreSQL user.

ETCDIR=${ETCDIR-/etc/temboard}
VARDIR=${VARDIR-/var/lib/temboard}
LOGDIR=${LOGDIR-/var/log/temboard}
LOGFILE=${LOGFILE-/var/log/temboard-auto-configure.log}
SYSUSER=${SYSUSER-temboard}


catchall() {
	local rc=$?
	trap - INT EXIT TERM
	set +x
	if [ $rc -gt 0 ] ; then
		fatal "Failure. See ${LOGFILE} for details."
	else
		rm -f "${LOGFILE}"
	fi
	exec 3>&-
}


fatal() {
	echo -e "\\e[1;31m$*\\e[0m" | tee -a /dev/fd/3 >&2
	exit 1
}


log() {
	echo "$@" | tee -a /dev/fd/3 >&2
}


setup_logging() {
	if [ -n "${DEBUG-}" ] ; then
		exec 3>/dev/null
	else
		exec 3>&2 2>"$LOGFILE" 1>&2
		chmod 0600 "$LOGFILE"
		trap 'catchall' INT EXIT TERM
	fi

	# Now, log everything.
	set -x
}


setup_ssl() {
	local pki;
	for d in /etc/pki/tls /etc/ssl /etc/temboard; do
		if [ -d $d ] ; then
			pki=$d
			break
		fi
	done
	if [ -z "${pki-}" ] ; then
		fatal "Failed to find PKI directory."
	fi

	if [ -f $pki/certs/ssl-cert-snakeoil.pem ] && [ -f $pki/private/ssl-cert-snakeoil.key ] ; then
		log "Using snake-oil SSL certificate."
		sslcert=$pki/certs/ssl-cert-snakeoil.pem
		sslkey=$pki/private/ssl-cert-snakeoil.key
	else
		sslcert=$pki/certs/temboard-auto.pem
		sslkey=$pki/private/temboard-auto.key
		if ! [ -f $sslcert ] ; then
			log "Generating self-signed certificate."
			openssl req -new -x509 -days 365 -nodes \
				-subj "/C=XX/ST= /L=Default/O=Default/OU= /CN= " \
				-out $sslcert -keyout $sslkey
		fi
		chmod 640 $sslkey
		chgrp "$SYSUSER" "$sslkey"
	fi
	readlink -e $sslcert $sslkey
}


generate_configuration() {
	local sslcert=$1; shift
	local sslkey=$1; shift
	local created cookie_secret version

	sudo -iu "$SYSUSER" test -r "$sslcert"
	sudo -iu "$SYSUSER" test -r "$sslkey"
	created="$(date)"
	cookie_secret="$(pwgen 128)"
	version="$(temboard --version | sed 's/^/# /')"

	cat <<-EOF
	#
	#   T E M B O A R D   U I   C O N F I G U R A T I O N
	#
	# Generated by ${BASH_SOURCE[0]} on $created.
	#
	$version
	#
	# See https://temboard.rtfd.io/ for details on configuration
	# possibilities.
	#

	[temboard]
	port = ${TEMBOARD_PORT-8888}
	ssl_cert_file = $sslcert
	ssl_key_file = $sslkey
	cookie_secret = $cookie_secret
	home = ${VARDIR}

	[repository]
	host = ${PGHOST-/var/run/postgresql}
	port = ${PGPORT-5432}
	user = temboard
	password = ${TEMBOARD_PASSWORD}
	dbname = ${TEMBOARD_DATABASE-temboard}

	[logging]
	method = stderr
	level = ${TEMBOARD_LOGGING_LEVEL-INFO}

	[monitoring]
	# purge_after = 730

	[statements]
	# purge_after = 7
	EOF
}

pwgen() {
	# Generates a random password of 32 hexadecimal characters.
	od -vN $((${1-32} / 2)) -An -tx1 /dev/urandom | tr -d ' \n'
}


#       M A I N

exec 0>&-  # Close stdin.

cd "$(readlink -m "${BASH_SOURCE[0]}/..")"

setup_logging

export TEMBOARD_PASSWORD=${TEMBOARD_PASSWORD-$(pwgen)}
if ! getent passwd "$SYSUSER" ; then
	log "Creating system user temBoard."
	useradd \
		--system --user-group --shell "$SHELL" \
		--home-dir "$VARDIR" \
		--comment "temBoard Web UI" "$SYSUSER" &>/dev/null
fi

if getent group ssl-cert &>/dev/null && ! getent group ssl-cert | grep -q "$SYSUSER"; then
	if command -v adduser &>/dev/null ; then
		adduser "$SYSUSER" ssl-cert
	else
		fatal "$SYSUSER is not member of ssl-cert group and can't execute adduser. Make sure to add user in group before executing."
	fi
fi


log "Configuring temboard in ${ETCDIR}."
mapfile -t sslfiles < <(set -eu; setup_ssl)
install -o "$SYSUSER" -g "$SYSUSER" -m 0750 -d "$ETCDIR" "$LOGDIR" "$VARDIR"
install -o "$SYSUSER" -g "$SYSUSER" -m 0640 /dev/null "$ETCDIR/temboard.conf"
generate_configuration "${sslfiles[@]}" > "$ETCDIR/temboard.conf"

log "Creating Postgres user, database and schema."
# For temboard migratedb
TEMBOARD_CONFIGFILE="$ETCDIR/temboard.conf" ./create_repository.sh

if [ "$(whoami)" != "$SYSUSER" ] ; then
	# Run as temboard UNIX user. Wipe environment, this requires properly
	# temboard.conf.
	run_as_temboard=(sudo -nu "$SYSUSER")
else
	run_as_temboard=(env)
fi

"${run_as_temboard[@]}" TEMBOARD_CONFIGFILE="$ETCDIR/temboard.conf" temboard generate-key

if grep -q systemd /proc/1/cmdline && [ -w /etc/systemd/system ] ; then
	start_cmd="systemctl enable --now temboard"
	if systemctl is-system-running &>/dev/null ; then
		systemctl daemon-reload
	fi
else
	start_cmd="sudo -iu $SYSUSER temboard -c ${ETCDIR}/temboard.conf"
fi

log
log "Success. You can now start temboard using:"
log
log "    ${start_cmd}"
log
log "Remember to replace default admin user!!!"
log
