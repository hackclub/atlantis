#!/bin/sh
#
# The timer the management commands have always needed, as its own container.
#
# Three things have to happen on a schedule and none of them had anywhere to
# run: close_week settles finished challenge weeks and DMs whoever got dropped,
# submit_airtable retries submissions that failed at finalization, and
# check_timelapse_activity looks for dead air in compiled footage. (A fourth,
# snapshot_metrics, runs once a day at a set time; see snapshot_loop.) Every
# one is idempotent and safe to run late or twice, which is what makes a plain
# loop enough and why nothing here tries to catch up on a run it missed.
#
# A loop rather than cron, deliberately. cron in a container is awkward in a
# specific way: the daemon does not hand its own environment to the jobs it
# runs, so everything in .env — the database URL, the Slack token, the R2
# credentials — would have to be dumped to a file inside the image and sourced
# back per job. A loop inherits the environment the way any other process does,
# logs to stdout where `docker compose logs` can see it, and needs nothing
# installed that isn't already here. The cost is that jobs drift by up to one
# tick, which does not matter for work measured in minutes.
#
# Intervals are seconds and can be overridden per environment.

set -u

TICK="${SCHEDULER_TICK:-60}"
CLOSE_WEEK_EVERY="${CLOSE_WEEK_EVERY:-3600}"
SUBMIT_AIRTABLE_EVERY="${SUBMIT_AIRTABLE_EVERY:-600}"
CHECK_ACTIVITY_EVERY="${CHECK_ACTIVITY_EVERY:-900}"
# Each pass runs ffmpeg over whole videos, so it is bounded rather than left to
# work through a backlog in one go and hold the container for an hour.
CHECK_ACTIVITY_LIMIT="${CHECK_ACTIVITY_LIMIT:-20}"

STATE_DIR="${SCHEDULER_STATE_DIR:-/tmp/scheduler}"
mkdir -p "$STATE_DIR"

log() {
	echo "[scheduler] $(date -u +%Y-%m-%dT%H:%M:%SZ) $*"
}

# Run `name` if `interval` seconds have passed since it last started.
#
# The stamp is written before the job rather than after, so a job that takes
# longer than its interval doesn't immediately become due again, and one that
# dies doesn't retry in a tight loop.
run_due() {
	name="$1"
	interval="$2"
	shift 2

	stamp="$STATE_DIR/$name"
	now="$(date +%s)"
	last=0
	if [ -f "$stamp" ]; then
		last="$(cat "$stamp")"
	fi

	if [ "$((now - last))" -lt "$interval" ]; then
		return 0
	fi

	echo "$now" > "$stamp"
	log "running $name"
	if python manage.py "$@"; then
		log "$name finished"
	else
		# Logged and stepped over: one command failing must not take the other
		# two down with it, and the next tick will try it again.
		log "$name FAILED (exit $?)" >&2
	fi
}

# Nothing is due until the schema is actually there. On a first deploy this
# container and the web one come up together, and web is what runs the
# migration — so rather than racing it, wait until the database answers and has
# nothing left to apply. This also covers the database simply being slow to
# accept connections, since migrate --check fails either way.
log "waiting for the database and migrations"
until python manage.py migrate --check >/dev/null 2>&1; do
	sleep 5
done
log "database ready; scheduling every ${TICK}s"

# snapshot_metrics has to run at one particular minute, 23:59 Eastern, which
# the tick loop below can't promise: one ffmpeg pass can hold it for minutes.
# So it gets a loop of its own that sleeps straight to the next 23:59 in the
# challenge zone. The target is worked out fresh each day as an absolute time,
# so the host being on UTC and DST moving the offset both come out right.
SNAPSHOT_TZ="${CHALLENGE_TIMEZONE:-America/New_York}"

snapshot_loop() {
	while true; do
		now="$(date +%s)"
		target="$(TZ="$SNAPSHOT_TZ" date -d 'today 23:59' +%s)"
		# Still inside 23:59 counts as today's; only once the minute has gone
		# is it tomorrow's.
		if [ "$now" -ge "$((target + 60))" ]; then
			target="$(TZ="$SNAPSHOT_TZ" date -d 'tomorrow 23:59' +%s)"
		fi
		if [ "$target" -gt "$now" ]; then
			sleep "$((target - now))"
		fi

		log "running snapshot_metrics"
		if python manage.py snapshot_metrics; then
			log "snapshot_metrics finished"
		else
			log "snapshot_metrics FAILED (exit $?)" >&2
		fi
		# Past the minute before looking again, so it isn't taken twice.
		sleep 60
	done
}
snapshot_loop &

while true; do
	run_due close_week "$CLOSE_WEEK_EVERY" close_week
	run_due submit_airtable "$SUBMIT_AIRTABLE_EVERY" submit_airtable
	run_due check_activity "$CHECK_ACTIVITY_EVERY" \
		check_timelapse_activity --limit "$CHECK_ACTIVITY_LIMIT"
	sleep "$TICK"
done
