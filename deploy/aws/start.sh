#!/usr/bin/env bash
#
# Converge -- bring a stopped demo box back, and print its URL.
#
#   ./deploy/aws/start.sh
#
# Runs on YOUR machine. No SSH, no .env edit, no rebuild: every container has
# `restart: unless-stopped` and Docker starts at boot, so the stack comes back
# on its own; and the client connects to whatever host served the page, so the
# NEW public DNS name a stop/start always produces just works.
#
# Safe to run when the instance is already running: it only prints the URL.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAME="${CONVERGE_NAME:-converge}"

command -v aws >/dev/null 2>&1 || { echo "!! aws CLI not found. brew install awscli" >&2; exit 1; }
aws() { command aws --region "$REGION" "$@"; }

read -r ID STATE <<<"$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=$NAME" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[0].Instances[0].[InstanceId,State.Name]' --output text)"

if [ -z "${ID:-}" ] || [ "$ID" = "None" ]; then
    echo "!! no instance tagged Name=$NAME in $REGION. It was torn down (or never" >&2
    echo "!! created) -- provision a new one with the runbook, section 4." >&2
    exit 1
fi

case "$STATE" in
    stopping)
        echo "$ID is still stopping; waiting for it to finish first..."
        aws ec2 wait instance-stopped --instance-ids "$ID"
        STATE=stopped ;;
esac

if [ "$STATE" = "stopped" ]; then
    echo "starting $ID ..."
    aws ec2 start-instances --instance-ids "$ID" >/dev/null
fi
aws ec2 wait instance-running --instance-ids "$ID"

HOST=$(aws ec2 describe-instances --instance-ids "$ID" \
    --query 'Reservations[0].Instances[0].PublicDnsName' --output text)

# Running is not the same as serving: the OS boots, then Docker, then six
# containers, then their healthchecks. Poll the public origin -- the path a
# user takes -- rather than trusting the instance state.
printf 'waiting for the app at http://%s ' "$HOST"
for _ in $(seq 1 60); do
    if curl -fsS -m 3 "http://$HOST/healthz" >/dev/null 2>&1; then
        printf '\n\nUp:  http://%s/?doc=demo\n' "$HOST"
        echo "Open it in two NEW windows (Cmd+N, type the URL) -- not duplicated tabs."
        exit 0
    fi
    printf '.'
    sleep 5
done

echo
echo "!! the instance is running but http://$HOST/healthz did not answer within 5 minutes." >&2
echo "!! SSH in and check: cd /opt/converge && docker compose -f docker-compose.yml -f docker-compose.prod.yml ps" >&2
exit 1
