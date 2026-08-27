#!/usr/bin/env bash
#
# Converge -- delete every AWS resource Stage 5 created, and prove it.
#
# Runs on YOUR machine, against YOUR account, with YOUR credentials:
#
#   ./deploy/aws/teardown.sh                 # show what would be deleted
#   ./deploy/aws/teardown.sh --yes           # actually delete it
#
# Dry-run by default. Nothing is deleted until --yes, because a teardown script
# you are afraid of is a teardown script you will not run -- and an instance you
# forgot about is the actual cost risk in this project, not the hourly rate.
#
# Deletes, in dependency order:
#   1. the EC2 instance         (stops instance-hour billing)
#   2. its Elastic IP, if any   (stops the idle-address charge)
#   3. the security group       (cannot be deleted while the instance holds it)
#   4. the key pair             (and points at the local .pem to remove)
#
# The root EBS volume goes with the instance: it is DeleteOnTermination=true, so
# terminating the instance destroys the Postgres data with it. That is the whole
# durability story on a single box -- see the runbook. Back up FIRST if you care:
#
#   ssh -i ~/.ssh/converge-key.pem ubuntu@$HOST \
#     'cd /opt/converge && docker compose exec -T postgres pg_dump -U converge converge' \
#     > converge-backup-$(date +%F).sql

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
NAME="${CONVERGE_NAME:-converge}"
INSTANCE_TAG="$NAME"
SG_NAME="$NAME-sg"
KEY_NAME="$NAME-key"
KEY_FILE="${KEY_FILE:-$HOME/.ssh/$KEY_NAME.pem}"

APPLY=false
[ "${1:-}" = "--yes" ] && APPLY=true

command -v aws >/dev/null 2>&1 || { echo "!! aws CLI not found. brew install awscli" >&2; exit 1; }

aws() { command aws --region "$REGION" "$@"; }

banner() { printf '\n\033[1m%s\033[0m\n' "$*"; }
act()    { if $APPLY; then "$@"; else echo "   would run: $*"; fi }

banner "Converge teardown -- region $REGION, name prefix $NAME"
$APPLY || echo "DRY RUN. Nothing will be deleted. Re-run with --yes to apply."

# Confirm which account, so this cannot quietly hit the wrong one.
if ! ACCOUNT=$(aws sts get-caller-identity --query Account --output text 2>/dev/null); then
    echo "!! could not authenticate. Run \`aws configure\` first." >&2
    exit 1
fi
echo "account: $ACCOUNT"

# -----------------------------------------------------------------------------
banner "1. EC2 instances tagged Name=$INSTANCE_TAG"
# Excludes already-terminated ones so re-runs stay quiet.
INSTANCE_IDS=$(aws ec2 describe-instances \
    --filters "Name=tag:Name,Values=$INSTANCE_TAG" \
              "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].InstanceId' --output text)

if [ -z "$INSTANCE_IDS" ]; then
    echo "   none found"
else
    aws ec2 describe-instances --instance-ids $INSTANCE_IDS \
        --query 'Reservations[].Instances[].{id:InstanceId,type:InstanceType,state:State.Name,ip:PublicIpAddress}' \
        --output table
    act aws ec2 terminate-instances --instance-ids $INSTANCE_IDS
    if $APPLY; then
        echo "   waiting for termination (this is what stops the billing)..."
        aws ec2 wait instance-terminated --instance-ids $INSTANCE_IDS
        echo "   terminated"
    fi
fi

# -----------------------------------------------------------------------------
banner "2. Elastic IPs tagged Name=$NAME"
# Only relevant if you allocated one for a stable hostname. An EIP is free while
# attached to a RUNNING instance and billed hourly the moment it is not -- which
# is exactly the state the step above just created. This is the single easiest
# way to keep paying AWS after you think you have finished.
ALLOC_IDS=$(aws ec2 describe-addresses \
    --filters "Name=tag:Name,Values=$NAME" \
    --query 'Addresses[].AllocationId' --output text 2>/dev/null || true)

if [ -z "$ALLOC_IDS" ] || [ "$ALLOC_IDS" = "None" ]; then
    echo "   none found"
else
    for alloc in $ALLOC_IDS; do
        echo "   releasing $alloc"
        act aws ec2 release-address --allocation-id "$alloc"
    done
fi

# -----------------------------------------------------------------------------
banner "3. security group $SG_NAME"
# Must come after termination: AWS refuses to delete a group still attached to
# a network interface, and a terminating instance holds its ENI for a moment.
SG_ID=$(aws ec2 describe-security-groups \
    --filters "Name=group-name,Values=$SG_NAME" \
    --query 'SecurityGroups[0].GroupId' --output text 2>/dev/null || echo "None")

if [ "$SG_ID" = "None" ] || [ -z "$SG_ID" ]; then
    echo "   none found"
else
    echo "   $SG_ID"
    if $APPLY; then
        # The ENI can linger a few seconds after the instance is gone.
        for attempt in 1 2 3 4 5 6; do
            if aws ec2 delete-security-group --group-id "$SG_ID" 2>/dev/null; then
                echo "   deleted"
                break
            fi
            echo "   still in use, retrying ($attempt/6)..."
            sleep 10
        done
    else
        echo "   would run: aws ec2 delete-security-group --group-id $SG_ID"
    fi
fi

# -----------------------------------------------------------------------------
banner "4. key pair $KEY_NAME"
if aws ec2 describe-key-pairs --key-names "$KEY_NAME" >/dev/null 2>&1; then
    act aws ec2 delete-key-pair --key-name "$KEY_NAME"
    echo "   the private key is local and not deleted by AWS:"
    echo "     rm -i $KEY_FILE"
else
    echo "   none found"
fi

# -----------------------------------------------------------------------------
banner "5. what is still billable in $REGION"
# The honest check. Deleting what this script created is not the same as having
# nothing running, and an EBS volume orphaned by some earlier experiment bills
# just as happily as a forgotten instance.
echo
echo "instances (any state but terminated):"
aws ec2 describe-instances \
    --filters "Name=instance-state-name,Values=pending,running,stopping,stopped" \
    --query 'Reservations[].Instances[].{id:InstanceId,type:InstanceType,state:State.Name,name:Tags[?Key==`Name`]|[0].Value}' \
    --output table 2>/dev/null || echo "  (none)"

echo "EBS volumes (billed whether or not attached):"
aws ec2 describe-volumes \
    --query 'Volumes[].{id:VolumeId,size:Size,state:State,attached:Attachments[0].InstanceId}' \
    --output table 2>/dev/null || echo "  (none)"

echo "Elastic IPs (billed hourly when not attached to a running instance):"
aws ec2 describe-addresses \
    --query 'Addresses[].{ip:PublicIp,alloc:AllocationId,instance:InstanceId}' \
    --output table 2>/dev/null || echo "  (none)"

echo "EBS snapshots (billed per GB-month):"
aws ec2 describe-snapshots --owner-ids self \
    --query 'Snapshots[].{id:SnapshotId,size:VolumeSize,started:StartTime}' \
    --output table 2>/dev/null || echo "  (none)"

banner "Done"
if $APPLY; then
    cat <<EOF
Resources deleted. Two things worth knowing:

  - Billing lags. Charges already incurred still appear, and the Cost Explorer
    figure updates over hours, not seconds. An empty table above is the real
    signal, not a \$0 dashboard.
  - This only covered region $REGION. If you experimented elsewhere, re-run
    with AWS_REGION set to that region. The console's region switcher hides
    resources rather than showing you everything at once, which is how
    forgotten instances happen.

Check every region at once:
  for r in \$(aws ec2 describe-regions --query 'Regions[].RegionName' --output text); do
    n=\$(aws ec2 describe-instances --region \$r \\
      --filters Name=instance-state-name,Values=pending,running,stopping,stopped \\
      --query 'length(Reservations[].Instances[])' --output text)
    [ "\$n" != "0" ] && echo "\$r: \$n instance(s)"
  done
EOF
else
    echo "Nothing was deleted. Re-run with --yes to apply:"
    echo "  $0 --yes"
fi
