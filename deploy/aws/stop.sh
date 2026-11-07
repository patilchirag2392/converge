#!/usr/bin/env bash
#
# Converge -- pause the demo box between demos.
#
#   ./deploy/aws/stop.sh
#
# Stops instance-hour and public-IPv4 billing. The 20 GiB disk keeps billing
# (about $1.60/month) because that is where the data lives -- documents survive.
# To get to $0 and delete everything, use teardown.sh instead.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAME="${CONVERGE_NAME:-converge}"

command -v aws >/dev/null 2>&1 || { echo "!! aws CLI not found. brew install awscli" >&2; exit 1; }
aws() { command aws --region "$REGION" "$@"; }

ID=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=$NAME" "Name=instance-state-name,Values=pending,running" \
    --query 'Reservations[0].Instances[0].InstanceId' --output text)

if [ -z "$ID" ] || [ "$ID" = "None" ]; then
    echo "nothing running tagged Name=$NAME in $REGION -- already stopped or torn down."
    exit 0
fi

echo "stopping $ID ..."
aws ec2 stop-instances --instance-ids "$ID" >/dev/null
aws ec2 wait instance-stopped --instance-ids "$ID"
echo "stopped. Bring it back with ./deploy/aws/start.sh"
