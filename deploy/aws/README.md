# Deploying Converge to AWS — a single EC2 box

The Stage 4 compose stack, running on one EC2 instance, reachable at a public
URL. Deliberately the simplest thing that works: no ECS, no Fargate, no
Kubernetes, no load balancer service, no RDS. Everything here is a resource you
can point at and explain.

**Nothing in this document has been run against a real AWS account by the
author of the deployment config.** The stack itself is verified — the production
overlay was built and exercised locally, including a real browser syncing across
instances through the production Nginx (see [NOTES.md](../../NOTES.md)). The AWS
resource commands are written from the API contracts and desk-checked, not
executed. Where something is more likely to differ on your account, it says so.

---

## Contents

1. [What this creates, and what it costs](#1-what-this-creates-and-what-it-costs)
2. [Before you start: a budget alarm](#2-before-you-start-a-budget-alarm)
3. [Prerequisites](#3-prerequisites)
4. [Provisioning, one command at a time](#4-provisioning-one-command-at-a-time)
5. [Ports: who can reach what, and why](#5-ports-who-can-reach-what-and-why)
6. [Deploying the stack on the box](#6-deploying-the-stack-on-the-box)
7. [Verifying it](#7-verifying-it)
8. [The request path, end to end](#8-the-request-path-end-to-end)
9. [Persistence: what happens to your data](#9-persistence-what-happens-to-your-data)
10. [Day-to-day operations](#10-day-to-day-operations)
11. [Optional: HTTPS](#11-optional-https)
12. [Teardown](#12-teardown)

---

## 1. What this creates, and what it costs

Five resources, four of them free:

| Resource | What it is | Cost |
|---|---|---|
| EC2 instance, `t3.micro` | the box everything runs on | **hourly, see below** |
| EBS root volume, 20 GiB `gp3` | its disk — including all Postgres data | **per GB-month** |
| Public IPv4 address | how the internet reaches it | **hourly since Feb 2024** |
| Security group | the firewall | free |
| Key pair | the SSH key | free |

### The bill, if you leave it running 24/7

Prices are **us-east-1, on-demand, as of writing**. They change, they differ by
region, and you should sanity-check them against
<https://aws.amazon.com/ec2/pricing/on-demand/> rather than trust this table.

| Line item | Rate | × 730 h/month |
|---|---|---|
| `t3.micro` | $0.0104 /hr | **$7.59** |
| 20 GiB gp3 | $0.08 /GB-month | **$1.60** |
| 1 public IPv4 | $0.005 /hr | **$3.65** |
| Data transfer out | first 100 GB/month free | **$0.00** |
| | | **≈ $12.84 / month** |

**That is the no-free-tier number.** It is the one to plan against, because it
is what you pay if the free tier doesn't apply or has expired.

### If the free tier applies

The 12-month free tier covers 750 hours/month of `t2.micro` **or** `t3.micro`
(which is one instance running continuously), 30 GB of EBS, and — since AWS
started charging for IPv4 — 750 hours/month of public IPv4. All three of our
billable lines. So: **≈ $0/month**, as long as you run exactly one instance.

Two honest caveats:

- **AWS changed the free tier in mid-2025** for newly created accounts, from the
  12-month always-free model to a credit-based free plan. If your account is
  new, usage draws down credits instead of being free. I can't tell which model
  your account is on — check the **Billing → Free tier** page in the console
  before assuming.
- Free tier is **per account, not per instance**. If you already have something
  running, this instance may not be covered.

### The costs that actually surprise people

- **Stopping the instance does not stop all billing.** Instance-hours stop; the
  EBS volume keeps billing (~$1.60/month for 20 GiB) because your data is still
  on it. Only *terminating* takes that to zero.
- **An Elastic IP is free while attached to a running instance, and billed the
  moment it isn't.** So the instant you stop or terminate the instance, an EIP
  you allocated starts costing ~$3.65/month for nothing. This is the single most
  common way to keep paying AWS after you think you've finished, which is why
  `teardown.sh` releases it explicitly.
- **Region drift.** The console shows one region at a time. An instance you
  launched in the wrong region is invisible unless you go looking. `teardown.sh`
  ends with an all-regions check.

---

## 2. Before you start: a budget alarm

Do this first. It costs nothing and it is the only thing standing between you
and a surprise. Two minutes.

**Console route (recommended — the CLI equivalent needs a long JSON body):**

1. Sign in and go to **Billing and Cost Management → Budgets**.
2. **Create budget → Use a template → Zero spend budget** (alerts on the first
   cent above the free tier), or **Monthly cost budget** with a limit of `$5`.
3. Enter your email. Confirm.

Also turn on **Billing → Billing preferences → Free tier usage alerts**, which
emails you at 85% of a free-tier limit.

> Budget alerts are *notifications*, not caps. AWS has no hard spending limit —
> nothing will shut the instance off for you. The alarm buys you the chance to
> run `teardown.sh`; it does not run it.

---

## 3. Prerequisites

### The AWS CLI

```bash
brew install awscli
aws --version
```

### Credentials

You need an access key. **Do not use your account root user for this.** Create
an IAM user instead:

1. **IAM → Users → Create user**, name it e.g. `converge-deploy`.
2. Attach the AWS-managed **`AmazonEC2FullAccess`** policy. Broader than this
   deployment strictly needs, and the honest justification is that scoping it
   precisely is a bigger exercise than this stage — a real setup would restrict
   by resource tag. It is still enormously better than root credentials, which
   cannot be scoped at all and cannot be revoked without breaking everything.
3. **Create access key → Command Line Interface**. Copy both values.
4. `aws configure` — paste the key, the secret, set the region, `json`.

```bash
# Confirm which identity you're about to act as. Do this every time.
aws sts get-caller-identity
```

### The branch has to be on GitHub

The instance builds from a `git clone`, so **the code must be pushed before you
launch it.** `bootstrap.sh` defaults to `REPO_REF=stage-5-aws`; if that branch
isn't on the remote, the clone fails and the instance boots with no application
on it (and the failure is buried in `/var/log/cloud-init-output.log`, which is
an annoying place to discover it).

```bash
git push -u origin stage-5-aws
git ls-remote --heads origin stage-5-aws     # must print a line
```

To deploy something else, override it at launch — `REPO_REF` and `REPO_URL` are
both environment variables in the script, and a tag is a better choice than a
branch for anything you want to be able to reproduce:

```bash
REPO_REF=stage-5 ...   # e.g. once the stage is tagged
```

### Shell variables

Everything below uses these. Set them once per terminal session.

```bash
export AWS_REGION=us-east-1          # cheapest, most availability
export CONVERGE_NAME=converge        # tag + name prefix for every resource
export MY_IP="$(curl -s https://checkip.amazonaws.com)"
echo "SSH will be locked to $MY_IP"
```

> `MY_IP` is your current public address. On home broadband it changes when the
> router reconnects, and on any other network it is simply different — so if SSH
> stops working later, this is almost always why. Section 10 has the one-liner
> to update it.

---

## 4. Provisioning, one command at a time

Deliberately not scripted. You asked to know what each command creates, and a
script that provisions five resources behind one line of output is the opposite
of that. Teardown *is* scripted, because there the risk runs the other way.

### 4.1 A key pair — creates: one EC2 key pair, and the only copy of a private key

```bash
aws ec2 create-key-pair \
  --region "$AWS_REGION" \
  --key-name "$CONVERGE_NAME-key" \
  --query KeyMaterial --output text > ~/.ssh/$CONVERGE_NAME-key.pem

chmod 400 ~/.ssh/$CONVERGE_NAME-key.pem
```

AWS keeps the public half and hands you the private half **once**. If you lose
that file you cannot SSH to the instance again — there is no recovery, only
relaunching. `chmod 400` is not decoration: OpenSSH refuses to use a key file
that others can read.

### 4.2 A security group — creates: one firewall, initially with no ingress

```bash
# Your default VPC. Almost every account has one; if this prints None, you'll
# need to create a VPC or use the console's launch wizard instead.
export VPC_ID=$(aws ec2 describe-vpcs --region "$AWS_REGION" \
  --filters Name=isDefault,Values=true \
  --query 'Vpcs[0].VpcId' --output text)
echo "VPC: $VPC_ID"

export SG_ID=$(aws ec2 create-security-group \
  --region "$AWS_REGION" \
  --group-name "$CONVERGE_NAME-sg" \
  --description "Converge: public HTTP, SSH from one address" \
  --vpc-id "$VPC_ID" \
  --query GroupId --output text)
echo "security group: $SG_ID"

aws ec2 create-tags --region "$AWS_REGION" --resources "$SG_ID" \
  --tags Key=Name,Value="$CONVERGE_NAME"
```

A new security group **denies all inbound and allows all outbound**. The two
rules below are the entire public attack surface of this deployment.

```bash
# Rule 1 — HTTP from anywhere. This is the app.
aws ec2 authorize-security-group-ingress \
  --region "$AWS_REGION" --group-id "$SG_ID" \
  --protocol tcp --port 80 --cidr 0.0.0.0/0

# Rule 2 — SSH from your address only.
aws ec2 authorize-security-group-ingress \
  --region "$AWS_REGION" --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "$MY_IP/32"
```

Read it back — this is the one piece of configuration where a mistake is a
security incident rather than a bug:

```bash
aws ec2 describe-security-groups --region "$AWS_REGION" --group-ids "$SG_ID" \
  --query 'SecurityGroups[0].IpPermissions[].{proto:IpProtocol,from:FromPort,cidr:IpRanges[0].CidrIp}' \
  --output table
```

You should see exactly two rules. If port 22 shows `0.0.0.0/0`, fix it now —
that is an SSH endpoint for the entire internet, and it will start seeing
credential-stuffing traffic within minutes.

### 4.3 The instance — creates: one EC2 instance and one 20 GiB EBS volume

Get the current Ubuntu 24.04 AMI from Canonical's public SSM parameter rather
than pasting an AMI id: ids are region-specific and go stale every time the
image is rebuilt.

```bash
export AMI_ID=$(aws ssm get-parameters --region "$AWS_REGION" \
  --names /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query 'Parameters[0].Value' --output text)
echo "AMI: $AMI_ID"
```

> If that prints `None` or errors, the parameter path has changed. Fall back to:
> ```bash
> aws ec2 describe-images --region "$AWS_REGION" --owners 099720109477 \
>   --filters 'Name=name,Values=ubuntu/images/hvm-ssd-gp3/ubuntu-noble-24.04-amd64-server-*' \
>   --query 'sort_by(Images,&CreationDate)[-1].ImageId' --output text
> ```
> (`099720109477` is Canonical's AWS account. Using it as a filter is how you
> avoid launching someone else's image that happens to be named "ubuntu".)

```bash
aws ec2 run-instances \
  --region "$AWS_REGION" \
  --image-id "$AMI_ID" \
  --instance-type t3.micro \
  --key-name "$CONVERGE_NAME-key" \
  --security-group-ids "$SG_ID" \
  --block-device-mappings '[{"DeviceName":"/dev/sda1","Ebs":{"VolumeSize":20,"VolumeType":"gp3","DeleteOnTermination":true}}]' \
  --metadata-options 'HttpTokens=required' \
  --user-data "file://$(git rev-parse --show-toplevel)/deploy/aws/bootstrap.sh" \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=$CONVERGE_NAME}]" \
  --query 'Instances[0].InstanceId' --output text
```

What each unobvious flag does:

- **`--instance-type t3.micro`** — 2 vCPU, 1 GiB RAM, free-tier eligible.
  1 GiB is genuinely tight for six containers plus a Node build, which is why
  `bootstrap.sh` adds 2 GiB of swap. See section 6.
- **`VolumeSize: 20`** — the Ubuntu default is 8 GiB, which is too small once
  you have ~440 MB of images, a `node_modules` tree during the build, and a
  growing operation log. 20 stays inside the 30 GB free-tier allowance.
- **`DeleteOnTermination: true`** — the default, stated explicitly because it is
  the most consequential setting in this command. **Terminating the instance
  destroys this volume and every row of Postgres data on it.** Section 9.
- **`HttpTokens=required`** — forces IMDSv2 for the instance metadata service.
  IMDSv1 is a plain unauthenticated GET, which is what makes SSRF in an app
  escalate into stealing instance credentials. Costs nothing to require.
- **`--user-data file://...bootstrap.sh`** — runs the bootstrap as root at first
  boot: apt, swap, Docker, clone. **It contains no secrets, on purpose** —
  user-data is readable from inside the instance forever, and via
  `aws ec2 describe-instance-attribute --attribute userData` from outside.

Save the id and wait for it:

```bash
export INSTANCE_ID=$(aws ec2 describe-instances --region "$AWS_REGION" \
  --filters "Name=tag:Name,Values=$CONVERGE_NAME" \
            "Name=instance-state-name,Values=pending,running" \
  --query 'Reservations[0].Instances[0].InstanceId' --output text)

aws ec2 wait instance-status-ok --region "$AWS_REGION" --instance-ids "$INSTANCE_ID"

export PUBLIC_HOST=$(aws ec2 describe-instances --region "$AWS_REGION" \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].PublicDnsName' --output text)
echo "public host: $PUBLIC_HOST"
```

### 4.4 Optional — an Elastic IP, for a URL that survives a stop/start

Skip this unless you need it. Without it, stopping and starting the instance
changes its public DNS name, which means editing `.env` and **rebuilding the
client image** (the hostname is baked into the bundle).

```bash
export ALLOC_ID=$(aws ec2 allocate-address --region "$AWS_REGION" --domain vpc \
  --query AllocationId --output text)
aws ec2 create-tags --region "$AWS_REGION" --resources "$ALLOC_ID" \
  --tags Key=Name,Value="$CONVERGE_NAME"
aws ec2 associate-address --region "$AWS_REGION" \
  --instance-id "$INSTANCE_ID" --allocation-id "$ALLOC_ID"
```

**Remember it exists.** Free while attached to a running instance; ~$3.65/month
the moment it isn't. `teardown.sh` releases anything tagged `Name=$CONVERGE_NAME`.

---

## 5. Ports: who can reach what, and why

### Open to the internet (`0.0.0.0/0`)

| Port | Why it must be public |
|---|---|
| **80/tcp** | The entire application. Nginx serves the client bundle *and* proxies the WebSocket and HTTP API on this one port — that is the point of `nginx.prod.conf`. A browser needs it. |

### Open to one address (`$MY_IP/32`)

| Port | Why it isn't public |
|---|---|
| **22/tcp** | Administration only. No end user needs SSH, so exposing it to the internet would add a permanently-attacked surface for zero functional gain. Locked to a single `/32`. |

### Not open at all

| Port | What's on it | Why it stays closed |
|---|---|---|
| **5432** | Postgres | Holds every operation in every document. Reachable only as `postgres:5432` on the Docker bridge network. A public Postgres is a data breach waiting for a weak password — and it *has* a password, which is exactly the thing you don't want to be the only defence. |
| **6379** | Redis | **No authentication configured at all.** An internet-reachable Redis is remote code execution, not just data exposure — `CONFIG SET dir` plus a crafted key is a well-known path to writing an SSH key onto the box. This one is not a judgement call. |
| **8000** ×2 | the two FastAPI instances | Reachable only as `server-a:8000` / `server-b:8000`. Closing them is what makes Nginx the single entry point rather than a suggestion, so round-robin and the WebSocket-upgrade handling can't be bypassed. |
| **80** (client container) | the static bundle | Nginx proxies to it as `client:80`. Publishing it would be a second public origin serving the same files. |
| **443/tcp** | nothing | **Deliberately not opened.** TLS isn't configured by default (section 11), and opening a port with nothing behind it is surface without function. Open it in the same breath as configuring a certificate, not before. |

### The two layers, and why both

The security group is one layer. The other is that `docker-compose.prod.yml`
**doesn't publish those ports in the first place**.

That is not redundancy for its own sake. A published container port is not
subject to the host's own firewall rules the way you'd expect — Docker writes
`iptables` DNAT rules that bypass much of what you might have configured — so
"the security group will catch it" is a habit worth not forming. And the
overlay has its own trap: Compose **merges** the `ports` list across files, so
an overlay that simply omits `ports` inherits the base file's and leaves
Postgres published. It takes `!reset []`. Verified by reading the output rather
than trusting the file:

```bash
# On the box, or locally. Should print exactly one published port: 80.
docker compose -f docker-compose.yml -f docker-compose.prod.yml config \
  | grep -E 'published|^  [a-z-]+:'
```

---

## 6. Deploying the stack on the box

```bash
ssh -i ~/.ssh/$CONVERGE_NAME-key.pem ubuntu@$PUBLIC_HOST
```

First check the bootstrap actually finished — user-data runs asynchronously, so
SSH may be available before Docker is:

```bash
sudo tail -30 /var/log/cloud-init-output.log     # look for "Bootstrap complete"
docker compose version                            # must be >= 2.24
free -h                                           # swap should show 2.0Gi
```

Then fill in the environment file. **These values only ever exist on this box.**

```bash
cd /opt/converge

# The hostname browsers will use, read from instance metadata (IMDSv2).
TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/public-hostname; echo

openssl rand -base64 24        # the database password — generate, don't invent

nano .env                      # set PUBLIC_HOST and POSTGRES_PASSWORD
```

> If you attached an Elastic IP or have a domain, `PUBLIC_HOST` is that, not the
> metadata hostname.

Bring it up:

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build --wait
```

Expect **5–10 minutes** on the first run — most of it building the client image.
The compose file refuses to start if either required variable is missing, so a
half-filled `.env` fails immediately rather than booting with a default password.

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
```

All six services should be `healthy`.

### If a server container never goes healthy

```
dependency failed to start: container converge-server-a-1 is unhealthy
```

Check the reason before guessing:

```bash
dc logs server-a | grep -iE 'password|fatal|Traceback'
```

`asyncpg.exceptions.InvalidPasswordError` means the Postgres volume was
initialised with a **different** password than the one now in `.env`. Postgres
runs `initdb` — the step that creates the role — **only on a first, empty
volume**. Changing `POSTGRES_PASSWORD` afterwards changes what the servers
*send*, not what the database *expects*. Either put the original password back,
or throw the volume away and start over:

```bash
dc down -v        # DELETES ALL DATA. Back up first if you care (section 9).
dc up -d --build --wait
```

(The server fails fast here rather than retrying for 30 seconds, because a wrong
password is a terminal error, not a transient one — see `TRANSIENT_CONNECT_ERRORS`
in `server/app/db.py`.)

### If the build is killed

```
=> ERROR [client builder 4/6] RUN npm ci        exit code: 137
```

Exit 137 is SIGKILL — the kernel's OOM killer, not an npm error. Check swap is
actually on (`free -h`; `swapon --show`). If swap is present and it still dies,
the honest options are a `t3.small` (~2× the cost, not free-tier eligible) or
building the images elsewhere and pulling them — Stage 4's CI already builds
all three, so publishing them to GHCR and pulling on the box is a small step
and the better long-term answer.

---

## 7. Verifying it

From **your laptop**, not the box — that's the path a user takes:

```bash
cd server
uv run python scripts/verify_deployment.py "http://$PUBLIC_HOST"
```

It checks, through the public origin only:

1. `GET /` returns the client HTML.
2. `GET /healthz` reaches **more than one instance** — round-robin works.
3. Two WebSocket clients land on **different** instances and an edit on one
   arrives at the other — the Stage 3 property, over the internet.
4. `GET /api/doc/...` still returns the text after both sockets closed — so it
   was persisted, not just held in memory.

Exit status is 0 only if all four pass.

Then the demo itself: open `http://$PUBLIC_HOST/?doc=demo` in two tabs, check
the `instance:` pill differs, and type in both.

> The `clients:` pill counts only sockets on *your* instance — a known
> limitation from Stage 3, not a deployment problem.

---

## 8. The request path, end to end

Tracing `http://ec2-1-2-3-4.compute-1.amazonaws.com/?doc=demo`.

### Getting the page

1. **DNS.** The EC2 public hostname resolves to the instance's public IPv4.
   (From outside the VPC it resolves to the public address; from *inside*, AWS
   resolves the same name to the private address.)
2. **Internet gateway → VPC → subnet.** The IGW does 1:1 NAT between the public
   address and the instance's private address on its network interface.
3. **Security group.** Stateful, evaluated on the inbound packet: `tcp/80` from
   `0.0.0.0/0` matches rule 1, so it's allowed. Because it's stateful, the
   response needs no matching outbound rule.
4. **The host kernel.** Docker published `80 → lb:80`, so an `iptables` DNAT
   rule in the `nat` table rewrites the destination to the `lb` container's
   address on the `converge_default` bridge (something like `172.18.0.7:80`).
5. **Nginx, in the `lb` container.** Longest-prefix match on the path. `/`
   doesn't match `/ws/`, `/api/`, `/healthz` or `/metrics`, so it falls to
   `location /` → `proxy_pass http://converge_client`.
6. **Docker's embedded DNS** at `127.0.0.11` resolves `client` to the client
   container. Nginx opens a connection to `client:80`.
7. **Nginx, in the `client` container.** `try_files` finds `/index.html` and
   serves it, with `Cache-Control: no-store` so a redeploy isn't served stale.
8. Back out the same chain. The browser parses the HTML and requests
   `/assets/index-*.js`, which takes the identical path and is served with
   `Cache-Control: public, immutable` — safe because the filename is
   content-hashed.

### Opening the socket

9. The bundle runs. `VITE_WS_URL` was inlined at build time as
   `ws://$PUBLIC_HOST:80`, so it opens
   `ws://ec2-1-2-3-4.compute-1.amazonaws.com/ws/demo?client_id=a1b2c3d4`.
10. Same route to Nginx (steps 1–4). Now `location /ws/` matches, which is the
    block that carries `proxy_http_version 1.1` and the `Upgrade` /
    `Connection` headers. Those are hop-by-hop headers and are **not** forwarded
    by default — without that block the handshake arrives stripped and fails as
    a plain HTTP request.
11. **Which instance?** `upstream converge_backends` is round-robin, and the
    choice is made **per TCP connection**. So the tab is pinned to whichever
    instance it got — `server-a:8000` say — for the whole session, and the
    second tab's separate connection usually gets `server-b`. That's what the
    `instance:` pill is showing, and why two tabs demonstrate anything at all.
12. **Uvicorn → FastAPI.** `hub.attach()` runs in this order: subscribe to
    `converge:ops:demo` on Redis, *then* load the document from Postgres, *then*
    register the socket. Subscribing first is what closes the join race — an
    operation published during the load is at worst applied twice, which is a
    no-op, whereas loading first would lose it.
13. The server sends a `snapshot` frame — the operation history, not the text —
    so the tab builds its own replica and can merge concurrent edits from the
    first keystroke.

### A keystroke, and how it reaches the other tab

14. The browser diffs the `<textarea>`, turns the change into RGA operations,
    and sends `{"type":"ops","ops":[...]}` over the open socket. Only the
    changed characters travel.
15. `server-a` does, strictly in this order: **persist** to Postgres (an append,
    `ON CONFLICT DO NOTHING`) → **apply** to its own replica → **broadcast** to
    its local sockets → **publish** to the Redis channel. Postgres first so
    nothing visible is undurable; local broadcast before publish so local
    clients don't wait on Redis.
16. **Redis** delivers to every subscriber of `converge:ops:demo`. `server-b` is
    subscribed, because it has a local client on that document.
17. `server-b`'s reader task compares the message's `origin` against its own
    instance id, drops its own echoes, applies the operations, and broadcasts
    to *its* local sockets.
18. Tab 2, connected to `server-b`, receives an `ops` frame and merges it into
    its replica. The caret is re-anchored by character id, not offset, so it
    doesn't jump.

Frames travelling back take **no new routing decision** — the WebSocket is a
persistent tunnel, and Nginx is relaying bytes on the upstream connection it
already holds open. Which is also why `proxy_read_timeout 1h` matters: an editor
socket is idle whenever nobody is typing, and the default 60 seconds would drop
it mid-session.

Measured on this stack, the Redis hop is a fraction of a millisecond. What's
new over the internet is the browser↔EC2 leg, which is tens of milliseconds and
dwarfs everything else — worth knowing when someone asks where the latency is.

---

## 9. Persistence: what happens to your data

**Where the data is.** Postgres writes to the Docker named volume `pgdata`,
which is a directory on the instance's root EBS volume
(`/var/lib/docker/volumes/converge_pgdata/_data`). There is no other copy
anywhere.

So the durability boundary is exactly **that one EBS volume, in one
availability zone**.

| Action | Instance | EBS volume | Your data | Billing |
|---|---|---|---|---|
| `docker compose down` | running | intact | **safe** — the named volume outlives containers | unchanged |
| `docker compose down -v` | running | intact | **gone** — `-v` deletes the volume | unchanged |
| **Stop** the instance | stopped | **intact** | **safe** | instance-hours stop, EBS keeps billing (~$1.60/mo) |
| **Reboot** | running | intact | **safe** | unchanged |
| **Terminate** the instance | gone | **deleted** | **gone, unrecoverably** | everything stops |

Two consequences worth internalising:

- **Stop is safe; terminate is destructive.** Same button area in the console,
  opposite outcomes. `DeleteOnTermination=true` on the root volume — the
  default, which section 4.3 sets explicitly so it isn't a surprise — means
  terminating deletes the disk with the instance.
- **Stopping changes the public DNS name** (unless you attached an Elastic IP).
  The data is fine, but the client bundle has the old hostname baked in, so
  after a start you must update `.env` and rebuild.

### Back it up before you tear down

The operation log *is* the document, so a `pg_dump` is a complete backup:

```bash
# From your laptop
ssh -i ~/.ssh/$CONVERGE_NAME-key.pem ubuntu@$PUBLIC_HOST \
  'cd /opt/converge && docker compose exec -T postgres pg_dump -U converge converge' \
  > converge-backup-$(date +%F).sql
```

Or an EBS snapshot, which captures the whole disk (billed per GB-month, so
delete it when done — `teardown.sh` lists snapshots for exactly this reason):

```bash
export VOL_ID=$(aws ec2 describe-instances --region "$AWS_REGION" \
  --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].BlockDeviceMappings[0].Ebs.VolumeId' --output text)
aws ec2 create-snapshot --region "$AWS_REGION" --volume-id "$VOL_ID" \
  --description "converge $(date +%F)"
```

### What you're giving up by not using RDS

This is the honest list, and none of it is hypothetical:

| RDS gives you | On this box you have |
|---|---|
| Automated daily backups + point-in-time recovery to any second in the window | Nothing, until you run `pg_dump` yourself |
| Data lifecycle **independent of compute** — terminate the app server, database survives | Terminating the instance destroys the database |
| Multi-AZ standby with automatic failover | One AZ. If it fails, you're down until you rebuild |
| Managed minor-version patching | You patch Postgres by changing the image tag and hoping |
| Storage that grows without touching the instance | 20 GiB, and resizing means resizing the instance's root volume |
| Metrics, slow-query insights, connection management | `docker compose logs` |

**And it's still the right call for this project**, for reasons that aren't
"it's cheaper":

- The failure this project is *about* is CRDT convergence across instances, not
  database durability. A single Postgres exercises that identically.
- Every operation is also replicated in the connected browsers' in-memory
  replicas. Not a backup — they're gone on refresh — but the data is not as
  singly-located as the table suggests.
- `db.t4g.micro` RDS is roughly $12–15/month plus storage once free tier
  lapses, which is more than doubling the bill of this deployment.

**The line to draw in an interview:** the moment this had real users, RDS (or at
minimum an EBS snapshot schedule) would come *before* any of the scaling work,
because "we lose everything if the box dies" is a worse property than "we can't
serve more than two instances".

---

## 10. Day-to-day operations

```bash
# Always from /opt/converge, and always with both files.
cd /opt/converge
alias dc='docker compose -f docker-compose.yml -f docker-compose.prod.yml'

dc ps                          # health of all six
dc logs -f lb                  # nginx access/error
dc logs -f server-a            # startup sequence, joins, edits
dc logs --tail 100 postgres
dc restart server-a            # one instance; the other keeps serving
dc down                        # stop everything, keep the data
```

**Deploy a change:**

```bash
cd /opt/converge && git pull
dc up -d --build --wait
```

**After a stop/start** (the hostname changed):

```bash
# new hostname
TOKEN=$(curl -sX PUT http://169.254.169.254/latest/api/token \
  -H 'X-aws-ec2-metadata-token-ttl-seconds: 60')
curl -s -H "X-aws-ec2-metadata-token: $TOKEN" \
  http://169.254.169.254/latest/meta-data/public-hostname; echo

nano .env                      # update PUBLIC_HOST
dc up -d --build               # --build is REQUIRED: the URL is baked into the bundle
```

**SSH broke because your IP changed:**

```bash
OLD=$(aws ec2 describe-security-groups --region "$AWS_REGION" --group-ids "$SG_ID" \
  --query "SecurityGroups[0].IpPermissions[?FromPort==\`22\`].IpRanges[0].CidrIp" --output text)
NEW="$(curl -s https://checkip.amazonaws.com)/32"
aws ec2 revoke-security-group-ingress --region "$AWS_REGION" --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "$OLD"
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$SG_ID" \
  --protocol tcp --port 22 --cidr "$NEW"
```

**Memory pressure** — the thing most likely to bite on 1 GiB:

```bash
free -h
docker stats --no-stream
```

---

## 11. Optional: HTTPS

Not configured by default, and the reason is specific rather than laziness:
**Let's Encrypt will not issue a certificate for an `*.compute.amazonaws.com`
hostname.** Those names are on the Public Suffix List and Amazon owns them; you
can't prove control of a domain you don't own. So HTTPS requires **a domain name
you control**. Options, cheapest first:

1. **Your own domain + Certbot on the box.** ~$10–15/year for the domain, $0 for
   the certificate. Point an `A` record at the instance's Elastic IP, then run
   Certbot in a container to obtain and renew.
2. **CloudFront in front of the instance.** Free certificate via ACM, works with
   a CloudFront `*.cloudfront.net` hostname so no domain needed — but it adds a
   service, request-based costs, and WebSocket configuration.
3. **An Application Load Balancer + ACM.** The "proper" answer and the wrong one
   here: an ALB is ~$16/month on its own, more than the entire rest of this
   deployment, to terminate TLS for one box.

Whichever you pick, three things must change together:

```bash
# 1. open 443
aws ec2 authorize-security-group-ingress --region "$AWS_REGION" --group-id "$SG_ID" \
  --protocol tcp --port 443 --cidr 0.0.0.0/0
```

2. Nginx needs a `listen 443 ssl` server block with the certificate, and
   `nginx.prod.conf` is where it goes.
3. **`PUBLIC_HOST` must produce `wss://`, not `ws://`** — browsers block a
   `ws://` socket opened from an `https://` page as mixed content, and the
   failure looks like "the page loads but never connects". The bundle takes the
   scheme from `VITE_WS_URL`, so this is a rebuild, and
   `verify_deployment.py` already maps an `https://` base URL to `wss://`.

---

## 12. Teardown

**Run this when you finish demoing.** An instance you forgot about is the actual
cost risk here — ~$12.84/month for something nobody is looking at.

```bash
# Dry run first: prints what it would delete, deletes nothing.
./deploy/aws/teardown.sh

# Then actually do it.
./deploy/aws/teardown.sh --yes
```

It terminates the instance (which deletes the root volume and all Postgres
data — **back up first** if you want it, section 9), releases any Elastic IP,
deletes the security group after the instance's network interface has let go of
it, and deletes the key pair. Then it lists everything still billable in the
region: instances, volumes, addresses, snapshots. **Empty tables are the signal**
— not a `$0` figure on the billing dashboard, which lags by hours.

Finally, remove the local private key, which AWS never had:

```bash
rm -i ~/.ssh/$CONVERGE_NAME-key.pem
```

### If you'd rather do it by hand

```bash
aws ec2 terminate-instances --region "$AWS_REGION" --instance-ids "$INSTANCE_ID"
aws ec2 wait instance-terminated --region "$AWS_REGION" --instance-ids "$INSTANCE_ID"
aws ec2 release-address --region "$AWS_REGION" --allocation-id "$ALLOC_ID"   # if you made one
aws ec2 delete-security-group --region "$AWS_REGION" --group-id "$SG_ID"
aws ec2 delete-key-pair --region "$AWS_REGION" --key-name "$CONVERGE_NAME-key"
```

Order matters: the security group can't be deleted while the instance still
holds it, and the instance's network interface lingers for a few seconds after
termination — hence the `wait`, and the retry loop in the script.

### Just pausing?

```bash
aws ec2 stop-instances --region "$AWS_REGION" --instance-ids "$INSTANCE_ID"
```

Instance-hours stop. The EBS volume keeps billing (~$1.60/month) and your data
is safe. The public DNS name changes on the next start — see section 10.
